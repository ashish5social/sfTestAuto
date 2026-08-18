"""FastAPI web application for the sfauto Test Runner."""

import logging
import uvicorn
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse

from src.core.config import config
from src.web.routes.generated_tests import router as generated_router
from src.web.routes.screencast import router as screencast_router


# ── Quieten the screencast-frame access logs ──────────────────────────
#
# Each running UI test posts ~30 JPEG frames/sec to /api/screencast/<run>/frame
# so the dashboard can render them live. uvicorn's default access logger
# emits one INFO line per request, which floods the terminal with
# hundreds of "POST /api/screencast/.../frame HTTP/1.1 200 OK" lines per
# second — completely useless noise that buries every other log.
#
# We install a logging.Filter on uvicorn.access that drops records
# matching the screencast-frame endpoint (and the WebSocket frame
# heartbeat) while preserving every other access log (so /api/generated/run,
# /reports/, /screenshots/, errors, etc. still show up).
class _DropScreencastNoise(logging.Filter):
    """Drop access-log records for high-volume screencast endpoints."""

    NOISY_SUBSTRINGS = (
        "/api/screencast/",     # POST per-frame uploads (~30/sec/test)
        "/ws/screencast/",      # WebSocket per-frame fan-out
    )

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access formats the message via record.getMessage(); the
        # raw args also contain the request line. Check both so we don't
        # rely on internal format changes between uvicorn versions.
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if any(s in msg for s in self.NOISY_SUBSTRINGS):
            return False
        return True


# Install the filter early — uvicorn creates the access logger on import
# of uvicorn.config, so getting the logger here and attaching the filter
# is enough; the filter applies to every record uvicorn emits later.
logging.getLogger("uvicorn.access").addFilter(_DropScreencastNoise())

app = FastAPI(
    title="sfauto",
    description="UI & API test automation for any Salesforce org",
    version="0.2.0",
)

# Test runner routes
app.include_router(generated_router)
# Live screencast bridge: HTTP POST (test process) + WebSocket (browser)
# Routes: POST /api/screencast/{run_id}/frame, WS /ws/screencast/{run_id}
app.include_router(screencast_router)

# Static files
FRONTEND_DIR = Path(__file__).parent / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Serve screenshots — always create + mount so the post-run video player
# (which requests /screenshots/Test_UI_*/recording_*.webm) works on a
# fresh clone before any tests have produced output.
config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(config.SCREENSHOTS_DIR)), name="screenshots")

# Serve HTML reports (so test reports can open in browser)
config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=str(config.REPORTS_DIR)), name="reports")

# Brand assets (logo / favicon). Served locally so the dashboard never
# hot-links an external CDN.
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/")
async def index():
    """Redirect to the test runner SPA."""
    return RedirectResponse(url="/runner")


@app.get("/runner", response_class=HTMLResponse)
async def test_runner():
    """Serve the generated-test runner SPA."""
    runner_path = FRONTEND_DIR / "runner.html"
    if runner_path.exists():
        return HTMLResponse(runner_path.read_text())
    return HTMLResponse("<h1>Test Runner</h1><p>runner.html not found.</p>")


@app.get("/health")
def health():
    """Health check endpoint."""
    errors = config.validate()
    return {
        "status": "ok" if not errors else "warning",
        "errors": errors,
        "version": "0.2.0",
    }


def start():
    """Start the dashboard server."""
    config.ensure_dirs()
    print(f"\n{'='*60}")
    print(f"  sfauto Test Runner")
    print(f"  http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    print(f"{'='*60}\n")
    uvicorn.run(
        "src.web.app:app",
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        reload=True,
    )


if __name__ == "__main__":
    start()
