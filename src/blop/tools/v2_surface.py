from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from statistics import quantiles
from urllib.parse import quote

from blop.engine.context_graph import diff_context_graph
from blop.schemas import CorrelationMatch, IncidentCluster, JourneyHealth, ReleaseReference, ReleaseSnapshot, RemediationDraft, TelemetrySignal
from blop.storage import sqlite


CRITICALITY_VALUES = ["revenue", "activation", "retention", "support", "other"]
SEVERITY_ORDER = {"blocker": 4, "high": 3, "medium": 2, "low": 1}
SIMILARITY_THRESHOLD = 0.45


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resource_envelope(app_url: str, data: dict) -> dict:
    return {
        "resource_version": "v2",
        "generated_at": _now_iso(),
        "app_url": app_url,
        "data": data,
    }


def _default_criticality_weights() -> dict[str, float]:
    return {
        "revenue": 1.0,
        "activation": 0.8,
        "retention": 0.7,
        "support": 0.5,
        "other": 0.3,
    }


def _severity_from_score(score: float) -> str:
    if score >= 80:
        return "blocker"
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _window_to_since_iso(window: str) -> str:
    """Convert a window string (24h, 7d, 30d) to an ISO datetime for filtering."""
    if window == "24h":
        delta = timedelta(hours=24)
    elif window == "30d":
        delta = timedelta(days=30)
    else:  # 7d default
        delta = timedelta(days=7)
    return (datetime.now(timezone.utc) - delta).isoformat()


def _jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two token sets derived from underscore/hash split."""
    tokens_a = set(re.split(r"[_#\s]+", a.lower()))
    tokens_b = set(re.split(r"[_#\s]+", b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _merge_similar_buckets(buckets: dict[str, list]) -> dict[str, list]:
    """Greedy single-link clustering: merge bucket keys with Jaccard >= threshold."""
    keys = list(buckets.keys())
    merged: dict[str, list] = {}
    assigned: set[str] = set()

    for i, key_a in enumerate(keys):
        if key_a in assigned:
            continue
        merged_members = list(buckets[key_a])
        assigned.add(key_a)
        for key_b in keys[i + 1:]:
            if key_b in assigned:
                continue
            if _jaccard_similarity(key_a, key_b) >= SIMILARITY_THRESHOLD:
                merged_members.extend(buckets[key_b])
                assigned.add(key_b)
        merged[key_a] = merged_members

    return merged


def _temporal_overlap(
    signal_ts_str: str,
    cluster_first_ts: str | None,
    cluster_last_ts: str | None,
    window_hours: float = 2.0,
) -> bool:
    """Return True if signal_ts falls within cluster time window expanded by window_hours."""
    if not cluster_first_ts:
        return True
    try:
        signal_ts = datetime.fromisoformat(signal_ts_str.replace("Z", "+00:00"))
        cluster_start = datetime.fromisoformat(cluster_first_ts.replace("Z", "+00:00"))
        cluster_end = cluster_start
        if cluster_last_ts:
            try:
                cluster_end = datetime.fromisoformat(cluster_last_ts.replace("Z", "+00:00"))
            except Exception:
                pass
        expanded_start = cluster_start - timedelta(hours=window_hours)
        expanded_end = cluster_end + timedelta(hours=window_hours)
        return expanded_start <= signal_ts <= expanded_end
    except Exception:
        return True  # unparseable timestamps → give benefit of doubt


TOOL_CONTRACTS = {
    "blop_v2_capture_context": {
        "request_schema": {
            "type": "object",
            "required": ["app_url"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "profile_name": {"type": "string"},
                "repo_path": {"type": "string"},
                "max_depth": {"type": "integer", "minimum": 1, "default": 2},
                "max_pages": {"type": "integer", "minimum": 1, "default": 20},
                "seed_urls": {"type": "array", "items": {"type": "string", "format": "uri"}},
                "include_url_pattern": {"type": "string"},
                "exclude_url_pattern": {"type": "string"},
                "intent_focus": {"type": "array", "items": {"type": "string", "enum": CRITICALITY_VALUES}},
            },
        },
        "response_schema": {
            "type": "object",
            "required": ["graph_id", "app_url", "created_at", "node_count", "edge_count", "diff_summary"],
            "properties": {
                "graph_id": {"type": "string"},
                "app_url": {"type": "string"},
                "created_at": {"type": "string", "format": "date-time"},
                "node_count": {"type": "integer"},
                "edge_count": {"type": "integer"},
                "archetype": {"type": "string", "enum": ["marketing_site", "saas_app", "editor_heavy", "checkout_heavy"]},
                "diff_summary": {
                    "type": "object",
                    "properties": {
                        "previous_graph_id": {"type": ["string", "null"]},
                        "added_nodes": {"type": "integer"},
                        "removed_nodes": {"type": "integer"},
                        "added_edges": {"type": "integer"},
                        "removed_edges": {"type": "integer"},
                        "confidence_delta": {"type": "number"},
                    },
                },
            },
        },
        "example": {"app_url": "https://app.example.com", "max_depth": 2, "max_pages": 20},
    },
    "blop_v2_compare_context": {
        "request_schema": {
            "type": "object",
            "required": ["app_url", "baseline_graph_id", "candidate_graph_id"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "baseline_graph_id": {"type": "string"},
                "candidate_graph_id": {"type": "string"},
                "impact_lens": {
                    "type": "array",
                    "items": {"type": "string", "enum": CRITICALITY_VALUES},
                    "default": ["revenue", "activation"],
                },
            },
        },
        "response_schema": {
            "type": "object",
            "properties": {
                "app_url": {"type": "string"},
                "baseline_graph_id": {"type": "string"},
                "candidate_graph_id": {"type": "string"},
                "structural_diff": {"type": "object"},
                "impact_summary": {"type": "array", "items": {"type": "object"}},
            },
        },
        "example": {
            "app_url": "https://app.example.com",
            "baseline_graph_id": "a1",
            "candidate_graph_id": "b2",
            "impact_lens": ["revenue", "activation"],
        },
    },
    "blop_v2_assess_release_risk": {
        "request_schema": {
            "type": "object",
            "required": ["app_url"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "release_id": {"type": "string"},
                "baseline_ref": {"type": "object"},
                "candidate_ref": {"type": "object"},
                "criticality_weights": {"type": "object"},
            },
        },
        "response_schema": {
            "type": "object",
            "required": ["release_id", "risk_score", "risk_level", "top_risks", "recommended_actions"],
            "properties": {
                "release_id": {"type": "string"},
                "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
                "risk_level": {"type": "string", "enum": ["low", "medium", "high", "blocker"]},
                "top_risks": {"type": "array", "items": {"type": "object"}},
                "recommended_actions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "example": {"app_url": "https://app.example.com", "release_id": "release_2026_03_18"},
    },
    "blop_v2_get_journey_health": {
        "request_schema": {
            "type": "object",
            "required": ["app_url"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "window": {"type": "string", "enum": ["24h", "7d", "30d"], "default": "7d"},
                "journey_filter": {"type": "array", "items": {"type": "string"}},
                "criticality_filter": {"type": "array", "items": {"type": "string", "enum": CRITICALITY_VALUES}},
            },
        },
        "response_schema": {"type": "object", "properties": {"app_url": {"type": "string"}, "window": {"type": "string"}, "journeys": {"type": "array"}}},
        "example": {"app_url": "https://app.example.com", "window": "7d"},
    },
    "blop_v2_cluster_incidents": {
        "request_schema": {
            "type": "object",
            "required": ["app_url"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "window": {"type": "string", "enum": ["24h", "7d", "30d"], "default": "7d"},
                "min_cluster_size": {"type": "integer", "minimum": 1, "default": 2},
            },
        },
        "response_schema": {"type": "object", "properties": {"cluster_count": {"type": "integer"}, "clusters": {"type": "array"}}},
        "example": {"app_url": "https://app.example.com", "window": "7d", "min_cluster_size": 2},
    },
    "blop_v2_generate_remediation": {
        "request_schema": {
            "type": "object",
            "required": ["cluster_id"],
            "properties": {
                "cluster_id": {"type": "string"},
                "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
                "include_owner_hints": {"type": "boolean", "default": True},
                "include_fix_hypotheses": {"type": "boolean", "default": True},
            },
        },
        "response_schema": {"type": "object", "properties": {"cluster_id": {"type": "string"}, "issue_draft": {"type": "string"}}},
        "example": {"cluster_id": "cluster_checkout_cta_step_3", "format": "markdown"},
    },
    "blop_v2_ingest_telemetry_signals": {
        "request_schema": {
            "type": "object",
            "required": ["app_url", "signals"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "source": {"type": "string", "enum": ["sentry", "datadog", "ga4", "custom"]},
                "signals": {"type": "array", "items": {"type": "object"}},
            },
        },
        "response_schema": {
            "type": "object",
            "properties": {
                "ingested": {"type": "integer"},
                "rejected": {"type": "integer"},
                "correlation_candidates": {"type": "integer"},
            },
        },
        "example": {
            "app_url": "https://app.example.com",
            "source": "sentry",
            "signals": [{"ts": "2026-03-18T10:00:00Z", "signal_type": "error_rate", "value": 0.14}],
        },
    },
    "blop_v2_get_correlation_report": {
        "request_schema": {
            "type": "object",
            "required": ["app_url"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "window": {"type": "string", "enum": ["24h", "7d", "30d"], "default": "7d"},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.6},
            },
        },
        "response_schema": {"type": "object", "properties": {"window": {"type": "string"}, "matches": {"type": "array"}}},
        "example": {"app_url": "https://app.example.com", "window": "7d", "min_confidence": 0.6},
    },
    "blop_v2_suggest_flows_for_diff": {
        "request_schema": {
            "type": "object",
            "required": ["app_url", "changed_files"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "changed_routes": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        "response_schema": {
            "type": "object",
            "properties": {
                "app_url": {"type": "string"},
                "changed_segments_detected": {"type": "array", "items": {"type": "string"}},
                "suggested_flow_ids": {"type": "array", "items": {"type": "string"}},
                "suggestions": {"type": "array", "items": {"type": "object"}},
            },
        },
        "example": {
            "app_url": "https://app.example.com",
            "changed_files": ["src/checkout/index.tsx", "src/payment/form.tsx"],
        },
    },
    "blop_v2_autogenerate_flows": {
        "request_schema": {
            "type": "object",
            "required": ["app_url"],
            "properties": {
                "app_url": {"type": "string", "format": "uri"},
                "profile_name": {"type": "string"},
                "criticality_filter": {"type": "array", "items": {"type": "string", "enum": CRITICALITY_VALUES}},
                "record": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        "response_schema": {
            "type": "object",
            "properties": {
                "app_url": {"type": "string"},
                "synthesized": {"type": "array", "items": {"type": "object"}},
                "recorded_flow_ids": {"type": "array", "items": {"type": "string"}},
                "total_unmatched_intents": {"type": "integer"},
            },
        },
        "example": {"app_url": "https://app.example.com", "criticality_filter": ["revenue", "activation"]},
    },
}


async def get_surface_contract() -> dict:
    return {
        "resource_version": "v2",
        "generated_at": _now_iso(),
        "naming_convention": {
            "tools_prefix": "blop_v2_",
            "resources_prefix": "blop://v2/",
        },
        "tool_contracts": TOOL_CONTRACTS,
    }


async def capture_context(
    app_url: str,
    profile_name: str | None = None,
    repo_path: str | None = None,
    max_depth: int = 2,
    max_pages: int = 20,
    seed_urls: list[str] | None = None,
    include_url_pattern: str | None = None,
    exclude_url_pattern: str | None = None,
    intent_focus: list[str] | None = None,
) -> dict:
    """Capture a context graph snapshot via pure crawl (no full LLM planning).

    Only triggers a lightweight plan_flows_from_inventory call when intent_focus is set.
    """
    from blop.engine.discovery import inventory_site, plan_flows_from_inventory
    from blop.engine.context_graph import build_context_graph
    from blop.storage.sqlite import get_latest_context_graph, save_context_graph

    inventory = await inventory_site(
        app_url=app_url,
        max_depth=max_depth,
        max_pages=max_pages,
        profile_name=profile_name,
        seed_urls=seed_urls,
        include_url_pattern=include_url_pattern,
        exclude_url_pattern=exclude_url_pattern,
    )

    # Persist inventory
    try:
        from blop.storage.sqlite import save_site_inventory
        await save_site_inventory(app_url, inventory.to_dict())
    except Exception:
        pass

    # Only call LLM if the caller asked for focused intent planning
    flows: list[dict] = []
    if intent_focus:
        valid = [v for v in intent_focus if v in CRITICALITY_VALUES]
        if valid:
            business_goal = f"Prioritize flows tagged as: {', '.join(valid)}"
            try:
                flows = await plan_flows_from_inventory(inventory, business_goal=business_goal)
            except Exception:
                flows = []

    previous_graph = await get_latest_context_graph(app_url, profile_name=profile_name)
    current_graph = build_context_graph(
        app_url=app_url,
        inventory=inventory,
        flows=flows,
        profile_name=profile_name,
    )
    graph_diff = diff_context_graph(previous_graph, current_graph)
    await save_context_graph(current_graph)

    return {
        "graph_id": current_graph.graph_id,
        "app_url": app_url,
        "created_at": current_graph.created_at,
        "node_count": len(current_graph.nodes),
        "edge_count": len(current_graph.edges),
        "archetype": current_graph.archetype,
        "diff_summary": {
            "previous_graph_id": graph_diff.previous_graph_id,
            "added_nodes": len(graph_diff.added_nodes),
            "removed_nodes": len(graph_diff.removed_nodes),
            "added_edges": len(graph_diff.added_edges),
            "removed_edges": len(graph_diff.removed_edges),
            "confidence_delta": graph_diff.confidence_delta,
        },
        "related_v2_resources": [
            f"blop://v2/context/{quote(app_url, safe='')}/latest",
            f"blop://v2/context/{quote(app_url, safe='')}/history/20",
        ],
    }


async def compare_context(
    app_url: str,
    baseline_graph_id: str,
    candidate_graph_id: str,
    impact_lens: list[str] | None = None,
) -> dict:
    baseline = await sqlite.get_context_graph(baseline_graph_id)
    candidate = await sqlite.get_context_graph(candidate_graph_id)
    if not baseline or not candidate:
        return {"error": "One or both graph IDs were not found"}
    if baseline.app_url != app_url or candidate.app_url != app_url:
        return {"error": "Graph IDs do not match app_url"}

    diff = diff_context_graph(baseline, candidate)
    lens = impact_lens or ["revenue", "activation"]
    impact_summary = []
    for criticality in lens:
        affected = 0
        for node in candidate.nodes:
            if node.node_type != "intent":
                continue
            if (node.metadata or {}).get("business_criticality", "other") != criticality:
                continue
            affected += 1
        raw_risk = (affected * 5) + (len(diff.added_nodes) * 1.2) + (len(diff.removed_nodes) * 1.6)
        risk_level = _severity_from_score(min(100.0, raw_risk))
        impact_summary.append(
            {
                "criticality": criticality,
                "risk_level": risk_level,
                "affected_journeys": affected,
            }
        )

    return {
        "app_url": app_url,
        "baseline_graph_id": baseline_graph_id,
        "candidate_graph_id": candidate_graph_id,
        "structural_diff": {
            "added_nodes": diff.added_nodes,
            "removed_nodes": diff.removed_nodes,
            "added_edges": diff.added_edges,
            "removed_edges": diff.removed_edges,
            "confidence_delta": diff.confidence_delta,
        },
        "impact_summary": impact_summary,
    }


async def assess_release_risk(
    app_url: str,
    release_id: str | None = None,
    baseline_ref: dict | None = None,
    candidate_ref: dict | None = None,
    criticality_weights: dict | None = None,
) -> dict:
    weights = _default_criticality_weights()
    if criticality_weights:
        for key, value in criticality_weights.items():
            if key in weights and isinstance(value, (int, float)):
                weights[key] = float(value)

    baseline = ReleaseReference.model_validate(baseline_ref or {})
    candidate = ReleaseReference.model_validate(candidate_ref or {})
    if not candidate.graph_id:
        latest = await sqlite.get_latest_context_graph(app_url)
        if latest:
            candidate.graph_id = latest.graph_id
    if not baseline.graph_id:
        history = await sqlite.list_context_graphs(app_url, limit=2)
        if len(history) >= 2:
            baseline.graph_id = history[1]["graph_id"]

    context_risk = 0.0
    top_risks: list[dict] = []
    if baseline.graph_id and candidate.graph_id:
        compare = await compare_context(
            app_url=app_url,
            baseline_graph_id=baseline.graph_id,
            candidate_graph_id=candidate.graph_id,
            impact_lens=["revenue", "activation", "retention", "support", "other"],
        )
        if not compare.get("error"):
            diff = compare["structural_diff"]
            context_risk = min(40.0, (len(diff["added_nodes"]) * 1.2) + (len(diff["removed_nodes"]) * 1.8))
            for item in compare["impact_summary"][:5]:
                top_risks.append(
                    {
                        "risk_id": uuid.uuid4().hex,
                        "title": f"{item['criticality']} journey exposure after context change",
                        "criticality": item["criticality"],
                        "evidence": [
                            f"affected_journeys={item['affected_journeys']}",
                            f"context_diff:{baseline.graph_id}->{candidate.graph_id}",
                        ],
                    }
                )

    run_risk = 0.0
    run_id = candidate.run_id
    if run_id:
        cases = await sqlite.list_cases_for_run(run_id)
        failed = [c for c in cases if c.status in ("fail", "error", "blocked")]
        total_weight = 0.0
        fail_weight = 0.0
        for case in cases:
            w = weights.get(case.business_criticality, weights["other"])
            total_weight += w
            if case.status in ("fail", "error", "blocked"):
                fail_weight += w
        run_risk = (fail_weight / total_weight) * 60.0 if total_weight else 0.0
        for case in failed[:5]:
            top_risks.append(
                {
                    "risk_id": case.case_id,
                    "title": f"{case.flow_name} failed in candidate run",
                    "criticality": case.business_criticality,
                    "evidence": [f"run_id={run_id}", f"step_failure_index={case.step_failure_index}"],
                }
            )

    risk_score = round(min(100.0, context_risk + run_risk), 2)
    risk_level = _severity_from_score(risk_score)
    final_release_id = release_id or f"release_{uuid.uuid4().hex[:12]}"
    recommended_actions = [
        "Block release if blocker/high risks are in revenue or activation journeys.",
        "Re-run targeted regressions for top failed journeys using hybrid mode.",
        "Generate remediation drafts for the top 3 incident clusters and assign owners.",
    ]
    snapshot = ReleaseSnapshot(
        release_id=final_release_id,
        app_url=app_url,
        created_at=_now_iso(),
        baseline_ref=baseline,
        candidate_ref=candidate,
        risk_score=risk_score,
        risk_level=risk_level,  # type: ignore[arg-type]
        top_risks=top_risks[:10],
        recommended_actions=recommended_actions,
        metadata={"weights": weights},
    )
    await sqlite.save_release_snapshot(snapshot)
    return snapshot.model_dump()


async def get_journey_health(
    app_url: str,
    window: str = "7d",
    journey_filter: list[str] | None = None,
    criticality_filter: list[str] | None = None,
) -> dict:
    since_iso = _window_to_since_iso(window)
    flows = await sqlite.list_flows()
    selected = [f for f in flows if f.get("app_url") == app_url]
    if journey_filter:
        allowed = set(journey_filter)
        selected = [f for f in selected if f.get("flow_name") in allowed or f.get("flow_id") in allowed]

    out: list[JourneyHealth] = []
    for flow in selected:
        flow_full = await sqlite.get_flow(flow["flow_id"])
        if not flow_full:
            continue
        if criticality_filter and flow_full.business_criticality not in criticality_filter:
            continue

        # Use real time-window filtering instead of run-count proxy
        cases = await sqlite.list_cases_for_flow_since(flow["flow_id"], since_iso, limit=500)
        if not cases:
            out.append(
                JourneyHealth(
                    journey_id=flow["flow_id"],
                    journey_name=flow["flow_name"],
                    criticality=flow_full.business_criticality,
                    run_count=0,
                )
            )
            continue
        pass_count = sum(1 for c in cases if c.status == "pass")
        pass_rate = round(pass_count / len(cases), 4)
        stability_values = [max(0.0, min(1.0, c.repair_confidence)) for c in cases]
        stability_score = round(sum(stability_values) / len(stability_values), 4) if stability_values else None

        latencies: list[int] = []
        for case in cases:
            for fp in case.stability_fingerprints:
                if fp.latency_ms > 0:
                    latencies.append(fp.latency_ms)
        if len(latencies) >= 2:
            p95 = int(quantiles(latencies, n=100)[94])
        elif latencies:
            p95 = latencies[0]
        else:
            p95 = None

        mid = max(1, len(cases) // 2)
        newer = cases[:mid]
        older = cases[mid:]
        newer_pass = sum(1 for c in newer if c.status == "pass") / len(newer)
        older_pass = sum(1 for c in older if c.status == "pass") / len(older) if older else newer_pass
        if newer_pass > older_pass + 0.1:
            trend = "improving"
        elif newer_pass < older_pass - 0.1:
            trend = "degrading"
        else:
            trend = "flat"

        out.append(
            JourneyHealth(
                journey_id=flow["flow_id"],
                journey_name=flow["flow_name"],
                criticality=flow_full.business_criticality,
                pass_rate=pass_rate,
                p95_duration_ms=p95,
                stability_score=stability_score,
                trend=trend,  # type: ignore[arg-type]
                run_count=len(cases),
            )
        )

    return {
        "app_url": app_url,
        "window": window,
        "journeys": [j.model_dump() for j in out],
    }


async def cluster_incidents(
    app_url: str,
    run_ids: list[str] | None = None,
    window: str = "7d",
    min_cluster_size: int = 2,
) -> dict:
    selected_run_ids = run_ids or []
    if not selected_run_ids:
        since_iso = _window_to_since_iso(window)
        # Filter runs by actual timestamp
        all_runs = await sqlite.list_runs(limit=500)
        selected_run_ids = [
            r["run_id"]
            for r in all_runs
            if r["app_url"] == app_url and (r.get("started_at") or "") >= since_iso
        ]

    # Build raw failure buckets
    raw_buckets: dict[str, list] = {}
    for run_id in selected_run_ids:
        cases = await sqlite.list_cases_for_run(run_id)
        for case in cases:
            if case.status not in ("fail", "error", "blocked"):
                continue
            key = f"{case.flow_name}#step_{case.step_failure_index if case.step_failure_index is not None else 'unknown'}"
            raw_buckets.setdefault(key, []).append(case)

    # Merge semantically similar buckets via Jaccard similarity
    buckets = _merge_similar_buckets(raw_buckets)

    saved_clusters: list[IncidentCluster] = []
    for key, members in buckets.items():
        if len(members) < min_cluster_size:
            continue
        criticality = sorted({m.business_criticality for m in members})
        max_sev = max((SEVERITY_ORDER.get(m.severity, 1) for m in members), default=1)
        severity = "blocker" if max_sev >= 4 else "high" if max_sev >= 3 else "medium" if max_sev >= 2 else "low"
        cluster_id = f"cluster_{uuid.uuid5(uuid.NAMESPACE_URL, app_url + key).hex[:20]}"
        cluster = IncidentCluster(
            cluster_id=cluster_id,
            app_url=app_url,
            title=f"Repeated failure at {key}",
            severity=severity,  # type: ignore[arg-type]
            affected_flows=len({m.flow_id for m in members}),
            affected_criticality=criticality,
            first_seen=min(m.run_id for m in members),
            last_seen=max(m.run_id for m in members),
            evidence_refs=[f"run:{m.run_id}/case:{m.case_id}" for m in members[:10]],
            member_case_ids=[m.case_id for m in members],
            status="open",
            metadata={"cluster_key": key, "member_count": len(members)},
        )
        await sqlite.save_incident_cluster(cluster)
        saved_clusters.append(cluster)

    return {
        "cluster_count": len(saved_clusters),
        "clusters": [c.model_dump() for c in saved_clusters],
    }


async def generate_remediation(
    cluster_id: str,
    format: str = "markdown",
    include_owner_hints: bool = True,
    include_fix_hypotheses: bool = True,
) -> dict:
    cluster = await sqlite.get_incident_cluster(cluster_id)
    if not cluster:
        return {"error": f"Cluster {cluster_id} not found"}
    samples = []
    for case_id in cluster.member_case_ids[:3]:
        case = await sqlite.get_case(case_id)
        if case:
            samples.append(case)
    repro_steps = []
    evidence = list(cluster.evidence_refs[:10])
    if samples:
        repro_steps = samples[0].repro_steps or [f"Replay flow {samples[0].flow_name} until failure step."]
        for c in samples:
            if c.trace_path:
                evidence.append(c.trace_path)
            evidence.extend(c.screenshots[:2])

    # Default template values
    owner_hints: list[str] = []
    if include_owner_hints:
        owner_hints = [f"Likely owner: team responsible for flow domain '{cluster.title.split('#')[0]}'."]
    fix_hypotheses: list[str] = []
    if include_fix_hypotheses:
        fix_hypotheses = [
            "Selector drift after UI change; add semantic locator fallback.",
            "Page transition timing issue; increase settle/wait condition before interaction.",
            "Auth/session precondition missing in the flow setup.",
        ]

    # Try Gemini for richer fix hypotheses and owner hints
    if (include_owner_hints or include_fix_hypotheses) and os.getenv("GOOGLE_API_KEY"):
        try:
            from blop.prompts import REMEDIATION_PROMPT
            from blop.engine.llm_factory import make_planning_llm, make_message

            llm = make_planning_llm(temperature=0.3, max_output_tokens=800)
            evidence_text = "\n".join(evidence[:5]) or "none"
            console_errors = "\n".join(
                [e for s in samples[:2] for e in (s.console_errors or [])[:2]]
            ) or "none"
            network_errors = "\n".join(
                [e for s in samples[:2] for e in (s.network_errors or [])[:2]]
            ) or "none"
            prompt = REMEDIATION_PROMPT.format(
                title=cluster.title,
                severity=cluster.severity,
                affected_flows=cluster.affected_flows,
                criticality_buckets=", ".join(cluster.affected_criticality),
                evidence=evidence_text,
                console_errors=console_errors,
                network_errors=network_errors,
            )
            response = await llm.ainvoke([make_message(prompt)])
            text = str(response.content) if hasattr(response, "content") else str(response)
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                llm_result = json.loads(m.group())
                if include_fix_hypotheses and llm_result.get("fix_hypotheses"):
                    fix_hypotheses = llm_result["fix_hypotheses"][:3]
                if include_owner_hints and llm_result.get("owner_hint"):
                    owner_hints = [llm_result["owner_hint"]]
        except Exception:
            pass  # fall back to template values

    issue_draft = (
        f"## {cluster.title}\n\n"
        f"- Severity: **{cluster.severity}**\n"
        f"- Affected flows: {cluster.affected_flows}\n"
        f"- Criticality buckets: {', '.join(cluster.affected_criticality)}\n\n"
        f"### Repro\n" + "\n".join([f"{idx + 1}. {step}" for idx, step in enumerate(repro_steps)]) + "\n\n"
        f"### Evidence\n" + "\n".join([f"- {ref}" for ref in evidence[:12]]) + "\n"
    )
    draft = RemediationDraft(
        cluster_id=cluster.cluster_id,
        incident_title=cluster.title,
        severity=cluster.severity,  # type: ignore[arg-type]
        issue_draft=issue_draft,
        repro_steps=repro_steps,
        evidence=evidence[:20],
        owner_hints=owner_hints,
        fix_hypotheses=fix_hypotheses,
        created_at=_now_iso(),
    )
    await sqlite.save_remediation_draft(draft)
    payload = draft.model_dump()
    return payload


async def ingest_telemetry_signals(
    app_url: str,
    signals: list[dict],
    source: str = "custom",
) -> dict:
    normalized: list[TelemetrySignal] = []
    rejected = 0
    for signal in signals:
        try:
            normalized.append(
                TelemetrySignal(
                    app_url=app_url,
                    source=source,  # type: ignore[arg-type]
                    ts=signal["ts"],
                    signal_type=signal["signal_type"],  # type: ignore[arg-type]
                    journey_key=signal.get("journey_key"),
                    route=signal.get("route"),
                    value=float(signal["value"]),
                    unit=signal.get("unit"),
                    tags=signal.get("tags", {}),
                )
            )
        except Exception:
            rejected += 1

    ingested, write_rejected = await sqlite.save_telemetry_signals(normalized)
    open_clusters = await sqlite.list_open_incident_clusters(app_url, limit=200)
    correlation_candidates = min(len(open_clusters), ingested)
    return {
        "ingested": ingested,
        "rejected": rejected + write_rejected,
        "correlation_candidates": correlation_candidates,
    }


async def get_correlation_report(
    app_url: str,
    window: str = "7d",
    min_confidence: float = 0.6,
) -> dict:
    clusters = await sqlite.list_open_incident_clusters(app_url, limit=300)
    signals = await sqlite.list_telemetry_signals(app_url, limit=2000)

    # Resolve cluster first/last seen run_ids to actual timestamps (batch, cached)
    cluster_timestamps: dict[str, tuple[str | None, str | None]] = {}
    seen_run_ids: dict[str, str | None] = {}  # run_id -> started_at cache

    async def _resolve_ts(run_id: str | None) -> str | None:
        if not run_id:
            return None
        if run_id in seen_run_ids:
            return seen_run_ids[run_id]
        run = await sqlite.get_run(run_id)
        ts = run.get("started_at") if run else None
        seen_run_ids[run_id] = ts
        return ts

    for cluster in clusters:
        first_ts = await _resolve_ts(cluster.first_seen)
        last_ts = await _resolve_ts(cluster.last_seen) if cluster.last_seen != cluster.first_seen else first_ts
        cluster_timestamps[cluster.cluster_id] = (first_ts, last_ts)

    matches: list[CorrelationMatch] = []
    for cluster in clusters:
        first_ts, last_ts = cluster_timestamps.get(cluster.cluster_id, (None, None))
        for signal in signals[:200]:
            signal_ts = signal.get("ts", "")
            temporal = _temporal_overlap(signal_ts, first_ts, last_ts)

            base = 0.3 if temporal else 0.05
            if signal.get("journey_key") and signal["journey_key"].lower() in cluster.title.lower():
                base += 0.3
            if signal.get("route") and signal["route"].lower() in cluster.title.lower():
                base += 0.2
            if cluster.severity in ("high", "blocker") and signal.get("signal_type") in ("error_rate", "conversion"):
                base += 0.15
            confidence = round(min(base, 0.99), 2)
            if confidence < min_confidence:
                continue
            matches.append(
                CorrelationMatch(
                    cluster_id=cluster.cluster_id,
                    telemetry_signal=f"{signal.get('signal_type')}@{signal.get('ts')}",
                    confidence=confidence,
                    business_impact_estimate=(
                        "Possible measurable customer impact in critical journey."
                        if cluster.severity in ("high", "blocker")
                        else "Potential localized impact."
                    ),
                )
            )
    out = {"window": window, "matches": [m.model_dump() for m in matches[:100]]}
    await sqlite.save_correlation_report(app_url=app_url, window=window, report=out)
    return out


async def suggest_flows_for_diff(
    app_url: str,
    changed_files: list[str],
    changed_routes: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """Suggest test flows to run based on changed files and routes using the context graph."""
    graph = await sqlite.get_latest_context_graph(app_url)
    if not graph:
        return {
            "app_url": app_url,
            "changed_segments_detected": [],
            "suggested_flow_ids": [],
            "suggestions": [],
            "note": "No context graph found. Run blop_v2_capture_context first.",
        }

    # Extract meaningful path segments from changed files
    stopwords = {
        "src", "app", "lib", "pages", "components", "utils", "js", "ts",
        "tsx", "jsx", "index", "test", "spec", "stories", "style", "css",
        "scss", "mod", "pkg", "go", "py",
    }
    raw_segments: list[str] = []
    for path in changed_files:
        for part in re.split(r"[/._\-]", path):
            if len(part) >= 3 and part.lower() not in stopwords:
                raw_segments.append(part.lower())
    if changed_routes:
        for route in changed_routes:
            for part in re.split(r"[/._\-]", route):
                if len(part) >= 3:
                    raw_segments.append(part.lower())
    changed_segments = list(dict.fromkeys(raw_segments))  # dedup, preserve order

    criticality_weights = _default_criticality_weights()

    # Score route nodes by segment overlap with changed_segments
    route_scores: dict[str, float] = {}
    for node in graph.nodes:
        if node.node_type != "route":
            continue
        score = sum(1.0 for seg in changed_segments if seg in node.label.lower())
        if score > 0:
            route_scores[node.node_id] = score

    # Find intent nodes connected to high-scoring routes via supports_intent edges
    intent_scores: dict[str, float] = {}
    for edge in graph.edges:
        if edge.edge_type != "supports_intent":
            continue
        route_score = route_scores.get(edge.source_id, 0.0)
        if route_score > 0:
            intent_id = edge.target_id
            intent_scores[intent_id] = intent_scores.get(intent_id, 0.0) + route_score

    # Build intent → criticality map
    intent_meta: dict[str, dict] = {}
    for node in graph.nodes:
        if node.node_type == "intent":
            intent_meta[node.node_id] = node.metadata or {}

    # Rank by (score * criticality_weight) DESC
    ranked: list[tuple[float, str]] = []
    for intent_id, score in intent_scores.items():
        bc = intent_meta.get(intent_id, {}).get("business_criticality", "other")
        w = criticality_weights.get(bc, 0.3)
        ranked.append((score * w, intent_id))
    ranked.sort(key=lambda x: x[0], reverse=True)

    # Match intent labels to recorded flows
    flows = await sqlite.list_flows()
    flow_by_name: dict[str, dict] = {f["flow_name"]: f for f in flows if f.get("app_url") == app_url}

    suggestions: list[dict] = []
    suggested_flow_ids: list[str] = []

    for score, intent_id in ranked[:limit]:
        intent_label = intent_id.replace("intent:", "")
        bc = intent_meta.get(intent_id, {}).get("business_criticality", "other")
        goal = intent_meta.get(intent_id, {}).get("goal", "")
        matched_flow = flow_by_name.get(intent_label)

        suggestion: dict = {
            "intent_label": intent_label,
            "business_criticality": bc,
            "match_score": round(score, 2),
            "rationale": (
                f"Changed segments {[s for s in changed_segments if any(s in n.label.lower() for n in graph.nodes if n.node_type == 'route')][:3]} "
                f"overlap with route nodes connected to '{intent_label}' intent (score={score:.1f})"
            ),
            "goal": goal,
        }
        if matched_flow:
            suggestion["flow_id"] = matched_flow["flow_id"]
            suggestion["flow_name"] = matched_flow["flow_name"]
            suggested_flow_ids.append(matched_flow["flow_id"])

        suggestions.append(suggestion)

    return {
        "app_url": app_url,
        "changed_segments_detected": changed_segments[:20],
        "suggested_flow_ids": suggested_flow_ids,
        "suggestions": suggestions,
    }


async def autogenerate_flows(
    app_url: str,
    profile_name: str | None = None,
    criticality_filter: list[str] | None = None,
    record: bool = False,
    limit: int = 5,
) -> dict:
    """Synthesize test flows for intent nodes in the context graph that lack recorded flows."""
    graph = await sqlite.get_latest_context_graph(app_url)
    if not graph:
        return {
            "app_url": app_url,
            "synthesized": [],
            "recorded_flow_ids": [],
            "total_unmatched_intents": 0,
            "note": "No context graph found. Run blop_v2_capture_context first.",
        }

    # All recorded flow names for this app
    flows = await sqlite.list_flows()
    recorded_names: set[str] = {f["flow_name"] for f in flows if f.get("app_url") == app_url}

    criticality_weights = _default_criticality_weights()

    # Collect intent nodes without a matching recorded flow
    candidates: list[tuple[float, object]] = []
    for node in graph.nodes:
        if node.node_type != "intent":
            continue
        flow_name = node.label
        if flow_name in recorded_names:
            continue
        bc = (node.metadata or {}).get("business_criticality", "other")
        if criticality_filter and bc not in criticality_filter:
            continue
        w = criticality_weights.get(bc, 0.3)
        candidates.append((w * node.confidence, node))

    candidates.sort(key=lambda x: x[0], reverse=True)

    synthesized: list[dict] = []
    recorded_flow_ids: list[str] = []

    for _, node in candidates[:limit]:
        bc = (node.metadata or {}).get("business_criticality", "other")
        goal = (node.metadata or {}).get("goal", f"Complete the {node.label} flow on {app_url}")
        flow_spec: dict = {
            "flow_name": node.label,
            "goal": goal,
            "starting_url": app_url,
            "business_criticality": bc,
        }

        if record:
            try:
                from blop.tools.record import record_test_flow
                result = await record_test_flow(
                    app_url=app_url,
                    flow_name=node.label,
                    goal=goal,
                    profile_name=profile_name,
                    business_criticality=bc,
                )
                if result.get("flow_id"):
                    flow_spec["flow_id"] = result["flow_id"]
                    recorded_flow_ids.append(result["flow_id"])
            except Exception:
                pass

        synthesized.append(flow_spec)

    return {
        "app_url": app_url,
        "synthesized": synthesized,
        "recorded_flow_ids": recorded_flow_ids,
        "total_unmatched_intents": len(candidates),
    }


async def get_context_latest_resource(app_url: str) -> dict:
    graph = await sqlite.get_latest_context_graph(app_url)
    if not graph:
        return _resource_envelope(app_url, {"error": f"No context graph found for {app_url}"})
    data = {
        "graph_id": graph.graph_id,
        "profile_name": graph.profile_name,
        "archetype": graph.archetype,
        "created_at": graph.created_at,
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "metadata": graph.metadata,
    }
    return _resource_envelope(app_url, data)


async def get_context_history_resource(app_url: str, limit: int = 20) -> dict:
    history = await sqlite.list_context_graphs(app_url, limit=limit)
    return _resource_envelope(app_url, {"history": history, "total": len(history)})


async def get_context_diff_resource(app_url: str, baseline_graph_id: str, candidate_graph_id: str) -> dict:
    compared = await compare_context(app_url, baseline_graph_id, candidate_graph_id)
    return _resource_envelope(app_url, compared)


async def get_release_risk_resource(release_id: str) -> dict:
    snapshot = await sqlite.get_release_snapshot(release_id)
    if not snapshot:
        return {
            "resource_version": "v2",
            "generated_at": _now_iso(),
            "app_url": "",
            "data": {"error": f"Release snapshot {release_id} not found"},
        }
    return _resource_envelope(snapshot.app_url, snapshot.model_dump())


async def get_journey_health_resource(app_url: str, window: str = "7d") -> dict:
    health = await get_journey_health(app_url=app_url, window=window)
    return _resource_envelope(app_url, health)


async def get_incidents_open_resource(app_url: str) -> dict:
    clusters = await sqlite.list_open_incident_clusters(app_url, limit=200)
    return _resource_envelope(app_url, {"clusters": [c.model_dump() for c in clusters], "total": len(clusters)})


async def get_incident_resource(cluster_id: str) -> dict:
    cluster = await sqlite.get_incident_cluster(cluster_id)
    if not cluster:
        return {
            "resource_version": "v2",
            "generated_at": _now_iso(),
            "app_url": "",
            "data": {"error": f"Cluster {cluster_id} not found"},
        }
    return _resource_envelope(cluster.app_url, cluster.model_dump())


async def get_incident_remediation_resource(cluster_id: str) -> dict:
    draft = await sqlite.get_remediation_draft(cluster_id)
    if not draft:
        generated = await generate_remediation(cluster_id=cluster_id, format="json")
        if generated.get("error"):
            return {
                "resource_version": "v2",
                "generated_at": _now_iso(),
                "app_url": "",
                "data": generated,
            }
        draft = RemediationDraft.model_validate(generated)
    cluster = await sqlite.get_incident_cluster(cluster_id)
    app_url = cluster.app_url if cluster else ""
    return _resource_envelope(app_url, draft.model_dump())


async def get_correlation_resource(app_url: str, window: str = "7d") -> dict:
    latest = await sqlite.get_latest_correlation_report(app_url=app_url, window=window)
    if latest:
        return _resource_envelope(app_url, latest["report"])
    report = await get_correlation_report(app_url=app_url, window=window, min_confidence=0.6)
    return _resource_envelope(app_url, report)
