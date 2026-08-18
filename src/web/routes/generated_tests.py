"""API routes for running generated Playwright tests via pytest.

Test runs are now multi-threaded: up to 4 pytest subprocesses execute in
parallel, drained by a worker pool in :mod:`src.web.parallel_runner`. The
SSE wire shape adds `slot` and `test_run_id` fields on every event so the
dashboard can render a 2x2 live grid of in-flight tests. See module docs
in parallel_runner.py for the full event vocabulary.
"""

import asyncio
import json
import os
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.config import config, PROJECT_ROOT
from src.core.step_renderer import render_step, render_steps
from src.web.routes.screencast import cleanup_run as cleanup_screencast_run
from src.web.combined_report import build_combined_report
from src.web.parallel_runner import ActiveRun, ParallelRunner

router = APIRouter(prefix="/api/generated")

# Directory containing tests, split by type. Each type folder is fully
# self-contained — the .py test scripts live at the top level, with the
# YAML definitions in <type>/definitions/ and JSON data in <type>/data/:
#
#   tests/
#   ├── ui/
#   │   ├── test_cci_tc1_*.py
#   │   ├── definitions/tc1_*.yaml
#   │   └── data/tc1_*.json
#   └── api/
#       ├── test_cci_tc3_*.py
#       ├── definitions/tc3_*.yaml
#       └── data/tc3_*.json
TESTS_DIR = PROJECT_ROOT / "tests"
UI_TESTS_DIR = TESTS_DIR / "ui"
API_TESTS_DIR = TESTS_DIR / "api"
REPORTS_DIR = PROJECT_ROOT / "reports"

# Buckets the frontend filters on.
TEST_TYPES = ("ui", "api")


def _defs_dir(test_type: str) -> Path:
    return TESTS_DIR / test_type / "definitions"


def _data_dir(test_type: str) -> Path:
    return TESTS_DIR / test_type / "data"
# File to persist run history
HISTORY_FILE = PROJECT_ROOT / "test_run_history.json"

# --------------- active run tracking ---------------
#
# The bookkeeping for in-flight runs (which subprocesses are alive, which
# slots have been cancelled) now lives in src.web.parallel_runner.ActiveRun
# so the parallel runner can mutate it without circular imports.

# Module-level dict: master_run_id -> ActiveRun
_active_runs: dict[str, ActiveRun] = {}


# --------------- helpers ---------------

def _load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_history(history: list[dict]):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))


def _classify_test(test_path: Path) -> str:
    """Return 'ui' or 'api' based on which subfolder the test lives in.
    Falls back to 'ui' for unknown locations so the dashboard treats them
    as the heavier flow by default."""
    parts = test_path.parts
    if "api" in parts:
        return "api"
    if "ui" in parts:
        return "ui"
    return "ui"


# ── Test metadata: parsed directly from the .py file via AST ──────────
#
# Previously the dashboard read metadata (name, objective, tags, step
# labels) from a sibling YAML in tests/{type}/definitions/. That left
# two sources of truth — change one without the other and the dashboard
# drifts from the actual test code. We now read everything from the .py
# file itself, parsed via Python's `ast` module (no execution needed).
#
# What we extract:
#   - display_name : first non-empty line of the Test class's docstring
#   - tags         : ``TAGS = [...]`` class-level attribute
#   - objective    : ``OBJECTIVE = "..."`` class-level attribute
#   - steps        : every ``start_step(N, label, ...)`` call in the test
#                    method's body (in number order). The label may be a
#                    plain string, an f-string (kept as a template), or
#                    a ``_step_label(N, "X")`` wrapper (we look inside).
#
# Placeholder substitution: any ``{field.nested[0]}`` token in a label
# or objective is rendered against the JSON data file at parse time,
# so the dashboard popup always reflects current JSON values.


def _load_json_data_for_test(test_path: Path) -> Optional[dict]:
    """Locate the JSON data file co-located with the test.

    Convention: tests/{type}/data/<test-stem-without-test_prefix>.json.
    Falls back to a few legacy locations so older tests keep loading.
    """
    test_type = _classify_test(test_path)
    stem = test_path.stem
    candidates: list[Path] = []
    for prefix in ("test_cci_", "test_"):
        if stem.startswith(prefix):
            candidates.append(_data_dir(test_type) / f"{stem[len(prefix):]}.json")
    candidates.append(_data_dir(test_type) / f"{stem}.json")
    candidates.append(TESTS_DIR / "data" / test_type / f"{stem}.json")
    candidates.append(TESTS_DIR / "data" / f"{stem}.json")
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _extract_string_from_ast_node(node) -> Optional[str]:
    """Best-effort turn an AST expression into a display-string.

    Handles:
      - ast.Constant(str) : plain string literal.
      - ast.JoinedStr     : f-string — placeholders become ``{expr}``
                            template tokens so the dashboard can either
                            show them verbatim OR substitute against JSON.
      - ast.Call to ``_step_label(N, X)`` : peeks inside and recurses
                            on X. This is the legacy wrapper used by
                            TC1–4 before the sf_ui refactor.
    Returns None when the expression isn't something we can render
    without executing the test (e.g. dynamic variables).
    """
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                # Reconstruct the placeholder expression so the dashboard
                # can render against the JSON data file ({addresses[0].region}).
                try:
                    expr_text = ast.unparse(v.value)
                except Exception:
                    expr_text = "?"
                parts.append("{" + expr_text + "}")
            else:
                parts.append("{?}")
        return "".join(parts)
    if isinstance(node, ast.Call):
        # _step_label(N, X) — legacy wrapper. Peek inside.
        func = node.func
        is_step_label = (
            (isinstance(func, ast.Name) and func.id == "_step_label")
            or (isinstance(func, ast.Attribute) and func.attr == "_step_label")
        )
        if is_step_label and len(node.args) >= 2:
            return _extract_string_from_ast_node(node.args[1])
    return None


def _resolve_module_constants(tree, data) -> dict:
    """Walk module-level ``NAME = <expr>`` assignments and return a flat
    {NAME: value} dict for everything we can statically evaluate.

    Test files commonly define convenience constants from the JSON:

        OPP = DATA["opportunity"]
        OPP_NEW_BTN = OPP["new_button_name"]
        PRODUCT_BANDWIDTH = PRODUCT["bandwidth"]
        QUOTE_NAME = f"{QUOTE['quote_name_prefix']}{ACCOUNT_NAME}"

    Step labels reference these directly via f-strings:
        f"Click '{OPP_NEW_BTN}'"

    Without this resolver, the dashboard popup shows the literal
    ``{OPP_NEW_BTN}`` token instead of the friendly text. With it, the
    popup substitutes correctly.

    Supported RHS expression types (anything else is silently skipped):
        - Constant (string / int / float / bool / None)
        - Name (looks up in the accumulated context)
        - Subscript (dict[key] / list[idx])
        - Attribute (object.attr — works for dict.key via dict access)
        - Call to ``.get(key)`` or ``.get(key, default)`` on a dict
        - f-string (JoinedStr) with nested FormattedValue expressions
        - String concatenation via the ``+`` operator

    Nothing executes — this is purely AST evaluation, so a malicious
    test can't run arbitrary code via the dashboard's metadata parser.
    """
    import ast

    # Start the context with the JSON data flattened at the top level
    # so existing ``{addresses[0].region}``-style placeholders keep
    # working. Also expose a ``DATA`` alias matching the test files'
    # convention.
    context: dict = {}
    if isinstance(data, dict):
        context.update(data)
    context["DATA"] = data

    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in context:
                return context[node.id]
            raise ValueError(f"unresolved name: {node.id}")
        if isinstance(node, ast.Subscript):
            container = _eval(node.value)
            slice_node = node.slice
            # ast.Index wrapper was removed in py3.9; keep the check
            # for older interpreters / forward compat.
            if hasattr(ast, "Index") and isinstance(slice_node, ast.Index):
                slice_node = slice_node.value  # type: ignore[attr-defined]
            key = _eval(slice_node)
            return container[key]
        if isinstance(node, ast.Attribute):
            obj = _eval(node.value)
            if isinstance(obj, dict):
                return obj[node.attr]
            return getattr(obj, node.attr)
        if isinstance(node, ast.Call):
            # Only ``<expr>.get(key)`` / ``<expr>.get(key, default)``.
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
                raise ValueError("unsupported call")
            obj = _eval(node.func.value)
            if not isinstance(obj, dict):
                raise ValueError("get() target is not a dict")
            if not node.args:
                raise ValueError("get() with no args")
            key = _eval(node.args[0])
            default = _eval(node.args[1]) if len(node.args) >= 2 else None
            return obj.get(key, default)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    parts.append(str(_eval(v.value)))
                else:
                    raise ValueError("unsupported f-string part")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _eval(node.left) + _eval(node.right)
        # Empty / non-empty container literals — common as default
        # values in ``DATA.get("x", {})`` and similar idioms.
        if isinstance(node, ast.Dict):
            return {_eval(k): _eval(v) for k, v in zip(node.keys, node.values) if k is not None}
        if isinstance(node, ast.List):
            return [_eval(e) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval(e) for e in node.elts)
        if isinstance(node, ast.Set):
            return {_eval(e) for e in node.elts}
        # Short-circuit boolean ops — covers the common
        # ``LOCATION = DATA.get("location", {}) or {}`` idiom that
        # coerces None / falsy values to an empty dict.
        if isinstance(node, ast.BoolOp):
            values = [_eval(v) for v in node.values]
            if isinstance(node.op, ast.Or):
                for v in values:
                    if v:
                        return v
                return values[-1]
            if isinstance(node.op, ast.And):
                for v in values:
                    if not v:
                        return v
                return values[-1]
        # Conditional expression: ``X if cond else Y``. Evaluate the
        # test — if it's a Name we can't resolve, fall through to one
        # of the branches so the popup still gets a usable value.
        if isinstance(node, ast.IfExp):
            try:
                cond = _eval(node.test)
                return _eval(node.body) if cond else _eval(node.orelse)
            except Exception:
                # Best effort: try the "else" branch (usually the
                # safer default for runtime-only conditions like
                # ``if _slot else ""``).
                return _eval(node.orelse)
        raise ValueError(f"unsupported expression: {type(node).__name__}")

    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            context[target.id] = _eval(stmt.value)
        except Exception:
            # Unresolvable — but for derived constants we still want
            # the popup to show SOMETHING readable instead of the raw
            # ``{NAME}`` token. Register a placeholder that mimics the
            # variable name so later f-strings that reference it (e.g.
            # ``f"Create Account {ACCOUNT_NAME}"``) at least render as
            # ``Create Account <account_name>``. Skip names with a
            # leading underscore — those are typically loop indices /
            # private state that shouldn't appear in display text.
            name = target.id
            if not name.startswith("_"):
                context[name] = f"<{name.lower()}>"

    return context


def _extract_class_metadata(tree) -> dict:
    """Pull display_name / tags / objective from the first Test class
    in an AST. Returns a dict with those keys (values may be None / [])."""
    import ast

    out = {"display_name": None, "tags": [], "objective": None}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test"):
            continue
        # Class docstring → display name (first non-empty line).
        doc = ast.get_docstring(node)
        if doc:
            for line in doc.strip().splitlines():
                if line.strip():
                    out["display_name"] = line.strip()
                    break
        # Top-level assignments inside the class body.
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                continue
            try:
                value = ast.literal_eval(stmt.value)
            except Exception:
                continue
            if target.id == "TAGS" and isinstance(value, list):
                out["tags"] = [str(t) for t in value]
            elif target.id == "OBJECTIVE" and isinstance(value, str):
                out["objective"] = value
        # We only consult the FIRST Test class — most files have exactly one.
        break
    return out


def _extract_steps_from_ast(tree) -> list[tuple[int, str]]:
    """Walk every Call expression in the module looking for tracker
    step starters. Returns a sorted, deduped list of (number, label).

    Matched call patterns:
      tracker.start_step(N, "label", ...)
      api_tracker.start_step(N, "label", ...)
      self.tracker.start_step(N, "label", ...)
      t.start_step(N, "label", ...)
      sf.step(N, "label", ...)                  ← new sf_ui pattern

    The label arg may be a plain string, an f-string, or a
    _step_label(N, "label") wrapper — all handled by
    ``_extract_string_from_ast_node``.
    """
    import ast

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = None
        if isinstance(func, ast.Attribute):
            attr = func.attr
        is_step_call = attr in ("start_step", "step")
        if not is_step_call or len(node.args) < 2:
            continue
        num_node, label_node = node.args[0], node.args[1]
        if not (isinstance(num_node, ast.Constant) and isinstance(num_node.value, int)):
            continue
        label = _extract_string_from_ast_node(label_node)
        if label:
            found.append((num_node.value, label))

    # Sort by number, dedupe (some patterns reference the same step
    # twice — e.g. in helpers — keep the first occurrence).
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for num, label in sorted(found):
        if num in seen:
            continue
        seen.add(num)
        out.append((num, label))
    return out


def _parse_test_file(path: Path) -> dict:
    """Extract metadata from a test file. Single source of truth = the
    .py file. No sibling YAML required.

    Returns the same dict shape the dashboard expects:
      id, filename, type, path, description, lines, modified,
      display_name, objective, steps, tags, yaml_file (always None now).
    """
    import ast

    content = path.read_text()
    # Module-level docstring — kept for the table tooltip fallback.
    description = ""
    if content.startswith('"""'):
        end = content.find('"""', 3)
        if end != -1:
            description = content[3:end].strip()
    lines = content.count("\n") + 1

    display_name = None
    objective = None
    tags: list[str] = []
    steps_pairs: list[tuple[int, str]] = []
    tree = None
    try:
        tree = ast.parse(content)
        meta = _extract_class_metadata(tree)
        display_name = meta.get("display_name")
        tags = meta.get("tags") or []
        objective = meta.get("objective")
        steps_pairs = _extract_steps_from_ast(tree)
    except SyntaxError:
        # Don't fail the whole /tests endpoint just because one file has
        # a syntax error — the dashboard still shows the row, just with
        # empty metadata. Surface the issue via the description.
        description = description or "(syntax error in test file)"

    # Substitute {placeholder} tokens in step labels + objective. The
    # render context = JSON data flattened + every module-level Python
    # constant we can statically resolve. That way a step label written
    # as `f"Click '{OPP_NEW_BTN}'"` shows the actual button name in the
    # popup, not the literal ``{OPP_NEW_BTN}`` token. JSON-path
    # placeholders like ``{addresses[0].region}`` still work the same.
    data = _load_json_data_for_test(path)
    render_ctx = data
    if tree is not None:
        try:
            render_ctx = _resolve_module_constants(tree, data)
        except Exception:
            render_ctx = data
    if render_ctx is not None:
        steps = [render_step(label, render_ctx) for _, label in steps_pairs]
        if objective:
            objective = render_step(objective, render_ctx)
    else:
        steps = [label for _, label in steps_pairs]

    test_type = _classify_test(path)
    rel_id = f"{test_type}/{path.name}"

    return {
        "id": rel_id,
        "filename": path.name,
        "type": test_type,
        "path": str(path),
        "description": description,
        "lines": lines,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        "display_name": display_name,
        "objective": objective,
        "steps": steps,
        "tags": tags,
        "yaml_file": None,  # kept for back-compat with old API consumers
    }


def _resolve_test_path(test_id: str) -> Optional[Path]:
    """Resolve a frontend-supplied test id ("ui/test_foo.py" or just
    "test_foo.py") to an actual file path under tests/{ui,api}/.

    Accepts:
      - "ui/test_x.py"  →  tests/ui/test_x.py
      - "api/test_x.py" →  tests/api/test_x.py
      - "test_x.py"     →  search both subfolders (back-compat)
    """
    if not test_id or ".." in Path(test_id).parts:
        return None
    p = Path(test_id)
    if len(p.parts) == 2 and p.parts[0] in TEST_TYPES:
        candidate = TESTS_DIR / p.parts[0] / p.parts[1]
        return candidate if candidate.exists() else None
    # bare filename — look it up in either subfolder
    for sub in TEST_TYPES:
        candidate = TESTS_DIR / sub / p.name
        if candidate.exists():
            return candidate
    return None


def _snapshot_reports() -> set[str]:
    """Return the set of .html filenames currently in reports/."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return {f.name for f in REPORTS_DIR.glob("*.html")}


def _new_report_urls(before: set[str]) -> list[str]:
    """Compare reports folder to a previous snapshot, return URLs for new reports."""
    after = _snapshot_reports()
    new_files = sorted(after - before)
    return [f"/reports/{f}" for f in new_files]


def _extract_verdicts(output: str, filename: str) -> list[dict]:
    """Extract per-test PASSED/FAILED/ERROR/SKIPPED from pytest verbose output.

    Matches lines like:
        tests/generated/test_foo.py::TestFoo::test_bar[chromium] PASSED
    But NOT lines like:
        ERROR at teardown of TestFoo.test_bar[chromium]
    """
    import re
    results = []
    # Pattern: path::class::method[param] STATUS  (pytest -v format)
    # Match any path (absolute or relative) that ends in .py::something.
    verdict_re = re.compile(
        r'(\S*test_\S+\.py::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)',
    )
    for line in output.splitlines():
        m = verdict_re.search(line.strip())
        if m:
            # Normalize test id to a relative tests/ path for display
            test_id = m.group(1)
            if "tests/" in test_id:
                test_id = "tests/" + test_id.split("tests/", 1)[1]
            # Normalize ERROR → failed so dashboard shows consistent status
            raw_result = m.group(2).lower()
            if raw_result == "error":
                raw_result = "failed"
            results.append({
                "test": test_id,
                "result": raw_result,
                "file": filename,
            })
    return results


# --------------- models ---------------

class RunRequest(BaseModel):
    tests: List[str]          # list of filenames
    # NOTE: `headless` is intentionally ignored. The dashboard now embeds the
    # browser inside the page (via CDP screencast → WebSocket), so Chrome is
    # always launched headless on the server. Field kept for back-compat with
    # any external callers that still pass it.
    headless: bool = True
    # Number of pytest subprocesses to run in parallel. Clamped to [1, 4]
    # by the runner. None means "use the env default" (MAX_PARALLEL or 4).
    parallelism: Optional[int] = None
    # Browser identity for UI tests. Accepted: "chrome" (default), "edge",
    # "firefox", "webkit"/"safari". The frontend disables this control
    # when no UI tests are selected so it's a no-op for API-only runs.
    # API tests ignore this completely.
    browser: Optional[str] = "chrome"


# --------------- endpoints ---------------

@router.get("/config")
def get_config():
    """Return frontend config defaults from environment / .env."""
    return {
        "headless": os.environ.get("BROWSER_HEADLESS", "false").lower() == "true",
    }


@router.get("/tests")
def list_generated_tests():
    """Return every *.py test file under tests/ui/ and tests/api/."""
    UI_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    API_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        list(UI_TESTS_DIR.glob("test_*.py")) + list(API_TESTS_DIR.glob("test_*.py")),
        key=lambda f: f.name,
    )
    return [_parse_test_file(f) for f in files]


@router.get("/reports")
def list_reports():
    """List all HTML reports available in the reports folder."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(REPORTS_DIR.glob("*.html"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [
        {
            "filename": f.name,
            "url": f"/reports/{f.name}",
            "size_kb": round(f.stat().st_size / 1024, 1),
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        }
        for f in files
    ]


@router.post("/run")
async def run_tests_stream(req: RunRequest):
    """
    Run the requested tests with bounded parallelism (up to 4 workers).

    Returns a Server-Sent Events stream. New event types compared to the
    pre-parallel implementation:

      * ``parallelism``  — emitted once, carries ``value`` (worker count)
        and ``total`` (test count) so the frontend can size its grid.
      * ``queue_state``  — pending / in_flight / completed counters,
        emitted every time a test starts or completes.
      * ``slot_idle``    — a worker finished its last test (queue empty).

    Existing events (``run_id``, ``start``, ``log``, ``complete``, ``done``)
    keep their old fields but now also carry ``slot`` (0..parallelism-1)
    and ``test_run_id`` (the child run id used by the screencast bridge so
    each parallel test publishes to its own WebSocket channel).
    """
    UI_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    API_TESTS_DIR.mkdir(parents=True, exist_ok=True)

    # Validate all files exist. The frontend sends test ids of the form
    # "{type}/{filename}" (e.g. "ui/test_cci_tc1_*.py"); bare filenames
    # are tolerated for back-compat.
    paths: list[Path] = []
    for name in req.tests:
        resolved = _resolve_test_path(name)
        if resolved is None:
            raise HTTPException(404, f"Test file not found: {name}")
        paths.append(resolved)

    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # Register active run for cancellation tracking
    active = ActiveRun(run_id)
    _active_runs[run_id] = active

    # Environment shared by every worker subprocess. The runner overrides
    # UI_TEST_RUN_ID / UI_TEST_SLOT per test so each parallel pytest gets
    # its own screencast channel.
    env = {**os.environ}
    env.pop("HEADED", None)
    env["BROWSER_HEADLESS"] = "true"
    env["UI_TEST_BRIDGE_URL"] = f"http://127.0.0.1:{config.DASHBOARD_PORT}"
    # Master run id is informational only — each worker spawns child
    # run_ids derived from it.
    env["UI_TEST_MASTER_RUN_ID"] = run_id

    # Resolve effective parallelism. Order of precedence:
    #   1. RunRequest.parallelism (explicit UI choice)
    #   2. MAX_PARALLEL env var (CI / docker config)
    #   3. Default to 4 when running interactively, 2 in CI
    if req.parallelism is not None:
        parallelism = req.parallelism
    elif os.environ.get("MAX_PARALLEL"):
        try:
            parallelism = int(os.environ["MAX_PARALLEL"])
        except ValueError:
            parallelism = 4
    else:
        # CI gets a conservative default (Chromium + video recording is
        # memory-hungry); dashboard gets the full 4.
        parallelism = 2 if os.environ.get("CI", "").lower() == "true" else 4

    runner = ParallelRunner(
        master_run_id=run_id,
        test_paths=paths,
        env=env,
        project_root=PROJECT_ROOT,
        reports_dir=REPORTS_DIR,
        parallelism=parallelism,
        active=active,
        browser=(req.browser or "chrome"),
    )

    async def event_stream():
        started_at = datetime.now().isoformat()

        # Send the run_id so the frontend can target cancel requests.
        yield _sse("run_id", {"run_id": run_id})

        # Build a side-cache of {filename: display_name} so combined_entries
        # can be enriched with friendly names (the runner doesn't know
        # how to read YAML meta).
        display_name_by_filename: dict[str, str] = {}
        for p in paths:
            try:
                meta = _parse_test_file(p)
                if meta.get("display_name"):
                    display_name_by_filename[p.name] = meta["display_name"]
            except Exception:
                pass

        async for evt in runner.run():
            etype = evt.pop("type")

            # The runner uses "type_field" for the ui/api classification
            # because "type" is reserved for the event discriminator.
            # Rename it back so the wire shape matches pre-parallel events.
            if "type_field" in evt:
                evt["type"] = evt.pop("type_field")

            yield _sse(etype, evt)

        finished_at = datetime.now().isoformat()
        duration_s_total = round(
            (datetime.fromisoformat(finished_at)
             - datetime.fromisoformat(started_at)).total_seconds(),
            1,
        )

        # Enrich combined_entries with display names looked up from YAML.
        for entry in runner.combined_entries:
            if not entry.get("display_name"):
                entry["display_name"] = display_name_by_filename.get(entry["filename"])

        # Final status logic mirrors the pre-parallel version: cancelled
        # if cancel-all was triggered, passed if every test passed, else
        # failed.
        final_status = (
            "cancelled" if active.cancelled
            else ("passed" if runner.overall_passed else "failed")
        )

        # Build the per-run combined HTML report — single offline-portable
        # file with every screenshot + video embedded.
        combined_url = ""
        try:
            combined_path = build_combined_report(
                run_id=run_id,
                started_at=started_at,
                duration_s=duration_s_total,
                status=final_status,
                entries=runner.combined_entries,
                out_dir=REPORTS_DIR,
            )
            combined_url = f"/reports/{combined_path.name}"
        except Exception as exc:
            # Combined-report failure must NOT kill the run.
            import traceback
            print(f"[combined-report] build failed: {exc}\n{traceback.format_exc()}")

        # Fallback report URLs (in case per-test snapshot diffs missed
        # anything — extremely rare but handled).
        all_report_urls = list(runner.all_report_urls)
        if not all_report_urls:
            all_report_urls = sorted(
                f"/reports/{f}"
                for f in (
                    {p.name for p in REPORTS_DIR.glob("*.html")}
                    - runner._reports_before_run
                )
            )

        result = {
            "run_id": run_id,
            "tests": [p.name for p in paths],
            "status": final_status,
            "returncode": 0 if runner.overall_passed else 1,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": duration_s_total,
            "test_results": runner.all_test_results,
            "output": "\n".join(runner.all_output_chunks),
            "total": len(runner.all_test_results),
            "passed": sum(1 for t in runner.all_test_results if t["result"] == "passed"),
            "failed": sum(1 for t in runner.all_test_results if t["result"] == "failed"),
            "cancelled": sum(1 for t in runner.all_test_results if t["result"] == "cancelled"),
            "parallelism": runner.parallelism,
            # Browser the run used (chrome / edge / firefox / webkit).
            # Surfaced as a column in the Run History table and in any
            # downstream consumers (Slack webhook, email, etc.).
            "browser": runner.browser,
            "combined_report_url": combined_url,
            "report_urls": all_report_urls,
        }

        # Persist to history
        history = _load_history()
        history.insert(0, result)
        _save_history(history[:200])

        # Cleanup: active run + every child screencast channel.
        _active_runs.pop(run_id, None)
        try:
            cleanup_screencast_run(run_id)  # master id (rarely used directly)
        except Exception:
            pass
        # Each child run_id used the form {master}_s{slot}_{seq}; the
        # screencast bridge cleans them up on its own once subscribers
        # disconnect, but we also explicitly drop any cached frame so
        # memory doesn't accumulate over many runs.
        try:
            seen_child_ids = {
                entry.get("test_run_id")
                for entry in runner.combined_entries
                if entry.get("test_run_id")
            }
            for cid in seen_child_ids:
                cleanup_screencast_run(cid)
        except Exception:
            pass

        # --- SSE: all done ---
        yield _sse("done", result)

    async def safe_event_stream():
        """Wraps event_stream() so any unexpected exception still produces
        a meaningful SSE `done` event instead of a dead connection."""
        try:
            async for chunk in event_stream():
                yield chunk
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _active_runs.pop(run_id, None)
            try:
                cleanup_screencast_run(run_id)
            except Exception:
                pass
            yield _sse("done", {
                "run_id": run_id,
                "tests": [p.name for p in paths],
                "status": "error",
                "returncode": 1,
                "started_at": datetime.now().isoformat(),
                "finished_at": datetime.now().isoformat(),
                "duration_s": 0,
                "test_results": [],
                "output": f"Server error:\n{tb}",
                "total": 0,
                "passed": 0,
                "failed": 0,
                "cancelled": 0,
                "report_urls": [],
            })

    return StreamingResponse(
        safe_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cancel/{run_id}")
async def cancel_run(run_id: str, mode: str = "all", slot: Optional[int] = None):
    """Cancel a running test execution.

    Query params:
      mode=all              kill every in-flight test + drop queued tests (default)
      mode=slot&slot=N      kill just slot N's current test; the worker picks
                            up the next queued test afterwards
      mode=current          back-compat alias for ``mode=all`` (the original
                            sequential runner only had one in-flight test)
    """
    active = _active_runs.get(run_id)
    if not active:
        raise HTTPException(404, "Run not found or already finished")

    if mode == "all" or mode == "current":
        active.kill_all()
        return {"message": f"Cancelling entire run {run_id}"}
    elif mode == "slot":
        if slot is None:
            raise HTTPException(400, "mode=slot requires `slot` query param")
        if slot < 0 or slot >= 4:
            raise HTTPException(400, "slot must be in 0..3")
        active.kill_slot(slot)
        return {"message": f"Cancelling slot {slot} in run {run_id}"}
    else:
        raise HTTPException(400, f"Invalid mode: {mode}. Use 'all' or 'slot'.")


def _sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/history")
def get_history(limit: int = 50):
    """Return past test runs, newest first."""
    return _load_history()[:limit]


@router.get("/history/{run_id}")
def get_run_detail(run_id: str):
    for entry in _load_history():
        if entry["run_id"] == run_id:
            return entry
    raise HTTPException(404, "Run not found")


@router.delete("/history/{run_id}")
def delete_run(run_id: str):
    history = [h for h in _load_history() if h["run_id"] != run_id]
    _save_history(history)
    return {"message": "Deleted"}
