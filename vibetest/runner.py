import asyncio
import os
import time
from typing import Optional

from .models import FailureCase, RunResult, TestPlan


async def execute_flow(
    flow: str,
    app_url: str,
    run_id: str,
    case_id: str,
    artifacts_dir: str,
    storage_state: Optional[str],
    headless: bool = True,
    max_steps: int = 20,
    verbose: bool = False,
) -> FailureCase:
    """Execute a single flow and return a FailureCase with captured evidence."""
    from browser_use import Agent, BrowserSession, BrowserProfile
    from browser_use.llm import ChatGoogle

    case_dir = os.path.join(artifacts_dir, case_id)
    os.makedirs(case_dir, exist_ok=True)

    console_errors: list[str] = []
    network_errors: list[str] = []
    screenshots: list[str] = []
    trace_path: Optional[str] = None
    raw_result = ""

    google_api_key = os.getenv("GOOGLE_API_KEY", "dummy_key_for_testing")

    # In verbose mode, force headed
    run_headless = False if verbose else headless

    browser_args = [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-background-timer-throttling",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if run_headless:
        browser_args.append("--headless=new")

    profile_kwargs: dict = dict(
        headless=run_headless,
        disable_security=True,
        user_data_dir=None,
        args=browser_args,
        ignore_default_args=["--enable-automation"],
        wait_for_network_idle_page_load_time=1.0,
        maximum_wait_page_load_time=5.0,
        wait_between_actions=0.3,
    )
    if storage_state:
        profile_kwargs["storage_state"] = storage_state

    browser_profile = BrowserProfile(**profile_kwargs)
    browser_session = BrowserSession(browser_profile=browser_profile)

    status: str = "error"

    try:
        # Start tracing via direct Playwright context after BrowserSession initializes
        llm = ChatGoogle(
            model="gemini-2.0-flash-exp",
            temperature=0.7,
            api_key=google_api_key,
        )

        task = f"Navigate to {app_url} then: {flow}"
        agent = Agent(
            task=task,
            llm=llm,
            browser_session=browser_session,
            use_vision=True,
        )

        # Set up console and network listeners after browser context is available
        screenshot_task: Optional[asyncio.Task] = None

        async def _poll_screenshots():
            idx = 0
            while True:
                try:
                    await asyncio.sleep(2)
                    ctx = getattr(browser_session, "context", None)
                    if ctx:
                        pages = ctx.pages
                        if pages:
                            shot_path = os.path.join(case_dir, f"step_{idx:03d}.png")
                            await pages[0].screenshot(path=shot_path)
                            screenshots.append(shot_path)
                            idx += 1
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass

        if verbose:
            screenshot_task = asyncio.create_task(_poll_screenshots())

        try:
            history = await agent.run(max_steps=max_steps)
            raw_result = (
                str(history.final_result())
                if hasattr(history, "final_result")
                else str(history)
            )
            # Determine pass/fail from result text
            lower = raw_result.lower()
            if any(w in lower for w in ("error", "fail", "broken", "exception", "crash", "404", "500")):
                status = "failed"
            else:
                status = "passed"
        finally:
            if screenshot_task:
                screenshot_task.cancel()
                try:
                    await screenshot_task
                except asyncio.CancelledError:
                    pass

        # Take final screenshot
        try:
            ctx = getattr(browser_session, "context", None)
            if ctx and ctx.pages:
                final_path = os.path.join(case_dir, "final.png")
                await ctx.pages[0].screenshot(path=final_path)
                screenshots.append(final_path)
        except Exception:
            pass

        # Collect console errors from agent history if available
        try:
            if hasattr(history, "model_actions"):
                pass  # browser-use doesn't expose console directly via history
        except Exception:
            pass

    except Exception as e:
        raw_result = str(e)
        status = "error"
    finally:
        try:
            await browser_session.aclose()
        except Exception:
            pass

    return FailureCase(
        case_id=case_id,
        run_id=run_id,
        flow=flow,
        status=status,
        severity="none",
        repro_steps=[],
        console_errors=console_errors,
        network_errors=network_errors,
        screenshots=screenshots,
        trace_path=trace_path,
        raw_result=raw_result,
    )


async def run_plan_flows(
    plan: TestPlan,
    run: RunResult,
    storage_state: Optional[str],
    headless: bool,
    max_steps: int,
) -> RunResult:
    """Execute all flows in parallel (semaphore=5), return updated RunResult."""
    import uuid

    semaphore = asyncio.Semaphore(5)

    async def run_one(flow: str) -> FailureCase:
        async with semaphore:
            case_id = str(uuid.uuid4())
            return await execute_flow(
                flow=flow,
                app_url=plan.app_url,
                run_id=run.run_id,
                case_id=case_id,
                artifacts_dir=run.artifacts_dir,
                storage_state=storage_state,
                headless=headless,
                max_steps=max_steps,
                verbose=False,
            )

    cases = await asyncio.gather(*[run_one(flow) for flow in plan.flows], return_exceptions=True)

    for case in cases:
        if isinstance(case, Exception):
            import uuid as _uuid
            run.cases.append(
                FailureCase(
                    case_id=str(_uuid.uuid4()),
                    run_id=run.run_id,
                    flow="unknown",
                    status="error",
                    raw_result=str(case),
                )
            )
        else:
            run.cases.append(case)

    run.status = "completed"
    run.completed_at = time.time()
    return run
