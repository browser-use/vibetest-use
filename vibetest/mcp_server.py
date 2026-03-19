import asyncio
import logging
import os
import sys

# Completely disable ALL logging to prevent JSON-RPC interference
logging.disable(logging.CRITICAL)
os.environ['ANONYMIZED_TELEMETRY'] = 'false'
os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'CRITICAL'

from mcp.server.fastmcp import FastMCP
from .agents import (
    run_pool,
    summarize_bug_reports,
    scan_page,
    take_screenshot,
    run_single_flow,
    run_test_suite,
    run_diff_test,
    run_assert_flow,
    generate_tests,
)
from .db import get_run
from . import db, auth, planner, runner, classifier
from .models import TestPlan, AuthConfig, RunResult

mcp = FastMCP("vibetest")


@mcp.tool()
async def start(url: str, num_agents: int = 3, headless: bool = False) -> str:
    """Launch browser agents to test a website for UI bugs and issues.

    Args:
        url: The website URL to test
        num_agents: Number of QA agents to spawn (default: 3)
        headless: Whether to run browsers in headless mode (default: False)

    Returns:
        test_id: Unique identifier for this test run
    """
    try:
        test_id = await run_pool(url, num_agents, headless=headless)
        if test_id is None:
            return "Error: No test ID returned from run_pool"
        return str(test_id)
    except Exception as e:
        return f"Error starting test: {str(e)}"


@mcp.tool()
async def results(test_id: str) -> dict:
    """Get the consolidated bug report for a test run.

    Args:
        test_id: The test ID returned from start, test_flow, or test_suite

    Returns:
        dict: Complete test results with severity-classified findings
    """
    try:
        summary = await summarize_bug_reports(test_id)

        if "error" in summary:
            return summary

        test_data = await get_run(test_id) or {}
        duration_seconds = test_data.get("duration", 0)
        if duration_seconds > 0:
            summary["duration_seconds"] = duration_seconds
            if duration_seconds < 60:
                summary["duration_formatted"] = f"{duration_seconds:.0f}s"
            else:
                minutes = int(duration_seconds // 60)
                seconds = int(duration_seconds % 60)
                summary["duration_formatted"] = f"{minutes}m {seconds}s"
        else:
            summary["duration_formatted"] = "unknown"

        return summary

    except Exception as e:
        return {"error": f"Error getting results: {str(e)}"}


@mcp.tool()
async def scan(url: str) -> dict:
    """Scan a page and return a structured inventory of interactive elements.

    Navigates to the URL with Playwright and extracts buttons, links, forms,
    inputs, and internal routes — no testing, no LLM call.

    Args:
        url: The website URL to scan

    Returns:
        dict: Page inventory with keys: buttons, links, forms, standalone_inputs, routes
    """
    try:
        return await scan_page(url)
    except Exception as e:
        return {"error": f"Error scanning page: {str(e)}"}


@mcp.tool()
async def screenshot(url: str, selector: str = None) -> object:
    """Capture a screenshot of a URL and return it as an image.

    Args:
        url: The website URL to screenshot
        selector: Optional CSS selector to screenshot a specific element

    Returns:
        Image content (PNG) for visual display in Cursor/Claude Code
    """
    try:
        from mcp.server.fastmcp import Image

        img_bytes = await take_screenshot(url, selector)
        return Image(data=img_bytes, media_type="image/png")
    except Exception as e:
        return {"error": f"Error taking screenshot: {str(e)}"}


@mcp.tool()
async def test_flow(url: str, flow: str, headless: bool = True) -> str:
    """Run a single browser-use agent with an explicit flow string.

    Args:
        url: The website URL to test
        flow: Plain-English description of what to do, e.g. "click the login button"
        headless: Whether to run the browser headlessly (default: True)

    Returns:
        test_id: Use with results() to retrieve findings
    """
    try:
        test_id = await run_single_flow(url, flow, headless=headless)
        return str(test_id)
    except Exception as e:
        return f"Error running test_flow: {str(e)}"


@mcp.tool()
async def test_suite(url: str, flows: list, agents: int = 5) -> str:
    """Run multiple explicit flows as a parallel test suite.

    Args:
        url: The website URL to test
        flows: List of plain-English flow strings
        agents: Max concurrent agents (default: 5)

    Returns:
        test_id: Use with results() to retrieve findings
    """
    try:
        test_id = await run_test_suite(url, flows, num_agents=agents, headless=True)
        return str(test_id)
    except Exception as e:
        return f"Error running test_suite: {str(e)}"


@mcp.tool()
async def diff_test(url: str, base_url: str, flows: list) -> dict:
    """Run the same flows against two URLs in parallel and compare bug reports.

    Useful for regression testing: compare a staging URL against production.

    Args:
        url: The URL under test (e.g. staging)
        base_url: The baseline URL (e.g. production)
        flows: List of plain-English flow strings to run on both

    Returns:
        dict with keys: <url> summary, <base_url> summary, regressions (issues in url not in base_url)
    """
    try:
        return await run_diff_test(url, base_url, flows)
    except Exception as e:
        return {"error": f"Error running diff_test: {str(e)}"}


@mcp.tool()
async def assert_flow(url: str, flow: str, assertion: str) -> dict:
    """Run a flow then ask Gemini whether an assertion holds.

    Args:
        url: The website URL to test
        flow: Plain-English description of what to do
        assertion: A statement that should be true after the flow, e.g. "user sees a success message"

    Returns:
        dict with keys: passed (bool), reason (str), flow_result (str)
    """
    try:
        return await run_assert_flow(url, flow, assertion)
    except Exception as e:
        return {"error": f"Error running assert_flow: {str(e)}"}


@mcp.tool()
async def gen_tests(source_dir: str, framework: str = "nextjs") -> list:
    """Scan local source files and generate plain-English test flows via Gemini.

    Supports nextjs (pages/**/*.tsx, app/**/page.tsx) and express (route files).

    Args:
        source_dir: Path to the project source directory (e.g. "./src" or ".")
        framework: "nextjs" or "express" (default: "nextjs")

    Returns:
        list of plain-English flow strings ready to pass to test_suite()
    """
    try:
        return await generate_tests(source_dir, framework)
    except Exception as e:
        return [f"Error generating tests: {str(e)}"]


# === TestSprite-style stateful tools ===

@mcp.tool()
async def plan_tests(
    app_url: str,
    repo_path: str = None,
    focus: str = None,
) -> dict:
    """Generate a structured test plan for an application.

    Scans the app (or repo) and uses Gemini to produce 3-8 plain-English test flows.

    Args:
        app_url: The website URL to test
        repo_path: Optional path to local source directory for code-based flow generation
        focus: Optional focus area, e.g. "checkout flow" or "authentication"

    Returns:
        dict with plan_id, flows, app_url, flow_count
    """
    try:
        flows = await planner.build_plan(app_url, repo_path, focus)
        plan = TestPlan(app_url=app_url, repo_path=repo_path, focus=focus, flows=flows)
        await db.save_plan(plan)
        return {
            "plan_id": plan.plan_id,
            "flows": plan.flows,
            "app_url": plan.app_url,
            "flow_count": len(plan.flows),
        }
    except Exception as e:
        return {"error": f"Error creating test plan: {str(e)}"}


@mcp.tool()
async def set_auth(
    auth_type: str,
    login_url: str = None,
    username_env: str = "TEST_USERNAME",
    password_env: str = "TEST_PASSWORD",
    storage_state_path: str = None,
    cookie_json_path: str = None,
) -> dict:
    """Configure authentication for test runs.

    Args:
        auth_type: One of "env_login", "storage_state", or "cookie_json"
        login_url: Login page URL (required for env_login)
        username_env: Name of env var holding the username (default: TEST_USERNAME)
        password_env: Name of env var holding the password (default: TEST_PASSWORD)
        storage_state_path: Path to a Playwright storage_state.json file
        cookie_json_path: Path to a JSON file containing cookie objects

    Returns:
        dict with auth_config_id, auth_type, note
    """
    try:
        config = AuthConfig(
            auth_type=auth_type,
            login_url=login_url,
            username_env=username_env,
            password_env=password_env,
            storage_state_path=storage_state_path,
            cookie_json_path=cookie_json_path,
        )
        await db.save_auth_config(config)
        return {
            "auth_config_id": config.auth_config_id,
            "auth_type": config.auth_type,
            "note": "Credentials are read from environment variables at run time",
        }
    except Exception as e:
        return {"error": f"Error setting auth config: {str(e)}"}


@mcp.tool()
async def run_tests(
    test_plan_id: str,
    headless: bool = True,
    max_steps: int = 20,
) -> dict:
    """Execute all flows in a test plan and return results.

    Args:
        test_plan_id: The plan_id returned from plan_tests()
        headless: Run browsers headlessly (default: True)
        max_steps: Max agent steps per flow (default: 20)

    Returns:
        dict with run_id, status, flow_count, artifacts_dir
    """
    import os
    import time

    try:
        plan = await db.get_plan(test_plan_id)
        if not plan:
            return {"error": f"Test plan {test_plan_id} not found"}

        auth_config = await db.get_latest_auth_config()
        storage_state = None
        if auth_config:
            storage_state = await auth.resolve_storage_state(auth_config)

        artifacts_dir = os.path.abspath(
            os.path.join(".vibetest", "artifacts", plan.plan_id)
        )
        os.makedirs(artifacts_dir, exist_ok=True)

        run = RunResult(
            plan_id=plan.plan_id,
            status="running",
            headless=headless,
            artifacts_dir=artifacts_dir,
        )
        await db.save_sprite_run(run)

        run = await runner.run_plan_flows(plan, run, storage_state, headless, max_steps)

        # Classify each case
        classified_cases = []
        for case in run.cases:
            classified = await classifier.classify_case(case, plan.app_url)
            classified_cases.append(classified)
        run.cases = classified_cases

        run.status = "completed"
        run.completed_at = time.time()
        await db.save_sprite_run(run)

        return {
            "run_id": run.run_id,
            "status": run.status,
            "flow_count": len(run.cases),
            "artifacts_dir": run.artifacts_dir,
        }
    except Exception as e:
        return {"error": f"Error running tests: {str(e)}"}


@mcp.tool()
async def get_results(run_id: str) -> dict:
    """Get structured results for a completed test run.

    Args:
        run_id: The run_id returned from run_tests()

    Returns:
        dict with summary, failed_cases, severity_counts, artifacts, next_actions
    """
    try:
        run = await db.get_sprite_run(run_id)
        if not run:
            return {"error": f"Run {run_id} not found"}

        plan = await db.get_plan(run.plan_id)
        url = plan.app_url if plan else "unknown"

        return await classifier.classify_run(run, url)
    except Exception as e:
        return {"error": f"Error getting results: {str(e)}"}


@mcp.tool()
async def debug_failure(run_id: str, case_id: str) -> dict:
    """Re-run a specific failed test case in headed mode with verbose evidence capture.

    Args:
        run_id: The run_id containing the failure
        case_id: The case_id of the specific failure to debug

    Returns:
        dict with case_id, flow, severity, repro_steps, screenshots, console_errors, network_errors, trace_path
    """
    try:
        run = await db.get_sprite_run(run_id)
        if not run:
            return {"error": f"Run {run_id} not found"}

        case = next((c for c in run.cases if c.case_id == case_id), None)
        if not case:
            return {"error": f"Case {case_id} not found in run {run_id}"}

        plan = await db.get_plan(run.plan_id)
        if not plan:
            return {"error": f"Plan {run.plan_id} not found"}

        auth_config = await db.get_latest_auth_config()
        storage_state = None
        if auth_config:
            storage_state = await auth.resolve_storage_state(auth_config)

        new_case = await runner.execute_flow(
            flow=case.flow,
            app_url=plan.app_url,
            run_id=run_id,
            case_id=case_id,
            artifacts_dir=run.artifacts_dir,
            storage_state=storage_state,
            headless=False,
            verbose=True,
        )
        new_case = await classifier.classify_case(new_case, plan.app_url)

        return {
            "case_id": new_case.case_id,
            "flow": new_case.flow,
            "severity": new_case.severity,
            "repro_steps": new_case.repro_steps,
            "screenshots": new_case.screenshots,
            "console_errors": new_case.console_errors,
            "network_errors": new_case.network_errors,
            "trace_path": new_case.trace_path,
        }
    except Exception as e:
        return {"error": f"Error debugging failure: {str(e)}"}


def run():
    """Entry point for the MCP server."""
    try:
        mcp.run()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    run()
