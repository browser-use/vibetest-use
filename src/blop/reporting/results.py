"""Aggregate run data into structured RunResult report."""
from __future__ import annotations

from blop.schemas import FailureCase


def _severity_label(case: FailureCase) -> str:
    """Return a human-readable label like 'BLOCKER in revenue flow: checkout'."""
    bc = getattr(case, "business_criticality", "other") or "other"
    sev = (case.severity or "none").upper()
    if bc != "other" and case.status != "pass":
        return f"{sev} in {bc} flow: {case.flow_name}"
    return sev


async def build_report(run: dict, cases: list[FailureCase]) -> dict:
    severity_counts: dict[str, int] = {
        "blocker": 0, "high": 0, "medium": 0, "low": 0, "none": 0, "pass": 0, "error": 0
    }
    for c in cases:
        if c.status == "pass":
            severity_counts["pass"] = severity_counts.get("pass", 0) + 1
        elif c.status in ("error", "blocked"):
            severity_counts["error"] = severity_counts.get("error", 0) + 1
        else:
            severity_counts[c.severity] = severity_counts.get(c.severity, 0) + 1

    failed = [c for c in cases if c.status in ("fail", "error", "blocked")]

    status = run.get("status", "unknown")
    extra: dict = {}
    if status == "waiting_auth":
        extra["waiting_auth_message"] = (
            "Run is waiting for auth. The auth profile could not be resolved. "
            "Check save_auth_profile and ensure your credentials env vars are set, then retry."
        )

    # Enrich case dicts with replay metadata and severity label
    cases_out = []
    for c in cases:
        d = c.model_dump()
        d["artifact_paths"] = c.screenshots
        d["severity_label"] = _severity_label(c)
        cases_out.append(d)

    failed_out = []
    for c in failed:
        d = c.model_dump()
        d["artifact_paths"] = c.screenshots
        d["severity_label"] = _severity_label(c)
        failed_out.append(d)

    return {
        "run_id": run.get("run_id", ""),
        "status": status,
        "started_at": run.get("started_at", ""),
        "completed_at": run.get("completed_at"),
        "cases": cases_out,
        "severity_counts": severity_counts,
        "failed_cases": failed_out,
        "artifacts_dir": run.get("artifacts_dir", ""),
        "run_mode": run.get("run_mode", "hybrid"),
        "next_actions": [],
        **extra,
    }
