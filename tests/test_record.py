"""Tests for tools/record.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from blop.schemas import FlowStep, RecordedFlow


def _make_steps() -> list[FlowStep]:
    return [
        FlowStep(step_id=0, action="navigate", value="https://example.com"),
        FlowStep(step_id=1, action="click", description="Click submit"),
    ]


@pytest.mark.asyncio
async def test_record_flow_happy_path():
    """Happy path: record_flow returns steps, flow saved, result has status=recorded."""
    from blop.tools.record import record_test_flow

    mock_steps = _make_steps()
    mock_record_flow = AsyncMock(return_value=mock_steps)
    mock_save_flow = AsyncMock()
    mock_get_auth_profile = AsyncMock(return_value=None)
    mock_artifacts_dir = "/tmp/runs/flow_abc123"

    with patch("blop.tools.record.recording.record_flow", mock_record_flow):
        with patch("blop.tools.record.sqlite.save_flow", mock_save_flow):
            with patch("blop.tools.record.sqlite.get_auth_profile", mock_get_auth_profile):
                with patch("blop.tools.record.file_store.artifacts_dir", return_value=mock_artifacts_dir):
                    with patch("blop.engine.context_graph.detect_app_archetype", return_value="saas_app"):
                        with patch("blop.engine.context_graph.editor_hints_from_archetype", return_value={}):
                            result = await record_test_flow(
                                app_url="https://example.com",
                                flow_name="test_flow",
                                goal="Complete the form",
                            )

    assert result["status"] == "recorded"
    assert result["step_count"] == 2
    assert "flow_id" in result
    assert result["flow_name"] == "test_flow"
    assert result["artifacts_dir"] == mock_artifacts_dir
    mock_record_flow.assert_called_once()
    mock_save_flow.assert_called_once()
    saved_flow = mock_save_flow.call_args[0][0]
    assert isinstance(saved_flow, RecordedFlow)
    assert saved_flow.flow_name == "test_flow"
    assert saved_flow.business_criticality == "other"


@pytest.mark.asyncio
async def test_record_flow_with_business_criticality():
    """business_criticality='revenue' is preserved in the saved flow."""
    from blop.tools.record import record_test_flow

    mock_steps = _make_steps()
    mock_record_flow = AsyncMock(return_value=mock_steps)
    mock_save_flow = AsyncMock()

    with patch("blop.tools.record.recording.record_flow", mock_record_flow):
        with patch("blop.tools.record.sqlite.save_flow", mock_save_flow):
            with patch("blop.tools.record.sqlite.get_auth_profile", AsyncMock(return_value=None)):
                with patch("blop.tools.record.file_store.artifacts_dir", return_value="/tmp/runs/f1"):
                    with patch("blop.engine.context_graph.detect_app_archetype", return_value="saas_app"):
                        with patch("blop.engine.context_graph.editor_hints_from_archetype", return_value={}):
                            result = await record_test_flow(
                                app_url="https://example.com",
                                flow_name="checkout",
                                goal="Complete checkout",
                                business_criticality="revenue",
                            )

    assert result["status"] == "recorded"
    saved_flow = mock_save_flow.call_args[0][0]
    assert saved_flow.business_criticality == "revenue"


@pytest.mark.asyncio
async def test_record_flow_invalid_url():
    """Invalid app_url returns error dict with URL validation message."""
    from blop.tools.record import record_test_flow

    result = await record_test_flow(
        app_url="not-a-url",
        flow_name="test_flow",
        goal="Test goal",
    )

    assert "error" in result
    assert "app_url" in result["error"].lower() or "http" in result["error"].lower()


@pytest.mark.asyncio
async def test_record_flow_empty_flow_name():
    """Empty flow_name returns error dict."""
    from blop.tools.record import record_test_flow

    result = await record_test_flow(
        app_url="https://example.com",
        flow_name="",
        goal="Test goal",
    )

    assert result == {"error": "flow_name is required"}


@pytest.mark.asyncio
async def test_record_flow_empty_goal():
    """Empty goal returns error dict."""
    from blop.tools.record import record_test_flow

    result = await record_test_flow(
        app_url="https://example.com",
        flow_name="test_flow",
        goal="",
    )

    assert result == {"error": "goal is required"}
