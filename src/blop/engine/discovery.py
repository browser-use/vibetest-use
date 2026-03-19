"""Inventory-first discovery: BFS crawl → Gemini planning → quality gate."""
from __future__ import annotations

import json
import os
import re
from typing import Optional
from urllib.parse import urlparse

from blop.config import BLOP_DISCOVERY_MAX_PAGES
from blop.schemas import SiteInventory


def _url_priority(url: str, business_signals: list[str], auth_signals: list[str], hotspot_paths: set[str]) -> float:
    """Higher score means crawl sooner."""
    u = url.lower()
    score = 0.2
    if any(sig in u for sig in ("pricing", "checkout", "billing", "payment", "plans", "subscribe")):
        score += 0.45
    if any(sig in u for sig in ("login", "signup", "register", "auth", "dashboard")):
        score += 0.25
    if any(sig.strip("/") in u for sig in business_signals):
        score += 0.2
    if any(sig.strip("/") in u for sig in auth_signals):
        score += 0.1
    path = urlparse(url).path or "/"
    if path in hotspot_paths:
        score += 0.35
    return score


def _adaptive_budget(base_max_pages: int, business_signals: list[str], auth_signals: list[str]) -> int:
    """Increase crawl budget for richer app signals, keep bounded."""
    signal_count = len(set(business_signals + auth_signals))
    if signal_count >= 8:
        return min(40, base_max_pages + 12)
    if signal_count >= 4:
        return min(30, base_max_pages + 6)
    return base_max_pages


async def _resolve_profile_storage_state(profile_name: Optional[str]) -> Optional[str]:
    """Resolve Playwright storage_state path from saved auth profile."""
    if not profile_name:
        return None
    try:
        from blop.storage.sqlite import get_auth_profile
        from blop.engine.auth import resolve_storage_state

        profile = await get_auth_profile(profile_name)
        if profile:
            return await resolve_storage_state(profile)
    except Exception:
        pass
    return None


def _extract_interactive_nodes_flat(
    node: dict,
    max_nodes: int = 50,
    _count: Optional[list[int]] = None,
) -> list[dict]:
    """Flatten an ARIA snapshot into compact interactive nodes."""
    if _count is None:
        _count = [0]
    interactive_roles = {
        "button", "link", "textbox", "checkbox", "radio", "combobox",
        "listbox", "menuitem", "tab", "switch", "searchbox", "spinbutton",
    }
    role = node.get("role", "")
    name = node.get("name", "")
    results: list[dict] = []
    if role in interactive_roles and name and _count[0] < max_nodes:
        entry: dict = {"role": role, "name": name}
        if node.get("disabled"):
            entry["disabled"] = True
        results.append(entry)
        _count[0] += 1

    for child in node.get("children", []):
        if _count[0] >= max_nodes:
            break
        if isinstance(child, dict):
            results.extend(_extract_interactive_nodes_flat(child, max_nodes=max_nodes, _count=_count))
    return results


async def _capture_page_structure(page, max_nodes: int = 50) -> list[dict]:
    """Capture compact interactive structure from Playwright accessibility tree."""
    try:
        accessibility = getattr(page, "accessibility", None)
        if not accessibility or not hasattr(accessibility, "snapshot"):
            return []
        snapshot = await accessibility.snapshot(interesting_only=True)
        if not snapshot or not isinstance(snapshot, dict):
            return []
        return _extract_interactive_nodes_flat(snapshot, max_nodes=max_nodes)
    except Exception:
        return []


async def inventory_site(
    app_url: str,
    max_depth: int = 2,
    max_pages: int = BLOP_DISCOVERY_MAX_PAGES,
    same_origin_only: bool = True,
    profile_name: Optional[str] = None,
    seed_urls: Optional[list[str]] = None,
    include_url_pattern: Optional[str] = None,
    exclude_url_pattern: Optional[str] = None,
) -> SiteInventory:
    """BFS crawl up to depth max_depth; extract buttons, links, forms, headings, and signals."""
    from playwright.async_api import async_playwright

    base_origin = urlparse(app_url).netloc
    visited: set[str] = set()
    queue: list[tuple[str, int, float]] = [(app_url, 0, 1.0)]
    if seed_urls:
        for seed in seed_urls:
            if seed and seed != app_url:
                queue.append((seed, 0, 0.9))

    all_buttons: list[dict] = []
    all_links: list[dict] = []
    all_forms: list[dict] = []
    all_headings: list[str] = []
    all_routes: set[str] = set()
    auth_signals: list[str] = []
    business_signals: list[str] = []
    page_structures: dict[str, list[dict]] = {}
    crawled_pages = 0
    adaptive_max_pages = max_pages
    hotspot_paths: set[str] = set()

    try:
        from blop.storage.sqlite import get_latest_context_graph
        previous_graph = await get_latest_context_graph(app_url, profile_name=profile_name)
        if previous_graph:
            for node in previous_graph.nodes:
                if node.node_type == "route" and node.confidence >= 0.7:
                    hotspot_paths.add(node.label)
    except Exception:
        pass

    storage_state = await _resolve_profile_storage_state(profile_name)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_kwargs: dict = {}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)

        try:
            while queue and crawled_pages < adaptive_max_pages:
                queue.sort(key=lambda item: item[2], reverse=True)
                url, depth, _priority = queue.pop(0)
                if url in visited:
                    continue
                if same_origin_only and urlparse(url).netloc != base_origin:
                    continue
                if include_url_pattern and not re.search(include_url_pattern, url):
                    continue
                if exclude_url_pattern and re.search(exclude_url_pattern, url):
                    continue
                visited.add(url)

                page = None
                try:
                    page = await context.new_page()
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=15000)
                    except Exception:
                        try:
                            await page.goto(url, timeout=15000)
                        except Exception:
                            await page.close()
                            page = None
                            continue

                    crawled_pages += 1

                    page_buttons = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('button, [role="button"], a.btn, .cta, [class*="btn"]'))
                            .map(el => ({text: el.textContent.trim().slice(0,120), id: el.id, href: el.getAttribute('href') || null}))
                            .filter(el => el.text).slice(0,25)"""
                    )
                    page_links = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('a[href]'))
                            .map(el => ({text: el.textContent.trim().slice(0,120), href: el.href}))
                            .filter(el => el.text && !el.href.startsWith('mailto:') && !el.href.startsWith('tel:'))
                            .slice(0,35)"""
                    )
                    page_forms = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('form'))
                            .map(form => ({
                                action: form.action,
                                inputs: Array.from(form.querySelectorAll('input, textarea, select'))
                                    .map(el => ({type: el.type, name: el.name, placeholder: el.placeholder, label: el.getAttribute('aria-label') || ''}))
                            })).slice(0,6)"""
                    )
                    page_headings = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('h1, h2, h3'))
                            .map(el => el.textContent.trim().slice(0,100))
                            .filter(t => t).slice(0,10)"""
                    )
                    page_routes = await page.evaluate(
                        """() => [...new Set(Array.from(document.querySelectorAll('a[href]'))
                            .map(el => { try { return new URL(el.href).pathname; } catch(e) { return null; } })
                            .filter(p => p && p !== '/'))].slice(0,30)"""
                    )
                    page_nodes = await _capture_page_structure(page, max_nodes=50)

                    all_buttons.extend(page_buttons)
                    all_links.extend(page_links)
                    all_forms.extend(page_forms)
                    all_headings.extend(page_headings)
                    for route in page_routes:
                        all_routes.add(route)
                    if page_nodes:
                        page_structures[url] = page_nodes

                    page_text_lower = " ".join(
                        [b.get("text", "") for b in page_buttons]
                        + [l.get("text", "") for l in page_links]
                        + page_headings
                    ).lower()

                    for signal in ("sign in", "login", "log in", "sign up", "register", "logout",
                                   "dashboard", "/auth", "/login", "/signup", "get started", "create account"):
                        if signal in page_text_lower and signal not in auth_signals:
                            auth_signals.append(signal)

                    for signal in ("pricing", "contact", "integration", "oauth", "checkout",
                                   "payment", "subscribe", "onboarding", "demo", "trial", "plans"):
                        if signal in page_text_lower and signal not in business_signals:
                            business_signals.append(signal)
                    adaptive_max_pages = _adaptive_budget(max_pages, business_signals, auth_signals)

                    routes_text = " ".join(page_routes).lower()
                    for signal in ("/pricing", "/contact", "/login", "/signup", "/auth", "/checkout", "/demo"):
                        if signal in routes_text and signal not in business_signals + auth_signals:
                            if signal in ("/login", "/signup", "/auth"):
                                if signal not in auth_signals:
                                    auth_signals.append(signal)
                            else:
                                if signal not in business_signals:
                                    business_signals.append(signal)

                    if depth < max_depth:
                        for link in page_links:
                            href = link.get("href", "")
                            if href and href.startswith("http"):
                                if urlparse(href).netloc != base_origin:
                                    continue
                                if include_url_pattern and not re.search(include_url_pattern, href):
                                    continue
                                if exclude_url_pattern and re.search(exclude_url_pattern, href):
                                    continue
                                if href not in visited:
                                    priority = _url_priority(href, business_signals, auth_signals, hotspot_paths)
                                    queue.append((href, depth + 1, priority))

                    await page.close()
                    page = None
                except Exception:
                    if page:
                        try:
                            await page.close()
                        except Exception:
                            pass
        finally:
            try:
                await context.close()
            except Exception:
                pass
            await browser.close()

    return SiteInventory(
        app_url=app_url,
        routes=sorted(list(all_routes))[:30],
        buttons=all_buttons[:30],
        links=all_links[:40],
        forms=all_forms[:10],
        headings=list(dict.fromkeys(all_headings))[:20],
        auth_signals=auth_signals,
        business_signals=business_signals,
        page_structures=page_structures,
        crawled_pages=crawled_pages,
    )


async def get_page_structure(
    app_url: str,
    target_url: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> dict:
    """Capture a single-page interactive structure snapshot via Playwright accessibility tree."""
    from playwright.async_api import async_playwright
    from blop.engine.interaction import wait_for_spa_ready

    storage_state = await _resolve_profile_storage_state(profile_name)
    url = target_url or app_url

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_kwargs: dict = {}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                await page.goto(url, timeout=20000)
            await wait_for_spa_ready(page, settle_ms=1200, timeout_ms=12000)
            nodes = await _capture_page_structure(page, max_nodes=80)
            return {
                "app_url": app_url,
                "requested_url": url,
                "current_url": page.url,
                "interactive_nodes": nodes,
                "interactive_node_count": len(nodes),
            }
        finally:
            await page.close()
            await browser.close()


async def plan_flows_from_inventory(
    inventory: SiteInventory,
    repo_context: Optional[str] = None,
    business_goal: Optional[str] = None,
) -> list[dict]:
    """Send inventory to LLM with DISCOVER_PROMPT and return typed flows."""
    from blop.prompts import DISCOVER_PROMPT
    from blop.engine.llm_factory import make_planning_llm, make_message

    provider = os.getenv("BLOP_LLM_PROVIDER", "google").lower()
    if provider == "google" and not os.getenv("GOOGLE_API_KEY"):
        return _fallback_flows(inventory.app_url)
    if provider == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        return _fallback_flows(inventory.app_url)
    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        return _fallback_flows(inventory.app_url)

    llm_kwargs: dict = {"temperature": 0.7, "max_output_tokens": 2000}
    # Extended thinking: enable for complex apps or when budget set
    thinking_budget = int(os.getenv("BLOP_THINKING_BUDGET", "0"))
    if thinking_budget > 0 and provider == "google":
        archetype_str = ""
        try:
            from blop.engine.context_graph import detect_app_archetype
            archetype_str = detect_app_archetype(inventory)
        except Exception:
            pass
        if archetype_str in ("editor_heavy", "checkout_heavy") or len(inventory.routes) > 12:
            llm_kwargs["thinking_budget"] = thinking_budget

    llm = make_planning_llm(**llm_kwargs)
    inventory_text = json.dumps(inventory.to_dict(), separators=(",", ":"))

    extra_context = ""
    if business_goal:
        extra_context += f"\nBusiness goal to prioritize: {business_goal}"
    if repo_context:
        extra_context += f"\nRepo context: {repo_context[:500]}"

    prompt = DISCOVER_PROMPT.format(
        app_url=inventory.app_url,
        inventory_text=inventory_text,
        extra_context=extra_context,
    )

    try:
        response = await llm.ainvoke([make_message(prompt)])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            flows = json.loads(m.group())
            required_keys = {"flow_name", "goal"}
            valid_flows = []
            for f in flows:
                if isinstance(f, dict) and required_keys.issubset(f.keys()):
                    f.setdefault("starting_url", inventory.app_url)
                    f.setdefault("preconditions", [])
                    f.setdefault("likely_assertions", [])
                    f.setdefault("severity_if_broken", "medium")
                    f.setdefault("confidence", 0.7)
                    f.setdefault("business_criticality", "other")
                    valid_flows.append(f)
            if valid_flows:
                return valid_flows
    except Exception:
        pass

    return _fallback_flows(inventory.app_url)


def quality_gate_flows(inventory: SiteInventory, flows: list[dict]) -> tuple[bool, list[str]]:
    """Check that flows are specific and cover key signals. Returns (passed, warnings)."""
    warnings: list[str] = []

    generic_names = {"page_loads", "nav_links", "forms_work"}
    flow_names = {f.get("flow_name", "") for f in flows}

    if flow_names.issubset(generic_names):
        warnings.append("All flows are generic fallbacks; inventory scan may have returned no rich signals")
        return False, warnings

    # Auth signals must produce an auth flow
    if inventory.auth_signals:
        auth_kws = {"login", "auth", "signin", "signup", "register", "sign_in", "sign_up"}
        has_auth_flow = any(
            any(kw in f.get("flow_name", "").lower() or kw in f.get("goal", "").lower()
                for kw in auth_kws)
            for f in flows
        )
        if not has_auth_flow:
            warnings.append(
                f"Auth signals detected ({inventory.auth_signals[:3]}) but no auth flow proposed"
            )

    # Confidence gate
    confidences = [f.get("confidence", 0.5) for f in flows]
    if all(c < 0.4 for c in confidences):
        warnings.append("All flows have low confidence (< 0.4)")
        return False, warnings

    return True, warnings


async def discover_flows(
    app_url: str,
    repo_path: Optional[str] = None,
    profile_name: Optional[str] = None,
    business_goal: Optional[str] = None,
    max_depth: int = 2,
    max_pages: int = BLOP_DISCOVERY_MAX_PAGES,
    seed_urls: Optional[list[str]] = None,
    include_url_pattern: Optional[str] = None,
    exclude_url_pattern: Optional[str] = None,
    return_inventory: bool = False,
) -> dict:
    """Crawl site, plan flows, quality-gate, return rich discovery result."""
    inventory = await inventory_site(
        app_url,
        max_depth=max_depth,
        max_pages=max_pages,
        profile_name=profile_name,
        seed_urls=seed_urls,
        include_url_pattern=include_url_pattern,
        exclude_url_pattern=exclude_url_pattern,
    )
    try:
        from blop.storage.sqlite import save_site_inventory
        await save_site_inventory(app_url, inventory.to_dict())
    except Exception:
        pass

    if repo_path:
        flows = await _flows_from_repo(repo_path, app_url, inventory, business_goal)
    else:
        flows = await plan_flows_from_inventory(inventory, business_goal=business_goal)

    # Clamp to 3-8 flows
    flows = flows[:8]
    if len(flows) < 3:
        flows += _fallback_flows(app_url)[: 3 - len(flows)]

    passed, warnings = quality_gate_flows(inventory, flows)
    from blop.engine.context_graph import build_context_graph, detect_app_archetype, diff_context_graph
    from blop.storage.sqlite import get_latest_context_graph, save_context_graph

    previous_graph = await get_latest_context_graph(app_url, profile_name=profile_name)
    current_graph = build_context_graph(
        app_url=app_url,
        inventory=inventory,
        flows=flows,
        profile_name=profile_name,
    )
    graph_diff = diff_context_graph(previous_graph, current_graph)
    await save_context_graph(current_graph)

    result = {
        "app_url": app_url,
        "inventory_summary": {
            "routes_found": len(inventory.routes),
            "auth_signals": inventory.auth_signals,
            "business_signals": inventory.business_signals,
            "structured_pages": len(inventory.page_structures),
            "crawled_pages": inventory.crawled_pages,
            "app_archetype": detect_app_archetype(inventory),
        },
        "flows": flows,
        "flow_count": len(flows),
        "quality": {"passed": passed, "warnings": warnings},
        "context_graph": {
            "graph_id": current_graph.graph_id,
            "node_count": len(current_graph.nodes),
            "edge_count": len(current_graph.edges),
            "archetype": current_graph.archetype,
            "diff": graph_diff.model_dump(),
        },
    }
    if return_inventory:
        result["inventory"] = inventory.to_dict()
    return result


async def explore_site_inventory(
    app_url: str,
    profile_name: Optional[str] = None,
    max_depth: int = 2,
    max_pages: int = BLOP_DISCOVERY_MAX_PAGES,
    seed_urls: Optional[list[str]] = None,
    include_url_pattern: Optional[str] = None,
    exclude_url_pattern: Optional[str] = None,
) -> dict:
    """Inventory-only crawl for exploratory mapping without LLM flow planning."""
    from blop.engine.context_graph import detect_app_archetype

    inventory = await inventory_site(
        app_url=app_url,
        max_depth=max_depth,
        max_pages=max_pages,
        profile_name=profile_name,
        seed_urls=seed_urls,
        include_url_pattern=include_url_pattern,
        exclude_url_pattern=exclude_url_pattern,
    )
    try:
        from blop.storage.sqlite import save_site_inventory
        await save_site_inventory(app_url, inventory.to_dict())
    except Exception:
        pass
    return {
        "app_url": app_url,
        "inventory_summary": {
            "routes_found": len(inventory.routes),
            "auth_signals": inventory.auth_signals,
            "business_signals": inventory.business_signals,
            "structured_pages": len(inventory.page_structures),
            "crawled_pages": inventory.crawled_pages,
            "app_archetype": detect_app_archetype(inventory),
        },
        "inventory": inventory.to_dict(),
    }


async def _flows_from_repo(
    repo_path: str,
    app_url: str,
    inventory: SiteInventory,
    business_goal: Optional[str] = None,
) -> list[dict]:
    import glob as glob_module

    patterns = [
        os.path.join(repo_path, "pages/**/*.tsx"),
        os.path.join(repo_path, "app/**/page.tsx"),
        os.path.join(repo_path, "src/**/*.tsx"),
    ]
    files: list[str] = []
    for pat in patterns:
        files.extend(glob_module.glob(pat, recursive=True))
    if not files:
        files = glob_module.glob(os.path.join(repo_path, "**/*.{ts,tsx,js,jsx}"), recursive=True)[:30]

    from blop.engine.llm_factory import make_planning_llm, make_message

    provider = os.getenv("BLOP_LLM_PROVIDER", "google").lower()
    has_key = (
        (provider == "google" and os.getenv("GOOGLE_API_KEY"))
        or (provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"))
        or (provider == "openai" and os.getenv("OPENAI_API_KEY"))
    )
    if not files or not has_key:
        return await plan_flows_from_inventory(inventory, business_goal=business_goal)

    llm = make_planning_llm(temperature=0.7, max_output_tokens=2000)
    file_list = "\n".join(files[:50])
    extra = f"\nBusiness goal: {business_goal}" if business_goal else ""

    prompt = f"""Based on these source files for {app_url}:{extra}
{file_list}

Generate 5-8 browser test flows as JSON with keys:
flow_name, goal, starting_url, preconditions, likely_assertions, severity_if_broken, confidence, business_criticality

business_criticality must be one of: revenue, activation, retention, support, other

Return only a JSON array:
[{{"flow_name": "...", "goal": "...", "starting_url": "...", "preconditions": [], "likely_assertions": ["..."], "severity_if_broken": "high", "confidence": 0.8, "business_criticality": "revenue"}}]"""

    try:
        response = await llm.ainvoke([make_message(prompt)])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            flows = json.loads(m.group())
            valid = []
            for f in flows:
                if isinstance(f, dict) and {"flow_name", "goal"}.issubset(f.keys()):
                    f.setdefault("starting_url", app_url)
                    f.setdefault("preconditions", [])
                    f.setdefault("likely_assertions", [])
                    f.setdefault("severity_if_broken", "medium")
                    f.setdefault("confidence", 0.7)
                    f.setdefault("business_criticality", "other")
                    valid.append(f)
            if valid:
                return valid
    except Exception:
        pass

    return await plan_flows_from_inventory(inventory, business_goal=business_goal)


def _fallback_flows(app_url: str) -> list[dict]:
    return [
        {
            "flow_name": "page_loads",
            "goal": f"Navigate to {app_url} and verify the page loads",
            "starting_url": app_url,
            "preconditions": [],
            "likely_assertions": ["page title visible", "no 404 error"],
            "severity_if_broken": "blocker",
            "confidence": 0.3,
        },
        {
            "flow_name": "nav_links",
            "goal": f"Check all navigation links on {app_url} are functional",
            "starting_url": app_url,
            "preconditions": [],
            "likely_assertions": ["links respond", "no broken pages"],
            "severity_if_broken": "medium",
            "confidence": 0.3,
        },
        {
            "flow_name": "forms_work",
            "goal": f"Test any forms or input fields on {app_url}",
            "starting_url": app_url,
            "preconditions": [],
            "likely_assertions": ["form submits", "validation works"],
            "severity_if_broken": "medium",
            "confidence": 0.3,
        },
    ]
