"""
Bounded-parallelism test runner.

Owns the work-stealing pool that backs the dashboard's /api/generated/run
endpoint. Up to N workers (default 4, max 4) drain a shared queue of test
paths in parallel. Each worker spawns its own pytest subprocess, captures
output line-by-line, and emits SSE-shaped events into a single event queue
that the outer endpoint streams to the browser.

Event shape (all backward-compatible — pre-parallel events kept their
fields; new events add `slot` and `test_run_id`):

  parallelism   {value, total}
  queue_state   {pending, in_flight, completed}
  start         {slot, test_run_id, index, total, filename, type, id}
  log           {slot, test_run_id, index, line}
  complete      {slot, test_run_id, index, total, filename, type, id,
                 status, started_at, finished_at, duration_s,
                 verdicts, output, report_urls, attempt}
  slot_idle     {slot}                  worker has no more work to pick up

Cancellation modes (handled by the route layer via _ActiveRun.kill_slot /
kill_all): "all" kills every in-flight test and drains the queue; "slot"
kills just one worker's current test (the worker then picks the next item
off the queue).

Failure policy: continue all remaining. A failure in slot 2 doesn't stop
slots 0/1/3.

This module deliberately knows nothing about FastAPI routing or the SSE
wire format — it yields plain dicts. The /run endpoint formats them as
SSE frames.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional


# ── Helpers shared with generated_tests.py ─────────────────────────────


def _classify_test(test_path: Path) -> str:
    """Return 'ui' or 'api' based on which subfolder the test lives in."""
    parts = test_path.parts
    if "api" in parts:
        return "api"
    if "ui" in parts:
        return "ui"
    return "ui"


_VERDICT_RE = re.compile(
    r"(\S*test_\S+\.py::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
)

# conftest.py prints exactly one "Report: <path>" (UI) or
# "API Report: <path>" (API) line per test, after the per-test HTML is
# written. Parsing those lines gives PER-TEST attribution that doesn't
# depend on snapshot-diffing the reports/ folder — critical for the
# parallel runner, where 4 subprocesses' snapshots otherwise see each
# others' new files and end up attributing the same report to every
# test.
_REPORT_LINE_RE = re.compile(
    r"^\s*(?:API\s+)?Report:\s*(?P<path>.+\.html)\s*$",
    re.MULTILINE,
)


def browser_pytest_args(browser: str) -> tuple[list[str], bool]:
    """Translate a friendly browser name into pytest-playwright CLI flags.

    Returns (args, is_chromium_based). ``is_chromium_based`` is True for
    Chrome / Edge / bare Chromium — used by the screencast fixture to
    decide whether CDP is available.

    Accepted names (case-insensitive):
      chrome   → --browser chromium --browser-channel chrome (real Chrome)
      chromium → --browser chromium                          (Playwright's bundled)
      edge     → --browser chromium --browser-channel msedge
      firefox  → --browser firefox
      webkit   → --browser webkit                            (Safari engine)
      safari   → --browser webkit                            (alias — Linux runs WebKit, macOS runs WebKit-via-Playwright,
                                                              NOT the literal Safari.app — see WRITING_TESTS.md)
    """
    name = (browser or "chrome").lower().strip()
    if name in ("chrome", ""):
        return (["--browser", "chromium", "--browser-channel", "chrome"], True)
    if name in ("chromium", "chrome-headless-shell"):
        return (["--browser", "chromium"], True)
    if name in ("edge", "msedge"):
        return (["--browser", "chromium", "--browser-channel", "msedge"], True)
    if name == "firefox":
        return (["--browser", "firefox"], False)
    if name in ("webkit", "safari"):
        return (["--browser", "webkit"], False)
    # Unknown → fall back to Chrome with a warning print so the run keeps going.
    print(f"[parallel-runner] Unknown browser '{browser}', defaulting to chrome")
    return (["--browser", "chromium", "--browser-channel", "chrome"], True)


def _extract_report_urls(output: str, reports_dir: Path) -> list[str]:
    """Return the /reports/<filename> URLs printed by this subprocess.

    We trust conftest's "Report: …" / "API Report: …" lines because
    they're emitted by the very fixture that wrote the file, on this
    pytest process. The snapshot-diff approach would attribute another
    parallel slot's report to us if both finished in the same window.
    """
    seen: list[str] = []
    for m in _REPORT_LINE_RE.finditer(output or ""):
        p = Path(m.group("path").strip())
        # Only surface files that actually exist under reports_dir —
        # ignore stray "Report:" lines from tests that printed them
        # for unrelated reasons.
        try:
            if p.is_absolute() and p.parent.resolve() == reports_dir.resolve() and p.exists():
                url = f"/reports/{p.name}"
                if url not in seen:
                    seen.append(url)
        except OSError:
            continue
    return seen


def _extract_verdicts(output: str, filename: str) -> list[dict]:
    """Parse per-test PASSED/FAILED/ERROR/SKIPPED out of pytest verbose output."""
    results = []
    for line in output.splitlines():
        m = _VERDICT_RE.search(line.strip())
        if m:
            test_id = m.group(1)
            if "tests/" in test_id:
                test_id = "tests/" + test_id.split("tests/", 1)[1]
            raw = m.group(2).lower()
            if raw == "error":
                raw = "failed"
            results.append({"test": test_id, "result": raw, "file": filename})
    return results


# ── Active run tracking — extended for per-slot cancellation ───────────


class ActiveRun:
    """Tracks the state of a running parallel execution.

    Multiple subprocesses can be in flight simultaneously (one per slot).
    Cancellation can target the whole run or a single slot.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.cancelled = False                              # cancel-all flag
        self.slot_procs: dict[int, asyncio.subprocess.Process] = {}
        self.slot_cancel: set[int] = set()                  # slots flagged to kill current test

    def register_proc(self, slot: int, proc: asyncio.subprocess.Process) -> None:
        self.slot_procs[slot] = proc

    def clear_proc(self, slot: int) -> None:
        self.slot_procs.pop(slot, None)
        self.slot_cancel.discard(slot)

    def kill_slot(self, slot: int) -> None:
        """SIGTERM the running subprocess in `slot` (process group on Unix)."""
        self.slot_cancel.add(slot)
        proc = self.slot_procs.get(slot)
        if proc and proc.returncode is None:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.kill()
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass

    def kill_all(self) -> None:
        """Cancel-all: flag the run + kill every in-flight worker subprocess."""
        self.cancelled = True
        for slot in list(self.slot_procs.keys()):
            self.kill_slot(slot)


# ── The parallel runner ────────────────────────────────────────────────


class ParallelRunner:
    """Orchestrates a bounded-parallelism test run.

    Usage:
        runner = ParallelRunner(master_run_id, paths, env, parallelism=4,
                                active=active_run, project_root=PROJECT_ROOT,
                                reports_dir=REPORTS_DIR)
        async for event in runner.run():
            yield format_sse(event)
        # afterwards: runner.combined_entries holds per-test report metadata
    """

    def __init__(
        self,
        master_run_id: str,
        test_paths: list[Path],
        env: dict,
        project_root: Path,
        reports_dir: Path,
        parallelism: int = 4,
        active: Optional[ActiveRun] = None,
        browser: str = "chrome",
    ) -> None:
        self.master_run_id = master_run_id
        self.test_paths = list(test_paths)
        self.env = dict(env)
        self.project_root = project_root
        self.reports_dir = reports_dir
        # Clamp to [1, 4]. The product calls for max 4 — that's all the UI
        # is built to render and what the GitHub Actions runner can handle
        # without OOMing on four parallel headless Chromiums.
        self.parallelism = max(1, min(int(parallelism or 4), 4))
        # But never request more workers than we have tests.
        self.parallelism = min(self.parallelism, max(1, len(test_paths)))
        self.active = active or ActiveRun(master_run_id)
        # Browser identity. Used to derive pytest --browser / --browser-channel
        # flags and to flip CDP-screencast off for non-Chromium browsers.
        self.browser = (browser or "chrome").lower().strip()

        # Multi-producer / single-consumer event channel. Workers push,
        # the run() generator drains.
        self._events: asyncio.Queue = asyncio.Queue()
        # Work queue — tuples of (index, test_path).
        self._work: asyncio.Queue = asyncio.Queue()

        # State the route layer needs after run() finishes.
        self.combined_entries: list[dict] = []
        self.all_test_results: list[dict] = []
        self.all_output_chunks: list[str] = []
        self.all_report_urls: list[str] = []
        self.overall_passed: bool = True
        # Snapshot of reports/ taken once before the run starts; used at the
        # very end as a fallback when individual per-test diffs missed
        # anything (rare but possible if a test writes its report after
        # the subprocess exits, e.g. via an atexit hook).
        self._reports_before_run: set[str] = set()

    # ── Public API ─────────────────────────────────────────────────

    async def run(self) -> AsyncGenerator[dict, None]:
        """Drain the queue with N concurrent workers. Yields plain event
        dicts as they arrive from any worker; ordering across slots is by
        arrival time."""
        # Snapshot reports/ once at start, used by the route layer when
        # individual per-test diffs are empty.
        self._reports_before_run = self._snapshot_reports()

        # Seed the work queue (1-based indices for human display).
        for i, p in enumerate(self.test_paths, 1):
            await self._work.put((i, p))

        # Emit the parallelism + initial queue state events so the
        # frontend can size its grid before any worker starts producing.
        yield {
            "type": "parallelism",
            "value": self.parallelism,
            "total": len(self.test_paths),
        }
        yield {
            "type": "queue_state",
            "pending": self._work.qsize(),
            "in_flight": 0,
            "completed": 0,
        }

        # Kick off workers. Each one loops until the work queue is empty
        # or the run is cancelled.
        worker_tasks = [
            asyncio.create_task(self._worker(slot))
            for slot in range(self.parallelism)
        ]

        # Drain events from the channel. Stop once every worker has
        # signalled it's done (sentinel event "_worker_done").
        workers_done = 0
        completed = 0
        in_flight = 0
        while workers_done < self.parallelism:
            evt = await self._events.get()

            # Internal sentinel — strip it before yielding.
            if evt.get("type") == "_worker_done":
                workers_done += 1
                # Slot is idle from here on.
                yield {"type": "slot_idle", "slot": evt["slot"]}
                continue

            # Maintain in-flight / completed counters for queue_state.
            t = evt.get("type")
            if t == "start":
                in_flight += 1
                pending = self._work.qsize()
                yield evt
                yield {
                    "type": "queue_state",
                    "pending": pending,
                    "in_flight": in_flight,
                    "completed": completed,
                }
                continue
            if t == "complete":
                in_flight -= 1
                completed += 1
                yield evt
                yield {
                    "type": "queue_state",
                    "pending": self._work.qsize(),
                    "in_flight": in_flight,
                    "completed": completed,
                }
                continue

            yield evt

        # Make sure all worker tasks are joined (they should already be
        # done since we counted their sentinels).
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    # ── Worker loop ────────────────────────────────────────────────

    async def _worker(self, slot: int) -> None:
        """Pull tests from the queue until empty or cancelled.

        Each worker waits ``slot * PARALLEL_START_STAGGER_SEC`` seconds
        before grabbing its first test. The default of 10s spreads out
        the initial Salesforce login / OAuth burst across workers and
        prevents 4 simultaneous identical timestamps from generating
        the same CCIAUTO_Biz_<ts> account name. Set the env var to 0
        to disable the stagger (CI typically doesn't need it because
        xdist already handles ordering)."""
        stagger_sec = float(os.environ.get("PARALLEL_START_STAGGER_SEC", "10") or 0)
        if stagger_sec > 0 and slot > 0:
            await asyncio.sleep(slot * stagger_sec)
        seq = 0
        try:
            while True:
                if self.active.cancelled:
                    # Drain remaining tests from the queue and emit
                    # cancellation events so the dashboard accounts for them.
                    await self._drain_cancelled(slot)
                    break
                try:
                    index, test_path = self._work.get_nowait()
                except asyncio.QueueEmpty:
                    break
                seq += 1
                child_run_id = f"{self.master_run_id}_s{slot}_{seq}"
                await self._run_one(slot, index, test_path, child_run_id)
        except Exception as exc:
            # Worker-level failure shouldn't crash the run — surface it
            # as an error event and keep the other workers going.
            import traceback
            await self._events.put({
                "type": "error",
                "slot": slot,
                "message": f"Worker {slot} crashed: {exc}",
                "traceback": traceback.format_exc(),
            })
        finally:
            await self._events.put({"type": "_worker_done", "slot": slot})

    async def _drain_cancelled(self, slot: int) -> None:
        """When a cancel-all has fired, mark any remaining queued tests as
        cancelled so the dashboard's totals stay honest."""
        while True:
            try:
                index, test_path = self._work.get_nowait()
            except asyncio.QueueEmpty:
                return
            test_type = _classify_test(test_path)
            test_id = f"{test_type}/{test_path.name}"
            now = datetime.now().isoformat()
            await self._events.put({
                "type": "complete",
                "slot": slot,
                "test_run_id": f"{self.master_run_id}_s{slot}_skip",
                "index": index,
                "total": len(self.test_paths),
                "filename": test_path.name,
                "type_field": test_type,
                "id": test_id,
                "status": "cancelled",
                "started_at": now,
                "finished_at": now,
                "duration_s": 0,
                "verdicts": [{
                    "test": f"tests/{test_id}",
                    "result": "cancelled",
                    "file": test_path.name,
                }],
                "output": "Cancelled by user (queued, never started)",
                "report_urls": [],
                "attempt": 1,
            })
            self.all_test_results.append({
                "test": f"tests/{test_id}",
                "result": "cancelled",
                "file": test_path.name,
            })
            self.combined_entries.append({
                "filename": test_path.name,
                "display_name": None,
                "type": test_type,
                "status": "cancelled",
                "duration_s": 0,
                "report_path": None,
            })

    # ── Per-test subprocess ────────────────────────────────────────

    async def _run_one(
        self,
        slot: int,
        index: int,
        test_path: Path,
        child_run_id: str,
    ) -> None:
        """Spawn pytest for one test in this slot, stream its output, and
        emit start/log/complete events."""
        test_type = _classify_test(test_path)
        test_id = f"{test_type}/{test_path.name}"
        started_at = datetime.now().isoformat()

        await self._events.put({
            "type": "start",
            "slot": slot,
            "test_run_id": child_run_id,
            "index": index,
            "total": len(self.test_paths),
            "filename": test_path.name,
            # NOTE: keys are named "type_field" / "id" because "type" is
            # reserved as the event-type discriminator. The route layer
            # remaps "type_field" → "type" when formatting SSE so the
            # frontend wire shape matches the pre-parallel events.
            "type_field": test_type,
            "id": test_id,
        })

        # Per-test reports snapshot — used to detect which HTML reports the
        # subprocess wrote.
        reports_before_test = self._snapshot_reports()

        # Per-subprocess env: tell conftest.py which run_id to publish
        # screencast frames to + which slot this is.
        env = dict(self.env)
        env["UI_TEST_RUN_ID"] = child_run_id
        env["UI_TEST_SLOT"] = str(slot)
        env["UI_TEST_MASTER_RUN_ID"] = self.master_run_id
        env["UI_TEST_BROWSER"] = self.browser

        # Resolve browser → pytest --browser flags. Non-Chromium browsers
        # can't use CDP, so the screencast fixture in conftest needs to
        # know to skip it (UI_TEST_BROWSER_IS_CHROMIUM gates that).
        browser_args, is_chromium = browser_pytest_args(self.browser)
        env["UI_TEST_BROWSER_IS_CHROMIUM"] = "true" if is_chromium else "false"
        if not is_chromium:
            # Belt + suspenders: also flip SCREENCAST_DISABLED so the
            # fixture skips even if it forgot to check the chromium flag.
            env["SCREENCAST_DISABLED"] = "true"

        cmd = [
            "python", "-m", "pytest",
            str(test_path),
            "-v", "-s",
            "--tb=short",
            "--no-header",
            "--headless",
            *browser_args,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.project_root),
            env=env,
            **({"preexec_fn": os.setsid} if sys.platform != "win32" else {}),
        )
        self.active.register_proc(slot, proc)

        output_lines: list[str] = []
        try:
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace").rstrip("\n")
                output_lines.append(line)
                await self._events.put({
                    "type": "log",
                    "slot": slot,
                    "test_run_id": child_run_id,
                    "index": index,
                    "line": line,
                })
            await proc.wait()
        finally:
            self.active.clear_proc(slot)

        # Determine status
        was_slot_cancelled = slot in self.active.slot_cancel
        was_cancelled = self.active.cancelled or was_slot_cancelled
        file_passed = proc.returncode == 0 and not was_cancelled
        if not file_passed and not was_cancelled:
            self.overall_passed = False
        status = "cancelled" if was_cancelled else ("passed" if file_passed else "failed")

        output = "\n".join(output_lines)

        # Friendly hint for the most common "I picked a browser I haven't
        # installed yet" failure. Playwright dumps a long stack trace that
        # buries the actual fix; we surface a one-liner the user can run.
        if not file_passed and not was_cancelled and (
            "Executable doesn't exist" in output
            or "browser executable not found" in output.lower()
            or "looks like Playwright Test or Playwright was just installed" in output
        ):
            hint = (
                f"\n\nHINT: the '{self.browser}' browser binary isn't installed. "
                f"Fix it with:\n"
                f"    source venv/bin/activate && playwright install {self.browser}\n"
                "Then re-run from the dashboard."
            )
            await self._events.put({
                "type": "log",
                "slot": slot,
                "test_run_id": child_run_id,
                "index": index,
                "line": hint,
            })
            output += hint
        self.all_output_chunks.append(
            f"\n{'='*60}\n  [slot {slot}] Running test {index}/{len(self.test_paths)}: {test_path.name}\n{'='*60}\n{output}\n"
        )

        verdicts = _extract_verdicts(output, test_path.name)
        if not verdicts:
            verdicts = [{
                "test": f"tests/{test_id}",
                "result": status,
                "file": test_path.name,
            }]
        if was_cancelled:
            for v in verdicts:
                v["result"] = "cancelled"
        self.all_test_results.extend(verdicts)

        # Per-test report attribution. Prefer the "Report: <path>" lines
        # this subprocess printed via conftest — that's deterministic
        # per-test even when 4 workers run in parallel. Fall back to
        # the snapshot diff only if conftest didn't print anything
        # (unusual — happens if the test had zero steps).
        test_report_urls = _extract_report_urls(output, self.reports_dir)
        if not test_report_urls:
            test_report_urls = self._new_report_urls(reports_before_test)
        self.all_report_urls.extend(test_report_urls)

        finished_at = datetime.now().isoformat()
        duration_s = round(
            (datetime.fromisoformat(finished_at)
             - datetime.fromisoformat(started_at)).total_seconds(),
            1,
        )

        # Resolve per-test report path for the combined report
        per_test_report_path = None
        if test_report_urls:
            report_filename = test_report_urls[0].rsplit("/", 1)[-1]
            candidate = self.reports_dir / report_filename
            if candidate.exists():
                per_test_report_path = candidate

        self.combined_entries.append({
            "filename": test_path.name,
            "display_name": None,        # route layer fills this in if it has metadata
            "type": test_type,
            "status": status,
            "duration_s": duration_s,
            "report_path": per_test_report_path,
        })

        await self._events.put({
            "type": "complete",
            "slot": slot,
            "test_run_id": child_run_id,
            "index": index,
            "total": len(self.test_paths),
            "filename": test_path.name,
            "type_field": test_type,
            "id": test_id,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": duration_s,
            "verdicts": verdicts,
            "output": output,
            "report_urls": test_report_urls,
            "attempt": 1,                # reserved for future retry support
        })

    # ── Reports folder helpers ─────────────────────────────────────

    def _snapshot_reports(self) -> set[str]:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        return {f.name for f in self.reports_dir.glob("*.html")}

    def _new_report_urls(self, before: set[str]) -> list[str]:
        after = self._snapshot_reports()
        new_files = sorted(after - before)
        return [f"/reports/{f}" for f in new_files]
