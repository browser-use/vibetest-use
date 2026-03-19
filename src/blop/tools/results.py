from __future__ import annotations

from urllib.parse import quote

from blop.reporting import results as reporting
from blop.schemas import FailureCase
from blop.storage import sqlite


async def get_test_results(run_id: str) -> dict:
    run = await sqlite.get_run(run_id)
    if not run:
        return {"error": f"Run {run_id} not found"}

    # Try run_cases table first, fall back to cases_json in runs
    cases = await sqlite.list_cases_for_run(run_id)
    if not cases and run.get("cases"):
        cases = [FailureCase(**c) for c in run["cases"]]

    report = await reporting.build_report(run, cases)
    events = await sqlite.list_run_health_events(run_id, limit=500)
    report["run_health"] = {
        "event_count": len(events),
        "latest_event_type": events[-1]["event_type"] if events else None,
    }
    app_url = run.get("app_url", "")
    encoded_app = quote(app_url, safe="") if app_url else ""
    if encoded_app:
        report["related_v2_resources"] = [
            f"blop://v2/journey/{encoded_app}/health/7d",
            f"blop://v2/incidents/{encoded_app}/open",
            f"blop://v2/correlation/{encoded_app}/7d",
            f"blop://v2/context/{encoded_app}/latest",
        ]
    else:
        report["related_v2_resources"] = []
    return report


async def list_runs(limit: int = 20, status: str | None = None) -> dict:
    runs = await sqlite.list_runs(limit=limit, status=status)
    return {
        "runs": runs,
        "total": len(runs),
        "related_v2_resources": [
            "blop://v2/contracts/tools",
        ],
    }


async def get_artifact_index_resource(run_id: str) -> dict:
    run = await sqlite.get_run(run_id)
    if not run:
        return {"error": f"Run {run_id} not found"}
    artifacts = await sqlite.list_artifacts_for_run(run_id)
    cases = await sqlite.list_cases_for_run(run_id)
    return {
        "run_id": run_id,
        "status": run.get("status", "unknown"),
        "artifacts_dir": run.get("artifacts_dir", ""),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "case_ids": [c.case_id for c in cases],
    }


async def get_flow_stability_profile_resource(flow_id: str) -> dict:
    flow = await sqlite.get_flow(flow_id)
    if not flow:
        return {"error": f"Flow {flow_id} not found"}

    cases = await sqlite.list_cases_for_flow(flow_id, limit=100)
    total = len(cases)
    if total == 0:
        return {
            "flow_id": flow_id,
            "flow_name": flow.flow_name,
            "total_runs": 0,
            "pass_rate": None,
            "failure_rate": None,
            "replay_modes": {},
            "avg_failed_step_index": None,
            "stability_score": None,
        }

    passed = sum(1 for c in cases if c.status == "pass")
    failed = sum(1 for c in cases if c.status in ("fail", "error", "blocked"))
    replay_modes: dict[str, int] = {}
    failed_step_indices: list[int] = []
    for case in cases:
        replay_modes[case.replay_mode] = replay_modes.get(case.replay_mode, 0) + 1
        if case.step_failure_index is not None:
            failed_step_indices.append(case.step_failure_index)

    pass_rate = round(passed / total, 4)
    failure_rate = round(failed / total, 4)
    avg_failed_step_index = (
        round(sum(failed_step_indices) / len(failed_step_indices), 2)
        if failed_step_indices
        else None
    )
    # Simple 0..1 proxy: high pass rate and low fallback usage means stable flow.
    fallback_ratio = replay_modes.get("goal_fallback", 0) / total
    stability_score = round(max(0.0, min(1.0, pass_rate - (fallback_ratio * 0.5))), 4)

    return {
        "flow_id": flow_id,
        "flow_name": flow.flow_name,
        "business_criticality": flow.business_criticality,
        "total_runs": total,
        "pass_rate": pass_rate,
        "failure_rate": failure_rate,
        "replay_modes": replay_modes,
        "avg_failed_step_index": avg_failed_step_index,
        "stability_score": stability_score,
    }


async def get_run_health_stream(run_id: str, limit: int = 500) -> dict:
    run = await sqlite.get_run(run_id)
    if not run:
        return {"error": f"Run {run_id} not found"}
    events = await sqlite.list_run_health_events(run_id, limit=limit)
    app_url = run.get("app_url", "")
    encoded_app = quote(app_url, safe="") if app_url else ""
    return {
        "run_id": run_id,
        "status": run.get("status", "unknown"),
        "event_count": len(events),
        "events": events,
        "related_v2_resources": [
            f"blop://v2/incidents/{encoded_app}/open",
            f"blop://v2/correlation/{encoded_app}/7d",
        ] if encoded_app else [],
    }


async def get_risk_analytics(limit_runs: int = 30) -> dict:
    runs = await sqlite.list_runs(limit=limit_runs)
    run_ids = [r["run_id"] for r in runs]

    flaky_steps: dict[str, int] = {}
    failing_transitions: dict[str, int] = {}
    business_risk: dict[str, dict[str, int]] = {
        "revenue": {"total": 0, "failed": 0},
        "activation": {"total": 0, "failed": 0},
        "retention": {"total": 0, "failed": 0},
        "support": {"total": 0, "failed": 0},
        "other": {"total": 0, "failed": 0},
    }

    for run_id in run_ids:
        cases = await sqlite.list_cases_for_run(run_id)
        for case in cases:
            bc = case.business_criticality
            if bc not in business_risk:
                bc = "other"
            business_risk[bc]["total"] += 1
            if case.status in ("fail", "error", "blocked"):
                business_risk[bc]["failed"] += 1

            if case.step_failure_index is not None and case.status in ("fail", "error", "blocked"):
                step_key = f"{case.flow_name}#step_{case.step_failure_index}"
                flaky_steps[step_key] = flaky_steps.get(step_key, 0) + 1

                # Transition proxy: flow_name + failure step index.
                transition_key = f"{case.flow_name}:transition_to_step_{case.step_failure_index}"
                failing_transitions[transition_key] = failing_transitions.get(transition_key, 0) + 1

    flaky_leaderboard = sorted(
        [{"key": k, "count": v} for k, v in flaky_steps.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:15]
    transition_leaderboard = sorted(
        [{"key": k, "count": v} for k, v in failing_transitions.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:15]

    business_risk_summary = {}
    for bc, stats in business_risk.items():
        total = stats["total"]
        failed = stats["failed"]
        failure_rate = round(failed / total, 4) if total else None
        business_risk_summary[bc] = {
            "total": total,
            "failed": failed,
            "failure_rate": failure_rate,
        }

    return {
        "analyzed_runs": len(run_ids),
        "flaky_steps_leaderboard": flaky_leaderboard,
        "failing_transitions": transition_leaderboard,
        "business_risk": business_risk_summary,
        "related_v2_resources": [
            "blop://v2/contracts/tools",
        ],
    }
