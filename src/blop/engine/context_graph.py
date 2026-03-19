from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from blop.schemas import ContextEdge, ContextGraphDiff, ContextNode, SiteContextGraph, SiteInventory


def detect_app_archetype(inventory: SiteInventory) -> str:
    text = " ".join(
        inventory.headings
        + [b.get("text", "") for b in inventory.buttons]
        + [l.get("text", "") for l in inventory.links]
        + inventory.routes
    ).lower()

    # Classify as canvas/WebGL-heavy only on clear creative-tool signals.
    # "workspace" and "project" are intentionally excluded — they appear in
    # project management, issue trackers, and team apps that are plain DOM apps.
    _canvas_signals = {"canvas", "timeline", "editor"}
    if any(k in text for k in _canvas_signals):
        return "editor_heavy"
    if any(k in text for k in ("checkout", "payment", "subscribe", "billing", "plans")):
        return "checkout_heavy"
    if any(k in text for k in ("pricing", "features", "contact", "about", "demo")) and len(inventory.routes) <= 8:
        return "marketing_site"
    return "saas_app"


def _route_confidence(route: str, business_signals: list[str], auth_signals: list[str]) -> float:
    r = route.lower()
    score = 0.45
    if any(sig.strip("/") in r for sig in business_signals):
        score += 0.25
    if any(sig.strip("/") in r for sig in auth_signals):
        score += 0.15
    if any(k in r for k in ("dashboard", "checkout", "billing", "project", "workspace", "settings")):
        score += 0.15
    return max(0.1, min(score, 0.95))


def build_context_graph(
    app_url: str,
    inventory: SiteInventory,
    flows: list[dict],
    profile_name: str | None = None,
) -> SiteContextGraph:
    now = datetime.now(timezone.utc).isoformat()
    archetype = detect_app_archetype(inventory)

    nodes: list[ContextNode] = []
    edges: list[ContextEdge] = []

    # Route nodes
    for route in inventory.routes:
        route_id = f"route:{route}"
        nodes.append(
            ContextNode(
                node_id=route_id,
                node_type="route",
                label=route,
                confidence=_route_confidence(route, inventory.business_signals, inventory.auth_signals),
                freshness_ts=now,
                metadata={"path_depth": route.count("/")},
            )
        )

    # Intent nodes from discovered flows
    for idx, flow in enumerate(flows):
        flow_name = flow.get("flow_name", f"flow_{idx}")
        intent_id = f"intent:{flow_name}"
        nodes.append(
            ContextNode(
                node_id=intent_id,
                node_type="intent",
                label=flow_name,
                confidence=float(flow.get("confidence", 0.6)),
                freshness_ts=now,
                metadata={
                    "goal": flow.get("goal", ""),
                    "business_criticality": flow.get("business_criticality", "other"),
                    "severity_if_broken": flow.get("severity_if_broken", "medium"),
                },
            )
        )

        start_url = flow.get("starting_url") or app_url
        start_path = urlparse(start_url).path or "/"
        route_id = f"route:{start_path}"
        edges.append(
            ContextEdge(
                source_id=route_id,
                target_id=intent_id,
                edge_type="supports_intent",
                weight=1.0,
                confidence=min(0.95, float(flow.get("confidence", 0.6)) + 0.1),
                metadata={"starting_url": start_url},
            )
        )

    # Transition edges between routes inferred from inventory links.
    # Derive source from the link's origin page when available, else default to "/".
    for link in inventory.links[:80]:
        href = (link.get("href") or "").strip()
        if not href:
            continue
        target_path = urlparse(href).path or "/"
        source_path = (link.get("source_route") or urlparse(link.get("source_url", "")).path) or "/"
        if target_path in inventory.routes and target_path != source_path:
            edges.append(
                ContextEdge(
                    source_id=f"route:{source_path}",
                    target_id=f"route:{target_path}",
                    edge_type="transitions_to",
                    weight=0.7,
                    confidence=0.5,
                    metadata={"anchor_text": link.get("text", "")},
                )
            )

    return SiteContextGraph(
        app_url=app_url,
        profile_name=profile_name,
        archetype=archetype,  # type: ignore[arg-type]
        created_at=now,
        nodes=nodes,
        edges=edges,
        metadata={
            "inventory_routes": len(inventory.routes),
            "inventory_forms": len(inventory.forms),
            "flow_count": len(flows),
        },
    )


def editor_hints_from_archetype(archetype: str) -> dict:
    """Return SpaHints kwargs for apps the context graph classifies as canvas/WebGL-heavy.

    Driven purely by the archetype label so callers don't need to re-implement
    keyword logic. Returns an empty dict for non-editor archetypes.
    """
    if archetype != "editor_heavy":
        return {}
    return {
        "is_editor_heavy": True,
        "editor_settle_ms": 8000,
        "settle_ms": 5000,
        "push_state_navigation": True,
    }


def diff_context_graph(previous: SiteContextGraph | None, current: SiteContextGraph) -> ContextGraphDiff:
    if previous is None:
        return ContextGraphDiff(
            app_url=current.app_url,
            previous_graph_id=None,
            current_graph_id=current.graph_id,
            added_nodes=[n.node_id for n in current.nodes],
            removed_nodes=[],
            added_edges=[f"{e.source_id}->{e.target_id}:{e.edge_type}" for e in current.edges],
            removed_edges=[],
            confidence_delta=0.0,
        )

    prev_nodes = {n.node_id for n in previous.nodes}
    curr_nodes = {n.node_id for n in current.nodes}
    prev_edges = {f"{e.source_id}->{e.target_id}:{e.edge_type}" for e in previous.edges}
    curr_edges = {f"{e.source_id}->{e.target_id}:{e.edge_type}" for e in current.edges}

    prev_conf = sum(n.confidence for n in previous.nodes) / max(len(previous.nodes), 1)
    curr_conf = sum(n.confidence for n in current.nodes) / max(len(current.nodes), 1)

    return ContextGraphDiff(
        app_url=current.app_url,
        previous_graph_id=previous.graph_id,
        current_graph_id=current.graph_id,
        added_nodes=sorted(curr_nodes - prev_nodes),
        removed_nodes=sorted(prev_nodes - curr_nodes),
        added_edges=sorted(curr_edges - prev_edges),
        removed_edges=sorted(prev_edges - curr_edges),
        confidence_delta=round(curr_conf - prev_conf, 4),
    )
