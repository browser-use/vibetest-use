import asyncio, os, uuid, json, time, re
from browser_use import Agent, BrowserSession, BrowserProfile
from browser_use.llm import ChatGoogle

from .db import save_run, get_run


def get_google_api_key():
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        return "dummy_key_for_testing"
    return key


def get_screen_dimensions():
    try:
        import screeninfo
        screen = screeninfo.get_monitors()[0]
        return screen.width, screen.height
    except Exception:
        return 1920, 1080


# === Auth helpers ===

_auth_state_cache: dict = {"path": None, "expires": 0}
AUTH_STATE_PATH = os.path.join(".vibetest", "auth_state.json")


def _auth_configured() -> bool:
    return bool(
        os.getenv("TEST_USERNAME")
        and os.getenv("TEST_PASSWORD")
        and os.getenv("TEST_AUTH_URL")
    )


async def save_auth_state() -> str:
    """Log in headlessly via Playwright and save storage_state.json."""
    from playwright.async_api import async_playwright

    os.makedirs(".vibetest", exist_ok=True)
    auth_url = os.getenv("TEST_AUTH_URL", "")
    username = os.getenv("TEST_USERNAME", "")
    password = os.getenv("TEST_PASSWORD", "")
    username_selector = os.getenv("TEST_USERNAME_SELECTOR", "#email")
    password_selector = os.getenv("TEST_PASSWORD_SELECTOR", "#password")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(auth_url)
        await page.fill(username_selector, username)
        await page.fill(password_selector, password)
        await page.press(password_selector, "Enter")
        await page.wait_for_load_state("networkidle")
        await context.storage_state(path=AUTH_STATE_PATH)
        await browser.close()

    return AUTH_STATE_PATH


async def get_or_refresh_auth_state() -> str | None:
    """Return cached auth state path, refreshing if older than 1 hour."""
    if not _auth_configured():
        return None
    now = time.time()
    if (
        _auth_state_cache["path"]
        and now < _auth_state_cache["expires"]
        and os.path.exists(_auth_state_cache["path"])
    ):
        return _auth_state_cache["path"]
    path = await save_auth_state()
    _auth_state_cache["path"] = path
    _auth_state_cache["expires"] = now + 3600
    return path


# === Browser profile factory ===

def _make_browser_profile(headless: bool, storage_state: str | None = None) -> BrowserProfile:
    browser_args = [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=TranslateUI",
        "--disable-component-extensions-with-background-pages",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
    ]
    if headless:
        browser_args.append("--headless=new")

    kwargs: dict = dict(
        headless=headless,
        disable_security=True,
        user_data_dir=None,
        args=browser_args,
        ignore_default_args=["--enable-automation"],
        wait_for_network_idle_page_load_time=1.0,
        maximum_wait_page_load_time=5.0,
        wait_between_actions=0.3,
    )
    if storage_state:
        kwargs["storage_state"] = storage_state
    return BrowserProfile(**kwargs)


# === Core runners ===

async def run_pool(base_url: str, num_agents: int = 3, headless: bool = False) -> str:
    test_id = str(uuid.uuid4())
    start_time = time.time()

    qa_tasks = await scout_page(base_url)

    llm = ChatGoogle(
        model="gemini-2.0-flash-exp",
        temperature=0.9,
        api_key=get_google_api_key(),
    )

    storage_state = await get_or_refresh_auth_state() if _auth_configured() else None

    async def run_single_agent(i: int):
        task_description = qa_tasks[i % len(qa_tasks)]
        try:
            window_config = {}
            if not headless:
                screen_width, screen_height = get_screen_dimensions()
                window_width, window_height = 300, 400
                margin, spacing = 10, 15
                usable_width = screen_width - (2 * margin)
                windows_per_row = max(1, usable_width // (window_width + spacing))
                row = i // windows_per_row
                col = i % windows_per_row
                x_offset = margin + col * (window_width + spacing)
                y_offset = margin + row * (window_height + spacing)
                if x_offset + window_width > screen_width:
                    x_offset = screen_width - window_width - margin
                if y_offset + window_height > screen_height:
                    y_offset = screen_height - window_height - margin
                window_config = {
                    "window_size": {"width": window_width, "height": window_height},
                    "viewport": {"width": 280, "height": 350},
                }

            browser_profile = _make_browser_profile(headless, storage_state)
            browser_session = BrowserSession(browser_profile=browser_profile)

            try:
                agent = Agent(
                    task=task_description,
                    llm=llm,
                    browser_session=browser_session,
                    use_vision=True,
                )
                history = await agent.run()
                result_text = (
                    str(history.final_result())
                    if hasattr(history, "final_result")
                    else str(history)
                )
                return {
                    "agent_id": i,
                    "task": task_description,
                    "result": result_text,
                    "timestamp": time.time(),
                    "status": "success",
                }
            finally:
                try:
                    await browser_session.aclose()
                except Exception:
                    pass
        except Exception as e:
            return {
                "agent_id": i,
                "task": task_description,
                "error": str(e),
                "timestamp": time.time(),
                "status": "error",
            }

    semaphore = asyncio.Semaphore(min(num_agents, 10))

    async def run_agent_with_semaphore(i: int):
        async with semaphore:
            return await run_single_agent(i)

    results = await asyncio.gather(
        *[run_agent_with_semaphore(i) for i in range(num_agents)],
        return_exceptions=True,
    )

    end_time = time.time()
    await asyncio.sleep(1)

    test_data = {
        "test_id": test_id,
        "url": base_url,
        "agents": num_agents,
        "start_time": start_time,
        "end_time": end_time,
        "duration": end_time - start_time,
        "results": [r for r in results if not isinstance(r, Exception)],
        "status": "completed",
    }

    await save_run(test_data)
    return test_id


async def run_single_flow(url: str, flow: str, headless: bool = True) -> str:
    """Run one browser-use agent with an explicit flow string."""
    test_id = str(uuid.uuid4())
    start_time = time.time()

    storage_state = await get_or_refresh_auth_state() if _auth_configured() else None
    llm = ChatGoogle(
        model="gemini-2.0-flash-exp", temperature=0.9, api_key=get_google_api_key()
    )
    browser_profile = _make_browser_profile(headless, storage_state)
    browser_session = BrowserSession(browser_profile=browser_profile)

    try:
        task = f"Navigate to {url} then: {flow}"
        agent = Agent(task=task, llm=llm, browser_session=browser_session, use_vision=True)
        history = await agent.run()
        result_text = (
            str(history.final_result())
            if hasattr(history, "final_result")
            else str(history)
        )
        agent_result = {
            "agent_id": 0,
            "task": flow,
            "result": result_text,
            "timestamp": time.time(),
            "status": "success",
        }
    except Exception as e:
        agent_result = {
            "agent_id": 0,
            "task": flow,
            "error": str(e),
            "timestamp": time.time(),
            "status": "error",
        }
    finally:
        try:
            await browser_session.aclose()
        except Exception:
            pass

    end_time = time.time()
    test_data = {
        "test_id": test_id,
        "url": url,
        "agents": 1,
        "start_time": start_time,
        "end_time": end_time,
        "duration": end_time - start_time,
        "results": [agent_result],
        "status": "completed",
    }
    await save_run(test_data)
    return test_id


async def run_test_suite(
    url: str, flows: list, num_agents: int = 5, headless: bool = True
) -> str:
    """Run explicit flows as a parallel test suite."""
    test_id = str(uuid.uuid4())
    start_time = time.time()

    storage_state = await get_or_refresh_auth_state() if _auth_configured() else None
    llm = ChatGoogle(
        model="gemini-2.0-flash-exp", temperature=0.9, api_key=get_google_api_key()
    )

    async def run_agent(i: int, flow: str):
        browser_profile = _make_browser_profile(headless, storage_state)
        browser_session = BrowserSession(browser_profile=browser_profile)
        try:
            task = f"Navigate to {url} then: {flow}"
            agent = Agent(task=task, llm=llm, browser_session=browser_session, use_vision=True)
            history = await agent.run()
            result_text = (
                str(history.final_result())
                if hasattr(history, "final_result")
                else str(history)
            )
            return {
                "agent_id": i,
                "task": flow,
                "result": result_text,
                "timestamp": time.time(),
                "status": "success",
            }
        except Exception as e:
            return {
                "agent_id": i,
                "task": flow,
                "error": str(e),
                "timestamp": time.time(),
                "status": "error",
            }
        finally:
            try:
                await browser_session.aclose()
            except Exception:
                pass

    semaphore = asyncio.Semaphore(min(num_agents, 10))

    async def run_with_semaphore(i: int, flow: str):
        async with semaphore:
            return await run_agent(i, flow)

    results = await asyncio.gather(
        *[run_with_semaphore(i, flow) for i, flow in enumerate(flows)],
        return_exceptions=True,
    )

    end_time = time.time()
    await asyncio.sleep(1)

    test_data = {
        "test_id": test_id,
        "url": url,
        "agents": len(flows),
        "start_time": start_time,
        "end_time": end_time,
        "duration": end_time - start_time,
        "results": [r for r in results if not isinstance(r, Exception)],
        "status": "completed",
    }
    await save_run(test_data)
    return test_id


async def run_diff_test(url_a: str, url_b: str, flows: list) -> dict:
    """Run the same flows against two URLs in parallel and diff the results."""
    test_id_a, test_id_b = await asyncio.gather(
        run_test_suite(url_a, flows, headless=True),
        run_test_suite(url_b, flows, headless=True),
    )
    summary_a, summary_b = await asyncio.gather(
        summarize_bug_reports(test_id_a),
        summarize_bug_reports(test_id_b),
    )

    def _issue_set(summary: dict) -> set:
        issues = set()
        for sev in ("high_severity", "medium_severity", "low_severity"):
            for issue in summary.get("severity_breakdown", {}).get(sev, []):
                issues.add(issue.get("description", ""))
        return issues

    issues_a = _issue_set(summary_a)
    issues_b = _issue_set(summary_b)
    regressions = [i for i in issues_a if i not in issues_b]

    return {url_a: summary_a, url_b: summary_b, "regressions": regressions}


async def run_assert_flow(url: str, flow: str, assertion: str) -> dict:
    """Run a flow then use Gemini to evaluate the assertion against the result."""
    test_id = await run_single_flow(url, flow, headless=True)
    test_data = await get_run(test_id)

    if not test_data or not test_data.get("results"):
        return {"passed": False, "reason": "Flow execution failed", "flow_result": ""}

    first = test_data["results"][0]
    flow_result = first.get("result") or first.get("error", "")

    if not os.getenv("GOOGLE_API_KEY"):
        return {
            "passed": False,
            "reason": "No GOOGLE_API_KEY set — cannot evaluate assertion",
            "flow_result": flow_result,
        }

    llm = ChatGoogle(
        model="gemini-1.5-flash", api_key=get_google_api_key(), temperature=0.1
    )
    from browser_use.llm.messages import UserMessage

    prompt = f"""Given this browser test result:
{flow_result}

Is the following assertion true?
Assertion: "{assertion}"

Respond with a JSON object only:
{{"passed": true, "reason": "brief explanation"}}"""

    response = await llm.ainvoke([UserMessage(content=prompt)])
    response_text = (
        str(response.content) if hasattr(response, "content") else str(response)
    )
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            return {
                "passed": bool(result.get("passed", False)),
                "reason": result.get("reason", ""),
                "flow_result": flow_result,
            }
        except Exception:
            pass

    return {
        "passed": False,
        "reason": "Could not parse LLM response",
        "flow_result": flow_result,
    }


# === Page scanning and screenshots ===

async def scan_page(base_url: str) -> dict:
    """Return a structured inventory of interactive elements using Playwright."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="networkidle", timeout=15000)
        except Exception:
            await page.goto(base_url, timeout=15000)

        buttons = await page.evaluate(
            """() => Array.from(document.querySelectorAll('button, [role="button"]'))
                .map(el => ({ text: el.textContent.trim().slice(0,120), id: el.id, class: el.className.slice(0,80) }))
                .filter(el => el.text)"""
        )
        links = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                .map(el => ({ text: el.textContent.trim().slice(0,120), href: el.href }))
                .filter(el => el.text)
                .slice(0, 50)"""
        )
        forms = await page.evaluate(
            """() => Array.from(document.querySelectorAll('form'))
                .map(form => ({
                    action: form.action,
                    method: form.method,
                    inputs: Array.from(form.querySelectorAll('input, textarea, select'))
                        .map(el => ({ type: el.type, name: el.name, placeholder: el.placeholder, id: el.id }))
                }))"""
        )
        standalone_inputs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, select'))
                .filter(el => !el.closest('form'))
                .map(el => ({ type: el.type, name: el.name, placeholder: el.placeholder, id: el.id }))"""
        )
        routes = await page.evaluate(
            """() => [...new Set(Array.from(document.querySelectorAll('a[href]'))
                .map(el => { try { return new URL(el.href).pathname; } catch(e) { return null; } })
                .filter(p => p && p !== '/'))].slice(0, 30)"""
        )

        await browser.close()

    return {
        "url": base_url,
        "buttons": buttons,
        "links": links,
        "forms": forms,
        "standalone_inputs": standalone_inputs,
        "routes": routes,
    }


async def take_screenshot(url: str, selector: str | None = None) -> bytes:
    """Capture a screenshot of a URL (or a specific element) using Playwright."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception:
            await page.goto(url, timeout=15000)

        if selector:
            element = await page.query_selector(selector)
            img_bytes = (
                await element.screenshot() if element else await page.screenshot()
            )
        else:
            img_bytes = await page.screenshot(full_page=True)

        await browser.close()

    return img_bytes


# === Test generation ===

async def generate_tests(source_dir: str, framework: str = "nextjs") -> list:
    """Scan local source files and use Gemini to produce plain-English test flows."""
    import glob as glob_module

    files: list[str] = []

    if framework == "nextjs":
        patterns = [
            os.path.join(source_dir, "pages/**/*.tsx"),
            os.path.join(source_dir, "pages/**/*.ts"),
            os.path.join(source_dir, "pages/**/*.jsx"),
            os.path.join(source_dir, "app/**/page.tsx"),
            os.path.join(source_dir, "app/**/page.ts"),
            os.path.join(source_dir, "app/**/page.jsx"),
        ]
        for pattern in patterns:
            files.extend(glob_module.glob(pattern, recursive=True))
    elif framework == "express":
        import subprocess

        result = subprocess.run(
            [
                "grep",
                "-rl",
                r"app\.\(get\|post\|put\|delete\)\|router\.\(get\|post\|put\|delete\)",
                source_dir,
                "--include=*.js",
                "--include=*.ts",
            ],
            capture_output=True,
            text=True,
        )
        if result.stdout:
            files = [f for f in result.stdout.strip().split("\n") if f]
    else:
        files = glob_module.glob(
            os.path.join(source_dir, "**/*.{ts,tsx,js,jsx,py}"), recursive=True
        )[:30]

    if not files:
        return [f"No {framework} route files found in {source_dir}"]

    if not os.getenv("GOOGLE_API_KEY"):
        return [f"No GOOGLE_API_KEY set — cannot generate tests from {len(files)} files"]

    file_list = "\n".join(files[:50])
    llm = ChatGoogle(
        model="gemini-1.5-flash", api_key=get_google_api_key(), temperature=0.7
    )
    from browser_use.llm.messages import UserMessage

    prompt = f"""Based on these {framework} route/page files:
{file_list}

Generate 6-10 plain-English browser test flow strings that exercise the key functionality of this application. Each flow should describe a concrete user action, for example:
- "Navigate to the login page, fill in credentials, and submit"
- "Click the dashboard link in the nav and verify the main table loads"

Return only a JSON array of strings, no other text:
["flow 1", "flow 2", ...]"""

    response = await llm.ainvoke([UserMessage(content=prompt)])
    response_text = (
        str(response.content) if hasattr(response, "content") else str(response)
    )
    json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except Exception:
            pass

    return [f"Test {framework} application — {len(files)} route files found"]


# === Summarization ===

async def summarize_bug_reports(test_id: str) -> dict:
    test_data = await get_run(test_id)
    if not test_data:
        return {"error": f"Test ID {test_id} not found"}

    agent_results = []
    bug_reports = []
    errors = []

    for result in test_data["results"]:
        if result["status"] == "success":
            agent_results.append(result)
            if result.get("result"):
                bug_reports.append(
                    {
                        "agent_id": result["agent_id"],
                        "task": result["task"],
                        "findings": result["result"],
                        "timestamp": result["timestamp"],
                    }
                )
        else:
            errors.append(result)

    bug_reports_text = "\n\n".join(
        [
            f"Agent {r['agent_id']} Report:\nTask: {r['task']}\nFindings: {r['findings']}"
            for r in bug_reports
        ]
    )

    summary = {
        "test_id": test_id,
        "total_agents": len(agent_results) + len(errors),
        "successful_agents": len(agent_results),
        "failed_agents": len(errors),
        "errors": errors,
        "summary_generated": time.time(),
    }

    if bug_reports and os.getenv("GOOGLE_API_KEY"):
        try:
            client = ChatGoogle(
                model="gemini-1.5-flash",
                api_key=get_google_api_key(),
                temperature=0.1,
            )

            prompt = f"""
You are an objective QA analyst. Review the following test reports from agents that explored the website {test_data['url']}.

Identify only actual functional issues, broken features, or technical problems. Do NOT classify subjective opinions, missing features that may be intentional, or design preferences as issues.

Only report issues if they represent:
- Broken functionality (buttons that don't work, forms that fail)
- Technical errors (404s, JavaScript errors, broken links)
- Accessibility violations (missing alt text, poor contrast)
- Performance problems (very slow loading, timeouts)

IMPORTANT: For each issue, provide SPECIFIC and DETAILED descriptions including:
- The exact element tested (button name, link text, form field, etc.)
- The specific action taken (clicked, typed, submitted, etc.)
- The exact result or error observed

Here are the test reports:
{bug_reports_text}

Format the output as JSON:
{{
    "high_severity": [{{ "category": "...", "description": "..." }}, ...],
    "medium_severity": [{{ "category": "...", "description": "..." }}, ...],
    "low_severity": [{{ "category": "...", "description": "..." }}, ...]
}}

Only include real issues. Deduplicate similar issues.
"""
            from browser_use.llm.messages import UserMessage

            response = await client.ainvoke([UserMessage(content=prompt)])
            response_text = (
                str(response.content) if hasattr(response, "content") else str(response)
            )

            try:
                json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                severity_analysis = (
                    json.loads(json_match.group())
                    if json_match
                    else {"high_severity": [], "medium_severity": [], "low_severity": []}
                )
            except Exception:
                severity_analysis = {
                    "high_severity": [],
                    "medium_severity": [],
                    "low_severity": [],
                }

            total_issues = sum(
                len(severity_analysis.get(k, []))
                for k in ("high_severity", "medium_severity", "low_severity")
            )

            if severity_analysis.get("high_severity"):
                overall_status, status_emoji = "high-severity", "🔴"
                status_description = "Critical issues found that need immediate attention"
            elif severity_analysis.get("medium_severity"):
                overall_status, status_emoji = "medium-severity", "🟠"
                status_description = "Moderate issues found that should be addressed"
            elif severity_analysis.get("low_severity"):
                overall_status, status_emoji = "low-severity", "🟡"
                status_description = "Minor issues found that could be improved"
            else:
                overall_status, status_emoji = "passing", "✅"
                status_description = "No technical issues detected during testing"

            summary.update(
                {
                    "overall_status": overall_status,
                    "status_emoji": status_emoji,
                    "status_description": status_description,
                    "total_issues": total_issues,
                    "severity_breakdown": severity_analysis,
                    "llm_analysis": {
                        "raw_response": response_text,
                        "model_used": "gemini-1.5-flash",
                    },
                }
            )

        except Exception as e:
            summary.update(
                {
                    "overall_status": "low-severity" if bug_reports else "passing",
                    "status_emoji": "🟡" if bug_reports else "✅",
                    "status_description": (
                        f"Found {len(bug_reports)} potential issues requiring manual review"
                        if bug_reports
                        else "No technical issues detected during testing"
                    ),
                    "total_issues": len(bug_reports),
                    "severity_breakdown": {
                        "high_severity": [],
                        "medium_severity": [],
                        "low_severity": (
                            [
                                {
                                    "category": "general",
                                    "description": f"Found {len(bug_reports)} potential issues requiring manual review",
                                }
                            ]
                            if bug_reports
                            else []
                        ),
                    },
                    "llm_analysis_error": str(e),
                }
            )
    else:
        summary.update(
            {
                "overall_status": "low-severity" if bug_reports else "passing",
                "status_emoji": "🟡" if bug_reports else "✅",
                "status_description": (
                    f"Found {len(bug_reports)} potential issues requiring manual review"
                    if bug_reports
                    else "No technical issues detected during testing"
                ),
                "total_issues": len(bug_reports),
                "severity_breakdown": {
                    "high_severity": [],
                    "medium_severity": [],
                    "low_severity": (
                        [
                            {
                                "category": "general",
                                "description": f"Found {len(bug_reports)} potential issues requiring manual review",
                            }
                        ]
                        if bug_reports
                        else []
                    ),
                },
            }
        )

    return summary


async def scout_page(base_url: str) -> list:
    """Scout agent that identifies interactive elements and returns testing tasks."""
    try:
        llm = ChatGoogle(
            model="gemini-2.0-flash-exp",
            temperature=0.9,
            api_key=get_google_api_key(),
        )

        browser_profile = _make_browser_profile(headless=True)
        browser_session = BrowserSession(browser_profile=browser_profile)

        try:
            scout_task = (
                f"Navigate to {base_url} using the go_to_url action, then identify ALL "
                "interactive elements on the page. Do NOT click anything, just observe and "
                "catalog what's available. List buttons, links, forms, input fields, menus, "
                "dropdowns, and any other clickable elements you can see. Provide a comprehensive inventory."
            )
            agent = Agent(
                task=scout_task,
                llm=llm,
                browser_session=browser_session,
                use_vision=True,
            )
            history = await agent.run()
        finally:
            try:
                await browser_session.aclose()
            except Exception:
                pass

        scout_result = (
            str(history.final_result())
            if hasattr(history, "final_result")
            else str(history)
        )

        partition_prompt = f"""
Based on this scout report of interactive elements found on {base_url}:

{scout_result}

Create a list of specific testing tasks, each focusing on different elements. Each task should specify exactly which elements to test. Aim for 6-8 distinct tasks that cover different elements without overlap.

Format as JSON array:
[
    "Navigate to {base_url} using go_to_url, then test the [specific element description] - click on [exact button/link text or location]",
    ...
]

Make each task very specific about which exact elements to test. ALWAYS start each task with navigation instructions.
"""
        from browser_use.llm.messages import UserMessage

        partition_response = await llm.ainvoke([UserMessage(content=partition_prompt)])
        response_text = (
            str(partition_response.content)
            if hasattr(partition_response, "content")
            else str(partition_response)
        )
        json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())

    except Exception:
        pass

    return [
        f"Test navigation elements in the header area of {base_url}",
        f"Test main content links and buttons in {base_url}",
        f"Test footer links and elements in {base_url}",
        f"Test any form elements found in {base_url}",
        f"Test sidebar or secondary navigation in {base_url}",
        f"Test any remaining interactive elements in {base_url}",
    ]
