import json
import os
import re

from .models import FailureCase, RunResult


async def classify_case(case: FailureCase, url: str) -> FailureCase:
    """Use Gemini to assign severity and generate repro_steps. Returns updated FailureCase."""
    if case.status == "passed":
        case.severity = "none"
        return case

    if case.status == "error" and not case.raw_result:
        case.severity = "high"
        return case

    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        case.severity = "medium" if case.status == "failed" else "high"
        return case

    from browser_use.llm import ChatGoogle
    from browser_use.llm.messages import UserMessage

    llm = ChatGoogle(model="gemini-1.5-flash", api_key=google_api_key, temperature=0.1)

    console_text = "\n".join(case.console_errors[:10]) if case.console_errors else "none"
    network_text = "\n".join(case.network_errors[:10]) if case.network_errors else "none"

    prompt = f"""You are a QA analyst reviewing a browser test result for {url}.

Test flow: "{case.flow}"
Status: {case.status}
Result: {case.raw_result[:2000]}
Console errors: {console_text}
Network errors: {network_text}

Classify the severity and provide reproduction steps.

Severity levels:
- blocker: Complete feature failure, prevents core user workflow
- high: Major functionality broken, significant user impact
- medium: Partial functionality issue, workaround exists
- low: Minor issue, cosmetic or edge case
- none: No real issue found

Return JSON only:
{{
  "severity": "blocker|high|medium|low|none",
  "repro_steps": ["step 1", "step 2", ...],
  "summary": "one-line description"
}}"""

    try:
        response = await llm.ainvoke([UserMessage(content=prompt)])
        response_text = str(response.content) if hasattr(response, "content") else str(response)
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            case.severity = result.get("severity", "medium")
            case.repro_steps = result.get("repro_steps", [])
    except Exception:
        case.severity = "medium" if case.status == "failed" else "high"

    return case


async def classify_run(run: RunResult, url: str) -> dict:
    """Aggregate classified cases into get_results response shape."""
    google_api_key = os.getenv("GOOGLE_API_KEY")
    next_actions: list[str] = []

    failed_cases = [c for c in run.cases if c.status in ("failed", "error")]

    if failed_cases and google_api_key:
        next_actions = await _generate_next_actions(failed_cases, url)

    failed_cases_data = [
        {
            "case_id": c.case_id,
            "flow": c.flow,
            "severity": c.severity,
            "status": c.status,
            "repro_steps": c.repro_steps,
            "screenshots": c.screenshots,
            "console_errors": c.console_errors,
            "network_errors": c.network_errors,
            "trace_path": c.trace_path,
        }
        for c in failed_cases
    ]

    # Collect all artifact paths
    all_screenshots: list[str] = []
    all_traces: list[str] = []
    for c in run.cases:
        all_screenshots.extend(c.screenshots)
        if c.trace_path:
            all_traces.append(c.trace_path)

    return {
        "summary": run.summary,
        "failed_cases": failed_cases_data,
        "severity_counts": run.summary,
        "artifacts": {
            "dir": run.artifacts_dir,
            "traces": all_traces,
            "screenshots": all_screenshots,
        },
        "next_actions": next_actions,
    }


async def _generate_next_actions(failed_cases: list[FailureCase], url: str) -> list[str]:
    """Ask Gemini for 3 concrete fix actions given the failures."""
    from browser_use.llm import ChatGoogle
    from browser_use.llm.messages import UserMessage

    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        return []

    llm = ChatGoogle(model="gemini-1.5-flash", api_key=google_api_key, temperature=0.3)

    failures_text = "\n".join(
        f"- Flow: {c.flow}\n  Severity: {c.severity}\n  Result: {c.raw_result[:300]}"
        for c in failed_cases[:5]
    )

    prompt = f"""Given these test failures for {url}:

{failures_text}

List 3 concrete fix actions for the developer. Each action should be specific and actionable.

Return only a JSON array of strings:
["Fix action 1", "Fix action 2", "Fix action 3"]"""

    try:
        response = await llm.ainvoke([UserMessage(content=prompt)])
        response_text = str(response.content) if hasattr(response, "content") else str(response)
        json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass

    return [
        "Review browser console errors for JavaScript failures",
        "Check network requests for failing API calls",
        "Verify authentication and session handling",
    ]
