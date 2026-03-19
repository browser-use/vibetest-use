"""Parse natural language commands into structured ExecutionIntent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from blop.config import BLOP_DISCOVERY_MAX_PAGES


@dataclass
class ExecutionIntent:
    intent: Literal["discover", "record", "regress", "debug"]
    scope: Literal["public", "authed", "both"]
    app_url: str
    repo_path: str | None = None
    profile_name: str | None = None
    business_goal: str | None = None
    priorities: list[str] = field(default_factory=list)
    max_depth: int = 2
    max_pages: int = BLOP_DISCOVERY_MAX_PAGES
    run_mode: Literal["hybrid", "strict_steps", "goal_fallback"] = "hybrid"


def normalize_run_mode(raw_mode: str | None) -> Literal["hybrid", "strict_steps", "goal_fallback"]:
    """Map legacy aliases to the canonical run mode values used by replay."""
    mode = (raw_mode or "hybrid").strip().lower()
    if mode in {"strict", "strict_steps"}:
        return "strict_steps"
    if mode in {"goal", "goal_fallback", "fallback"}:
        return "goal_fallback"
    # "explore" is currently a hybrid replay variant with repair enabled.
    return "hybrid"


async def parse_command(
    command: str,
    app_url: str,
    repo_path: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> ExecutionIntent:
    """Parse a natural language command string into a structured ExecutionIntent."""
    cmd_lower = (command or "").lower()

    # Determine intent
    if any(w in cmd_lower for w in ("discover", "find flows", "scan", "explore flows")):
        intent: Literal["discover", "record", "regress", "debug"] = "discover"
    elif any(w in cmd_lower for w in ("record", "capture", "save flow")):
        intent = "record"
    elif any(w in cmd_lower for w in ("debug", "diagnose", "investigate failure")):
        intent = "debug"
    else:
        intent = "regress"

    # Determine scope
    if any(w in cmd_lower for w in ("login", "auth", "dashboard", "account", "signed in", "logged in", "authed")):
        scope: Literal["public", "authed", "both"] = "authed"
    elif any(w in cmd_lower for w in ("public", "visitor", "anonymous", "unauthenticated")):
        scope = "public"
    else:
        scope = "both"

    # Determine run_mode
    if "strict" in cmd_lower:
        run_mode = "strict_steps"
    elif any(w in cmd_lower for w in ("goal fallback", "goal_fallback", "fallback to goal")):
        run_mode = "goal_fallback"
    else:
        run_mode = "hybrid"

    # Extract priorities
    priority_keywords = ("payment", "checkout", "signup", "registration", "onboarding", "pricing", "contact", "search")
    priorities = [kw for kw in priority_keywords if kw in cmd_lower]

    # Extract max_depth
    depth_match = re.search(r"depth[=:\s]+(\d+)", cmd_lower)
    max_depth = int(depth_match.group(1)) if depth_match else 2
    max_depth = max(1, min(max_depth, 5))

    pages_match = re.search(r"(?:max[_\s-]?pages|pages)[=:\s]+(\d+)", cmd_lower)
    max_pages = int(pages_match.group(1)) if pages_match else BLOP_DISCOVERY_MAX_PAGES
    max_pages = max(1, min(max_pages, 100))

    # Extract business goal from patterns like "goal: ..." or "focus on ..."
    business_goal: str | None = None
    for pat in (r"goal[:\s]+(.+?)(?:\.|$)", r"focus on[:\s]+(.+?)(?:\.|$)"):
        m = re.search(pat, cmd_lower)
        if m:
            business_goal = m.group(1).strip()
            break

    return ExecutionIntent(
        intent=intent,
        scope=scope,
        app_url=app_url,
        repo_path=repo_path,
        profile_name=profile_name,
        business_goal=business_goal,
        priorities=priorities,
        max_depth=max_depth,
        max_pages=max_pages,
        run_mode=normalize_run_mode(run_mode),
    )
