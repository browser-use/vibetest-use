"""Tests for blop.engine.context_graph."""
from __future__ import annotations

import pytest

from blop.schemas import SiteInventory


def _empty_inventory(app_url: str = "https://example.com") -> SiteInventory:
    return SiteInventory(
        app_url=app_url,
        routes=[],
        buttons=[],
        links=[],
        forms=[],
        headings=[],
        auth_signals=[],
        business_signals=[],
    )


def test_detect_editor_heavy():
    """Inventory with canvas/editor buttons -> editor_heavy."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = _empty_inventory()
    inventory.buttons = [{"text": "Open Canvas"}, {"text": "Timeline view"}]
    assert detect_app_archetype(inventory) == "editor_heavy"


def test_detect_checkout_heavy():
    """Inventory with checkout/payment buttons -> checkout_heavy."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = _empty_inventory()
    inventory.buttons = [{"text": "Checkout"}, {"text": "Subscribe now"}]
    assert detect_app_archetype(inventory) == "checkout_heavy"


def test_detect_marketing_site():
    """Inventory with pricing/plans headings -> marketing_site."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = _empty_inventory()
    inventory.headings = ["Pricing", "Features", "Contact us"]
    inventory.routes = ["/", "/pricing", "/features", "/contact", "/about"]
    assert detect_app_archetype(inventory) == "marketing_site"


def test_detect_saas_app_default():
    """Inventory with dashboard buttons -> saas_app."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = _empty_inventory()
    inventory.buttons = [{"text": "Dashboard"}, {"text": "Settings"}]
    inventory.routes = ["/dashboard", "/settings", "/teams", "/projects"]
    assert detect_app_archetype(inventory) == "saas_app"


def test_detect_editor_takes_precedence_over_checkout():
    """Editor signals take precedence over checkout when both present."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = _empty_inventory()
    inventory.buttons = [{"text": "Canvas Editor"}, {"text": "Checkout"}]
    assert detect_app_archetype(inventory) == "editor_heavy"


def test_detect_checkout_takes_precedence_over_marketing():
    """Checkout signals take precedence over marketing when both present."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = _empty_inventory()
    inventory.headings = ["Pricing"]
    inventory.buttons = [{"text": "Subscribe"}]
    assert detect_app_archetype(inventory) == "checkout_heavy"


def test_build_context_graph_has_nodes_and_edges():
    """Build graph with routes and flows, verify nodes and edges exist."""
    from blop.engine.context_graph import build_context_graph

    inventory = _empty_inventory()
    inventory.routes = ["/", "/dashboard", "/settings"]
    inventory.links = [
        {"href": "/dashboard", "text": "Dashboard", "source_route": "/"},
        {"href": "/settings", "text": "Settings", "source_route": "/dashboard"},
    ]
    flows = [
        {"flow_name": "login_flow", "goal": "Log in", "starting_url": "https://example.com/"},
        {"flow_name": "settings_flow", "goal": "Edit settings", "starting_url": "https://example.com/settings"},
    ]

    graph = build_context_graph("https://example.com", inventory, flows, profile_name="test_profile")

    assert graph.app_url == "https://example.com"
    assert graph.profile_name == "test_profile"
    assert graph.archetype == "saas_app"

    route_nodes = [n for n in graph.nodes if n.node_type == "route"]
    intent_nodes = [n for n in graph.nodes if n.node_type == "intent"]
    assert len(route_nodes) == 3
    assert len(intent_nodes) == 2

    assert any(n.node_id == "route:/" for n in graph.nodes)
    assert any(n.node_id == "intent:login_flow" for n in graph.nodes)

    assert len(graph.edges) >= 2
    supports_edges = [e for e in graph.edges if e.edge_type == "supports_intent"]
    assert len(supports_edges) == 2

    assert graph.metadata["inventory_routes"] == 3
    assert graph.metadata["flow_count"] == 2


def test_diff_context_graph_no_previous():
    """Diff with previous=None, verify all nodes are added."""
    from blop.engine.context_graph import build_context_graph, diff_context_graph

    inventory = _empty_inventory()
    inventory.routes = ["/", "/pricing"]
    flows = [{"flow_name": "pricing_flow", "goal": "View pricing"}]

    current = build_context_graph("https://example.com", inventory, flows)

    diff = diff_context_graph(None, current)

    assert diff.previous_graph_id is None
    assert diff.current_graph_id == current.graph_id
    assert len(diff.added_nodes) > 0
    assert len(diff.removed_nodes) == 0
    assert len(diff.added_edges) >= 1
    assert len(diff.removed_edges) == 0
    assert diff.confidence_delta == 0.0


def test_diff_context_graph_with_changes():
    """Diff two graphs with different nodes, verify added/removed counts."""
    from blop.engine.context_graph import build_context_graph, diff_context_graph

    inventory_old = _empty_inventory()
    inventory_old.routes = ["/", "/old-page"]
    flows_old = [{"flow_name": "old_flow", "goal": "Old goal"}]
    previous = build_context_graph("https://example.com", inventory_old, flows_old)

    inventory_new = _empty_inventory()
    inventory_new.routes = ["/", "/new-page"]
    flows_new = [{"flow_name": "new_flow", "goal": "New goal"}]
    current = build_context_graph("https://example.com", inventory_new, flows_new)

    diff = diff_context_graph(previous, current)

    assert diff.previous_graph_id == previous.graph_id
    assert diff.current_graph_id == current.graph_id

    prev_node_ids = {n.node_id for n in previous.nodes}
    curr_node_ids = {n.node_id for n in current.nodes}

    assert set(diff.added_nodes) == curr_node_ids - prev_node_ids
    assert set(diff.removed_nodes) == prev_node_ids - curr_node_ids

    assert "route:/new-page" in diff.added_nodes or "intent:new_flow" in diff.added_nodes
    assert "route:/old-page" in diff.removed_nodes or "intent:old_flow" in diff.removed_nodes


def test_editor_hints_from_archetype():
    """editor_heavy returns non-empty dict with is_editor_heavy=True."""
    from blop.engine.context_graph import editor_hints_from_archetype

    hints = editor_hints_from_archetype("editor_heavy")
    assert isinstance(hints, dict)
    assert len(hints) > 0
    assert hints["is_editor_heavy"] is True
    assert hints.get("editor_settle_ms") == 8000
    assert hints.get("settle_ms") == 5000
    assert hints.get("push_state_navigation") is True


def test_editor_hints_non_editor():
    """saas_app returns empty dict."""
    from blop.engine.context_graph import editor_hints_from_archetype

    assert editor_hints_from_archetype("saas_app") == {}
    assert editor_hints_from_archetype("marketing_site") == {}
    assert editor_hints_from_archetype("checkout_heavy") == {}
