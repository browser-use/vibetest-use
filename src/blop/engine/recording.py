"""Guided Browser-Use run that captures steps with selectors, screenshots, and assertions."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from blop.config import BLOP_AGENT_MAX_ACTIONS_PER_STEP, BLOP_AGENT_MAX_FAILURES
from blop.schemas import FlowStep, StructuredAssertion

# Shared agent instructions used by both recording and goal-fallback regression agents.
# Kept here so both contexts stay in sync without duplication.
class BlopActions:
    """Custom browser actions for common QA patterns (toast assertions, modal dismissal)."""

    @staticmethod
    async def assert_toast_visible(page, message_substring: str = "") -> str:
        """Wait up to 5s for a toast/alert/snackbar to appear. Returns 'visible' or 'not_found'."""
        selectors = [
            "[role='alert']",
            "[class*='toast']",
            "[class*='snackbar']",
            "[class*='notification']",
        ]
        import asyncio as _asyncio
        for _ in range(10):
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        text = (await el.inner_text()) or ""
                        if not message_substring or message_substring.lower() in text.lower():
                            return "visible"
                except Exception:
                    pass
            await _asyncio.sleep(0.5)
        return "not_found"

    @staticmethod
    async def dismiss_modal(page) -> str:
        """Click the first visible close/dismiss button inside a modal. Returns 'dismissed' or 'no_modal'."""
        close_selectors = [
            "[role='dialog'] button[aria-label*='close' i]",
            "[role='dialog'] button[aria-label*='dismiss' i]",
            "[role='dialog'] button[aria-label*='cancel' i]",
            "[class*='modal'] button[aria-label*='close' i]",
            "[class*='modal'] .close",
        ]
        for sel in close_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    return "dismissed"
            except Exception:
                pass
        return "no_modal"


SPA_AGENT_RULES = (
    "IMPORTANT — SPA & web-component rules: "
    "(1) After clicking a project card or nav link, wait 3–5 seconds before asserting that content is visible — views load asynchronously in SPAs. "
    "(2) If the page shows a loading spinner or skeleton, wait for it to disappear before proceeding. "
    "(3) Do NOT retry the same click if the URL has already changed — navigation may have succeeded even if content is still loading. "
    "(4) If a standard selector fails, try scrolling the element into view, then retry once. "
    "(5) Some UI elements live inside shadow DOM / web components — if they are not found by normal means, describe them for vision-based interaction. "
    "(6) If an element is not found, check if it is inside a shadow DOM or web component. "
    "(7) CANVAS / WEBGL APPS: If the primary UI renders into a <canvas> element (design tools, diagram builders, creative editors, game engines, etc.), "
    "wait up to 30 seconds for the application toolbar to appear — WebGL and WASM initialisation takes 15–30 seconds. "
    "A dark or blank canvas is NOT a failure; it means the application is still initialising. "
    "(8) In canvas-based applications, only assert DOM chrome elements: toolbar buttons, menu bar items, top-level controls, or the canvas element itself. "
    "Do not attempt to interact with content rendered inside the canvas — it is not in the accessibility tree. "
    "(9) The canvas application has loaded successfully when at least one actionable toolbar element (e.g. Export, Publish, File menu) is visible in the DOM."
)


async def record_flow(
    app_url: str,
    goal: str,
    storage_state: Optional[str],
    headless: bool = False,
    run_id: Optional[str] = None,
) -> list[FlowStep]:
    """Run a Browser-Use agent for `goal`; capture each action with selector, target_text,
    dom_fingerprint, per-step screenshot, and final assertion steps."""
    from browser_use import Agent, BrowserSession
    from blop.engine.browser import make_browser_profile
    from blop.engine.llm_factory import make_agent_llm
    from blop.storage import files as file_store

    llm = make_agent_llm()
    browser_profile = make_browser_profile(headless=headless, storage_state=storage_state)
    browser_session = BrowserSession(browser_profile=browser_profile)

    recording_id = run_id or uuid.uuid4().hex
    steps: list[FlowStep] = []
    step_counter = 0

    # Initial navigation step
    steps.append(FlowStep(
        step_id=step_counter,
        action="navigate",
        value=app_url,
        description=f"Navigate to {app_url}",
        url_after=app_url,
    ))
    step_counter += 1

    # Pre-agent discovery: use raw Playwright to extract SPA-internal links
    # that a browser-use click can't reliably trigger (e.g. React Router cards).
    # Only run when the goal implies navigating to a sub-page (not a dashboard/list page).
    # Skip for "create new" flows — those must start from the dashboard, not a pre-resolved URL.
    _deep_keywords = {"editor", "project", "workspace", "open", "enter", "launch", "canvas"}
    _create_keywords = {"create", "new project", "new file", "start fresh", "make new"}
    _is_create_flow = any(kw in goal.lower() for kw in _create_keywords)
    _needs_deep = not _is_create_flow and any(kw in goal.lower() for kw in _deep_keywords)
    entry_url_hint: Optional[str] = None
    if _needs_deep:
        try:
            entry_url_hint = await _resolve_spa_entry_url(
                app_url=app_url,
                storage_state=storage_state,
                headless=headless,
                goal=goal,
            )
        except Exception:
            pass
    if entry_url_hint and entry_url_hint != app_url:
        steps.append(FlowStep(
            step_id=step_counter,
            action="navigate",
            value=entry_url_hint,
            description=f"Navigate to discovered entry URL: {entry_url_hint}",
            url_after=entry_url_hint,
        ))
        step_counter += 1
        # The browser was already navigated to entry_url_hint by _discover_entry_url.
        # Tell the agent to start from there — no need to navigate again.
        task = f"Navigate to {entry_url_hint} then: {goal}"
    else:
        task = f"Navigate to {app_url} then: {goal}"
    step_screenshots: list[str] = []
    screenshot_task: Optional[asyncio.Task] = None
    step_idx_counter = [0]

    async def _poll_screenshots():
        while True:
            try:
                await asyncio.sleep(3)
                ctx = getattr(browser_session, "context", None)
                if ctx and ctx.pages:
                    shot_path = file_store.screenshot_path(recording_id, "record", step_idx_counter[0])
                    await ctx.pages[0].screenshot(path=shot_path)
                    step_screenshots.append(shot_path)
                    step_idx_counter[0] += 1
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    try:
        agent_kwargs: dict = dict(
            task=task,
            llm=llm,
            browser_session=browser_session,
            use_vision=True,
            use_judge=False,
            max_failures=BLOP_AGENT_MAX_FAILURES,
            max_actions_per_step=BLOP_AGENT_MAX_ACTIONS_PER_STEP,
            extend_system_message=(
                "You are a QA recorder. You MUST take explicit browser actions (click, fill, navigate) "
                "to complete every step described in the task. "
                "Do NOT call 'done' until you have visually confirmed that each requested action has been performed. "
                "After navigating to the URL, always look for and interact with the UI elements described. "
                + SPA_AGENT_RULES
            ),
        )
        # Attach BlopActions helpers if browser-use Agent supports additional_tools
        try:
            agent_kwargs["additional_tools"] = [
                BlopActions.assert_toast_visible,
                BlopActions.dismiss_modal,
            ]
        except Exception:
            pass
        agent = Agent(**agent_kwargs)
        screenshot_task = asyncio.create_task(_poll_screenshots())

        try:
            history = await agent.run(max_steps=50)
        finally:
            screenshot_task.cancel()
            try:
                await screenshot_task
            except asyncio.CancelledError:
                pass

        # Get the active page reference for ARIA/testid extraction
        page_ref = None
        try:
            ctx = getattr(browser_session, "context", None)
            if ctx and ctx.pages:
                page_ref = ctx.pages[0]
        except Exception:
            pass

        all_actions = history.model_actions() if hasattr(history, "model_actions") else []
        if os.getenv("BLOP_DEBUG"):
            try:
                with open("/tmp/blop_debug.log", "a") as _dbg:
                    _dbg.write(f"[blop-debug] agent history: {len(all_actions)} actions, is_done={getattr(history, 'is_done', lambda: '?')()}\n")
                    for _i, _a in enumerate(all_actions[:5]):
                        _dbg.write(f"  action[{_i}]: {str(_a)[:120]}\n")
                    try:
                        errs = history.errors()
                        _dbg.write(f"  errors(): {str(errs)[:500]}\n")
                    except Exception as _ee:
                        _dbg.write(f"  errors() failed: {_ee}\n")
                    _dbg.write(f"  history_len: {len(getattr(history, 'history', []))}\n")
            except Exception:
                pass
        if hasattr(history, "model_actions"):
            for i, action in enumerate(history.model_actions()):
                selector: Optional[str] = None
                value: Optional[str] = None
                target_text: Optional[str] = None
                url_before: Optional[str] = None
                url_after: Optional[str] = None
                interacted_xpath: Optional[str] = None

                # model_actions() returns list[dict] with ALL action keys (most None).
                # Find the non-None key to get the actual action type.
                if isinstance(action, dict):
                    action_name = next(
                        (k for k, v in action.items() if k != "interacted_element" and v is not None),
                        "click"
                    )
                    params = action.get(action_name) or {}
                    interacted = action.get("interacted_element")

                    idx = params.get("index") if isinstance(params, dict) else None
                    if idx is not None:
                        selector = f"[data-browser-use-index='{idx}']"
                    if isinstance(params, dict) and "text" in params:
                        value = str(params["text"])
                    if isinstance(params, dict) and "url" in params:
                        value = str(params["url"])
                        url_after = value

                    # Keep interacted xpath as a fallback locator only; we prefer semantic selectors.
                    if interacted is not None:
                        try:
                            interacted_xpath = interacted.xpath if hasattr(interacted, "xpath") else None
                            elem_text = (
                                interacted.get_meaningful_text_for_llm()
                                if hasattr(interacted, "get_meaningful_text_for_llm")
                                else None
                            )
                            if elem_text:
                                target_text = elem_text[:100]
                        except Exception:
                            pass

                    desc = str(action)[:200]
                    if not target_text:
                        target_text = _extract_target_text(desc)
                else:
                    # Fallback for typed action objects (older browser-use versions)
                    action_name = type(action).__name__.lower() if action else "click"
                    interacted_xpath = None
                    if hasattr(action, "index") and action.index is not None:
                        selector = f"[data-browser-use-index='{action.index}']"
                    if hasattr(action, "text") and action.text:
                        value = str(action.text)
                    if hasattr(action, "url") and action.url:
                        value = str(action.url)
                        url_after = value
                    desc = str(action)[:200] if action else ""
                    target_text = _extract_target_text(desc)

                mapped = _map_action(action_name)
                if not mapped:
                    continue
                screenshot_path = step_screenshots[i] if i < len(step_screenshots) else None

                # Capture semantic locators (ARIA role/name, testid, label)
                aria_role: Optional[str] = None
                aria_name: Optional[str] = None
                aria_snapshot: Optional[str] = None
                testid_selector: Optional[str] = None
                label_text: Optional[str] = None

                if page_ref is not None and mapped != "navigate":
                    aria_role, aria_name, aria_snapshot = await _capture_aria_for_element(
                        page_ref, target_text
                    )
                    if interacted_xpath:
                        testid_selector, label_text = await _capture_locator_attrs(
                            page_ref, interacted_xpath, mapped
                        )
                        if testid_selector:
                            selector = testid_selector
                        elif not selector:
                            selector = interacted_xpath

                if _is_brittle_selector(selector):
                    selector = None

                steps.append(FlowStep(
                    step_id=step_counter,
                    action=mapped,
                    selector=selector,
                    value=value,
                    description=desc,
                    target_text=target_text,
                    dom_fingerprint=_compute_fingerprint(mapped, selector, target_text, i),
                    url_before=url_before,
                    url_after=url_after,
                    screenshot_path=screenshot_path,
                    aria_role=aria_role,
                    aria_name=aria_name,
                    aria_snapshot=aria_snapshot,
                    testid_selector=testid_selector,
                    label_text=label_text,
                ))
                step_counter += 1

        # Take final screenshot and generate assertion steps
        try:
            ctx = getattr(browser_session, "context", None)
            if ctx and ctx.pages:
                final_page = ctx.pages[0]
                final_path = file_store.screenshot_path(recording_id, "record", 999)
                await final_page.screenshot(path=final_path)

                # Capture ARIA context for richer assertion generation
                aria_context = await _get_page_aria_context(final_page)

                assertion_steps = await _generate_assertions_from_screenshot(
                    final_page, goal, aria_context=aria_context
                )
                for assertion_text, structured in assertion_steps:
                    steps.append(FlowStep(
                        step_id=step_counter,
                        action="assert",
                        description=assertion_text,
                        value=assertion_text,
                        screenshot_path=final_path,
                        structured_assertion=structured,
                    ))
                    step_counter += 1
        except Exception:
            pass

    except Exception:
        pass
    finally:
        try:
            await browser_session.aclose()
        except Exception:
            pass

    # Guarantee at least a navigation + assertion
    if len(steps) <= 1:
        steps.append(FlowStep(
            step_id=step_counter,
            action="assert",
            description=goal,
            value=goal,
        ))

    return steps


async def _generate_assertions_from_screenshot(
    page,
    goal: str,
    aria_context: str = "",
) -> list[tuple[str, Optional[StructuredAssertion]]]:
    """Ask the configured LLM to generate 1-3 structured assertions based on the final page screenshot.

    Returns a list of (assertion_text, StructuredAssertion | None) tuples.
    Falls back to plain-string assertions if structured parsing fails.
    """
    from blop.config import check_llm_api_key
    has_key, _ = check_llm_api_key()
    if not has_key:
        return [(f"Page shows expected content for: {goal}", None)]

    try:
        from blop.engine.llm_factory import make_planning_llm

        img_bytes = await page.screenshot(type="jpeg", quality=85)
        b64 = base64.b64encode(img_bytes).decode()

        aria_section = f"\nARIA tree of final page state:\n{aria_context}\n" if aria_context else ""

        llm = make_planning_llm(temperature=0.1, max_output_tokens=600)
        prompt = f"""Look at this screenshot of a web page after completing: "{goal}"
{aria_section}
Generate 1-3 specific, verifiable assertions about what should be visible.

Return ONLY a JSON array where each item has these fields:
- type: one of "text_present" | "element_visible" | "url_contains" | "page_title" | "semantic"
- target: CSS selector, ARIA label, or URL substring (null for semantic)
- expected: expected text/value (null if not applicable)
- description: plain English assertion string

Prefer deterministic types (text_present, element_visible, url_contains) over "semantic" when possible.
Use "semantic" only for assertions requiring visual judgment.

Example:
[
  {{"type": "text_present", "target": "h1", "expected": "Dashboard", "description": "Dashboard heading is visible"}},
  {{"type": "url_contains", "target": null, "expected": "/dashboard", "description": "URL contains /dashboard"}}
]
"""
        from langchain_core.messages import HumanMessage
        response = await llm.ainvoke([HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ])])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if m:
            raw_list = json.loads(m.group())
            if isinstance(raw_list, list) and raw_list:
                results = []
                for item in raw_list[:3]:
                    if isinstance(item, str):
                        results.append((item, None))
                    elif isinstance(item, dict):
                        desc = item.get("description") or item.get("expected") or str(item)
                        try:
                            sa = StructuredAssertion(
                                assertion_type=item.get("type", "semantic"),
                                target=item.get("target"),
                                expected=item.get("expected"),
                                description=desc,
                            )
                        except Exception:
                            sa = None
                        results.append((desc, sa))
                return results
    except Exception:
        pass

    return [(f"Page shows expected content for: {goal}", None)]


async def _get_page_aria_context(page) -> str:
    """Return a compact ARIA tree string of the page's interactive elements (max 40 nodes)."""
    try:
        snapshot = await page.accessibility.snapshot(interesting_only=True)
        if not snapshot:
            return ""
        nodes = _extract_interactive_nodes(snapshot, max_nodes=40)
        return json.dumps(nodes, separators=(",", ":"))
    except Exception:
        return ""


async def _capture_aria_for_element(
    page, target_text: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (aria_role, aria_name, aria_snapshot_json) for the element matching target_text."""
    if not target_text:
        return None, None, None
    try:
        snapshot = await page.accessibility.snapshot(interesting_only=True)
        if not snapshot:
            return None, None, None
        node = _find_aria_node(snapshot, target_text)
        if node:
            role = node.get("role")
            name = node.get("name")
            # Compact subtree at depth 2
            sub = _serialize_aria_node(node, depth=0, max_depth=2)
            return role, name, json.dumps(sub, separators=(",", ":"))
    except Exception:
        pass
    return None, None, None


async def _capture_locator_attrs(
    page, xpath: str, action: str
) -> tuple[Optional[str], Optional[str]]:
    """Extract data-testid and label text via JavaScript for stable locators."""
    testid_selector: Optional[str] = None
    label_text: Optional[str] = None
    try:
        result = await page.evaluate(
            """(xpath) => {
                try {
                    const el = document.evaluate(
                        xpath, document, null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE, null
                    ).singleNodeValue;
                    if (!el) return null;
                    const testid = el.getAttribute('data-testid') ||
                                   el.getAttribute('data-cy') ||
                                   el.getAttribute('data-test');
                    let label = null;
                    if (el.getAttribute('aria-label')) {
                        label = el.getAttribute('aria-label');
                    } else if (el.getAttribute('placeholder')) {
                        label = el.getAttribute('placeholder');
                    } else if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) label = lbl.textContent.trim();
                    }
                    return {testid: testid, label: label};
                } catch(e) { return null; }
            }""",
            xpath,
        )
        if result:
            if result.get("testid"):
                testid_val = str(result["testid"]).replace("'", "\\'")
                testid_selector = f"[data-testid='{testid_val}']"
            if action == "fill" and result.get("label"):
                label_text = str(result["label"])[:100]
    except Exception:
        pass
    return testid_selector, label_text


def _find_aria_node(node: dict, target_text: str) -> Optional[dict]:
    """DFS search for an ARIA node whose name contains target_text (case-insensitive)."""
    name = (node.get("name") or "").lower()
    if target_text.lower() in name:
        return node
    for child in node.get("children", []):
        found = _find_aria_node(child, target_text)
        if found:
            return found
    return None


def _serialize_aria_node(node: dict, depth: int, max_depth: int) -> dict:
    """Serialize an ARIA node tree to a compact dict (bounded depth)."""
    out: dict = {}
    for key in ("role", "name", "value", "checked", "level", "disabled"):
        if node.get(key) is not None:
            out[key] = node[key]
    if depth < max_depth:
        children = [
            _serialize_aria_node(c, depth + 1, max_depth)
            for c in node.get("children", [])
            if c.get("role") not in ("generic", "none", "presentation")
        ]
        if children:
            out["children"] = children
    return out


def _extract_interactive_nodes(node: dict, max_nodes: int = 40, _count: Optional[list] = None) -> list[dict]:
    """Flatten the ARIA tree into a list of interactive leaf nodes."""
    if _count is None:
        _count = [0]
    interactive_roles = {
        "button", "link", "textbox", "checkbox", "radio", "combobox",
        "listbox", "menuitem", "tab", "switch", "searchbox", "spinbutton",
    }
    results = []
    role = node.get("role", "")
    if role in interactive_roles and node.get("name"):
        if _count[0] < max_nodes:
            results.append({"role": role, "name": node["name"]})
            _count[0] += 1
    for child in node.get("children", []):
        if _count[0] >= max_nodes:
            break
        results.extend(_extract_interactive_nodes(child, max_nodes, _count))
    return results


def _extract_target_text(description: str) -> Optional[str]:
    """Pull the most likely visible label from an action description string."""
    m = re.search(r"['\"](.+?)['\"]", description)
    if m:
        return m.group(1)[:100]
    words = description.split()[:6]
    text = " ".join(words)
    return text[:100] if text else None


def _compute_fingerprint(action: str, selector: Optional[str], target_text: Optional[str], index: int) -> str:
    content = f"{action}|{selector or ''}|{target_text or ''}|{index}"
    return hashlib.md5(content.encode()).hexdigest()[:12]


def _is_brittle_selector(selector: Optional[str]) -> bool:
    """Identify recorder-only selectors that should not drive strict replay."""
    if not selector:
        return False
    sel = selector.strip().lower()
    if "data-browser-use-index" in sel:
        return True
    if sel.startswith("/") or sel.startswith("xpath="):
        # Long absolute XPaths with positional indices are highly unstable across renders.
        if "position()" in sel or "nth" in sel:
            return True
        if sel.count("/") > 7:
            return True
    return False


def _map_action(action_name: str) -> Optional[str]:
    name = action_name.lower().replace("_", "")
    # Actions to skip (no browser interaction to replay)
    skip = {"done", "extractpagecontent", "extract", "screenshot", "saveaspdf", "searchpage", "findelements", "scroll", "scrolldown", "scrollup", "scrolltoelement"}
    if name in skip:
        return None
    mapping = {
        "clickelement": "click",
        "click": "click",
        "inputtext": "fill",
        "input": "fill",
        "sendkeys": "fill",
        "navigate": "navigate",
        "gotourl": "navigate",
        "goback": "navigate",
        "searchgoogle": "navigate",
        "selectdropdownoption": "select",
        "selectoption": "select",
        "uploadfile": "upload",
        "dragdrop": "drag",
        "wait": "wait",
        "switchtab": "navigate",
    }
    for key, val in mapping.items():
        if key in name:
            return val
    return "click"


async def _resolve_spa_entry_url(
    app_url: str,
    storage_state: Optional[str],
    headless: bool,
    goal: str,
) -> Optional[str]:
    """Find the deepest useful entry URL for SPA dashboards.

    Strategy 1: <a href> links pointing to known sub-paths (/editor/, /project/, etc.)
    Strategy 2: data attributes encoding project/item IDs (data-project-id, data-href)
    Strategy 3: Intercept history.pushState before clicking card-like elements and
                capture the URL the SPA navigates to without a full page load.
    """
    from playwright.async_api import async_playwright
    from urllib.parse import urljoin

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--use-gl=swiftshader"],
        )
        ctx_kwargs: dict = {}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        try:
            await page.goto(app_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(5000)  # SPA render settle — extended for canvas/WebGL apps

            # Strategy 1: anchor links pointing to known deep paths
            sub_path_patterns = [
                "/editor/", "/project/", "/workspace/", "/video/",
                "/canvas/", "/document/", "/flow/",
            ]
            all_hrefs: list[str] = await page.evaluate("""() =>
                Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h && !h.endsWith('#') && !h.endsWith('/'))
            """)
            for href in all_hrefs:
                for pattern in sub_path_patterns:
                    if pattern in href:
                        return href

            # Strategy 2: data attributes encoding project/item IDs
            data_link = await page.evaluate("""() => {
                const candidates = document.querySelectorAll(
                    '[data-project-id], [data-id], [data-item-id], [data-href]'
                );
                for (const el of candidates) {
                    const href = el.dataset.href || el.dataset.projectId || el.dataset.id;
                    if (href && href.startsWith('/')) return href;
                }
                return null;
            }""")
            if data_link:
                return urljoin(app_url, data_link)

            # Strategy 3: intercept pushState, click first card-like element
            # React Router / Next.js / Vue Router all use history.pushState
            await page.evaluate("""() => {
                window.__blopNavHistory = [];
                const orig = history.pushState.bind(history);
                history.pushState = function(...args) {
                    if (args[2]) window.__blopNavHistory.push(String(args[2]));
                    return orig(...args);
                };
            }""")

            initial_url = page.url
            card_selectors = [
                "[data-testid*='project']", "[data-testid*='item']",
                "[class*='project'][class*='cursor']",
                "[class*='card'][class*='cursor']",
                "[class*='item'][class*='cursor']",
                ".group.relative.cursor-pointer",
                "[role='gridcell']", "[role='listitem']",
            ]
            for sel in card_selectors:
                try:
                    el = page.locator(sel).first
                    if not await el.count():
                        continue

                    # Before clicking, try to extract href from the element or a child anchor
                    # to avoid triggering a full page navigation (reload) just to discover the URL.
                    href_via_dom: Optional[str] = await page.evaluate(
                        """(selector) => {
                            const el = document.querySelector(selector);
                            if (!el) return null;
                            // Is it an anchor itself?
                            if (el.tagName === 'A' && el.href) return el.href;
                            // Does it contain an anchor?
                            const a = el.querySelector('a[href]');
                            if (a && a.href) return a.href;
                            // data-href attribute
                            if (el.dataset && el.dataset.href) return el.dataset.href;
                            return null;
                        }""",
                        sel,
                    )
                    if href_via_dom:
                        for pattern in sub_path_patterns:
                            if pattern in href_via_dom:
                                return href_via_dom

                    # Fallback: actually click and wait for URL change / pushState.
                    # NOTE: this triggers a full page navigation on anchor-based SPAs —
                    # acceptable here since this browser context is discarded afterward.
                    await el.click(timeout=3000)
                    # Poll for URL change or pushState event (200ms × 20 = 4s)
                    for _ in range(20):
                        await page.wait_for_timeout(200)
                        new_url = page.url
                        if new_url != initial_url and new_url != app_url:
                            return new_url
                        nav_history: list[str] = await page.evaluate("window.__blopNavHistory || []")
                        if nav_history:
                            path = nav_history[-1]
                            if path and path.startswith("/"):
                                return urljoin(app_url, path)
                    break
                except Exception:
                    continue

        except Exception:
            pass
        finally:
            await browser.close()

    return None
