"""Tests for business_criticality field propagation end-to-end."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blop.schemas import FailureCase, FlowStep, RecordedFlow


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_recorded_flow_accepts_valid_criticality_values():
    """RecordedFlow accepts all valid business_criticality values."""
    valid = ["revenue", "activation", "retention", "support", "other"]
    for val in valid:
        flow = RecordedFlow(
            flow_name="test",
            app_url="https://example.com",
            goal="test",
            steps=[],
            created_at=datetime.now(timezone.utc).isoformat(),
            business_criticality=val,
        )
        assert flow.business_criticality == val


def test_recorded_flow_defaults_to_other():
    """RecordedFlow defaults to 'other' when business_criticality not provided."""
    flow = RecordedFlow(
        flow_name="test",
        app_url="https://example.com",
        goal="test",
        steps=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    assert flow.business_criticality == "other"


def test_failure_case_propagates_business_criticality():
    """FailureCase accepts business_criticality from source flow."""
    case = FailureCase(
        run_id="run1",
        flow_id="flow1",
        flow_name="checkout_with_credit_card",
        status="fail",
        business_criticality="revenue",
    )
    assert case.business_criticality == "revenue"


# ---------------------------------------------------------------------------
# SQLite round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sqlite_roundtrip_preserves_business_criticality(tmp_path):
    """save_flow with criticality='revenue', get_flow returns 'revenue'."""
    db_path = str(tmp_path / "test.db")

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite as store
        await store.init_db()

        flow = RecordedFlow(
            flow_id="flow-rev-1",
            flow_name="checkout_with_credit_card",
            app_url="https://example.com",
            goal="Complete checkout with credit card",
            steps=[FlowStep(step_id=0, action="navigate", value="https://example.com/checkout")],
            created_at=datetime.now(timezone.utc).isoformat(),
            business_criticality="revenue",
        )
        await store.save_flow(flow)
        retrieved = await store.get_flow("flow-rev-1")

    assert retrieved is not None
    assert retrieved.business_criticality == "revenue"


# ---------------------------------------------------------------------------
# Discovery result includes business_criticality
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_flows_includes_business_criticality():
    """Gemini response with business_criticality → flows in result have it."""
    from blop.engine.discovery import plan_flows_from_inventory
    from blop.schemas import SiteInventory

    inventory = SiteInventory(
        app_url="https://example.com",
        routes=["/checkout", "/pricing"],
        buttons=[{"text": "Buy Now", "id": "", "href": None}],
        links=[],
        forms=[],
        headings=["Checkout"],
        auth_signals=["login"],
        business_signals=["checkout", "pricing"],
    )

    gemini_response = json.dumps([
        {
            "flow_name": "checkout_with_credit_card",
            "goal": "Complete a checkout",
            "starting_url": "https://example.com/checkout",
            "preconditions": [],
            "likely_assertions": ["order confirmed"],
            "severity_if_broken": "blocker",
            "confidence": 0.9,
            "business_criticality": "revenue",
        },
        {
            "flow_name": "user_signup_onboarding",
            "goal": "Sign up and complete onboarding",
            "starting_url": "https://example.com/signup",
            "preconditions": [],
            "likely_assertions": ["welcome screen visible"],
            "severity_if_broken": "blocker",
            "confidence": 0.85,
            "business_criticality": "activation",
        },
        {
            "flow_name": "help_center_search",
            "goal": "Search for help articles",
            "starting_url": "https://example.com/help",
            "preconditions": [],
            "likely_assertions": ["results appear"],
            "severity_if_broken": "medium",
            "confidence": 0.7,
            "business_criticality": "support",
        },
    ])

    mock_response = MagicMock()
    mock_response.content = gemini_response
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = mock_response

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("browser_use.llm.ChatGoogle", return_value=mock_llm):
            flows = await plan_flows_from_inventory(inventory)

    assert len(flows) >= 3
    bc_values = {f.get("business_criticality") for f in flows}
    assert "revenue" in bc_values
    assert "activation" in bc_values


@pytest.mark.asyncio
async def test_discover_flows_falls_back_to_other_when_field_missing():
    """Gemini response missing business_criticality → field defaults to 'other'."""
    from blop.engine.discovery import plan_flows_from_inventory
    from blop.schemas import SiteInventory

    inventory = SiteInventory(
        app_url="https://example.com",
        routes=[],
        buttons=[],
        links=[],
        forms=[],
        headings=[],
        auth_signals=[],
        business_signals=[],
    )

    gemini_response = json.dumps([
        {"flow_name": "page_loads", "goal": "Page loads", "likely_assertions": ["visible"]},
        {"flow_name": "nav_links", "goal": "Test nav links", "likely_assertions": ["works"]},
        {"flow_name": "forms_work", "goal": "Test forms", "likely_assertions": ["submits"]},
    ])

    mock_response = MagicMock()
    mock_response.content = gemini_response
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = mock_response

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("browser_use.llm.ChatGoogle", return_value=mock_llm):
            flows = await plan_flows_from_inventory(inventory)

    assert all(f.get("business_criticality", "other") == "other" for f in flows)


# ---------------------------------------------------------------------------
# Report severity labels
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_report_severity_label_blocker_in_revenue_flow():
    """Failed revenue flow → severity_label shows 'BLOCKER in revenue flow: checkout_with_credit_card'."""
    from blop.reporting.results import build_report

    cases = [
        FailureCase(
            run_id="run1",
            flow_id="flow1",
            flow_name="checkout_with_credit_card",
            status="fail",
            severity="blocker",
            business_criticality="revenue",
        )
    ]
    run = {
        "run_id": "run1",
        "status": "completed",
        "started_at": "2026-03-16T10:00:00Z",
        "completed_at": "2026-03-16T10:05:00Z",
        "artifacts_dir": "/tmp/runs/run1",
        "run_mode": "hybrid",
    }

    report = await build_report(run, cases)

    labels = [c["severity_label"] for c in report["cases"]]
    assert any("BLOCKER" in lbl and "revenue" in lbl and "checkout_with_credit_card" in lbl for lbl in labels)


@pytest.mark.asyncio
async def test_build_report_passing_flow_shows_none():
    """Passing flow → severity_label is 'NONE'."""
    from blop.reporting.results import build_report

    cases = [
        FailureCase(
            run_id="run1",
            flow_id="flow2",
            flow_name="view_usage_dashboard",
            status="pass",
            severity="none",
            business_criticality="retention",
        )
    ]
    run = {
        "run_id": "run1",
        "status": "completed",
        "started_at": "2026-03-16T10:00:00Z",
        "completed_at": "2026-03-16T10:05:00Z",
        "artifacts_dir": "/tmp/runs/run1",
        "run_mode": "hybrid",
    }

    report = await build_report(run, cases)

    labels = [c["severity_label"] for c in report["cases"]]
    assert labels == ["NONE"]
