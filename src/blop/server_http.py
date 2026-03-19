"""Optional HTTP SSE streaming server for blop run health events.

Install optional deps:
    pip install blop[server]

Run:
    blop-http
    # or: python -m blop.server_http

Endpoints:
    GET /runs/{run_id}/stream  — SSE stream of run health events
    GET /health                — liveness probe
"""
from __future__ import annotations

import asyncio
import os

try:
    from fastapi import FastAPI
    from sse_starlette.sse import EventSourceResponse
    import uvicorn
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False

if _HAS_DEPS:
    app = FastAPI(title="blop HTTP server", version="0.2.0")

    async def _run_event_stream(run_id: str):
        """Yield SSE events until the run reaches a terminal state."""
        from blop.storage.sqlite import get_run, list_run_health_events
        seen: set[str] = set()
        while True:
            run = await get_run(run_id)
            events = await list_run_health_events(run_id, limit=500)
            for event in events:
                eid = event["event_id"]
                if eid not in seen:
                    seen.add(eid)
                    yield {"event": event["event_type"], "data": str(event["payload"])}
            if run and run.get("status") in ("completed", "failed", "cancelled"):
                yield {"event": "terminal", "data": run["status"]}
                break
            await asyncio.sleep(1.0)

    @app.get("/runs/{run_id}/stream")
    async def stream_run(run_id: str):
        """Stream SSE health events for a run until it reaches a terminal state."""
        return EventSourceResponse(_run_event_stream(run_id))

    @app.get("/health")
    async def health():
        return {"status": "ok"}


def run() -> int:
    if not _HAS_DEPS:
        print(
            "fastapi, uvicorn, and sse-starlette are required.\n"
            "Install with: pip install blop[server]"
        )
        return 1
    host = os.getenv("BLOP_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("BLOP_HTTP_PORT", "8765"))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())
