from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from blop.config import validate_app_url
from blop.engine import auth as auth_engine
from blop.engine import recording
from blop.schemas import RecordedFlow, RecordedFlowResult
from blop.storage import sqlite, files as file_store


async def record_test_flow(
    app_url: str,
    flow_name: str,
    goal: str,
    profile_name: Optional[str] = None,
    command: Optional[str] = None,
    business_criticality: str = "other",
) -> dict:
    url_err = validate_app_url(app_url)
    if url_err:
        return {"error": url_err}
    if not flow_name or not flow_name.strip():
        return {"error": "flow_name is required"}
    if not goal or not goal.strip():
        return {"error": "goal is required"}

    # If command provided, parse for additional intent context
    if command:
        from blop.engine.planner import parse_command
        intent = await parse_command(command, app_url, profile_name=profile_name)
        if intent.profile_name and not profile_name:
            profile_name = intent.profile_name

    profile = None
    if profile_name:
        profile = await sqlite.get_auth_profile(profile_name)

    storage_state: Optional[str] = None
    if profile:
        storage_state = await auth_engine.resolve_storage_state(profile)

    import uuid
    run_id = uuid.uuid4().hex

    steps = await recording.record_flow(
        app_url=app_url,
        goal=goal,
        storage_state=storage_state,
        headless=False,
        run_id=run_id,
    )

    # Collect assertion texts from assert steps
    assertions_json = [
        s.value or s.description
        for s in steps
        if s.action == "assert" and (s.value or s.description)
    ]

    valid_criticalities = {"revenue", "activation", "retention", "support", "other"}
    bc = business_criticality if business_criticality in valid_criticalities else "other"

    # Derive spa_hints from the context graph archetype so replay inherits
    # the right wait strategy without manual tuning.
    from blop.engine.context_graph import detect_app_archetype, editor_hints_from_archetype
    from blop.schemas import SpaHints, SiteInventory

    # Build a minimal inventory from recorded steps to classify the archetype.
    nav_urls = [s.value or "" for s in steps if s.action == "navigate"]
    click_texts = [s.target_text or s.description for s in steps if s.action == "click"]
    _mini_inventory = SiteInventory(
        app_url=app_url,
        routes=list({s.url_before or "" for s in steps if s.url_before} | set(nav_urls)),
        buttons=[{"text": t} for t in click_texts if t],
        links=[],
        forms=[],
        headings=[],
        auth_signals=[],
        business_signals=[],
    )
    archetype = detect_app_archetype(_mini_inventory)
    _hint_kwargs = editor_hints_from_archetype(archetype)
    spa_hints = SpaHints(**_hint_kwargs) if _hint_kwargs else SpaHints()
    run_mode_override = "goal_fallback" if _hint_kwargs else None

    flow = RecordedFlow(
        flow_name=flow_name,
        app_url=app_url,
        goal=goal,
        steps=steps,
        created_at=datetime.now(timezone.utc).isoformat(),
        assertions_json=assertions_json,
        entry_url=app_url,
        business_criticality=bc,
        spa_hints=spa_hints,
        run_mode_override=run_mode_override,
    )
    await sqlite.save_flow(flow)

    artifacts_dir = file_store.artifacts_dir(flow.flow_id)

    return RecordedFlowResult(
        flow_id=flow.flow_id,
        flow_name=flow_name,
        step_count=len(steps),
        status="recorded",
        artifacts_dir=artifacts_dir,
    ).model_dump()
