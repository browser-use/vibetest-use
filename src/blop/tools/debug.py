from __future__ import annotations

import json
import os
import re

from blop.engine import auth as auth_engine
from blop.engine import classifier, regression as regression_engine
from blop.schemas import DebugResult, FailureCase
from blop.storage import sqlite


async def debug_test_case(run_id: str, case_id: str) -> dict:
    run = await sqlite.get_run(run_id)
    if not run:
        return {"error": f"Run {run_id} not found"}

    cases = await sqlite.list_cases_for_run(run_id)
    if not cases and run.get("cases"):
        cases = [FailureCase(**c) for c in run["cases"]]

    case = next((c for c in cases if c.case_id == case_id), None)
    if not case:
        return {"error": f"Case {case_id} not found in run {run_id}"}

    flow = await sqlite.get_flow(case.flow_id)
    if not flow:
        return {"error": f"Flow {case.flow_id} not found"}

    profile_name = run.get("profile_name")
    storage_state = None
    if profile_name:
        profile = await sqlite.get_auth_profile(profile_name)
        if profile:
            storage_state = await auth_engine.resolve_storage_state(profile)

    # Re-run in headed + verbose mode, using hybrid replay
    new_case = await regression_engine.execute_flow(
        flow=flow,
        app_url=run["app_url"],
        run_id=run_id,
        case_id=case_id,
        storage_state=storage_state,
        headless=False,
        verbose=True,
        run_mode=run.get("run_mode", "hybrid"),
    )
    new_case = await classifier.classify_case(new_case, run["app_url"])

    from blop.storage.files import console_log_path
    log_path = console_log_path(run_id, case_id)
    console_log = ""
    if os.path.exists(log_path):
        with open(log_path) as f:
            console_log = f.read()

    # Generate plain-English "why this failed" explanation
    why_failed = await _explain_failure(new_case, flow, run["app_url"])

    return DebugResult(
        case_id=new_case.case_id,
        run_id=run_id,
        status=new_case.status,
        screenshots=new_case.screenshots,
        console_log=console_log or "\n".join(new_case.console_errors),
        repro_steps=new_case.repro_steps,
        step_failure_index=new_case.step_failure_index,
        replay_mode=new_case.replay_mode,
        assertion_failures=new_case.assertion_failures,
        why_failed=why_failed,
    ).model_dump()


async def _explain_failure(case: FailureCase, flow, url: str) -> str:
    """Generate a plain-English explanation of why the test failed."""
    from blop.config import check_llm_api_key
    has_key, _ = check_llm_api_key()
    if not has_key or case.status == "pass":
        return ""

    from blop.prompts import NEXT_ACTIONS_PROMPT

    step_desc = "unknown"
    step_index = case.step_failure_index or 0
    if flow and flow.steps and step_index < len(flow.steps):
        step_desc = flow.steps[step_index].description

    try:
        from blop.engine.llm_factory import make_planning_llm, make_message

        llm = make_planning_llm(temperature=0.2, max_output_tokens=600)
        prompt = NEXT_ACTIONS_PROMPT.format(
            flow_name=case.flow_name,
            goal=flow.goal if flow else case.flow_name,
            step_index=step_index,
            step_description=step_desc,
            replay_mode=case.replay_mode,
            assertion_failures=", ".join(case.assertion_failures[:3]) or "none",
            console_errors=", ".join(case.console_errors[:3]) or "none",
        )

        response = await llm.ainvoke([make_message(prompt)])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            result = json.loads(m.group())
            return result.get("why_failed", "")
    except Exception:
        pass

    return ""
