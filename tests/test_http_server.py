"""Tests for blop.server_http (HTTP SSE server)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# Skip entire module if FastAPI/server deps not installed
pytest.importorskip("fastapi")

try:
    from blop.server_http import app
except (ImportError, AttributeError):
    pytest.skip("blop[server] not installed (fastapi, sse-starlette, uvicorn)", allow_module_level=True)


@pytest.mark.asyncio
async def test_health_endpoint():
    """GET /health returns 200 with {status: ok}."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_stream_run_completed():
    """Stream endpoint yields SSE events and terminal when run is completed."""
    from httpx import ASGITransport, AsyncClient

    completed_run = {
        "run_id": "run_abc",
        "status": "completed",
        "app_url": "https://example.com",
        "started_at": "2026-03-19T10:00:00Z",
        "completed_at": "2026-03-19T10:05:00Z",
    }
    health_events = [
        {"event_id": "evt_1", "event_type": "case_started", "payload": {"flow_id": "f1", "case_id": "c1"}},
        {"event_id": "evt_2", "event_type": "case_completed", "payload": {"flow_id": "f1", "status": "pass"}},
    ]

    with patch("blop.storage.sqlite.get_run", new=AsyncMock(return_value=completed_run)):
        with patch(
            "blop.storage.sqlite.list_run_health_events",
            new=AsyncMock(return_value=health_events),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/runs/run_abc/stream")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    # Consume SSE stream and collect data payloads
    data_payloads: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data_payloads.append(line[5:].strip())
        if len(data_payloads) >= 3:
            break

    # Should have: case_started payload, case_completed payload, and terminal "completed"
    assert len(data_payloads) >= 2
    assert "completed" in data_payloads[-1]


@pytest.mark.asyncio
async def test_stream_run_empty_events_then_terminal():
    """Stream with no health events but completed run yields terminal event."""
    from httpx import ASGITransport, AsyncClient

    completed_run = {"run_id": "run_xyz", "status": "completed"}
    with patch("blop.storage.sqlite.get_run", new=AsyncMock(return_value=completed_run)):
        with patch(
            "blop.storage.sqlite.list_run_health_events",
            new=AsyncMock(return_value=[]),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/runs/run_xyz/stream")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    # Consume stream; should get terminal event quickly (run is already completed)
    chunks: list[str] = []
    async for line in response.aiter_lines():
        chunks.append(line)
        if line.startswith("data:") and "completed" in line:
            break
        if len(chunks) > 20:
            break

    assert any("completed" in line for line in chunks)
