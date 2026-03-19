from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from blop.config import BLOP_DISCOVERY_MAX_PAGES, validate_app_url
from blop.engine import discovery


async def discover_test_flows(
    app_url: str,
    repo_path: Optional[str] = None,
    profile_name: Optional[str] = None,
    business_goal: Optional[str] = None,
    command: Optional[str] = None,
    max_depth: int = 2,
    max_pages: int = BLOP_DISCOVERY_MAX_PAGES,
    seed_urls: Optional[list[str]] = None,
    include_url_pattern: Optional[str] = None,
    exclude_url_pattern: Optional[str] = None,
    return_inventory: bool = False,
) -> dict:
    url_err = validate_app_url(app_url)
    if url_err:
        return {"error": url_err}
    # If command is provided, parse it for intent/priorities
    if command:
        from blop.engine.planner import parse_command
        intent = await parse_command(command, app_url, repo_path=repo_path, profile_name=profile_name)
        if intent.business_goal and not business_goal:
            business_goal = intent.business_goal
        if intent.max_depth != 2:
            max_depth = intent.max_depth
        if intent.max_pages != BLOP_DISCOVERY_MAX_PAGES:
            max_pages = intent.max_pages
        if intent.profile_name and not profile_name:
            profile_name = intent.profile_name

    result = await discovery.discover_flows(
        app_url=app_url,
        repo_path=repo_path,
        profile_name=profile_name,
        business_goal=business_goal,
        max_depth=max_depth,
        max_pages=max_pages,
        seed_urls=seed_urls,
        include_url_pattern=include_url_pattern,
        exclude_url_pattern=exclude_url_pattern,
        return_inventory=return_inventory,
    )
    encoded_app = quote(app_url, safe="")
    result["related_v2_resources"] = [
        f"blop://v2/context/{encoded_app}/latest",
        f"blop://v2/context/{encoded_app}/history/20",
        "blop://v2/contracts/tools",
    ]
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
    url_err = validate_app_url(app_url)
    if url_err:
        return {"error": url_err}
    result = await discovery.explore_site_inventory(
        app_url=app_url,
        profile_name=profile_name,
        max_depth=max_depth,
        max_pages=max_pages,
        seed_urls=seed_urls,
        include_url_pattern=include_url_pattern,
        exclude_url_pattern=exclude_url_pattern,
    )
    encoded_app = quote(app_url, safe="")
    result["related_v2_resources"] = [
        f"blop://v2/context/{encoded_app}/latest",
        f"blop://v2/context/{encoded_app}/history/20",
    ]
    return result


async def get_inventory_resource(app_url: str) -> dict:
    from blop.storage.sqlite import get_latest_site_inventory

    latest = await get_latest_site_inventory(app_url)
    if not latest:
        return {"error": f"No inventory found for {app_url}"}
    latest["related_v2_resources"] = [
        f"blop://v2/context/{quote(app_url, safe='')}/latest",
    ]
    return latest


async def get_context_graph_resource(app_url: str, profile_name: Optional[str] = None) -> dict:
    from blop.storage.sqlite import get_latest_context_graph

    graph = await get_latest_context_graph(app_url, profile_name=profile_name)
    if not graph:
        return {"error": f"No context graph found for {app_url}"}
    payload = graph.model_dump()
    payload["related_v2_resources"] = [
        f"blop://v2/context/{quote(app_url, safe='')}/latest",
    ]
    return payload


async def get_page_structure(
    app_url: str,
    url: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> dict:
    url_err = validate_app_url(url or app_url)
    if url_err:
        return {"error": url_err}
    return await discovery.get_page_structure(
        app_url=app_url,
        target_url=url,
        profile_name=profile_name,
    )
