from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from blop.engine import auth as auth_engine
from blop.engine import classifier, regression as regression_engine
from blop.schemas import RunStartedResult
from blop.storage import sqlite, files as file_store


async def run_regression_test(
    app_url: str,
    flow_ids: list[str],
    profile_name: Optional[str] = None,
    headless: bool = True,
    run_mode: str = "hybrid",
    command: Optional[str] = None,
) -> dict:
    from blop.engine.planner import normalize_run_mode
    from blop.config import check_llm_api_key, validate_app_url
    run_mode = normalize_run_mode(run_mode)

    url_err = validate_app_url(app_url)
    if url_err:
        return {"run_id": None, "status": "error", "message": url_err, "flow_count": 0, "artifacts_dir": ""}
    has_key, key_name = check_llm_api_key()
    if not has_key:
        return {
            "run_id": None,
            "status": "error",
            "message": (
                f"{key_name} is not set. Add it to your .env file or environment. "
                "Without it, all test results will be invalid (assertions pass vacuously)."
            ),
            "flow_count": 0,
            "artifacts_dir": "",
        }

    # If command provided, parse for additional intent
    if command:
        from blop.engine.planner import parse_command
        intent = await parse_command(command, app_url, profile_name=profile_name)
        run_mode = normalize_run_mode(intent.run_mode)
        if intent.profile_name and not profile_name:
            profile_name = intent.profile_name

    run_id = uuid.uuid4().hex

    profile = None
    if profile_name:
        profile = await sqlite.get_auth_profile(profile_name)

    storage_state: Optional[str] = None
    if profile:
        try:
            storage_state = await auth_engine.resolve_storage_state(profile)
        except Exception:
            # Auth resolution failure — transition to waiting_auth immediately
            artifacts_dir = file_store.artifacts_dir(run_id)
            await sqlite.create_run(run_id, app_url, profile_name, flow_ids, headless, artifacts_dir, run_mode)
            await sqlite.update_run_status(run_id, "waiting_auth")
            return {
                "run_id": run_id,
                "status": "waiting_auth",
                "flow_count": len(flow_ids),
                "artifacts_dir": artifacts_dir,
                "message": f"Auth profile '{profile_name}' could not be resolved. Check save_auth_profile and your credentials.",
            }

    artifacts_dir = file_store.artifacts_dir(run_id)
    await sqlite.create_run(run_id, app_url, profile_name, flow_ids, headless, artifacts_dir, run_mode)
    await sqlite.save_run_health_event(
        run_id,
        "run_queued",
        {
            "app_url": app_url,
            "flow_count": len(flow_ids),
            "run_mode": run_mode,
            "profile_name": profile_name,
        },
    )

    # Fire-and-forget; caller polls get_test_results
    task = asyncio.create_task(
        _run_and_persist(run_id, flow_ids, app_url, storage_state, headless, run_mode)
    )

    def _on_task_done(t: asyncio.Task) -> None:
        # If the task died before its own try/except could update the DB, mark it failed
        if not t.cancelled() and t.exception() is not None:
            asyncio.create_task(sqlite.update_run(run_id, "failed", [], None))

    task.add_done_callback(_on_task_done)

    return RunStartedResult(
        run_id=run_id,
        status="queued",
        flow_count=len(flow_ids),
        artifacts_dir=artifacts_dir,
    ).model_dump()


async def _run_and_persist(
    run_id: str,
    flow_ids: list[str],
    app_url: str,
    storage_state: Optional[str],
    headless: bool,
    run_mode: str = "hybrid",
) -> None:
    from datetime import datetime, timezone

    # Transition: queued → running
    await sqlite.update_run_status(run_id, "running")
    await sqlite.save_run_health_event(
        run_id,
        "run_started",
        {
            "flow_count": len(flow_ids),
            "run_mode": run_mode,
            "headless": headless,
        },
    )

    try:
        flows = []
        for fid in flow_ids:
            flow = await sqlite.get_flow(fid)
            if flow:
                flows.append(flow)

        cases = await regression_engine.run_flows(
            flows=flows,
            app_url=app_url,
            run_id=run_id,
            storage_state=storage_state,
            headless=headless,
            run_mode=run_mode,
        )

        # Attach business_criticality from source flow to each case, then classify
        classified = []
        flow_criticality = {f.flow_id: f.business_criticality for f in flows}
        for case in cases:
            case.business_criticality = flow_criticality.get(case.flow_id, "other")
            classified.append(await classifier.classify_case(case, app_url))
            await sqlite.save_case(case)
            await sqlite.save_run_health_event(
                run_id,
                "case_completed",
                {
                    "case_id": case.case_id,
                    "flow_id": case.flow_id,
                    "flow_name": case.flow_name,
                    "status": case.status,
                    "severity": case.severity,
                    "replay_mode": case.replay_mode,
                    "step_failure_index": case.step_failure_index,
                    "business_criticality": case.business_criticality,
                    "repair_confidence": case.repair_confidence,
                    "healing_decision": case.healing_decision,
                },
            )

        completed_at = datetime.now(timezone.utc).isoformat()
        await sqlite.update_run(run_id, "completed", classified, completed_at)
        await sqlite.save_run_health_event(
            run_id,
            "run_completed",
            {
                "completed_at": completed_at,
                "passed": sum(1 for c in classified if c.status == "pass"),
                "failed": sum(1 for c in classified if c.status in ("fail", "error", "blocked")),
            },
        )
    except Exception:
        await sqlite.update_run(run_id, "failed", [], None)
        await sqlite.save_run_health_event(
            run_id,
            "run_failed",
            {"reason": "unhandled_exception"},
        )
