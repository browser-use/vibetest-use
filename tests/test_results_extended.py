"""Tests for get_run_health_stream and get_risk_analytics from blop.tools.results."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from blop.schemas import FailureCase
from blop.tools import results


@pytest.mark.asyncio
async def test_health_stream_happy_path():
    """get_run_health_stream returns run status and events when run exists."""
    run = {
        "run_id": "run1",
        "status": "running",
        "app_url": "https://example.com",
    }
    events = [
        {"event_id": "e1", "run_id": "run1", "event_type": "started", "payload": {}, "created_at": "2026-03-19T10:00:00Z"},
        {"event_id": "e2", "run_id": "run1", "event_type": "case_complete", "payload": {"case_id": "c1"}, "created_at": "2026-03-19T10:01:00Z"},
    ]
    with patch("blop.storage.sqlite.get_run", new_callable=AsyncMock, return_value=run):
        with patch("blop.storage.sqlite.list_run_health_events", new_callable=AsyncMock, return_value=events):
            out = await results.get_run_health_stream("run1", limit=500)
    assert out["run_id"] == "run1"
    assert out["status"] == "running"
    assert out["event_count"] == 2
    assert len(out["events"]) == 2
    assert out["events"][0]["event_type"] == "started"


@pytest.mark.asyncio
async def test_health_stream_not_found():
    """get_run_health_stream returns error when run does not exist."""
    with patch("blop.storage.sqlite.get_run", new_callable=AsyncMock, return_value=None):
        out = await results.get_run_health_stream("nonexistent", limit=500)
    assert "error" in out
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_risk_analytics_with_cases():
    """get_risk_analytics aggregates business_risk from cases with various criticality and statuses."""
    runs = [
        {"run_id": "run1"},
        {"run_id": "run2"},
    ]
    case1 = FailureCase(
        run_id="run1",
        flow_id="f1",
        flow_name="checkout",
        status="fail",
        business_criticality="revenue",
        step_failure_index=2,
    )
    case2 = FailureCase(
        run_id="run1",
        flow_id="f2",
        flow_name="signup",
        status="pass",
        business_criticality="activation",
    )
    case3 = FailureCase(
        run_id="run2",
        flow_id="f3",
        flow_name="billing",
        status="error",
        business_criticality="revenue",
        step_failure_index=1,
    )
    async def list_cases_side_effect(run_id: str):
        if run_id == "run1":
            return [case1, case2]
        return [case3]

    with patch("blop.storage.sqlite.list_runs", new_callable=AsyncMock, return_value=runs):
        with patch(
            "blop.storage.sqlite.list_cases_for_run",
            new_callable=AsyncMock,
            side_effect=list_cases_side_effect,
        ):
            out = await results.get_risk_analytics(limit_runs=30)

    assert out["analyzed_runs"] == 2
    assert out["business_risk"]["revenue"]["total"] == 2
    assert out["business_risk"]["revenue"]["failed"] == 2
    assert out["business_risk"]["activation"]["total"] == 1
    assert out["business_risk"]["activation"]["failed"] == 0
    assert "flaky_steps_leaderboard" in out
    assert "failing_transitions" in out


@pytest.mark.asyncio
async def test_risk_analytics_empty():
    """get_risk_analytics returns analyzed_runs=0 when no runs exist."""
    with patch("blop.storage.sqlite.list_runs", new_callable=AsyncMock, return_value=[]):
        out = await results.get_risk_analytics(limit_runs=30)
    assert out["analyzed_runs"] == 0
    assert out["business_risk"]["revenue"]["total"] == 0
    assert out["business_risk"]["revenue"]["failed"] == 0
