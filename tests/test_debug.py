"""Tests for tools/debug.py."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from blop.schemas import FailureCase, FlowStep, RecordedFlow


def _make_flow(flow_id: str = "flow1") -> RecordedFlow:
    return RecordedFlow(
        flow_id=flow_id,
        flow_name="test_flow",
        app_url="https://example.com",
        goal="Complete the form",
        steps=[
            FlowStep(step_id=0, action="navigate", value="https://example.com"),
            FlowStep(step_id=1, action="click", description="Click submit"),
        ],
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_case(case_id: str = "case1", flow_id: str = "flow1") -> FailureCase:
    return FailureCase(
        case_id=case_id,
        run_id="run1",
        flow_id=flow_id,
        flow_name="test_flow",
        status="fail",
        repro_steps=["Navigate to page", "Click submit"],
        step_failure_index=1,
        assertion_failures=["Expected success message"],
        console_errors=[],
        screenshots=[],
        replay_mode="hybrid",
    )


@pytest.mark.asyncio
async def test_debug_case_happy_path(tmp_path):
    """Happy path: run and case found, flow replayed, result has expected fields."""
    from blop.tools.debug import debug_test_case

    run_id = "run1"
    case_id = "case1"
    flow_id = "flow1"

    mock_run = {
        "run_id": run_id,
        "app_url": "https://example.com",
        "profile_name": None,
        "cases": [],
        "run_mode": "hybrid",
    }
    mock_case = _make_case(case_id=case_id, flow_id=flow_id)
    mock_flow = _make_flow(flow_id=flow_id)
    replayed_case = _make_case(case_id=case_id, flow_id=flow_id)
    replayed_case.status = "fail"
    classified_case = replayed_case

    mock_get_run = AsyncMock(return_value=mock_run)
    mock_list_cases = AsyncMock(return_value=[mock_case])
    mock_get_flow = AsyncMock(return_value=mock_flow)
    mock_execute_flow = AsyncMock(return_value=replayed_case)
    mock_classify_case = AsyncMock(return_value=classified_case)

    log_path = tmp_path / "console" / run_id / f"{case_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("Console log content")

    with patch("blop.tools.debug.sqlite.get_run", mock_get_run):
        with patch("blop.tools.debug.sqlite.list_cases_for_run", mock_list_cases):
            with patch("blop.tools.debug.sqlite.get_flow", mock_get_flow):
                with patch(
                    "blop.tools.debug.regression_engine.execute_flow",
                    mock_execute_flow,
                ):
                    with patch(
                        "blop.tools.debug.classifier.classify_case",
                        mock_classify_case,
                    ):
                        with patch(
                            "blop.storage.files.console_log_path",
                            return_value=str(log_path),
                        ):
                            with patch(
                                "blop.config.check_llm_api_key",
                                return_value=(False, ""),
                            ):
                                result = await debug_test_case(
                                    run_id=run_id,
                                    case_id=case_id,
                                )

    assert "error" not in result
    assert result["case_id"] == case_id
    assert result["run_id"] == run_id
    assert result["status"] == "fail"
    assert result["repro_steps"] == ["Navigate to page", "Click submit"]
    assert result["step_failure_index"] == 1
    mock_execute_flow.assert_called_once()
    mock_classify_case.assert_called_once()


@pytest.mark.asyncio
async def test_debug_case_run_not_found():
    """Run not found returns error dict."""
    from blop.tools.debug import debug_test_case

    mock_get_run = AsyncMock(return_value=None)

    with patch("blop.tools.debug.sqlite.get_run", mock_get_run):
        result = await debug_test_case(
            run_id="nonexistent_run",
            case_id="case1",
        )

    assert result == {"error": "Run nonexistent_run not found"}


@pytest.mark.asyncio
async def test_debug_case_case_not_found():
    """Case not found in run returns error dict."""
    from blop.tools.debug import debug_test_case

    mock_run = {
        "run_id": "run1",
        "app_url": "https://example.com",
        "profile_name": None,
        "cases": [],
        "run_mode": "hybrid",
    }
    mock_get_run = AsyncMock(return_value=mock_run)
    mock_list_cases = AsyncMock(return_value=[])

    with patch("blop.tools.debug.sqlite.get_run", mock_get_run):
        with patch("blop.tools.debug.sqlite.list_cases_for_run", mock_list_cases):
            result = await debug_test_case(
                run_id="run1",
                case_id="nonexistent_case",
            )

    assert "error" in result
    assert "Case nonexistent_case not found" in result["error"]
    assert "run1" in result["error"]
