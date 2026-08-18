"""
StepRunner — context manager that wraps the per-step bookkeeping.

Before, every step in a test looked like:

    tracker.start_step(1, "Login", "Authenticate to Salesforce")
    try:
        # ... actual work ...
        tracker.add_assertion("Logged in", True)
        tracker.pass_step(sf.screenshot("01_logged_in"))
    except Exception as e:
        tracker.fail_step(str(e), sf.screenshot("01_login_FAILED"))
        pytest.fail(f"Step 1 - Login: {e}")

After, it looks like:

    with sf.step(1, "Login"):
        # ... actual work ...
        sf.assert_("Logged in", True)

The context manager:
  - Calls tracker.start_step(number, label, description) on enter.
  - On clean exit: snaps a screenshot named ``"{NN}_{slug(label)}"``,
    calls tracker.pass_step(screenshot_path).
  - On exception: snaps ``"{NN}_{slug(label)}_FAILED"``, calls
    tracker.fail_step(error, screenshot_path), then re-raises as a
    pytest.fail() so the test stops and the report shows the right step.

Use ``sf.assert_(description, condition)`` inside the block to attach
assertions without typing ``tracker.add_assertion`` every time.

For API tests, this same module is used (the tracker has the same
contract), but screenshots are skipped (API tests have no page).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator, Optional

if TYPE_CHECKING:  # pragma: no cover — type hints only
    from src.core.step_tracker import StepTracker


def _slug(label: str, max_len: int = 50) -> str:
    """Filename-safe slug from a step label."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").lower()
    return s[:max_len] or "step"


class StepRunner:
    """Holds a tracker + screenshot taker, hands out ``step`` context
    managers. Bound to a per-test instance by the ``sf`` fixture.

    Parameters
    ----------
    tracker : StepTracker
        The active step tracker fixture from conftest.
    screenshot_taker : callable or None
        ``f(name) -> str`` returning a path to a saved screenshot.
        ``None`` for API tests (no browser to screenshot).
    """

    def __init__(
        self,
        tracker: "StepTracker",
        screenshot_taker: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._tracker = tracker
        self._screenshot = screenshot_taker

    @contextmanager
    def step(
        self,
        number: int,
        label: str,
        description: str = "",
    ) -> Iterator[None]:
        """Run a test step with auto-bookkeeping.

        On enter
            tracker.start_step(number, label, description) is called
            so the dashboard sees the step start immediately.

        On clean exit
            A screenshot named ``"{NN}_{slug}"`` is captured (if a
            screenshot_taker was provided), then tracker.pass_step(path)
            is called.

        On exception
            A failure screenshot named ``"{NN}_{slug}_FAILED"`` is
            captured, tracker.fail_step(str(exc), path) is called, and
            the exception is re-raised. The test class's pytest hook
            then turns this into a pytest.fail() with the right step
            number.

        Use ``sf.assert_(description, condition)`` inside the block to
        attach assertions without manual tracker.add_assertion calls.
        """
        nn = f"{number:02d}"
        slug = _slug(label)
        self._tracker.start_step(number, label, description)
        try:
            yield
        except Exception as exc:
            path = self._screenshot(f"{nn}_{slug}_FAILED") if self._screenshot else None
            self._tracker.fail_step(str(exc), path)
            # Re-raise so pytest sees a failure. The tracker fixture's
            # teardown converts overall_status=FAIL into pytest.fail().
            raise
        else:
            path = self._screenshot(f"{nn}_{slug}") if self._screenshot else None
            self._tracker.pass_step(path)

    def assert_(self, description: str, passed: bool) -> None:
        """Convenience wrapper around tracker.add_assertion."""
        self._tracker.add_assertion(description, bool(passed))


# Module-level alias so plain ``from src.core.sf_ui import step`` works
# too. ``step`` here is the @contextmanager decorator — callers can
# pass it a tracker + screenshot_taker on demand if they need a runner
# without going through the ``sf`` fixture.
@contextmanager
def step(
    tracker: "StepTracker",
    number: int,
    label: str,
    description: str = "",
    *,
    screenshot_taker: Optional[Callable[[str], str]] = None,
) -> Iterator[None]:
    """Standalone step context manager. Equivalent to constructing a
    StepRunner and calling .step()."""
    runner = StepRunner(tracker, screenshot_taker)
    with runner.step(number, label, description):
        yield
