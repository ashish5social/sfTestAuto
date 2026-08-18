"""
APITracker — records each step of an API-driven test as a sequence of
one or more API calls (request + response + timing + status).

Parallels StepTracker from src.core for UI tests, but:
  - No screenshot field
  - Each step has a list of APICall records instead
  - Each APICall carries the full request body, response body, status,
    duration, and a label

Test files interact with the tracker via the `api_tracker` pytest
fixture. The fixture is injected by tests/conftest.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


def _resolve_org_base_url() -> str:
    return os.getenv("SF_LOGIN_URL", "").rstrip("/")


@dataclass
class APICall:
    """A single API request/response pair captured during a test step."""

    # Human-readable identifier: "IP: Validate_QuoteForContract", "REST: POST /sobjects/Account"
    name: str
    method: str                  # "GET" | "POST" | "PATCH" | "DELETE"
    url: str                     # full URL (with instance)
    request_body: Any = None     # dict / str / None
    response_body: Any = None    # dict / str / None
    status_code: int | None = None
    duration_ms: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Stringify datetimes so json.dumps works
        d["started_at"] = self.started_at.isoformat()
        d["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        return d


class APITracker:
    """Track each step of an API-driven test (step → list of APICall)."""

    def __init__(self, test_name: str = "Unnamed API Test"):
        self.test_name = test_name
        self.steps: list[dict] = []
        self.start_time = datetime.now()
        self.end_time: datetime | None = None
        self.overall_status = "PASS"
        self.failure_step = None
        self.failure_error = None

    # ── Step lifecycle ────────────────────────────────────────

    def start_step(self, number: int, name: str, description: str = ""):
        self.steps.append(
            {
                "number": number,
                "name": name,
                "description": description,
                "status": "RUNNING",
                "started_at": datetime.now(),
                "ended_at": None,
                "duration_sec": 0,
                "error": None,
                "assertions": [],
                "records": [],            # [{label, name, url}]
                "api_calls": [],          # list[APICall]
            }
        )
        desc_suffix = f" - {description}" if description else ""
        print(f"\n  ▶ Step {number}: {name}{desc_suffix}")

    def pass_step(self):
        if not self.steps:
            return
        s = self.steps[-1]
        s["status"] = "PASS"
        s["ended_at"] = datetime.now()
        s["duration_sec"] = (s["ended_at"] - s["started_at"]).total_seconds()
        print(f"    ✓ Step {s['number']} PASSED in {s['duration_sec']:.2f}s")

    def fail_step(self, error: str):
        if not self.steps:
            return
        s = self.steps[-1]
        s["status"] = "FAIL"
        s["error"] = str(error)
        s["ended_at"] = datetime.now()
        s["duration_sec"] = (s["ended_at"] - s["started_at"]).total_seconds()
        self.overall_status = "FAIL"
        if self.failure_step is None:
            self.failure_step = s["number"]
            self.failure_error = str(error)
        print(f"    ✗ Step {s['number']} FAILED in {s['duration_sec']:.2f}s: {error}")

    # ── API call + assertion capture ──────────────────────────

    def log_api_call(self, call: APICall):
        """Attach an APICall to the current (last) step and stream a
        human-readable summary (plus truncated request/response bodies)
        to stdout so the dashboard's live terminal-log view sees them.

        Volume control:
          - Each body is rendered as compact JSON, capped at ~700 chars.
          - Synthetic INFO entries (auth/discovery summaries with
            method='INFO' or no real bodies) get the one-liner only.
          - Set CCI_API_LOG_FULL=1 to disable truncation entirely.
        """
        if not self.steps:
            return
        self.steps[-1]["api_calls"].append(call)
        tag = "ok" if (call.status_code and 200 <= call.status_code < 300) else "err"
        print(
            f"    → {call.method} {call.name}  [{call.status_code or '?'}]  "
            f"{call.duration_ms}ms  ({tag})"
        )

        # Synthetic INFO rows (auth, namespace probes) carry no request
        # body the user cares about — skip the body dump for them.
        if call.method == "INFO":
            return

        full = os.getenv("CCI_API_LOG_FULL", "").lower() in ("1", "true", "yes")
        cap = None if full else 700

        def _fmt(prefix: str, body: Any) -> None:
            if body is None or body == "":
                return
            try:
                text = body if isinstance(body, str) else __import__("json").dumps(
                    body, default=str, separators=(",", ":")
                )
            except Exception:
                text = str(body)
            # Collapse any newlines so the terminal renderer can treat
            # each printed line independently for syntax-colouring.
            text = text.replace("\n", " ").replace("\r", "")
            full_len = len(text)
            if cap is not None and full_len > cap:
                text = f"{text[:cap]} … (truncated, {full_len} chars total)"
            print(f"        {prefix}: {text}")

        _fmt("REQ ", call.request_body)
        _fmt("RES ", call.response_body)
        if call.error:
            print(f"        ERR : {call.error}")

    def add_assertion(self, description: str, passed: bool):
        if not self.steps:
            return
        self.steps[-1]["assertions"].append({"description": description, "passed": passed})

    def add_record(
        self,
        label: str,
        name: str,
        record_id: str | None = None,
        url: str | None = None,
        object_type: str | None = None,
        step_number: int | None = None,
    ):
        """Attach a Salesforce record link to a step (same contract as StepTracker)."""
        if not self.steps:
            return
        final_url = url
        if not final_url and record_id:
            base = _resolve_org_base_url()
            if base:
                obj = object_type or "Record"
                final_url = f"{base}/lightning/r/{obj}/{record_id}/view"
        target = None
        if step_number is not None:
            for s in self.steps:
                if s.get("number") == step_number:
                    target = s
                    break
        if target is None:
            target = self.steps[-1]
        target.setdefault("records", []).append(
            {"label": label, "name": name, "url": final_url}
        )

    # ── Aggregates (used by the reporter) ────────────────────

    @property
    def passed_steps(self) -> int:
        return sum(1 for s in self.steps if s["status"] == "PASS")

    @property
    def failed_steps(self) -> int:
        return sum(1 for s in self.steps if s["status"] == "FAIL")

    @property
    def total_duration(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return (datetime.now() - self.start_time).total_seconds()

    @property
    def total_api_calls(self) -> int:
        return sum(len(s.get("api_calls", [])) for s in self.steps)

    def finish(self):
        self.end_time = datetime.now()
