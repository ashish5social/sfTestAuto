"""
Pytest-Playwright configuration & framework orchestrator for CCI Test Automation.

This is the ONLY place where framework concerns live:
  - Browser launch config (maximized, no viewport)
  - Video recording (auto-enabled for every test)
  - StepTracker injection (via `tracker` fixture)
  - Screenshot/wait helpers (via `sf` fixture)
  - HTML report generation (auto-runs after every test that uses `tracker`)

Generated test files in tests/generated/ should contain ONLY pure
Playwright test logic. They receive `tracker` and `sf` as fixtures.
"""

import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Ensure project root is on sys.path so src.core imports work
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env from project root so SF_USERNAME / SF_PASSWORD are available
load_dotenv(PROJECT_ROOT / ".env")

from src.core.step_tracker import StepTracker
from src.core.html_reporter import generate_html_report
from src.core.playwright_helpers import (
    screenshot, wait_spinner, wait_page_ready,
    click_shadow_button, click_shadow_order_link,
    compare_screenshot,
)
# sf_ui library — high-level Salesforce / Vlocity test helpers. Tests
# call into this via the SFHelpers methods below (sf.step, sf.click,
# sf.fill, ...) or import the modules directly. See docs/WRITING_TESTS.md.
from src.core.sf_ui.step import StepRunner
from src.core.sf_ui import auth as sfui_auth
from src.core.sf_ui import navigation as sfui_nav
from src.core.sf_ui import forms as sfui_forms
from src.core.sf_ui import actions as sfui_actions
from src.core.sf_ui import waits as sfui_waits
from src.core.sf_ui import cart as sfui_cart

# API test fixtures (parallel to UI tracker/sf) — kept isolated in src.api so
# UI tests never import them and API tests never pull in Playwright/reporter.
from src.api.api_tracker import APITracker
from src.api.sf_api_client import SFApiClient
from src.api.api_reporter import generate_api_report


# Output directories — can be overridden via env vars for CI environments
import os
_output_base = Path(os.environ.get("CCI_OUTPUT_DIR", str(PROJECT_ROOT)))
VIDEO_TEMP_DIR = _output_base / "videos_tmp"
REPORT_DIR = _output_base / "reports"
SCREENSHOTS_BASE = _output_base / "screenshots"

# Module-level store for pending report data.
# After tracker teardown we have everything except the video file (page still open).
# The pytest_runtest_makereport hook fires after ALL fixtures (including page)
# are torn down, so we can safely copy the video and regenerate the report.
_pending_reports: dict[str, dict] = {}


# ── Register --headless as a pytest CLI option ────────────────────────────────

def pytest_addoption(parser):
    """Register --headless so `python -m pytest --headless` works.
    When passed, pytest-playwright runs in headless mode instead of headed.

    Also registers ``--update-goldens`` for the visual-regression helper:
    when the flag is passed, every call to ``sf.screenshot_with_golden(...)``
    treats its current frame as the new baseline (overwriting any
    existing golden). Equivalent to setting ``UPDATE_GOLDENS=true``.
    """
    parser.addoption(
        "--headless",
        action="store_true",
        default=False,
        help="Run browser in headless mode (default: headed)",
    )
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Visual regression: overwrite all golden images with the current run's screenshots",
    )


def pytest_configure(config):
    """Propagate --update-goldens to the env var the helper checks.
    Using an env var keeps the helper testable in isolation (without a
    pytest config object) while still letting CLI users pass --update-goldens."""
    if config.getoption("--update-goldens"):
        import os as _os
        _os.environ["UPDATE_GOLDENS"] = "true"


# ── Browser launch configuration ─────────────────────────────────────────────

def _active_browser_name(request) -> str:
    """Identify which Playwright browser this session is running with.

    Order of precedence:
      1. pytest-playwright's --browser CLI flag (used by parallel_runner)
      2. UI_TEST_BROWSER env (set by parallel_runner as a fallback)
      3. "chromium" — pytest-playwright's default
    """
    try:
        sel = request.config.getoption("--browser")
        if sel:
            # pytest-playwright returns a list — take the first.
            name = sel[0] if isinstance(sel, (list, tuple)) else sel
            return str(name).lower()
    except Exception:
        pass
    env = os.environ.get("UI_TEST_BROWSER", "").lower().strip()
    if env in ("chrome", "edge", "msedge", "chromium"):
        return "chromium"
    if env in ("firefox",):
        return "firefox"
    if env in ("webkit", "safari"):
        return "webkit"
    return "chromium"


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, request):
    """Per-browser launch arguments.

    Chromium / Chrome / Edge get the standard CCI flag set:
      --deny-permission-prompts   silently deny geolocation/notifications
      --no-sandbox                required in GitHub Actions / Docker
      --disable-dev-shm-usage     CI containers have tiny /dev/shm
      --start-maximized           headed only — needs a real display

    Firefox and WebKit don't accept any of those flags. They get an
    empty args list — their equivalent of "deny permissions" lives in
    browser_context_args (see below).
    """
    headless = (
        request.config.getoption("--headless")
        or os.environ.get("BROWSER_HEADLESS", "").lower() == "true"
    )
    browser_name = _active_browser_name(request)
    args_list = list(browser_type_launch_args.get("args", []))
    if browser_name == "chromium":
        args_list.extend([
            "--deny-permission-prompts",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ])
        if not headless:
            args_list.append("--start-maximized")
    # Firefox / WebKit: nothing extra. Their security model handles
    # most popups out of the box and they reject Chromium flags with
    # a launch error if we sneak them in.
    out = {**browser_type_launch_args, "args": args_list}
    if not headless:
        out["headless"] = False
    return out


# Detect headless mode at module level for use in context fixture
_HEADLESS = os.environ.get("BROWSER_HEADLESS", "").lower() == "true"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, request):
    """Configure viewport and video recording.

    - Headed mode: no_viewport=True so the browser fills the maximized window.
    - Headless mode (CI): explicit 1920×1080 viewport so the page renders at
      full size — --start-maximized has no effect without a real display.
    """
    VIDEO_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    headless = request.config.getoption("--headless") or _HEADLESS

    ctx = {
        **browser_context_args,
        "record_video_dir": str(VIDEO_TEMP_DIR),
        "record_video_size": {"width": 1280, "height": 720},
        # Permissions are denied at browser launch level via --deny-permission-prompts.
        # To ALLOW specific permissions instead, remove that launch arg and uncomment:
        #   "permissions": ["geolocation", "notifications"],
    }

    if headless:
        # Set an explicit large viewport for headless (no real window to maximize)
        ctx["viewport"] = {"width": 1920, "height": 1080}
    else:
        # Headed: let the browser fill the maximized OS window
        ctx["no_viewport"] = True

    return ctx


# ── StepTracker fixture ─────────────────────────────────────────────────────

@pytest.fixture()
def tracker(request, page):
    """
    Provides a StepTracker instance to the test.

    On teardown:
      1. Finalizes the tracker
      2. Captures the temp video path (page still open, so save_as won't work yet)
      3. Generates HTML report WITHOUT video
      4. Queues a pending report so the pytest hook can add video after page closes

    Tests use this to call tracker.start_step(), tracker.pass_step(), etc.
    """
    # Add a short uuid suffix so 4 parallel tests starting in the same
    # second don't fight over the same report/screenshot path.
    run_ts = datetime.now().strftime("%m%d_%H%M%S") + f"_{uuid.uuid4().hex[:4]}"

    # Try to get a human-friendly name from the test class or function
    test_name = "Unnamed Test"
    if request.cls and request.cls.__doc__:
        test_name = request.cls.__doc__.strip().split("\n")[0]
    elif request.node.name:
        test_name = request.node.name.replace("_", " ").title()

    t = StepTracker(test_name=test_name)
    t._run_timestamp = run_ts
    yield t

    # ── Teardown ──
    t.finalize()

    # If any step was marked FAIL (either via fail_step or via failed assertions
    # surfaced in pass_step), raise so pytest marks the test FAILED too. This
    # keeps the dashboard verdict honest: a test with any failing step is a fail.
    _tracker_status = t.overall_status
    _tracker_failure_step = t.failure_step
    _tracker_failure_error = t.failure_error

    if not t.steps:
        return

    extra_data = getattr(t, "extra_data", None)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_file = REPORT_DIR / f"test_report_{run_ts}.html"

    # Grab the temp video path (file is still being written)
    temp_video_path = None
    try:
        if page.video:
            temp_video_path = str(page.video.path())
    except Exception:
        pass

    # Determine where the final video should live
    screenshot_dir = SCREENSHOTS_BASE / f"Test_CCI_UI_{run_ts}"
    video_dest = str(screenshot_dir / f"recording_{run_ts}.webm")

    # Generate report WITHOUT video for now (so it exists even if video copy fails)
    generate_html_report(t, report_file, video_path=None, extra_data=extra_data)

    # Queue for the hook to add video after page closes
    if temp_video_path:
        _pending_reports[request.node.nodeid] = {
            "tracker": t,
            "report_file": report_file,
            "temp_video_path": temp_video_path,
            "video_dest": video_dest,
            "screenshot_dir": screenshot_dir,
            "extra_data": extra_data,
        }
    else:
        # No video expected — print report link now
        print(f"\n  Report: {report_file}")

    # Finally, if the tracker recorded any failure, fail the test so pytest
    # reports it accurately (and the dashboard reflects real pass/fail counts).
    if _tracker_status == "FAIL":
        pytest.fail(
            f"Test failed at step {_tracker_failure_step}: {_tracker_failure_error}",
            pytrace=False,
        )


# ── Pytest hook: runs after ALL teardown (including page close) ──────────────

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    After the teardown phase completes, the page is closed and the video
    file is finalized on disk. Now we can safely copy it and regenerate
    the HTML report with the embedded video player.
    """
    outcome = yield
    report = outcome.get_result()

    # Only act after the teardown phase
    if report.when != "teardown":
        return

    pending = _pending_reports.pop(item.nodeid, None)
    if not pending:
        return

    temp_video = pending["temp_video_path"]
    video_dest = pending["video_dest"]
    screenshot_dir = pending["screenshot_dir"]

    # Copy the finalized video from Playwright's temp dir, compressing with
    # ffmpeg if available to keep the HTML report under GitHub's 100 MB limit.
    video_path = None
    try:
        temp_path = Path(temp_video)
        if temp_path.exists() and temp_path.stat().st_size > 0:
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            # Try ffmpeg compression first (target ~15 MB for a ~20 min test)
            compressed = False
            try:
                import subprocess
                result = subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", str(temp_path),
                        "-c:v", "libvpx-vp9",
                        "-b:v", "200k",      # low bitrate — UI tests don't need high quality
                        "-crf", "40",         # quality level (higher = smaller)
                        "-r", "10",           # 10 fps is plenty for UI automation replay
                        "-vf", "scale=1280:720",
                        "-an",                # strip audio (not needed)
                        str(video_dest),
                    ],
                    capture_output=True, timeout=120,
                )
                if result.returncode == 0 and Path(video_dest).stat().st_size > 0:
                    compressed = True
                    orig_mb = temp_path.stat().st_size / (1024 * 1024)
                    new_mb = Path(video_dest).stat().st_size / (1024 * 1024)
                    print(f"  Video compressed: {orig_mb:.1f} MB -> {new_mb:.1f} MB")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass  # ffmpeg not installed or timed out — fall back to copy

            if not compressed:
                shutil.copy2(str(temp_path), video_dest)
            video_path = video_dest
    except Exception:
        pass

    # Regenerate the HTML report WITH video embedded
    if video_path:
        try:
            generate_html_report(
                pending["tracker"],
                pending["report_file"],
                video_path=video_path,
                extra_data=pending["extra_data"],
            )
        except Exception:
            pass

    print(f"\n  Report: {pending['report_file']}")


# ── Salesforce helpers fixture (screenshot, wait functions pre-bound) ────────

class SFHelpers:
    """Convenience wrapper around Playwright helpers with directory pre-bound.

    Combines:
      - The old back-compat methods (screenshot, wait_spinner, ...) that
        existing tests (TC2/3/4 until refactored) still call.
      - The new sf_ui library exposed as instance methods (sf.step, sf.click,
        sf.fill, sf.fill_lookup, sf.set_picklist, ...) so a test reads as:

            with sf.step(1, "Login"):
                sf.login()

            with sf.step(2, "Create account"):
                sf.fill("Account Name", "CCIAUTO_001")
                sf.click("Save")

        Direct module access is also fine when a test needs the raw
        function:

            from src.core.sf_ui.cart import add_product_to_cart
            add_product_to_cart(page, "Dedicated Internet Access")

    Read ``docs/WRITING_TESTS.md`` for the full method catalog and the
    "how do I…" recipes.
    """

    def __init__(self, page, screenshot_dir: Path):
        self._page = page
        self._screenshot_dir = screenshot_dir
        # _tracker and _test_stem are populated by the `sf` fixture once
        # the matching tracker is built. The StepRunner (sf.step) needs
        # both. Until then sf.step() will raise — that's intentional;
        # call sf.step() only from inside a test method that already has
        # the tracker fixture wired in.
        self._tracker = None
        self._test_stem = None
        self._step_runner = None  # lazy-initialised when tracker is set

    # ── Back-compat: existing tests use these names ────────────────────

    def screenshot(self, name: str, page=None) -> str:
        """Take a screenshot and return its path. Pass an alternate
        ``page`` to screenshot a different tab (e.g. a popup window).
        """
        return screenshot(page or self._page, name, self._screenshot_dir)

    def wait_spinner(self, timeout: int = 30000):
        sfui_waits.wait_spinner(self._page, timeout)

    def wait_page_ready(self, extra_ms: int = 2000):
        sfui_waits.wait_page_ready(self._page, extra_ms)

    def click_shadow_button(self, button_text: str):
        sfui_actions.click_shadow_button(self._page, button_text)

    def click_shadow_order_link(self) -> str:
        return sfui_actions.click_shadow_order_link(self._page)

    # ── sf_ui: step lifecycle ──────────────────────────────────────────

    def step(self, number: int, label: str, description: str = ""):
        """Open a step context manager. See StepRunner.step() docs.

        Usage:
            with sf.step(1, "Login to Salesforce"):
                sf.login()
                sf.assert_("on Lightning", "lightning" in page.url)
        """
        if self._step_runner is None:
            if self._tracker is None:
                raise RuntimeError(
                    "sf.step() requires a tracker — make sure your test "
                    "uses the `tracker` fixture (or `api_tracker` for API tests)."
                )
            self._step_runner = StepRunner(self._tracker, self.screenshot)
        return self._step_runner.step(number, label, description)

    def assert_(self, description: str, passed: bool) -> None:
        """Attach an assertion to the current step."""
        if self._tracker is None:
            raise RuntimeError("sf.assert_() requires a tracker")
        self._tracker.add_assertion(description, bool(passed))

    # ── sf_ui: auth ────────────────────────────────────────────────────

    def login(self, **overrides) -> str:
        """Login to Salesforce. Picks up credentials from env vars
        (SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN, SF_CLIENT_ID,
        SF_CLIENT_SECRET, SF_LOGIN_URL) and falls back to the
        frontdoor bypass when the org requires identity verification.
        Pass keyword overrides to supply credentials directly.

        Returns 'standard' or 'frontdoor' indicating which path was used.
        """
        creds = sfui_auth.env_credentials()
        creds.update(overrides)
        return sfui_auth.login_with_frontdoor_fallback(self._page, **creds)

    # ── sf_ui: navigation ──────────────────────────────────────────────

    def open_list_view(self, sobject: str, **kwargs):
        sfui_nav.open_list_view(self._page, sobject, **kwargs)

    def open_record(self, sobject: str, record_id: str, **kwargs):
        sfui_nav.open_record(self._page, sobject, record_id, **kwargs)

    def extract_record_id(self, url: str = None, sobject: str = None):
        """Pull the record id from the given URL (defaults to current
        page URL). Pass ``sobject="Account"`` to match strictly."""
        return sfui_nav.extract_record_id_from_url(
            url or self._page.url, sobject=sobject,
        )

    # ── sf_ui: forms ───────────────────────────────────────────────────

    def fill(self, label: str, value: str) -> bool:
        """Fill a text/textarea field by visible label."""
        return sfui_forms.fill_field_by_label(self._page, label, value)

    def fill_date(self, label: str, value: str) -> bool:
        return sfui_forms.fill_date_field(self._page, label, value)

    def fill_lookup(self, label: str, search_value: str, **kwargs) -> bool:
        return sfui_forms.fill_lookup(self._page, label, search_value, **kwargs)

    def select_record_type(self, value: str) -> bool:
        return sfui_forms.select_record_type(self._page, value)

    def set_picklist(self, label: str, value: str) -> bool:
        return sfui_forms.select_picklist(self._page, label, value)

    def set_stage(self, value: str) -> bool:
        return sfui_forms.set_stage(self._page, value)

    def wait_form_ready(self, required_labels, **kwargs):
        sfui_forms.wait_for_form_dialog_ready(self._page, required_labels, **kwargs)

    # ── sf_ui: actions ─────────────────────────────────────────────────

    def click(self, name, timeout_ms: int = 10000) -> bool:
        """Click any button / link / Aura action by visible text. Returns
        True if something was clicked, False if nothing matched."""
        return sfui_actions.click_button(self._page, name, timeout_ms)

    # ── sf_ui: waits ───────────────────────────────────────────────────

    def wait_for_toast(self, text, **kwargs) -> bool:
        return sfui_waits.wait_for_toast(self._page, text, **kwargs)

    def wait_for_config_update(self, timeout_ms: int = 45000):
        sfui_waits.wait_for_config_update_complete(self._page, timeout_ms)

    def wait_until(self, predicate, **kwargs):
        sfui_waits.wait_until(predicate, **kwargs)

    # ── sf_ui: cart ────────────────────────────────────────────────────

    def search_catalog(self, search_term: str, **kwargs) -> bool:
        return sfui_cart.search_catalog(self._page, search_term, **kwargs)

    def add_product_to_cart(self, product_text: str, **kwargs) -> bool:
        return sfui_cart.add_product_to_cart(self._page, product_text, **kwargs)

    def configure_attr(self, label: str, value: str, **kwargs) -> bool:
        return sfui_cart.configure_attribute(self._page, label, value, **kwargs)

    def wait_summary_loaded(self, expected_products=None):
        sfui_cart.wait_summary_loaded(self._page, expected_products)

    def screenshot_with_golden(self, name: str, *, threshold_pct: float = 0.02) -> dict:
        """Visual-regression screenshot: capture + diff against the
        stored golden image for the active test.

        Goldens live alongside the test file at
        ``tests/ui/goldens/{test_stem}/{safe_name}.png``. Pass
        ``UPDATE_GOLDENS=true`` (env) or ``--update-goldens`` on the
        pytest command line to (re)create the goldens for the current
        run.

        Returns the same dict ``compare_screenshot`` returns. The result
        is automatically appended to the current tracker step so the
        HTML report can show the three-pane (current/golden/diff)
        comparison."""
        # Resolve the test stem from the StepTracker's run timestamp
        # (so a single test's goldens live in one folder regardless
        # of which step is calling).
        test_stem = (
            getattr(self, "_test_stem", None)
            or "unknown_test"
        )
        # tests/ui/goldens/{test_stem}/
        project_root = Path(__file__).parent.parent if False else PROJECT_ROOT
        golden_dir = project_root / "tests" / "ui" / "goldens" / test_stem
        result = compare_screenshot(
            self._page,
            name,
            screenshot_dir=self._screenshot_dir,
            golden_dir=golden_dir,
            threshold_pct=threshold_pct,
        )
        # Attach to current step (if a tracker is in scope).
        tr = getattr(self, "_tracker", None)
        if tr and tr.steps:
            tr.steps[-1].setdefault("golden_diffs", []).append(result)
        return result


@pytest.fixture()
def sf(request, page, tracker):
    """
    Provides SFHelpers with screenshot directory pre-configured.
    Uses the same timestamp as the tracker for consistent naming.

    Usage in tests:
        shot = sf.screenshot("01_logged_in")
        sf.wait_page_ready(3000)
        sf.wait_spinner()
        sf.screenshot_with_golden("01_logged_in")   # visual regression
    """
    screenshot_dir = SCREENSHOTS_BASE / f"Test_CCI_UI_{tracker._run_timestamp}"
    helpers = SFHelpers(page, screenshot_dir)
    # Stash references the visual-regression helper needs. We deliberately
    # set these as private attributes rather than constructor args to keep
    # the SFHelpers class easy to instantiate from non-pytest code.
    helpers._tracker = tracker
    # Goldens are filed under tests/ui/goldens/{test_stem}/. test_stem is
    # derived from the actual test filename so each test owns its own
    # baselines and they don't collide with another test's screenshots.
    test_path = Path(request.fspath)
    helpers._test_stem = test_path.stem
    return helpers


# ── Live CDP screencast ──────────────────────────────────────────────────
#
# When UI_TEST_RUN_ID is set (the FastAPI dashboard injects it on every
# run), attach a Chrome DevTools Protocol screencast to the active page
# and POST each frame to the FastAPI bridge. The bridge fans the frames
# out over WebSocket to any connected browser tabs that are watching the
# run. Browser is launched headless on the server (CDP is a separate
# channel from the OS window manager — frames still flow).
#
# Cost: ~50-150 KB per JPEG at quality=80 / 1280x720, native FPS. On a
# local-only loopback rig the bandwidth is negligible.

@pytest.fixture(autouse=True)
def _live_screencast(request):
    """No-op unless UI_TEST_RUN_ID is set. Attaches CDP screencast →
    POSTs JPEG frames to FastAPI for live browser streaming.

    Suppressed when ``SCREENCAST_DISABLED=true`` or ``CI=true`` is set
    in the environment — CI runners have no dashboard to stream to, and
    the JPEG-per-frame upload would waste CPU and memory we'd rather
    spend on running tests in parallel. Video recording still happens
    via the Playwright recorder, so the per-test HTML report retains an
    embedded ``<video>`` regardless.
    """
    run_id = os.getenv("UI_TEST_RUN_ID")
    if not run_id:
        yield
        return

    # CI / explicit-disable bypass.
    if (
        os.getenv("SCREENCAST_DISABLED", "").lower() == "true"
        or os.getenv("CI", "").lower() == "true"
    ):
        yield
        return

    # Non-Chromium browsers don't expose CDP at all. Firefox uses a
    # different remote-debugging protocol; WebKit has no protocol at
    # all. Attempting page.context.new_cdp_session() would throw with
    # an unhelpful "CDP not supported" message. Skip cleanly instead.
    is_chromium = os.getenv("UI_TEST_BROWSER_IS_CHROMIUM", "").lower()
    if is_chromium == "false":
        print(
            f"[screencast] Skipped — browser '{os.getenv('UI_TEST_BROWSER', '?')}' "
            "is not Chromium-based, CDP unavailable. The recorded video + "
            "per-test HTML report still work."
        )
        yield
        return

    # Only attach if the test actually uses the `page` fixture (UI tests).
    # API-only tests don't launch Playwright, so there's no page to attach to.
    page = None
    try:
        if "page" in request.fixturenames:
            page = request.getfixturevalue("page")
    except Exception:
        page = None
    if page is None:
        yield
        return

    import base64 as _b64
    import threading
    try:
        import urllib.request as _ur
    except Exception:
        yield
        return

    bridge = (
        os.getenv("UI_TEST_BRIDGE_URL")
        or "http://127.0.0.1:8091"
    ).rstrip("/")
    frame_url = f"{bridge}/api/screencast/{run_id}/frame"

    cdp = None
    try:
        cdp = page.context.new_cdp_session(page)
    except Exception as e:
        print(f"[screencast] Could not open CDP session: {e}")
        yield
        return

    def _post(jpeg_bytes: bytes) -> None:
        try:
            req = _ur.Request(
                frame_url,
                data=jpeg_bytes,
                headers={"Content-Type": "image/jpeg"},
                method="POST",
            )
            _ur.urlopen(req, timeout=2)
        except Exception:
            # Bridge may be down briefly during boot; drop the frame.
            pass

    def on_frame(params):
        try:
            data = params.get("data")
            session_id = params.get("sessionId")
            if data:
                jpeg = _b64.b64decode(data)
                # Don't block CDP thread on POST; spin a one-shot thread
                # so consecutive frames don't queue up if a POST is slow.
                threading.Thread(
                    target=_post, args=(jpeg,), daemon=True
                ).start()
            if session_id is not None:
                cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    try:
        cdp.on("Page.screencastFrame", on_frame)
        # Max-fidelity local screencast: native FPS (Chromium pushes frames
        # whenever the compositor renders one), full 1920x1080 viewport,
        # JPEG quality 80.
        cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 80,
            "maxWidth": 1920,
            "maxHeight": 1080,
            "everyNthFrame": 1,
        })
        _slot = os.getenv("UI_TEST_SLOT")
        slot_label = f" slot={_slot}" if _slot is not None else ""
        print(f"[screencast] Started CDP screencast for run_id={run_id}{slot_label}")
    except Exception as e:
        print(f"[screencast] startScreencast failed: {e}")

    try:
        yield
    finally:
        try:
            cdp.send("Page.stopScreencast", {})
        except Exception:
            pass
        try:
            cdp.detach()
        except Exception:
            pass


# ── Network capture hook (activated by CCI_CAPTURE=1) ────────────────────────
#
# When CCI_CAPTURE=1, this fixture attaches page.on("request") and
# page.on("response") listeners to the active Playwright `page` and records
# every Vlocity / OmniScript / IP / Apex REST call to /tmp/cci_capture.jsonl.
#
# It's autouse=True so we don't need to modify the test file, but it's a
# complete no-op unless the env var is set — zero overhead in normal runs.
# After the run, scripts/capture_tc1_api_calls.py reads /tmp/cci_capture.jsonl
# and summarizes by endpoint.

_CAPTURE_INTERESTING = [
    "/integrationprocedure/", "/apexrest/", "/actions/custom/", "/aura",
    "/services/data/", "/cpq/", "/vlocity_cmt", "/omnistudio",
]


def _capture_is_interesting(url: str) -> bool:
    u = (url or "").lower()
    return any(p in u for p in _CAPTURE_INTERESTING)


def _capture_mask(headers: dict) -> dict:
    """Redact auth-sensitive headers so captures are safe to share."""
    masked = {}
    for k, v in (headers or {}).items():
        kl = k.lower()
        if kl in ("authorization", "cookie", "x-sfdc-session") or "token" in kl:
            masked[k] = "***REDACTED***"
        else:
            masked[k] = v
    return masked


@pytest.fixture(autouse=True)
def _cci_capture(request):
    """No-op unless CCI_CAPTURE=1. Attaches network listeners to `page`."""
    if os.getenv("CCI_CAPTURE") != "1":
        yield
        return

    # Resolve the Playwright page — only active once the `page` fixture has
    # been built. We don't trigger it ourselves (that would launch a browser
    # for API tests too). If the test doesn't use `page`, capture simply
    # doesn't attach for that test.
    page = None
    try:
        if "page" in request.fixturenames:
            page = request.getfixturevalue("page")
    except Exception:
        page = None
    if page is None:
        yield
        return

    import json as _json
    import time as _time
    capture_log = Path("/tmp/cci_capture.jsonl")
    capture_log.parent.mkdir(parents=True, exist_ok=True)
    # Truncate per-test so each run starts clean
    capture_log.write_text("")

    pending = {}

    def on_request(req):
        try:
            if not _capture_is_interesting(req.url):
                return
            body = None
            try:
                body = req.post_data
            except Exception:
                body = None
            pending[req] = {
                "type": "request",
                "ts": _time.time(),
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "headers": _capture_mask(dict(req.headers or {})),
                "body": body,
            }
        except Exception:
            pass

    def on_response(resp):
        try:
            req = resp.request
            if not _capture_is_interesting(req.url):
                return
            body_text = None
            try:
                body_text = resp.text()
            except Exception:
                body_text = None
            rec = pending.pop(req, {
                "type": "request",
                "method": req.method,
                "url": req.url,
                "body": None,
            })
            rec.update({
                "type": "pair",
                "status": resp.status,
                "response_headers": _capture_mask(dict(resp.headers or {})),
                "response_body": body_text,
                "response_ts": _time.time(),
            })
            if rec.get("response_body") and len(rec["response_body"]) > 20000:
                rec["response_body_truncated"] = True
                rec["response_body"] = rec["response_body"][:20000]
            with capture_log.open("a") as f:
                f.write(_json.dumps(rec, default=str) + "\n")
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    print(f"\n  [CCI_CAPTURE=1] Network capture active → {capture_log}")

    yield


# ── API test fixtures ────────────────────────────────────────────────────────
#
# These parallel `tracker` and `sf` but are for API-only tests (like TC3).
# API tests never ask for `page`, so Playwright is never launched — no
# browser, no video, no screenshots. Reports are generated by
# src.api.api_reporter (request/response cards instead of screenshots).

@pytest.fixture()
def api_tracker(request):
    """
    Provides an APITracker instance to an API-only test.

    On teardown:
      1. Finalizes the tracker
      2. Generates an HTML report (no video, just request/response cards)
      3. Fails the test if any step was recorded as FAIL

    Tests use this to call api_tracker.start_step(), pass_step(), etc.
    The sf_api fixture wraps it so every SF call is auto-logged.
    """
    # Suffix avoids report-file collisions in parallel runs (4 tests can
    # all start in the same wall-clock second).
    run_ts = datetime.now().strftime("%m%d_%H%M%S") + f"_{uuid.uuid4().hex[:4]}"

    # Derive a friendly test name
    test_name = "Unnamed API Test"
    if request.cls and request.cls.__doc__:
        test_name = request.cls.__doc__.strip().split("\n")[0]
    elif request.node.name:
        test_name = request.node.name.replace("_", " ").title()

    t = APITracker(test_name=test_name)
    t._run_timestamp = run_ts  # align with UI naming convention
    yield t

    # ── Teardown ──
    t.finish()

    _status = t.overall_status
    _failure_step = t.failure_step
    _failure_error = t.failure_error

    if not t.steps:
        return

    extra_data = getattr(t, "extra_data", None)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_file = REPORT_DIR / f"test_report_api_{run_ts}.html"

    try:
        generate_api_report(t, report_file, extra_data=extra_data)
        print(f"\n  API Report: {report_file}")
    except Exception as e:
        print(f"\n  API Report generation failed: {e}")

    if _status == "FAIL":
        pytest.fail(
            f"API test failed at step {_failure_step}: {_failure_error}",
            pytrace=False,
        )


@pytest.fixture()
def sf_api(api_tracker):
    """
    Provides an SFApiClient bound to the active api_tracker.

    The client authenticates lazily on first use — OAuth client_credentials
    preferred (SF_CLIENT_ID + SF_CLIENT_SECRET), SOAP fallback
    (SF_USERNAME + SF_PASSWORD + SF_SECURITY_TOKEN).

    Every call (soql, create, update, call_ip, ...) is auto-logged to
    api_tracker, so the report shows request body, response body, status,
    and duration without the test having to log manually.

    Usage:
        acc_id = sf_api.create("Account", {"Name": "CCIAUTO_Biz_..."}, name="Create Account")
        body = sf_api.call_ip("Business_CalculateMRRs", {...}, name="IP: CalculateMRRs")
    """
    return SFApiClient(tracker=api_tracker)
