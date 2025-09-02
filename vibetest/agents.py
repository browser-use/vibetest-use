import asyncio, os, uuid, json, time
from browser_use import Agent, BrowserSession, BrowserProfile
from browser_use.llm import ChatGoogle

def get_google_api_key():
    """Get Google API key from environment, with fallback for testing."""
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        return "dummy_key_for_testing"
    return key

_test_results = {}

def get_screen_dimensions():
    """Get screen dimensions with fallback for headless environments"""
    try:
        import screeninfo
        screen = screeninfo.get_monitors()[0]
        return screen.width, screen.height
    except Exception:
        return 1920, 1080

async def run_pool(base_url: str, num_agents: int = 3, headless: bool = False) -> str:
    test_id = str(uuid.uuid4())
    start_time = time.time()
    
    qa_tasks = await scout_page(base_url)
    
    llm = ChatGoogle(
        model="gemini-2.0-flash-exp",
        temperature=0.9,
        api_key=get_google_api_key()
    )

    async def run_single_agent(i: int):
        task_description = qa_tasks[i % len(qa_tasks)]
        
        try:
            # browser configuration with optimizations for faster startup
            browser_args = [
                '--disable-gpu', 
                '--no-sandbox', 
                '--disable-dev-shm-usage',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-features=TranslateUI',
                '--disable-component-extensions-with-background-pages',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-background-networking'
            ]
            if headless:
                browser_args.append('--headless=new')
            
            window_config = {}
            
            if not headless:
                # window positioning for non-headless mode
                screen_width, screen_height = get_screen_dimensions()
                
                window_width = 300
                window_height = 400
                viewport_width = 280
                viewport_height = 350
                
                margin = 10
                spacing = 15
                
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
                    "viewport": {"width": viewport_width, "height": viewport_height}
                }
                # Note: window_position removed due to API incompatibility - expects width/height but needs x/y
            
            browser_profile = BrowserProfile(
                headless=headless,
                disable_security=True,
                user_data_dir=None,
                args=browser_args,
                ignore_default_args=['--enable-automation'],
                wait_for_network_idle_page_load_time=1.0,  # Reduced for faster execution
                maximum_wait_page_load_time=5.0,  # Reduced timeout
                wait_between_actions=0.3,  # Faster actions
                **window_config
            )
            
            browser_session = BrowserSession(browser_profile=browser_profile)
            
            try:
                agent = Agent(
                    task=task_description,
                    llm=llm,
                    browser_session=browser_session,
                    use_vision=True
                )
                
                history = await agent.run()
                
                result_text = str(history.final_result()) if hasattr(history, 'final_result') else str(history)
                
                return {
                    "agent_id": i,
                    "task": task_description,
                    "result": result_text,
                    "timestamp": time.time(),
                    "status": "success"
                }
            finally:
                # Properly close browser session to prevent lingering processes
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
                "status": "error"
            }

    # run agents in parallel
    semaphore = asyncio.Semaphore(min(num_agents, 10))
    
    async def run_agent_with_semaphore(i: int):
        async with semaphore:
            return await run_single_agent(i)
    
    results = await asyncio.gather(
        *[run_agent_with_semaphore(i) for i in range(num_agents)], 
        return_exceptions=True
    )
    
    end_time = time.time()
    
    # Allow time for browser sessions to close gracefully
    await asyncio.sleep(1)
    
    # store results
    test_data = {
        "test_id": test_id,
        "url": base_url,
        "agents": num_agents,
        "start_time": start_time,
        "end_time": end_time,
        "duration": end_time - start_time,
        "results": [r for r in results if not isinstance(r, Exception)],
        "status": "completed"
    }
    
    _test_results[test_id] = test_data
    
    return test_id


# === Standardized summarization with severity classification ===
async def summarize_bug_reports(test_id: str) -> dict:
    if test_id not in _test_results:
        return {"error": f"Test ID {test_id} not found"}

    test_data = _test_results[test_id]
    
    # separate results and prepare for analysis
    agent_results = []
    bug_reports = []
    errors = []
    
    for result in test_data["results"]:
        if result["status"] == "success":
            agent_results.append(result)
            if "result" in result and result["result"]:
                bug_reports.append({
                    "agent_id": result["agent_id"],
                    "task": result["task"],
                    "findings": result["result"],
                    "timestamp": result["timestamp"]
                })
        else:
            errors.append(result)

    bug_reports_text = "\n\n".join([
        f"Agent {report['agent_id']} Report:\nTask: {report['task']}\nFindings: {report['findings']}"
        for report in bug_reports
    ])

    summary = {
        "test_id": test_id,
        "total_agents": len(agent_results) + len(errors),
        "successful_agents": len(agent_results),
        "failed_agents": len(errors),
        "errors": errors,
        "summary_generated": time.time()
    }

    # llm analysis of findings
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

IMPORTANT: For each issue you identify, provide SPECIFIC and DETAILED descriptions including:
- The exact element that was tested (button name, link text, form field, etc.)
- The specific action taken (clicked, typed, submitted, etc.)
- The exact result or error observed (404 error, no response, broken redirect, etc.)
- Any relevant context from the agent's testing

DO NOT use vague descriptions like "broken link" or "404 error". Instead use specific descriptions like:
- "Upon clicking the 'Contact Us' button in the header navigation, the page redirected to a 404 error"
- "When submitting the newsletter signup form with a valid email, the form displayed 'Server Error 500' instead of confirmation"

Here are the test reports:
{bug_reports_text}

Format the output as JSON with the following structure:
{{
    "high_severity": [
        {{ "category": "category_name", "description": "specific detailed description with exact steps and results" }},
        ...
    ],
    "medium_severity": [
        {{ "category": "category_name", "description": "specific detailed description with exact steps and results" }},
        ...
    ],
    "low_severity": [
        {{ "category": "category_name", "description": "specific detailed description with exact steps and results" }},
        ...
    ]
}}

Only include real issues found during testing. Provide clear, concise descriptions. Deduplicate similar issues.
"""

            # Format prompt as proper message for the LLM
            from browser_use.llm.messages import UserMessage
            messages = [UserMessage(content=prompt)]
            response = await client.ainvoke(messages)
            
            # parse json response and calculate severity
            try:
                import re
                # Handle different response types - check multiple possible attributes
                if hasattr(response, 'content'):
                    response_text = str(response.content)
                elif hasattr(response, 'completion'):
                    response_text = str(response.completion)
                elif hasattr(response, 'text'):
                    response_text = str(response.text)
                else:
                    response_text = str(response)
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    severity_analysis = json.loads(json_match.group())
                else:
                    severity_analysis = {
                        "high_severity": [],
                        "medium_severity": [],
                        "low_severity": []
                    }
            except:
                severity_analysis = {
                    "high_severity": [],
                    "medium_severity": [],
                    "low_severity": []
                }
            
            total_issues = (
                len(severity_analysis.get("high_severity", [])) +
                len(severity_analysis.get("medium_severity", [])) +
                len(severity_analysis.get("low_severity", []))
            )
            
            # determine overall status
            if len(severity_analysis.get("high_severity", [])) > 0:
                overall_status = "high-severity"
                status_emoji = "🔴"
                status_description = "Critical issues found that need immediate attention"
            elif len(severity_analysis.get("medium_severity", [])) > 0:
                overall_status = "medium-severity"
                status_emoji = "🟠"
                status_description = "Moderate issues found that should be addressed"
            elif len(severity_analysis.get("low_severity", [])) > 0:
                overall_status = "low-severity"
                status_emoji = "🟡"
                status_description = "Minor issues found that could be improved"
            else:
                overall_status = "passing"
                status_emoji = "✅"
                status_description = "No technical issues detected during testing"
            
            summary.update({
                "overall_status": overall_status,
                "status_emoji": status_emoji,
                "status_description": status_description,
                "total_issues": total_issues,
                "severity_breakdown": severity_analysis,
                "llm_analysis": {
                    "raw_response": response_text,
                    "model_used": "gemini-1.5-flash"
                }
            })
            
        except Exception as e:
            # fallback analysis
            summary.update({
                "overall_status": "low-severity" if bug_reports else "passing",
                "status_emoji": "🟡" if bug_reports else "✅",
                "status_description": f"Found {len(bug_reports)} potential issues requiring manual review" if bug_reports else "No technical issues detected during testing",
                "total_issues": len(bug_reports),
                "severity_breakdown": {
                    "high_severity": [],
                    "medium_severity": [],
                    "low_severity": [{"category": "general", "description": f"Found {len(bug_reports)} potential issues requiring manual review"}] if bug_reports else []
                },
                "llm_analysis_error": str(e)
            })
    else:
        # no llm analysis available
        summary.update({
            "overall_status": "low-severity" if bug_reports else "passing",
            "status_emoji": "🟡" if bug_reports else "✅",
            "status_description": f"Found {len(bug_reports)} potential issues requiring manual review" if bug_reports else "No technical issues detected during testing",
            "total_issues": len(bug_reports),
            "severity_breakdown": {
                "high_severity": [],
                "medium_severity": [],
                "low_severity": [{"category": "general", "description": f"Found {len(bug_reports)} potential issues requiring manual review"}] if bug_reports else []
            }
        })

    return summary

async def scout_page(base_url: str) -> list:
    """Scout agent that identifies all interactive elements on the page"""
    try:
        llm = ChatGoogle(
        model="gemini-2.0-flash-exp",
        temperature=0.9,
        api_key=get_google_api_key()
    )
        
        browser_profile = BrowserProfile(
            headless=True,
            disable_security=True,
            user_data_dir=None,
            args=[
                '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage', '--headless=new',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-features=TranslateUI',
                '--disable-component-extensions-with-background-pages',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-background-networking'
            ],
            wait_for_network_idle_page_load_time=1.0,  # Reduced for faster execution
            maximum_wait_page_load_time=5.0,  # Reduced timeout
            wait_between_actions=0.3  # Faster actions
        )
        
        browser_session = BrowserSession(browser_profile=browser_profile)
        
        try:
            scout_task = f"""Navigate to {base_url} using the go_to_url action, then identify ALL interactive elements on the page. Do NOT click anything, just observe and catalog what's available. List buttons, links, forms, input fields, menus, dropdowns, and any other clickable elements you can see. Provide a comprehensive inventory."""
            
            agent = Agent(
                task=scout_task,
                llm=llm,
                browser_session=browser_session,
                use_vision=True
            )
            
            history = await agent.run()
        finally:
            # Properly close scout browser session to prevent lingering processes
            try:
                await browser_session.aclose()
            except Exception:
                pass
        
        scout_result = str(history.final_result()) if hasattr(history, 'final_result') else str(history)
        
        # partition elements with llm
        partition_prompt = f"""
Based on this scout report of interactive elements found on {base_url}:

{scout_result}

Create a list of specific testing tasks, each focusing on different elements. Each task should specify exactly which elements to test (by their text, location, or description). Aim for 6-8 distinct tasks that cover different elements without overlap.

Format as JSON array:
[
    "Navigate to {base_url} using go_to_url, then test the [specific element description] - click on [exact button/link text or location]",
    "Navigate to {base_url} using go_to_url, then test the [different specific element] - interact with [exact description]",
    ...
]

Make each task very specific about which exact elements to test. ALWAYS start each task with navigation instructions.
"""
        
        # Format prompt as proper message for the LLM  
        from browser_use.llm.messages import UserMessage
        partition_messages = [UserMessage(content=partition_prompt)]
        partition_response = await llm.ainvoke(partition_messages)
        
        # parse response
        import re
        # Handle different response types - check multiple possible attributes
        if hasattr(partition_response, 'content'):
            response_text = str(partition_response.content)
        elif hasattr(partition_response, 'completion'):
            response_text = str(partition_response.completion)
        elif hasattr(partition_response, 'text'):
            response_text = str(partition_response.text)
        else:
            response_text = str(partition_response)
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if json_match:
            element_tasks = json.loads(json_match.group())
        else:
            # fallback tasks
            element_tasks = [
                f"Test navigation elements in the header area of {base_url}",
                f"Test main content links and buttons in {base_url}",
                f"Test footer links and elements in {base_url}",
                f"Test any form elements found in {base_url}",
                f"Test sidebar or secondary navigation in {base_url}",
                f"Test any remaining interactive elements in {base_url}"
            ]
        
        return element_tasks
        
    except Exception as e:
        # fallback tasks if scouting fails
        return [
            f"Test navigation elements in the header area of {base_url}",
            f"Test main content links and buttons in {base_url}",
            f"Test footer links and elements in {base_url}",
            f"Test any form elements found in {base_url}",
            f"Test sidebar or secondary navigation in {base_url}",
            f"Test any remaining interactive elements in {base_url}"
        ]
