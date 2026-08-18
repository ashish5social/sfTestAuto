"""Live screencast bridge — POST frames in / WebSocket frames out.

The flow:
  1. Test process (pytest subprocess) starts a Playwright CDP screencast
     after the page is created (see conftest.py screencast fixture).
  2. On each Page.screencastFrame event, the test process POSTs the JPEG
     bytes to /api/screencast/{run_id}/frame on this FastAPI server.
  3. Browsers connect to /ws/screencast/{run_id} and receive base64-
     encoded frames as soon as they arrive.

Per run_id we keep a list of asyncio.Queue's — one per connected WS
client — so multiple browser tabs can watch the same run if needed.
Frames are dropped (oldest first) when the queue is over capacity, so
a slow client never blocks the test.

Ported from /Users/ashishkumarjha/ih_rev_cpq-product-tests
(ui-testing/src/web/routes/screencast.py) — small project, kept verbatim.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Dict, List

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse


router = APIRouter()


# ── In-memory bridge ─────────────────────────────────────────────────────
# Each run_id has a list of asyncio.Queue's, one per WebSocket subscriber.
# Frames are pushed by the HTTP POST handler and drained by each WS task.

_subscribers: Dict[str, List[asyncio.Queue]] = {}
_last_frame: Dict[str, bytes] = {}      # most recent frame, for late joiners
_frame_seq: Dict[str, int] = {}         # incremental frame counter per run
_first_seen: Dict[str, float] = {}      # first frame timestamp
QUEUE_MAX = 30                          # ~2s of frames at 15 FPS


def _publish(run_id: str, jpeg: bytes) -> None:
    _last_frame[run_id] = jpeg
    _frame_seq[run_id] = _frame_seq.get(run_id, 0) + 1
    _first_seen.setdefault(run_id, time.time())
    queues = _subscribers.get(run_id) or []
    for q in queues:
        # Drop oldest if full so slow clients don't back-pressure the test
        while q.qsize() >= QUEUE_MAX:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(jpeg)
        except Exception:
            pass


# ── HTTP POST: test process → bridge ────────────────────────────────────

@router.post("/api/screencast/{run_id}/frame")
async def post_frame(run_id: str, request: Request):
    """Test process posts a single JPEG frame here.
    Body is the raw JPEG bytes (Content-Type: image/jpeg)."""
    data = await request.body()
    if not data:
        return JSONResponse({"error": "empty body"}, status_code=400)
    _publish(run_id, data)
    return {
        "run_id": run_id,
        "size": len(data),
        "seq": _frame_seq.get(run_id, 0),
        "subscribers": len(_subscribers.get(run_id) or []),
    }


# ── HTTP GET: meta info for debugging ───────────────────────────────────

@router.get("/api/screencast/{run_id}/info")
def screencast_info(run_id: str):
    return {
        "run_id": run_id,
        "frame_seq": _frame_seq.get(run_id, 0),
        "subscribers": len(_subscribers.get(run_id) or []),
        "first_seen": _first_seen.get(run_id),
        "has_last_frame": run_id in _last_frame,
    }


# ── WebSocket: browser → bridge ─────────────────────────────────────────

@router.websocket("/ws/screencast/{run_id}")
async def ws_screencast(ws: WebSocket, run_id: str):
    """Browser opens this to receive live frames as base64 JPEG strings."""
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
    _subscribers.setdefault(run_id, []).append(q)

    # Send the most recent frame immediately so a tab joining mid-run
    # doesn't sit blank until the next CDP event.
    last = _last_frame.get(run_id)
    if last:
        try:
            await ws.send_text(base64.b64encode(last).decode("ascii"))
        except Exception:
            pass

    try:
        while True:
            try:
                # Use a 30s timeout so a finished run eventually drops
                # idle clients rather than holding sockets forever.
                jpeg = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a keep-alive ping; let the client detect run-finish
                # via SSE on the run endpoint instead.
                try:
                    await ws.send_text("")  # empty frame = keep-alive
                except Exception:
                    break
                continue
            try:
                await ws.send_text(base64.b64encode(jpeg).decode("ascii"))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            _subscribers.get(run_id, []).remove(q)
        except ValueError:
            pass


# ── Cleanup helper called from generated_tests.py when a run finishes ───

def cleanup_run(run_id: str) -> None:
    """Drop any cached frame and disconnect subscribers for a finished run."""
    _last_frame.pop(run_id, None)
    _frame_seq.pop(run_id, None)
    _first_seen.pop(run_id, None)
    queues = _subscribers.pop(run_id, None) or []
    for q in queues:
        # Push a sentinel so the WS handler can break out immediately
        try:
            q.put_nowait(b"")
        except Exception:
            pass
