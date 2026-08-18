"""
Step Tracker — records result, screenshot, duration per test step.

Used by conftest.py to auto-track test progress. Test files interact
with the tracker via the `tracker` pytest fixture.
"""

import os
from datetime import datetime


def _resolve_org_base_url() -> str:
    """Return the Salesforce org base URL (no trailing slash).

    Derived from SF_LOGIN_URL. Used to build /lightning/r/{Obj}/{id}/view links.
    """
    url = os.getenv("SF_LOGIN_URL", "").strip()
    if not url:
        return ""
    # Strip trailing slash
    return url.rstrip("/")


class StepTracker:
    """Track each test step with status, screenshot, timing, and error info."""

    def __init__(self, test_name: str = "Unnamed Test"):
        self.steps: list[dict] = []
        self.test_name = test_name
        self.start_time = datetime.now()
        self.end_time = None
        self.overall_status = "PASS"
        self.failure_step = None
        self.failure_error = None

    def start_step(self, number: int, name: str, description: str = ""):
        self.steps.append({
            "number": number,
            "name": name,
            "description": description,
            "status": "RUNNING",
            "screenshot": None,
            "started_at": datetime.now(),
            "ended_at": None,
            "duration_sec": 0,
            "error": None,
            "assertions": [],
            "records": [],  # list of {"label","name","url"} — Salesforce record links
        })
        desc_suffix = f" - {description}" if description else ""
        print(f"\n  ▶ Step {number}: {name}{desc_suffix}")

    def add_record(
        self,
        label: str,
        name: str,
        record_id: str = None,
        url: str = None,
        object_type: str = None,
        step_number: int = None,
    ):
        """Attach a Salesforce record reference to a test step.

        Renders as a clickable link ("{label}: {name}") in the HTML report.

        Args:
            label:       Human-readable label, e.g., "Account", "Quote Name".
            name:        The record's display name, e.g., "SFAUTO_Quote_0416_230247".
            record_id:   15/18-char Salesforce record ID. If provided together
                         with object_type, a /lightning/r/.../view URL is built.
            url:         Optional full URL. Takes precedence over record_id.
            object_type: Salesforce object API name, e.g. "Account",
                         "Opportunity", "SBQQ__Quote__c". Required (with
                         record_id) when url is not supplied.
            step_number: Optional — attach to a specific step by its number
                         (useful when the record ID is only discovered in a
                         later step but the name was shown in an earlier one).
                         Defaults to the current (latest) step.
        """
        if not self.steps:
            return  # no active step — silently skip
        final_url = url
        if not final_url and record_id:
            base = _resolve_org_base_url()
            if base:
                obj = object_type or "Record"
                final_url = f"{base}/lightning/r/{obj}/{record_id}/view"
        # Resolve target step
        target = None
        if step_number is not None:
            for s in self.steps:
                if s.get("number") == step_number:
                    target = s
                    break
        if target is None:
            target = self.steps[-1]
        target.setdefault("records", []).append({
            "label": label,
            "name": name,
            "url": final_url,
            "record_id": record_id,
            "object_type": object_type,
        })
        # Registering a record also enrolls it for teardown cleanup — see
        # the _cleanup_records fixture in tests/conftest.py. Tests that
        # create data are expected to declare it here anyway (it is what
        # renders the record links in the report), so cleanup comes free.
        if record_id and object_type:
            if not hasattr(self, "created_records"):
                self.created_records = []
            self.created_records.append((object_type, record_id, name))

    def pass_step(self, screenshot_path: str = None):
        step = self.steps[-1]
        step["ended_at"] = datetime.now()
        step["duration_sec"] = round(
            (step["ended_at"] - step["started_at"]).total_seconds(), 1
        )
        step["screenshot"] = screenshot_path

        # If any assertion failed within this step, mark it FAIL automatically.
        failed = [a for a in step["assertions"] if not a.get("passed", True)]
        if failed:
            step["status"] = "FAIL"
            err = "; ".join(f.get("description", "assertion failed") for f in failed[:5])
            step["error"] = f"{len(failed)} assertion(s) failed: {err}"
            self.overall_status = "FAIL"
            if self.failure_step is None:
                self.failure_step = step["number"]
                self.failure_error = step["error"]
            print(f"    ✖ FAIL ({step['duration_sec']}s): {step['error'][:150]}")
        else:
            step["status"] = "PASS"
            print(f"    ✔ PASS ({step['duration_sec']}s)")

    def fail_step(self, error: str, screenshot_path: str = None):
        step = self.steps[-1]
        step["status"] = "FAIL"
        step["ended_at"] = datetime.now()
        step["duration_sec"] = round(
            (step["ended_at"] - step["started_at"]).total_seconds(), 1
        )
        step["error"] = error
        step["screenshot"] = screenshot_path
        self.overall_status = "FAIL"
        if self.failure_step is None:
            self.failure_step = step["number"]
            self.failure_error = error
        print(f"    ✖ FAIL ({step['duration_sec']}s): {error[:120]}")

    def add_assertion(self, description: str, passed: bool):
        self.steps[-1]["assertions"].append(
            {"description": description, "passed": passed}
        )

    def finalize(self):
        self.end_time = datetime.now()
        total = len(self.steps)
        passed = self.passed_steps
        failed = self.failed_steps
        duration = self.total_duration
        status = "PASSED" if self.overall_status == "PASS" else "FAILED"
        print(f"\n  Result: {status} ({passed}/{total} steps passed, {duration}s)")

    @property
    def total_duration(self) -> float:
        if self.end_time and self.start_time:
            return round((self.end_time - self.start_time).total_seconds(), 1)
        return 0

    @property
    def passed_steps(self) -> int:
        return sum(1 for s in self.steps if s["status"] == "PASS")

    @property
    def failed_steps(self) -> int:
        return sum(1 for s in self.steps if s["status"] == "FAIL")
