"""Integration and unit checks for the v2 MCP surface."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blop.schemas import ContextEdge, ContextNode, SiteContextGraph


# ---------------------------------------------------------------------------
# Existing tests (kept intact)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_surface_contract_shape():
    """Contract endpoint publishes request/response schemas and examples."""
    from blop.tools import v2_surface

    contract = await v2_surface.get_surface_contract()

    assert contract["resource_version"] == "v2"
    assert "tool_contracts" in contract
    tool_contracts = contract["tool_contracts"]

    expected_tools = [
        "blop_v2_capture_context",
        "blop_v2_compare_context",
        "blop_v2_assess_release_risk",
        "blop_v2_get_journey_health",
        "blop_v2_cluster_incidents",
        "blop_v2_generate_remediation",
        "blop_v2_ingest_telemetry_signals",
        "blop_v2_get_correlation_report",
    ]
    for tool_name in expected_tools:
        assert tool_name in tool_contracts
        tool = tool_contracts[tool_name]
        assert "request_schema" in tool
        assert "response_schema" in tool
        assert "example" in tool
        assert tool["request_schema"].get("type") == "object"
        assert tool["response_schema"].get("type") == "object"


@pytest.mark.asyncio
async def test_v2_context_resources_use_standard_envelope(tmp_path):
    """v2 context resources consistently use the standard resource envelope."""
    from blop.storage import sqlite
    from blop.tools import v2_surface

    db_path = str(tmp_path / "v2_surface.db")
    app_url = "https://example.com"

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        await sqlite.init_db()
        await sqlite.save_context_graph(
            SiteContextGraph(
                app_url=app_url,
                created_at=datetime.now(timezone.utc).isoformat(),
                nodes=[ContextNode(node_id="route:/", node_type="route", label="/", confidence=0.9)],
                edges=[ContextEdge(source_id="route:/", target_id="intent:signup", edge_type="supports_intent")],
            )
        )
        latest = await v2_surface.get_context_latest_resource(app_url)
        history = await v2_surface.get_context_history_resource(app_url, limit=5)

    for payload in (latest, history):
        assert payload["resource_version"] == "v2"
        assert "generated_at" in payload
        assert payload["app_url"] == app_url
        assert "data" in payload

    assert latest["data"]["node_count"] == 1
    assert latest["data"]["edge_count"] == 1
    assert history["data"]["total"] >= 1


@pytest.mark.asyncio
async def test_v1_results_include_related_v2_resources():
    """v1 test-results payload links to v2 resources for migration guidance."""
    from blop.tools import results

    run = {
        "run_id": "run_123",
        "app_url": "https://example.com",
        "status": "completed",
        "started_at": "2026-03-18T10:00:00Z",
        "completed_at": "2026-03-18T10:05:00Z",
        "artifacts_dir": "/tmp/runs/run_123",
        "cases": [],
        "run_mode": "hybrid",
    }

    with patch("blop.storage.sqlite.get_run", new=AsyncMock(return_value=run)):
        with patch("blop.storage.sqlite.list_cases_for_run", new=AsyncMock(return_value=[])):
            with patch("blop.storage.sqlite.list_run_health_events", new=AsyncMock(return_value=[])):
                report = await results.get_test_results("run_123")

    assert "related_v2_resources" in report
    assert any("blop://v2/context/" in uri for uri in report["related_v2_resources"])
    assert any("blop://v2/journey/" in uri for uri in report["related_v2_resources"])


# ---------------------------------------------------------------------------
# Tier 1 fix tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_context_does_not_call_llm_planner(tmp_path):
    """capture_context uses pure crawl; discover_test_flows should NOT be called."""
    from blop.tools import v2_surface
    from blop.schemas import SiteInventory

    db_path = str(tmp_path / "capture.db")
    app_url = "https://example.com"

    fake_inventory = SiteInventory(
        app_url=app_url,
        routes=["/", "/pricing"],
        buttons=[],
        links=[],
        forms=[],
        headings=[],
        auth_signals=[],
        business_signals=[],
        page_structures={},
        crawled_pages=2,
    )

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.engine.discovery.inventory_site", new=AsyncMock(return_value=fake_inventory)):
            with patch("blop.engine.discovery.plan_flows_from_inventory") as mock_plan:
                mock_plan.return_value = []
                # No intent_focus → plan_flows_from_inventory should NOT be called
                result = await v2_surface.capture_context(app_url=app_url)

    mock_plan.assert_not_called()
    assert "graph_id" in result
    assert result["app_url"] == app_url
    assert "diff_summary" in result


@pytest.mark.asyncio
async def test_capture_context_calls_planner_only_when_intent_focus(tmp_path):
    """capture_context calls plan_flows_from_inventory only when intent_focus is given."""
    from blop.tools import v2_surface
    from blop.schemas import SiteInventory

    db_path = str(tmp_path / "capture_intent.db")
    app_url = "https://example.com"

    fake_inventory = SiteInventory(
        app_url=app_url, routes=["/"], buttons=[], links=[], forms=[],
        headings=[], auth_signals=[], business_signals=[], page_structures={}, crawled_pages=1,
    )

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.engine.discovery.inventory_site", new=AsyncMock(return_value=fake_inventory)):
            with patch("blop.engine.discovery.plan_flows_from_inventory", new=AsyncMock(return_value=[])) as mock_plan:
                await v2_surface.capture_context(app_url=app_url, intent_focus=["revenue"])

    mock_plan.assert_called_once()


@pytest.mark.asyncio
async def test_cluster_incidents_merges_similar_flow_names():
    """checkout_credit#step_3 and checkout_paypal#step_3 should merge into one cluster (Jaccard >= 0.45)."""
    from blop.tools.v2_surface import _jaccard_similarity, _merge_similar_buckets, SIMILARITY_THRESHOLD

    # Jaccard("checkout_credit#step_3", "checkout_paypal#step_3")
    # tokens_a = {checkout, credit, step, 3}
    # tokens_b = {checkout, paypal, step, 3}
    # intersection = {checkout, step, 3} → 3
    # union = {checkout, credit, step, 3, paypal} → 5
    # similarity = 3/5 = 0.6 >= 0.45
    sim = _jaccard_similarity("checkout_credit#step_3", "checkout_paypal#step_3")
    assert sim >= SIMILARITY_THRESHOLD, f"Expected >= {SIMILARITY_THRESHOLD}, got {sim}"

    buckets = {
        "checkout_credit#step_3": [MagicMock()],
        "checkout_paypal#step_3": [MagicMock()],
    }
    merged = _merge_similar_buckets(buckets)
    assert len(merged) == 1, f"Expected 1 merged cluster, got {len(merged)}"
    canonical_key = list(merged.keys())[0]
    assert len(merged[canonical_key]) == 2


@pytest.mark.asyncio
async def test_cluster_incidents_respects_min_cluster_size(tmp_path):
    """Single-member buckets should not produce saved clusters."""
    from blop.tools import v2_surface
    from blop.schemas import FailureCase

    db_path = str(tmp_path / "cluster.db")
    app_url = "https://example.com"

    single_case = MagicMock(spec=FailureCase)
    single_case.status = "fail"
    single_case.flow_name = "unique_flow"
    single_case.flow_id = "fid_1"
    single_case.step_failure_index = 1
    single_case.business_criticality = "other"
    single_case.severity = "medium"
    single_case.run_id = "run_a"
    single_case.case_id = "case_a"

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.list_runs", new=AsyncMock(return_value=[
            {"run_id": "run_a", "app_url": app_url, "started_at": datetime.now(timezone.utc).isoformat()}
        ])):
            with patch("blop.storage.sqlite.list_cases_for_run", new=AsyncMock(return_value=[single_case])):
                with patch("blop.storage.sqlite.save_incident_cluster", new=AsyncMock()) as mock_save:
                    result = await v2_surface.cluster_incidents(
                        app_url=app_url,
                        run_ids=["run_a"],
                        min_cluster_size=2,
                    )

    assert result["cluster_count"] == 0
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_generate_remediation_calls_gemini(tmp_path):
    """generate_remediation should call the LLM and use its fix_hypotheses."""
    from blop.tools import v2_surface
    from blop.schemas import IncidentCluster

    db_path = str(tmp_path / "remediation.db")
    cluster_id = "cluster_test_abc"

    cluster = IncidentCluster(
        cluster_id=cluster_id,
        app_url="https://example.com",
        title="Repeated failure at checkout_flow#step_2",
        severity="high",
        affected_flows=2,
        affected_criticality=["revenue"],
        first_seen="run_1",
        last_seen="run_2",
        evidence_refs=["run:run_1/case:case_1"],
        member_case_ids=["case_1"],
        status="open",
    )

    fake_llm_response = MagicMock()
    fake_llm_response.content = '{"issue_body": "Checkout fails at step 2.", "fix_hypotheses": ["Fix A", "Fix B", "Fix C"], "owner_hint": "Payments team"}'
    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(return_value=fake_llm_response)

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path, "GOOGLE_API_KEY": "test-key"}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_incident_cluster", new=AsyncMock(return_value=cluster)):
            with patch("blop.storage.sqlite.get_case", new=AsyncMock(return_value=None)):
                with patch("blop.storage.sqlite.save_remediation_draft", new=AsyncMock()):
                    with patch("blop.engine.llm_factory.make_planning_llm", return_value=fake_llm):
                        result = await v2_surface.generate_remediation(
                            cluster_id=cluster_id,
                            include_fix_hypotheses=True,
                            include_owner_hints=True,
                        )

    assert result.get("fix_hypotheses") != [
        "Selector drift after UI change; add semantic locator fallback.",
        "Page transition timing issue; increase settle/wait condition before interaction.",
        "Auth/session precondition missing in the flow setup.",
    ], "Expected LLM fix_hypotheses, got template defaults"
    assert "Fix A" in result.get("fix_hypotheses", [])


@pytest.mark.asyncio
async def test_get_journey_health_respects_window_filter(tmp_path):
    """Cases from before the window should be excluded from pass_rate calculation."""
    from blop.tools import v2_surface
    from blop.schemas import RecordedFlow, FailureCase

    db_path = str(tmp_path / "journey.db")
    app_url = "https://example.com"
    flow_id = "flow_xyz"

    # A flow recorded for the app
    mock_flow_list = [{"flow_id": flow_id, "flow_name": "test_flow", "app_url": app_url}]
    mock_flow = MagicMock(spec=RecordedFlow)
    mock_flow.business_criticality = "revenue"

    # No cases in window → pass_rate should reflect empty data
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.list_flows", new=AsyncMock(return_value=mock_flow_list)):
            with patch("blop.storage.sqlite.get_flow", new=AsyncMock(return_value=mock_flow)):
                with patch("blop.storage.sqlite.list_cases_for_flow_since", new=AsyncMock(return_value=[])) as mock_since:
                    result = await v2_surface.get_journey_health(app_url=app_url, window="24h")

    # Verify list_cases_for_flow_since was called (not the old list_cases_for_flow)
    mock_since.assert_called_once_with(flow_id, pytest.approx, limit=500) if False else mock_since.assert_called_once()
    args = mock_since.call_args
    assert args[0][0] == flow_id  # first positional arg is flow_id
    # second positional arg is the since_iso — should be approx 24h ago
    since_iso = args[0][1]
    since_dt = datetime.fromisoformat(since_iso)
    expected = datetime.now(timezone.utc) - timedelta(hours=24)
    assert abs((since_dt - expected).total_seconds()) < 60, "since_iso should be ~24h ago"

    journeys = result["journeys"]
    assert len(journeys) == 1
    assert journeys[0]["run_count"] == 0


@pytest.mark.asyncio
async def test_get_correlation_report_temporal_overlap(tmp_path):
    """Signal within cluster time window should get base >= 0.3."""
    from blop.tools.v2_surface import _temporal_overlap

    now = datetime.now(timezone.utc)
    cluster_first = now - timedelta(hours=1)
    cluster_last = now - timedelta(minutes=30)
    signal_ts = (now - timedelta(minutes=45)).isoformat()

    result = _temporal_overlap(signal_ts, cluster_first.isoformat(), cluster_last.isoformat())
    assert result is True


@pytest.mark.asyncio
async def test_get_correlation_report_out_of_window(tmp_path):
    """Signal 72h before cluster should be out of temporal window."""
    from blop.tools.v2_surface import _temporal_overlap

    now = datetime.now(timezone.utc)
    cluster_first = now - timedelta(hours=1)
    signal_ts = (now - timedelta(hours=74)).isoformat()

    result = _temporal_overlap(signal_ts, cluster_first.isoformat(), None)
    assert result is False


@pytest.mark.asyncio
async def test_suggest_flows_for_diff_matches_segments(tmp_path):
    """Changed file src/checkout/index.tsx should suggest checkout_flow."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "suggest.db")
    app_url = "https://example.com"

    graph = SiteContextGraph(
        app_url=app_url,
        created_at=datetime.now(timezone.utc).isoformat(),
        nodes=[
            ContextNode(node_id="route:/checkout", node_type="route", label="/checkout", confidence=0.9),
            ContextNode(
                node_id="intent:checkout_flow",
                node_type="intent",
                label="checkout_flow",
                confidence=0.8,
                metadata={"business_criticality": "revenue", "goal": "Complete checkout"},
            ),
        ],
        edges=[
            ContextEdge(
                source_id="route:/checkout",
                target_id="intent:checkout_flow",
                edge_type="supports_intent",
                weight=1.0,
                confidence=0.9,
            )
        ],
    )

    mock_flow = {"flow_id": "fid_checkout", "flow_name": "checkout_flow", "app_url": app_url}

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_latest_context_graph", new=AsyncMock(return_value=graph)):
            with patch("blop.storage.sqlite.list_flows", new=AsyncMock(return_value=[mock_flow])):
                result = await v2_surface.suggest_flows_for_diff(
                    app_url=app_url,
                    changed_files=["src/checkout/index.tsx"],
                )

    assert "checkout" in result["changed_segments_detected"]
    assert "fid_checkout" in result["suggested_flow_ids"]
    assert len(result["suggestions"]) >= 1
    assert result["suggestions"][0]["intent_label"] == "checkout_flow"


@pytest.mark.asyncio
async def test_autogenerate_flows_finds_unmatched_intents(tmp_path):
    """Intent node without a matching recorded flow should appear in synthesized."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "autogen.db")
    app_url = "https://example.com"

    graph = SiteContextGraph(
        app_url=app_url,
        created_at=datetime.now(timezone.utc).isoformat(),
        nodes=[
            ContextNode(
                node_id="intent:onboarding_flow",
                node_type="intent",
                label="onboarding_flow",
                confidence=0.85,
                metadata={"business_criticality": "activation", "goal": "Complete onboarding"},
            ),
            ContextNode(
                node_id="intent:existing_flow",
                node_type="intent",
                label="existing_flow",
                confidence=0.9,
                metadata={"business_criticality": "revenue"},
            ),
        ],
        edges=[],
    )

    # existing_flow has a recorded flow; onboarding_flow does not
    existing = {"flow_id": "fid_existing", "flow_name": "existing_flow", "app_url": app_url}

    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_latest_context_graph", new=AsyncMock(return_value=graph)):
            with patch("blop.storage.sqlite.list_flows", new=AsyncMock(return_value=[existing])):
                result = await v2_surface.autogenerate_flows(app_url=app_url, record=False)

    assert result["total_unmatched_intents"] == 1
    assert len(result["synthesized"]) == 1
    assert result["synthesized"][0]["flow_name"] == "onboarding_flow"
    assert result["synthesized"][0]["business_criticality"] == "activation"
    assert result["recorded_flow_ids"] == []


@pytest.mark.asyncio
async def test_ingest_telemetry_signals_validates_schema():
    """Signal missing required 'ts' field should be rejected."""
    from blop.tools import v2_surface

    with patch("blop.storage.sqlite.save_telemetry_signals", new=AsyncMock(return_value=(0, 0))):
        with patch("blop.storage.sqlite.list_open_incident_clusters", new=AsyncMock(return_value=[])):
            result = await v2_surface.ingest_telemetry_signals(
                app_url="https://example.com",
                signals=[
                    {"signal_type": "error_rate", "value": 0.5},  # missing "ts" → rejected
                    {"ts": "2026-03-18T10:00:00Z", "signal_type": "error_rate", "value": 0.1},  # valid
                ],
            )

    # First signal (missing 'ts') should be rejected at the normalization step
    assert result["rejected"] >= 1


@pytest.mark.asyncio
async def test_new_tools_in_contract():
    """suggest_flows_for_diff and autogenerate_flows must appear in the v2 contract."""
    from blop.tools import v2_surface

    contract = await v2_surface.get_surface_contract()
    tool_contracts = contract["tool_contracts"]

    assert "blop_v2_suggest_flows_for_diff" in tool_contracts
    assert "blop_v2_autogenerate_flows" in tool_contracts

    suggest = tool_contracts["blop_v2_suggest_flows_for_diff"]
    assert "changed_files" in suggest["request_schema"]["properties"]

    autogen = tool_contracts["blop_v2_autogenerate_flows"]
    assert "criticality_filter" in autogen["request_schema"]["properties"]


# ---------------------------------------------------------------------------
# Error-path tests for v2 tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_context_missing_graph(tmp_path):
    """compare_context with non-existent graph IDs should return an error."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "compare.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_context_graph", new=AsyncMock(return_value=None)):
            result = await v2_surface.compare_context(
                app_url="https://example.com",
                baseline_graph_id="nonexistent_1",
                candidate_graph_id="nonexistent_2",
            )

    assert "error" in result


@pytest.mark.asyncio
async def test_assess_release_risk_no_data(tmp_path):
    """assess_release_risk with no context graph should handle gracefully."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "risk.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_latest_context_graph", new=AsyncMock(return_value=None)):
            with patch("blop.storage.sqlite.list_runs", new=AsyncMock(return_value=[])):
                with patch("blop.storage.sqlite.list_open_incident_clusters", new=AsyncMock(return_value=[])):
                    result = await v2_surface.assess_release_risk(
                        app_url="https://example.com",
                    )

    assert "risk_score" in result or "error" in result


@pytest.mark.asyncio
async def test_get_journey_health_no_flows(tmp_path):
    """get_journey_health with no flows should return empty journeys."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "journey_empty.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.list_flows", new=AsyncMock(return_value=[])):
            result = await v2_surface.get_journey_health(
                app_url="https://example.com",
                window="7d",
            )

    assert result["journeys"] == []


@pytest.mark.asyncio
async def test_cluster_incidents_no_runs(tmp_path):
    """cluster_incidents with no runs should return 0 clusters."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "cluster_empty.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.list_runs", new=AsyncMock(return_value=[])):
            result = await v2_surface.cluster_incidents(
                app_url="https://example.com",
            )

    assert result["cluster_count"] == 0


@pytest.mark.asyncio
async def test_generate_remediation_missing_cluster(tmp_path):
    """generate_remediation with non-existent cluster should return error."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "remediation_err.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_incident_cluster", new=AsyncMock(return_value=None)):
            result = await v2_surface.generate_remediation(
                cluster_id="nonexistent",
            )

    assert "error" in result


@pytest.mark.asyncio
async def test_get_correlation_report_empty_db(tmp_path):
    """get_correlation_report with no clusters or signals should return empty correlations."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "correlation_empty.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.list_open_incident_clusters", new=AsyncMock(return_value=[])):
            with patch("blop.storage.sqlite.list_telemetry_signals", new=AsyncMock(return_value=[])):
                with patch("blop.storage.sqlite.save_correlation_report", new=AsyncMock(return_value="report_123")):
                    result = await v2_surface.get_correlation_report(
                        app_url="https://example.com",
                        window="7d",
                    )

    assert result["matches"] == []


@pytest.mark.asyncio
async def test_suggest_flows_for_diff_no_graph(tmp_path):
    """suggest_flows_for_diff with no context graph should return a note, no suggestions."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "suggest_err.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_latest_context_graph", new=AsyncMock(return_value=None)):
            result = await v2_surface.suggest_flows_for_diff(
                app_url="https://example.com",
                changed_files=["src/checkout.tsx"],
            )

    assert "note" in result or "error" in result
    assert result.get("suggested_flow_ids") == []


@pytest.mark.asyncio
async def test_autogenerate_flows_no_graph(tmp_path):
    """autogenerate_flows with no context graph should return a note, no synthesized flows."""
    from blop.tools import v2_surface

    db_path = str(tmp_path / "autogen_err.db")
    with patch.dict(os.environ, {"BLOP_DB_PATH": db_path}):
        from blop.storage import sqlite
        await sqlite.init_db()

        with patch("blop.storage.sqlite.get_latest_context_graph", new=AsyncMock(return_value=None)):
            result = await v2_surface.autogenerate_flows(
                app_url="https://example.com",
            )

    assert "note" in result or "error" in result
    assert result.get("synthesized") == []


@pytest.mark.asyncio
async def test_ingest_telemetry_all_rejected():
    """All malformed signals should be rejected."""
    from blop.tools import v2_surface

    with patch("blop.storage.sqlite.save_telemetry_signals", new=AsyncMock(return_value=(0, 0))):
        with patch("blop.storage.sqlite.list_open_incident_clusters", new=AsyncMock(return_value=[])):
            result = await v2_surface.ingest_telemetry_signals(
                app_url="https://example.com",
                signals=[
                    {"value": 0.5},  # missing ts and signal_type
                    {"signal_type": "error_rate"},  # missing ts
                ],
            )

    assert result["rejected"] >= 2 or result["accepted"] == 0
