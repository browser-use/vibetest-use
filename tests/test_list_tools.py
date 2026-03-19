"""Tests for list_runs and list_recorded_tests (underlying sqlite.list_runs/list_flows)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from blop.schemas import RecordedTestsResult
from blop.tools import results


@pytest.mark.asyncio
async def test_list_runs_returns_runs():
    """list_runs returns runs with total=3 when sqlite returns 3 run dicts."""
    mock_runs = [
        {"run_id": "r1", "status": "completed"},
        {"run_id": "r2", "status": "running"},
        {"run_id": "r3", "status": "completed"},
    ]
    with patch("blop.storage.sqlite.list_runs", new_callable=AsyncMock, return_value=mock_runs):
        out = await results.list_runs(limit=20, status=None)
    assert out["total"] == 3
    assert out["runs"] == mock_runs
    assert "related_v2_resources" in out


@pytest.mark.asyncio
async def test_list_runs_empty():
    """list_runs returns total=0 when sqlite returns empty list."""
    with patch("blop.storage.sqlite.list_runs", new_callable=AsyncMock, return_value=[]):
        out = await results.list_runs(limit=20)
    assert out["total"] == 0
    assert out["runs"] == []


@pytest.mark.asyncio
async def test_list_runs_with_status_filter():
    """list_runs passes status param through to sqlite.list_runs."""
    mock_list_runs = AsyncMock(return_value=[])
    with patch("blop.storage.sqlite.list_runs", mock_list_runs):
        await results.list_runs(limit=10, status="completed")
    mock_list_runs.assert_called_once_with(limit=10, status="completed")


@pytest.mark.asyncio
async def test_list_recorded_tests():
    """list_recorded_tests returns flows and total from sqlite.list_flows (same logic as server)."""
    mock_flows = [
        {
            "flow_id": "f1",
            "flow_name": "checkout",
            "app_url": "https://example.com",
            "goal": "Complete checkout",
            "created_at": "2026-03-19T10:00:00Z",
            "run_mode_override": None,
        },
        {
            "flow_id": "f2",
            "flow_name": "signup",
            "app_url": "https://example.com",
            "goal": "Sign up",
            "created_at": "2026-03-19T09:00:00Z",
            "run_mode_override": None,
        },
    ]
    from blop.storage import sqlite

    with patch.object(sqlite, "list_flows", new_callable=AsyncMock, return_value=mock_flows):
        flows = await sqlite.list_flows()
        result = RecordedTestsResult(flows=flows, total=len(flows)).model_dump()
    assert result["total"] == 2
    assert result["flows"] == mock_flows
    assert result["flows"][0]["flow_name"] == "checkout"
