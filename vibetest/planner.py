import json
import os
import re
from typing import Optional


async def build_plan(app_url: str, repo_path: Optional[str], focus: Optional[str]) -> list[str]:
    """Return 3-8 plain-English flow strings."""
    from .agents import generate_tests, scan_page

    if repo_path:
        flows = await generate_tests(repo_path)
    else:
        page_inventory = await scan_page(app_url)
        flows = await _flows_from_inventory(page_inventory, app_url)

    if focus:
        flows = await _apply_focus(flows, focus, app_url)

    # Clamp to 3-8 flows
    flows = flows[:8]
    if len(flows) < 3:
        flows = flows + _fallback_flows(app_url)[: 3 - len(flows)]

    return flows


async def _flows_from_inventory(inventory: dict, app_url: str) -> list[str]:
    """Ask Gemini to turn a page element inventory into test flows."""
    from browser_use.llm import ChatGoogle
    from browser_use.llm.messages import UserMessage

    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        return _fallback_flows(app_url)

    buttons = inventory.get("buttons", [])[:10]
    links = inventory.get("links", [])[:10]
    forms = inventory.get("forms", [])[:5]
    routes = inventory.get("routes", [])[:10]

    inventory_text = json.dumps(
        {"buttons": buttons, "links": links, "forms": forms, "routes": routes},
        indent=2,
    )

    llm = ChatGoogle(model="gemini-1.5-flash", api_key=google_api_key, temperature=0.7)
    prompt = f"""Given this page element inventory from {app_url}:

{inventory_text}

Generate 5-8 plain-English browser test flow strings that exercise key user workflows.
Each flow should describe a concrete user action sequence, for example:
- "Navigate to the login page, fill in email and password, click submit, verify redirect"
- "Click the main navigation link, verify the page loads correctly"

Return only a JSON array of strings, no other text:
["flow 1", "flow 2", ...]"""

    response = await llm.ainvoke([UserMessage(content=prompt)])
    response_text = str(response.content) if hasattr(response, "content") else str(response)
    json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except Exception:
            pass

    return _fallback_flows(app_url)


async def _apply_focus(flows: list[str], focus: str, app_url: str) -> list[str]:
    """Prepend a focus-specific flow and filter existing flows by relevance."""
    from browser_use.llm import ChatGoogle
    from browser_use.llm.messages import UserMessage

    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        return [f"Focus on: {focus} at {app_url}"] + flows

    llm = ChatGoogle(model="gemini-1.5-flash", api_key=google_api_key, temperature=0.5)
    prompt = f"""Given these test flows for {app_url}:
{json.dumps(flows, indent=2)}

The user wants to focus on: "{focus}"

Filter and reorder the flows to prioritize those most relevant to the focus area.
Add 1-2 new flows specifically targeting the focus if needed.
Return 3-8 flows total as a JSON array:
["flow 1", "flow 2", ...]"""

    response = await llm.ainvoke([UserMessage(content=prompt)])
    response_text = str(response.content) if hasattr(response, "content") else str(response)
    json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except Exception:
            pass

    return [f"Test {focus} functionality at {app_url}"] + flows[:5]


def _fallback_flows(app_url: str) -> list[str]:
    return [
        f"Navigate to {app_url} and verify the page loads correctly",
        f"Check all navigation links on {app_url} are functional",
        f"Test any forms or input fields on {app_url}",
    ]
