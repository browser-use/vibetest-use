"""Tests for engine/classifier.py — assertion-first severity with SaaS scenarios."""
from __future__ import annotations

import pytest

from blop.schemas import FailureCase


def make_case(
    flow_name: str,
    status: str,
    business_criticality: str = "other",
    assertion_failures: list[str] | None = None,
    network_errors: list[str] | None = None,
    console_errors: list[str] | None = None,
) -> FailureCase:
    return FailureCase(
        run_id="run1",
        flow_id="flow1",
        flow_name=flow_name,
        status=status,
        business_criticality=business_criticality,
        assertion_failures=assertion_failures or [],
        network_errors=network_errors or [],
        console_errors=console_errors or [],
    )


# ---------------------------------------------------------------------------
# Deterministic classifier tests
# ---------------------------------------------------------------------------

def test_revenue_flow_assertion_failure_is_blocker():
    """checkout_with_credit_card (revenue) with assertion failure → blocker."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "checkout_with_credit_card",
        status="fail",
        business_criticality="revenue",
        assertion_failures=["Expected order confirmation page, got error page"],
    )
    assert classify_failure_deterministic(case) == "blocker"


def test_activation_flow_assertion_failure_is_blocker():
    """user_signup_onboarding (activation) with assertion failure → blocker."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "user_signup_onboarding",
        status="fail",
        business_criticality="activation",
        assertion_failures=["Welcome email not triggered"],
    )
    assert classify_failure_deterministic(case) == "blocker"


def test_retention_flow_assertion_failure_is_high():
    """team_member_invite (retention) with assertion failure → high (not blocker)."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "team_member_invite",
        status="fail",
        business_criticality="retention",
        assertion_failures=["Invite email not sent"],
    )
    assert classify_failure_deterministic(case) == "high"


def test_support_flow_assertion_failure_is_high():
    """help_center_search (support) with assertion failure → high."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "help_center_search",
        status="fail",
        business_criticality="support",
        assertion_failures=["Search results did not appear"],
    )
    assert classify_failure_deterministic(case) == "high"


def test_revenue_flow_step_failure_no_assertion_is_high():
    """upgrade_plan_to_pro (revenue) step failure without assertion failure → high."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "upgrade_plan_to_pro",
        status="fail",
        business_criticality="revenue",
        assertion_failures=[],
    )
    assert classify_failure_deterministic(case) == "high"


def test_other_flow_step_failure_is_medium():
    """view_usage_dashboard (other) step failure → medium."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "view_usage_dashboard",
        status="fail",
        business_criticality="other",
    )
    assert classify_failure_deterministic(case) == "medium"


def test_auth_401_failure_is_blocker():
    """Network error with 401 → blocker regardless of criticality."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "help_center_search",
        status="fail",
        business_criticality="support",
        network_errors=["401 Unauthorized /api/search"],
    )
    assert classify_failure_deterministic(case) == "blocker"


def test_http_5xx_is_blocker():
    """500 HTTP error in network errors → blocker."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "team_member_invite",
        status="fail",
        business_criticality="retention",
        network_errors=["500 Internal Server Error /api/invite"],
    )
    assert classify_failure_deterministic(case) == "blocker"


def test_js_crash_is_blocker():
    """Uncaught TypeError in console errors → blocker."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case(
        "checkout_with_credit_card",
        status="fail",
        business_criticality="revenue",
        console_errors=["Uncaught TypeError: Cannot read properties of undefined"],
    )
    assert classify_failure_deterministic(case) == "blocker"


def test_pass_status_is_none():
    """Passing flow → severity none."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case("view_usage_dashboard", status="pass", business_criticality="retention")
    assert classify_failure_deterministic(case) == "none"


def test_blocked_status_is_blocker():
    """Status 'blocked' → severity blocker."""
    from blop.engine.classifier import classify_failure_deterministic

    case = make_case("checkout_with_credit_card", status="blocked")
    assert classify_failure_deterministic(case) == "blocker"


# ---------------------------------------------------------------------------
# classify_run aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classify_run_returns_severity_counts():
    """classify_run returns dict with expected severity_counts keys."""
    from blop.engine.classifier import classify_run

    cases = [
        FailureCase(run_id="r1", flow_id="f1", flow_name="checkout_with_credit_card",
                    status="fail", severity="blocker", business_criticality="revenue"),
        FailureCase(run_id="r1", flow_id="f2", flow_name="view_usage_dashboard",
                    status="pass", severity="none", business_criticality="retention"),
        FailureCase(run_id="r1", flow_id="f3", flow_name="help_center_search",
                    status="fail", severity="medium", business_criticality="support"),
    ]

    import os
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(os.environ, {}, clear=True):
        result = await classify_run(cases, "https://example.com")

    counts = result["severity_counts"]
    assert set(counts.keys()) >= {"blocker", "high", "medium", "low", "none", "pass"}
    assert counts["pass"] == 1
    assert counts["blocker"] == 1
    assert counts["medium"] == 1
