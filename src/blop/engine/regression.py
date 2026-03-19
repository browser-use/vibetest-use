"""Flow replay engine — step-by-step hybrid replay with agent repair fallback."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Optional, TYPE_CHECKING

from blop.schemas import FailureCase, RecordedFlow, ReplayStepResult, ReplayTrace, StabilityFingerprint
from blop.storage import files as file_store

if TYPE_CHECKING:
    from playwright.async_api import Page


AUTO_HEAL_MIN_CONFIDENCE = float(os.getenv("BLOP_AUTO_HEAL_MIN_CONFIDENCE", "0.78"))
AUTO_HEAL_MAX_BEHAVIOR_RISK = float(os.getenv("BLOP_AUTO_HEAL_MAX_BEHAVIOR_RISK", "0.25"))


def _selector_entropy(selector: Optional[str]) -> float:
    if not selector:
        return 1.0
    # Heuristic: deep CSS chains and nth-child patterns are more brittle.
    depth = selector.count(" ") + selector.count(">")
    brittle_tokens = sum(selector.count(t) for t in ("nth-child", ":has(", ":nth-of-type", "[class*="))
    score = min(1.0, (depth * 0.08) + (brittle_tokens * 0.18))
    return round(score, 4)


def _aria_consistency(step) -> float:
    score = 0.0
    if getattr(step, "aria_role", None):
        score += 0.35
    if getattr(step, "aria_name", None):
        score += 0.35
    if getattr(step, "label_text", None):
        score += 0.2
    if getattr(step, "testid_selector", None):
        score += 0.1
    return round(min(1.0, score), 4)


def _should_auto_heal(repair_confidence: float, behavior_risk: float) -> bool:
    return repair_confidence >= AUTO_HEAL_MIN_CONFIDENCE and behavior_risk <= AUTO_HEAL_MAX_BEHAVIOR_RISK


# ---------------------------------------------------------------------------
# Hybrid step-by-step executor
# ---------------------------------------------------------------------------

async def execute_recorded_flow(
    flow: RecordedFlow,
    run_id: str,
    case_id: str,
    storage_state: Optional[str],
    headless: bool = True,
    run_mode: str = "hybrid",
) -> FailureCase:
    """Replay a RecordedFlow step-by-step; repair broken selectors before falling back to agent."""
    from playwright.async_api import async_playwright
    from blop.engine.browser import make_browser_profile

    trace = ReplayTrace(
        flow_id=flow.flow_id,
        flow_name=flow.flow_name,
        run_mode="strict_steps",
    )

    browser_profile = make_browser_profile(headless=headless, storage_state=storage_state)

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": browser_profile.headless,
            "args": browser_profile.browser_args if hasattr(browser_profile, "browser_args") else [],
        }
        browser = await p.chromium.launch(**{k: v for k, v in launch_kwargs.items() if v or k == "headless"})

        ctx_kwargs: dict = {}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)

        # Start Playwright tracing for debugging artifacts
        trace_zip = file_store.trace_path(run_id, case_id)
        tracing_enabled = False
        try:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
            tracing_enabled = True
        except Exception:
            pass

        # Capture console errors
        page = await context.new_page()
        page.on("console", lambda msg: trace.console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("response", lambda resp: trace.network_errors.append(f"{resp.status} {resp.url}")
                 if resp.status >= 500 else None)

        try:
            unrecoverable = False
            deferred_asserts: list[tuple[int, object]] = []  # (step_idx, step) for assert steps

            spa_hints = getattr(flow, "spa_hints", None)

            for step_idx, step in enumerate(flow.steps):
                if step.action == "assert":
                    deferred_asserts.append((step_idx, step))
                    continue

                step_result = await _execute_single_step(
                    page=page,
                    step=step,
                    step_idx=step_idx,
                    run_id=run_id,
                    case_id=case_id,
                    run_mode=run_mode,
                    trace=trace,
                    spa_hints=spa_hints,
                )
                trace.step_results.append(step_result)

                if step_result.status == "fail":
                    if trace.step_failure_index is None:
                        trace.step_failure_index = step_idx
                    if run_mode == "strict_steps":
                        unrecoverable = True
                        break

                if step_result.screenshot_path:
                    trace.screenshots.append(step_result.screenshot_path)

            # Tiered assertion evaluation: deterministic first, vision batch for semantic only
            if deferred_asserts:
                assertion_eval_results = await _evaluate_assertions(page, deferred_asserts)
                for result in assertion_eval_results:
                    step_obj = result["step"]
                    passed = result["passed"]
                    eval_type = result["eval_type"]
                    trace.assertion_results.append({
                        "assertion": step_obj.value or step_obj.description,
                        "passed": passed,
                        "eval_type": eval_type,
                        **({"failed": True} if not passed else {}),
                    })
                    trace.step_results.append(ReplayStepResult(
                        step_id=step_obj.step_id,
                        action="assert",
                        status="pass" if passed else "fail",
                        replay_mode=eval_type,
                    ))

            # Take final screenshot
            try:
                final_path = file_store.screenshot_path(run_id, case_id, 999)
                await page.screenshot(path=final_path)
                trace.screenshots.append(final_path)
            except Exception:
                pass

        finally:
            if tracing_enabled:
                try:
                    await context.tracing.stop(path=trace_zip)
                    trace.trace_path = trace_zip
                except Exception:
                    pass
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    return _trace_to_failure_case(trace, flow, run_id, case_id)


async def _execute_single_step(
    page: "Page",
    step,
    step_idx: int,
    run_id: str,
    case_id: str,
    run_mode: str,
    trace: ReplayTrace,
    spa_hints=None,
) -> ReplayStepResult:
    """Tiered fallback: testid → aria_role → by_label → CSS → text → agent repair."""
    from blop.engine.interaction import click_locator, fill_locator, wait_for_spa_ready

    step_start = time.perf_counter()
    action = step.action
    selector = step.selector
    value = step.value
    target_text = step.target_text
    base_selector_entropy = _selector_entropy(selector)
    base_aria_consistency = _aria_consistency(step)

    def _result(
        *,
        status: str,
        replay_mode: str,
        error: str | None = None,
        screenshot_path: str | None = None,
        retry_count: int = 0,
        repair_confidence: float = 0.0,
        failure_reason: str | None = None,
    ) -> ReplayStepResult:
        elapsed_ms = int((time.perf_counter() - step_start) * 1000)
        return ReplayStepResult(
            step_id=step.step_id,
            action=action,
            status=status,
            replay_mode=replay_mode,
            error=error,
            screenshot_path=screenshot_path,
            elapsed_ms=elapsed_ms,
            retry_count=retry_count,
            selector_entropy=base_selector_entropy,
            aria_consistency=base_aria_consistency,
            repair_confidence=repair_confidence,
            failure_reason=failure_reason,
        )

    # Auth redirect patterns — session expiry detected mid-run
    _AUTH_REDIRECT = ("/login", "/signin", "/sign-in", "/auth", "/account/login", "?redirect=")

    def _reason_from_exception(exc: Exception) -> str:
        text = str(exc).lower()
        if "timeout" in text:
            return "spa_not_ready"
        if "auth redirect detected" in text:
            return "auth_expired"
        if any(
            kw in text for kw in (
                "intercept",
                "outside of the viewport",
                "receives pointer events",
                "another element",
                "not visible",
            )
        ):
            return "click_intercepted"
        if "strict mode violation" in text or "resolved to" in text:
            return "ambiguous_locator"
        if "no node found" in text or "waiting for selector" in text or "not found" in text:
            return "locator_not_found"
        return "step_execution_failed"

    async def _pick_locator_candidate(locator, *, allow_ambiguous: bool = False):
        count = await locator.count()
        if count == 0:
            return None, "locator_not_found", None

        limit = min(count, 8)
        visible = []
        for idx in range(limit):
            candidate = locator.nth(idx)
            try:
                if await candidate.is_visible():
                    visible.append(candidate)
            except Exception:
                continue

        candidates = visible if visible else [locator.nth(idx) for idx in range(limit)]
        if len(candidates) == 1:
            return candidates[0], None, None
        if allow_ambiguous:
            return candidates[0], None, None
        return (
            None,
            "ambiguous_locator",
            f"Locator matched {count} elements; refusing first-match fallback",
        )

    async def _apply_action(locator):
        if action == "click":
            clicked = await click_locator(locator, timeout=5000, allow_force=True)
            if clicked:
                return True, None, None
            return False, "click_intercepted", "Click target was not actionable"
        if action == "fill":
            if not value:
                return False, "invalid_step", "Fill step missing value"
            filled = await fill_locator(locator, value, timeout=5000)
            if filled:
                return True, None, None
            return False, "fill_failed", "Could not fill target field"
        if action == "select":
            if not value:
                return False, "invalid_step", "Select step missing value"
            try:
                await locator.select_option(value)
                return True, None, None
            except Exception as exc:
                return False, _reason_from_exception(exc), str(exc)
        if action == "upload":
            if not value:
                return False, "invalid_step", "Upload step missing file path"
            try:
                await locator.set_input_files(value)
                return True, None, None
            except Exception as exc:
                return False, _reason_from_exception(exc), str(exc)
        if action == "drag":
            if not value:
                return False, "invalid_step", "Drag step missing drop selector"
            drop_target = page.locator(value)
            drop_count = await drop_target.count()
            if drop_count == 0:
                return False, "locator_not_found", "Drag drop target not found"
            drop_candidate, drop_reason, drop_error = await _pick_locator_candidate(drop_target)
            if not drop_candidate:
                return False, drop_reason, drop_error
            try:
                await locator.drag_to(drop_candidate)
                return True, None, None
            except Exception as exc:
                return False, _reason_from_exception(exc), str(exc)
        return False, "unsupported_action", f"Unsupported action: {action}"

    async def _try_locator(locator, replay_mode: str):
        selected, reason, reason_error = await _pick_locator_candidate(locator)
        if not selected:
            return False, reason, reason_error, replay_mode
        ok, apply_reason, apply_error = await _apply_action(selected)
        if ok:
            return True, None, None, replay_mode
        return False, apply_reason, apply_error, replay_mode

    last_reason: str | None = None
    last_error: str | None = None

    # Tier 0: Navigate steps
    if action == "navigate":
        try:
            nav_url = value or step.description
            # Retroactive editor-heavy detection: flows recorded before auto-detection was
            # added won't have spa_hints.is_editor_heavy set. Re-classify via context graph
            # archetype so they still get the extended canvas wait without re-recording.
            _effective_hints = spa_hints
            if spa_hints and not spa_hints.is_editor_heavy:
                from blop.engine.context_graph import detect_app_archetype, editor_hints_from_archetype
                from blop.schemas import SiteInventory
                _probe = SiteInventory(
                    app_url=nav_url or "",
                    routes=[nav_url or ""],
                    buttons=[],
                    links=[],
                    forms=[],
                    headings=[],
                    auth_signals=[],
                    business_signals=[],
                )
                _archetype = detect_app_archetype(_probe)
                _hint_kwargs = editor_hints_from_archetype(_archetype)
                if _hint_kwargs:
                    from blop.schemas import SpaHints as _SpaHints
                    _effective_hints = _SpaHints(**{**spa_hints.model_dump(), **_hint_kwargs})
            elif spa_hints is None:
                _effective_hints = None

            nav_timeout = 45000 if (_effective_hints and _effective_hints.is_editor_heavy) else 30000
            # domcontentloaded is the safe default — networkidle times out on SPAs that have
            # background polling/websockets. wait_for_spa_ready() below handles SPA settling.
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=nav_timeout)
            # Detect silent auth redirect (session expired mid-run)
            _current = page.url.lower()
            if any(pat in _current for pat in _AUTH_REDIRECT):
                return _result(status="fail", replay_mode="selector", error=f"Auth redirect detected: {page.url}")
            # Wait for SPA to reach a usable state before continuing
            await wait_for_spa_ready(
                page,
                wait_for_selector=_effective_hints.wait_for_selector if _effective_hints else None,
                wait_for_shadow_selector=_effective_hints.wait_for_shadow_selector if _effective_hints else None,
                settle_ms=_effective_hints.settle_ms if _effective_hints else 1500,
                spa_hints=_effective_hints,
            )
            shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
            return _result(status="pass", replay_mode="selector", screenshot_path=shot)
        except Exception as e:
            return _result(
                status="fail",
                replay_mode="selector",
                error=str(e),
                failure_reason=_reason_from_exception(e),
            )

    # Explicit wait steps (supports numeric seconds in value)
    if action == "wait":
        try:
            wait_secs = float(value) if value else max(0.1, float(getattr(step, "wait_after_secs", 0.5)))
        except Exception:
            wait_secs = max(0.1, float(getattr(step, "wait_after_secs", 0.5)))
        await page.wait_for_timeout(int(wait_secs * 1000))
        shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
        return _result(status="pass", replay_mode="wait", screenshot_path=shot)

    # Tier 1: data-testid selector (most stable)
    testid_sel = getattr(step, "testid_selector", None)
    if testid_sel:
        try:
            ok, reason, err, _ = await _try_locator(page.locator(testid_sel), "testid")
            if ok:
                shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
                return _result(status="pass", replay_mode="testid", screenshot_path=shot)
            last_reason, last_error = reason, err
        except Exception:
            pass

    # Tier 2: ARIA role + name
    aria_role = getattr(step, "aria_role", None)
    aria_name = getattr(step, "aria_name", None)
    if aria_role and aria_name:
        try:
            for exact_mode in (True, False):
                loc = page.get_by_role(aria_role, name=aria_name, exact=exact_mode)
                ok, reason, err, _ = await _try_locator(
                    loc,
                    "aria_role_exact" if exact_mode else "aria_role_fuzzy",
                )
                if ok:
                    shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
                    return _result(
                        status="pass",
                        replay_mode="aria_role_exact" if exact_mode else "aria_role",
                        screenshot_path=shot,
                    )
                if reason != "locator_not_found":
                    last_reason, last_error = reason, err
        except Exception:
            pass

    # Tier 3: by-label (fill actions only)
    label_text = getattr(step, "label_text", None)
    if action in ("fill", "upload") and label_text and value:
        try:
            for exact_mode in (True, False):
                loc = page.get_by_label(label_text, exact=exact_mode)
                ok, reason, err, _ = await _try_locator(
                    loc,
                    "by_label_exact" if exact_mode else "by_label",
                )
                if ok:
                    shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
                    return _result(
                        status="pass",
                        replay_mode="by_label_exact" if exact_mode else "by_label",
                        screenshot_path=shot,
                    )
                if reason != "locator_not_found":
                    last_reason, last_error = reason, err
        except Exception:
            pass

    # Tier 4: CSS selector
    if selector:
        try:
            ok, reason, err, _ = await _try_locator(page.locator(selector), "selector")
            if ok:
                shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
                return _result(status="pass", replay_mode="selector", screenshot_path=shot)
            if reason:
                last_reason, last_error = reason, err
        except Exception:
            pass

    # Tier 5: Text-based lookup
    if target_text:
        try:
            for exact_mode in (True, False):
                text_loc = page.get_by_text(target_text, exact=exact_mode)
                ok, reason, err, _ = await _try_locator(
                    text_loc,
                    "text_lookup_exact" if exact_mode else "text_lookup_fuzzy",
                )
                if ok:
                    shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
                    return _result(
                        status="pass",
                        replay_mode="text_lookup_exact" if exact_mode else "text_lookup",
                        screenshot_path=shot,
                    )
                if reason != "locator_not_found":
                    last_reason, last_error = reason, err
        except Exception:
            pass

    # Tier 6: Hybrid repair via agent (ARIA-enhanced)
    if run_mode in ("hybrid", "explore"):
        trace.run_mode = "hybrid_repair"
        repair_result = await repair_step_with_agent(step, page)
        if repair_result and repair_result.get("_quota_error"):
            return _result(
                status="fail",
                replay_mode="agent_repair",
                error="Gemini quota/rate-limit exceeded",
                failure_reason="llm_quota_error",
            )
        if repair_result:
            repair_confidence = float(repair_result.get("repair_confidence", 0.6))
            behavior_risk = float(repair_result.get("behavior_risk", 0.35))
            if not _should_auto_heal(repair_confidence, behavior_risk):
                return _result(
                    status="fail",
                    replay_mode="agent_repair",
                    error=(
                        "Repair proposed but not auto-applied (confidence/risk threshold not met): "
                        f"confidence={repair_confidence:.2f}, risk={behavior_risk:.2f}"
                    ),
                    repair_confidence=repair_confidence,
                    failure_reason="repair_rejected",
                )
            locator_type = repair_result.get("repaired_locator_type", "css")
            repaired_selector = repair_result.get("repaired_selector")
            repaired_role = repair_result.get("repaired_role")
            repaired_name = repair_result.get("repaired_name")
            repaired_value = repair_result.get("repaired_value", value)
            repaired_action = repair_result.get("repaired_action", action)
            try:
                el = None
                if locator_type == "role" and repaired_role and repaired_name:
                    loc = page.get_by_role(repaired_role, name=repaired_name, exact=True)
                    el, _, _ = await _pick_locator_candidate(loc)
                    if el is None:
                        loc = page.get_by_role(repaired_role, name=repaired_name, exact=False)
                        el, _, _ = await _pick_locator_candidate(loc)
                elif locator_type == "label" and repaired_name:
                    loc = page.get_by_label(repaired_name, exact=False)
                    el, _, _ = await _pick_locator_candidate(loc)
                elif locator_type == "text" and repaired_name:
                    loc = page.get_by_text(repaired_name, exact=False)
                    el, _, _ = await _pick_locator_candidate(loc)
                elif repaired_selector:
                    loc = page.locator(repaired_selector)
                    el, _, _ = await _pick_locator_candidate(loc)

                if el:
                    original_action = action
                    original_value = value
                    try:
                        action = repaired_action
                        value = repaired_value
                        ok, repaired_reason, repaired_error = await _apply_action(el)
                        if not ok:
                            return _result(
                                status="fail",
                                replay_mode="agent_repair",
                                error=repaired_error or "Repaired action failed",
                                repair_confidence=repair_confidence,
                                failure_reason=repaired_reason or "repair_failed",
                            )
                    finally:
                        action = original_action
                        value = original_value
                else:
                    from blop.engine.vision import click_by_vision
                    desc = target_text or step.description
                    await click_by_vision(page, desc)

                shot = await _take_step_screenshot(page, run_id, case_id, step_idx)
                return _result(
                    status="repaired",
                    replay_mode="agent_repair",
                    screenshot_path=shot,
                    repair_confidence=repair_confidence,
                )
            except Exception as e:
                return _result(
                    status="fail",
                    replay_mode="agent_repair",
                    error=str(e),
                    repair_confidence=repair_confidence,
                    failure_reason=_reason_from_exception(e),
                )

    return _result(
        status="fail",
        replay_mode="selector",
        error=last_error or "No selector, text, or repair succeeded",
        failure_reason=last_reason or "locator_not_found",
    )


async def repair_step_with_agent(step, page: "Page") -> Optional[dict]:
    """Send REPAIR_STEP_PROMPT + ARIA context + screenshot to Gemini; return repaired action dict."""
    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        return None

    from blop.prompts import REPAIR_STEP_PROMPT

    try:
        from browser_use.llm import ChatGoogle
        from browser_use.llm.messages import UserMessage

        # Capture ARIA tree for context-efficient repair
        aria_tree = ""
        try:
            snapshot = await page.accessibility.snapshot(interesting_only=True)
            if snapshot:
                nodes = _extract_interactive_nodes_flat(snapshot, max_nodes=30)
                if nodes:
                    aria_tree = json.dumps(nodes, separators=(",", ":"))
        except Exception:
            pass

        # Use cheaper model when ARIA context is available (selection task, not vision)
        model = "gemini-1.5-flash" if aria_tree else "gemini-2.5-flash"

        img_bytes = await page.screenshot(type="jpeg", quality=85)
        b64 = base64.b64encode(img_bytes).decode()
        current_url = page.url

        aria_section = f"\nAvailable interactive elements (ARIA):\n{aria_tree}\n" if aria_tree else ""

        prompt = REPAIR_STEP_PROMPT.format(
            action=step.action,
            selector=step.selector or "none",
            target_text=step.target_text or "none",
            description=step.description,
            current_url=current_url,
            aria_section=aria_section,
        )
        prompt += (
            "\nReturn JSON with repair_confidence (0..1) and behavior_risk (0..1) "
            "in addition to any repaired locator/action fields."
        )

        llm = ChatGoogle(model=model, api_key=google_api_key, temperature=0.2, max_output_tokens=300)
        response = await llm.ainvoke([UserMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ])])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        _e = str(e).lower()
        if "429" in _e or "quota" in _e or "resource_exhausted" in _e or "rate" in _e:
            return {"_quota_error": True}

    return None


def _extract_interactive_nodes_flat(node: dict, max_nodes: int = 30, _count: Optional[list] = None) -> list[dict]:
    """Flatten an ARIA snapshot into a compact list of interactive nodes for repair context."""
    if _count is None:
        _count = [0]
    interactive_roles = {
        "button", "link", "textbox", "checkbox", "radio", "combobox",
        "listbox", "menuitem", "tab", "switch", "searchbox", "spinbutton",
    }
    results = []
    role = node.get("role", "")
    name = node.get("name", "")
    if role in interactive_roles and name:
        if _count[0] < max_nodes:
            entry: dict = {"role": role, "name": name}
            if node.get("disabled"):
                entry["disabled"] = True
            results.append(entry)
            _count[0] += 1
    for child in node.get("children", []):
        if _count[0] >= max_nodes:
            break
        results.extend(_extract_interactive_nodes_flat(child, max_nodes, _count))
    return results


async def _take_step_screenshot(page: "Page", run_id: str, case_id: str, step_idx: int) -> Optional[str]:
    try:
        path = file_store.screenshot_path(run_id, case_id, step_idx)
        await page.screenshot(path=path)
        return path
    except Exception:
        return None


async def _evaluate_assertions(page: "Page", deferred_asserts: list) -> list[dict]:
    """Evaluate assertions using deterministic checks where possible, vision batch for semantic."""
    results = []
    semantic_batch: list[tuple] = []  # (step_obj, text)

    for _step_idx, step_obj in deferred_asserts:
        sa = getattr(step_obj, "structured_assertion", None)
        text = step_obj.value or step_obj.description

        if sa is None or sa.assertion_type == "semantic":
            semantic_batch.append((step_obj, text))
            continue

        try:
            passed = await _eval_deterministic(page, sa)
        except Exception:
            # Deterministic eval failed — fall back to vision
            semantic_batch.append((step_obj, text))
            continue

        results.append({"step": step_obj, "passed": passed, "eval_type": sa.assertion_type})

    # Batch evaluate remaining semantic assertions in a single vision call
    if semantic_batch:
        from blop.engine.vision import assert_all_by_vision
        texts = [t for _, t in semantic_batch]
        try:
            vision_results = await assert_all_by_vision(page, texts)
            for (step_obj, _), passed in zip(semantic_batch, vision_results):
                results.append({"step": step_obj, "passed": passed, "eval_type": "vision_batch"})
        except Exception as e:
            _e = str(e).lower()
            if "429" in _e or "quota" in _e or "resource_exhausted" in _e or "rate" in _e:
                # Quota/rate-limit error — mark assertions as failed (not silently passed)
                for step_obj, _ in semantic_batch:
                    results.append({"step": step_obj, "passed": False, "eval_type": "quota_error"})
            else:
                for step_obj, _ in semantic_batch:
                    results.append({"step": step_obj, "passed": False, "eval_type": "vision_batch"})

    return results


async def _eval_deterministic(page: "Page", sa) -> bool:
    """Evaluate a non-semantic StructuredAssertion without an LLM call."""
    t = sa.assertion_type
    target = sa.target
    expected = sa.expected

    if t == "text_present":
        if target:
            try:
                el_text = await page.locator(target).first.text_content(timeout=3000)
                result = (expected or "") in (el_text or "")
            except Exception:
                # Target not found — check full body text
                body = await page.evaluate("() => document.body.innerText")
                result = (expected or "") in (body or "")
        else:
            body = await page.evaluate("() => document.body.innerText")
            result = (expected or "") in (body or "")

    elif t == "element_visible":
        if target:
            result = await page.locator(target).first.is_visible(timeout=3000)
        else:
            result = False

    elif t == "url_contains":
        result = (expected or "") in page.url

    elif t == "page_title":
        title = await page.title()
        result = (expected or "") in title

    elif t == "count":
        if target and expected:
            count = await page.locator(target).count()
            try:
                result = count == int(expected)
            except ValueError:
                result = False
        else:
            result = False

    else:
        raise ValueError(f"Unknown deterministic type: {t}")

    return (not result) if sa.negated else result


def _trace_to_failure_case(
    trace: ReplayTrace,
    flow: RecordedFlow,
    run_id: str,
    case_id: str,
) -> FailureCase:
    """Convert a ReplayTrace to a FailureCase."""
    assertion_failures = [
        r["assertion"] for r in trace.assertion_results if not r.get("passed", True)
    ]
    failed_steps = [r for r in trace.step_results if r.status == "fail"]
    failure_reason_codes = sorted({r.failure_reason for r in failed_steps if r.failure_reason})

    if trace.network_errors:
        status: str = "fail"
    elif assertion_failures or failed_steps:
        status = "fail"
    else:
        status = "pass"

    # Detect auth-blocked scenarios: check raw_result and step errors only.
    # Console errors are intentionally excluded — background resource 401s (analytics,
    # CDN, third-party APIs) fire on otherwise-authenticated pages and would produce
    # false positives. Actual auth redirects are caught by the navigate-step check
    # which sets step.error = "Auth redirect detected: <url>".
    auth_kws = ("401", "403", "unauthorized", "forbidden", "login required", "auth redirect detected")
    _auth_blocked = (
        ("auth_expired" in failure_reason_codes)
        or any(kw in trace.raw_result.lower() for kw in auth_kws)
        or any(r.error and any(kw in r.error.lower() for kw in auth_kws) for r in trace.step_results)
    )
    if _auth_blocked:
        status = "blocked"
        if not trace.raw_result:
            trace.raw_result = (
                "Session expired mid-run. Re-run after refreshing auth profile "
                "with capture_auth_session."
            )

    repro: list[str] = []
    for r in trace.step_results:
        if r.status in ("fail",):
            repro.append(f"Step {r.step_id} ({r.action}) failed via {r.replay_mode}: {r.error or 'unknown'}")

    fingerprints: list[StabilityFingerprint] = []
    repair_confidences: list[float] = []
    for r in trace.step_results:
        drift = min(1.0, max(0.0, (r.selector_entropy * 0.6) + (r.retry_count * 0.1) - (r.aria_consistency * 0.3)))
        fingerprints.append(
            StabilityFingerprint(
                selector_entropy=r.selector_entropy,
                aria_consistency=r.aria_consistency,
                latency_ms=r.elapsed_ms,
                retry_count=r.retry_count,
                drift_score=round(drift, 4),
            )
        )
        if r.repair_confidence > 0:
            repair_confidences.append(r.repair_confidence)

    avg_repair_confidence = (
        round(sum(repair_confidences) / len(repair_confidences), 4)
        if repair_confidences
        else 0.0
    )
    healing_decision = "none"
    if any(r.status == "repaired" for r in trace.step_results):
        healing_decision = "auto_heal"
    elif any((r.error or "").startswith("Repair proposed but not auto-applied") for r in trace.step_results):
        healing_decision = "propose_patch"

    return FailureCase(
        case_id=case_id,
        run_id=run_id,
        flow_id=flow.flow_id,
        flow_name=flow.flow_name,
        status=status,
        severity="none",
        failure_reason_codes=failure_reason_codes,
        repro_steps=repro,
        console_errors=trace.console_errors[:20],
        network_errors=trace.network_errors[:20],
        screenshots=trace.screenshots,
        raw_result=trace.raw_result,
        replay_mode=trace.run_mode,
        step_failure_index=trace.step_failure_index,
        assertion_failures=assertion_failures,
        assertion_results=trace.assertion_results,
        trace_path=trace.trace_path,
        repair_confidence=avg_repair_confidence,
        stability_fingerprints=fingerprints,
        healing_decision=healing_decision,
    )


# ---------------------------------------------------------------------------
# Goal-replay fallback (original behaviour, kept for goal_fallback mode)
# ---------------------------------------------------------------------------

async def execute_flow(
    flow: RecordedFlow,
    app_url: str,
    run_id: str,
    case_id: str,
    storage_state: Optional[str],
    headless: bool = True,
    verbose: bool = False,
    max_steps: int = 50,
    run_mode: str = "hybrid",
) -> FailureCase:
    """Replay a RecordedFlow.

    Uses hybrid step-by-step replay by default (run_mode='hybrid').
    Falls back to full goal-replay agent when run_mode='goal_fallback'.
    """
    # Per-flow override takes precedence over the run-level run_mode
    effective_mode = getattr(flow, "run_mode_override", None) or run_mode
    if effective_mode != "goal_fallback" and flow.steps:
        return await execute_recorded_flow(
            flow=flow,
            run_id=run_id,
            case_id=case_id,
            storage_state=storage_state,
            headless=False if verbose else headless,
            run_mode=effective_mode,
        )

    return await _goal_fallback(
        flow=flow,
        app_url=app_url,
        run_id=run_id,
        case_id=case_id,
        storage_state=storage_state,
        headless=headless,
        verbose=verbose,
        max_steps=max_steps,
    )


async def _goal_fallback(
    flow: RecordedFlow,
    app_url: str,
    run_id: str,
    case_id: str,
    storage_state: Optional[str],
    headless: bool,
    verbose: bool,
    max_steps: int,
) -> FailureCase:
    """Original goal-based agent replay."""
    from browser_use import Agent, BrowserSession
    from browser_use.llm import ChatGoogle
    from blop.engine.browser import make_browser_profile

    google_api_key = os.getenv("GOOGLE_API_KEY", "dummy_key")
    run_headless = False if verbose else headless

    browser_profile = make_browser_profile(headless=run_headless, storage_state=storage_state)
    browser_session = BrowserSession(browser_profile=browser_profile)

    console_errors: list[str] = []
    network_errors: list[str] = []
    screenshots: list[str] = []
    raw_result = ""
    status = "error"

    try:
        llm = ChatGoogle(model="gemini-2.5-flash", temperature=0.7, api_key=google_api_key)
        task = f"Navigate to {app_url} then: {flow.goal}"
        # Append login hint so agent can authenticate if session cookies are stale
        _username = os.getenv("TEST_USERNAME", "")
        _password = os.getenv("TEST_PASSWORD", "")
        _login_url = os.getenv("LOGIN_URL", "") or os.getenv("TEST_AUTH_URL", "")
        if _username and _password and _login_url:
            task += (
                f"\n\nIf you encounter a login page, use these credentials: "
                f"email={_username} password={_password} (login URL: {_login_url}). "
                f"Do NOT create a new account — log in with the provided credentials."
            )
        from blop.engine.recording import SPA_AGENT_RULES
        _is_heavy = getattr(flow, "spa_hints", None) and getattr(flow.spa_hints, "is_editor_heavy", False)
        _system_msg = SPA_AGENT_RULES
        if _is_heavy:
            _system_msg += (
                " IMPORTANT: This flow targets a canvas/WebGL-heavy application. "
                "Wait patiently — up to 45 seconds — for the canvas view to initialise before declaring failure. "
                "Only assert on DOM toolbar elements, never on canvas content."
            )
        agent = Agent(task=task, llm=llm, browser_session=browser_session, use_vision=True,
                      extend_system_message=_system_msg)

        screenshot_task: Optional[asyncio.Task] = None
        step_idx = 0

        async def _poll_screenshots():
            nonlocal step_idx
            while True:
                try:
                    await asyncio.sleep(3)
                    shot_path = file_store.screenshot_path(run_id, case_id, step_idx)
                    await browser_session.take_screenshot(path=shot_path)
                    screenshots.append(shot_path)
                    step_idx += 1
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass

        # Always capture screenshots — provides evidence the agent is doing real work
        screenshot_task = asyncio.create_task(_poll_screenshots())

        goal_trace_path: Optional[str] = None
        try:
            history = await agent.run(max_steps=max_steps)

            # Guaranteed final screenshot after agent completes
            try:
                final_path = file_store.screenshot_path(run_id, case_id, 999)
                await browser_session.take_screenshot(path=final_path)
                if final_path not in screenshots:
                    screenshots.append(final_path)
            except Exception:
                pass

            # Prefer done-action text over final_result() which only returns ExtractAction content
            # model_dump() includes ALL action type keys (most None) — find the non-None one
            raw_result = ""
            done_success = True
            done_found = False
            if hasattr(history, "model_actions"):
                try:
                    for action in reversed(history.model_actions()):
                        done_val = action.get("done")
                        if done_val is not None:
                            if isinstance(done_val, dict):
                                raw_result = str(done_val.get("text") or done_val)
                                done_success = bool(done_val.get("success", True))
                            else:
                                raw_result = str(done_val)
                            done_found = True
                            break
                except Exception:
                    pass
            if not raw_result:
                raw_result = str(history.final_result()) if hasattr(history, "final_result") else str(history)

            # When no done action was found, fall back to keyword scanning of raw_result.
            # This catches cases where the agent silently ended without a done action.
            if not done_found:
                _kw_fail = any(w in raw_result.lower() for w in (
                    "error", "fail", "broken", "exception", "crash",
                    "404", "500", "unable", "could not", "cannot",
                ))
                if _kw_fail:
                    done_success = False

            # Trust the agent's done_success boolean as the primary signal.
            # Additionally catch hard browser-level failures (404, error pages)
            # that the agent may not detect — e.g. logout redirecting to a 404.
            final_url = ""
            final_page_text = ""
            try:
                final_url = await browser_session.get_current_page_url() or ""
                page = await browser_session.get_current_page()
                if page:
                    final_page_text = (await page.inner_text("body") or "").lower()[:500]
            except Exception:
                pass

            _hard_fail = (
                "404" in final_page_text
                or "page not found" in final_page_text
                or "did you forget to add the page" in final_page_text
                or "500" in final_page_text
                or "internal server error" in final_page_text
            )

            if not done_success or _hard_fail:
                if _hard_fail and done_success:
                    raw_result += f" [browser ended on error page: {final_url}]"
                status = "fail"
            else:
                status = "pass"
        finally:
            if screenshot_task:
                screenshot_task.cancel()
                try:
                    await screenshot_task
                except asyncio.CancelledError:
                    pass

        try:
            ctx = getattr(browser_session, "context", None)
            if ctx and ctx.pages:
                final_path = file_store.screenshot_path(run_id, case_id, step_idx)
                await ctx.pages[0].screenshot(path=final_path)
                screenshots.append(final_path)
        except Exception:
            pass

        if console_errors:
            log_path = file_store.console_log_path(run_id, case_id)
            with open(log_path, "w") as f:
                f.write("\n".join(console_errors))

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
        flow_id=flow.flow_id,
        flow_name=flow.flow_name,
        status=status,
        severity="none",
        repro_steps=[],
        console_errors=console_errors,
        network_errors=network_errors,
        screenshots=screenshots,
        raw_result=raw_result,
        replay_mode="goal_fallback",
        trace_path=goal_trace_path,
    )


async def run_flows(
    flows: list[RecordedFlow],
    app_url: str,
    run_id: str,
    storage_state: Optional[str],
    headless: bool,
    max_steps: int = 50,
    run_mode: str = "hybrid",
) -> list[FailureCase]:
    """Execute all flows in parallel (semaphore=3 with tracing, 5 without)."""
    import uuid as _uuid

    # Playwright tracing with DOM snapshots is memory-intensive; reduce concurrency
    # whenever at least one flow will execute in step-replay mode.
    _tracing_active = any(
        (getattr(flow, "run_mode_override", None) or run_mode) != "goal_fallback"
        for flow in flows
    )
    semaphore = asyncio.Semaphore(3 if _tracing_active else 5)

    async def run_one(flow: RecordedFlow) -> FailureCase:
        async with semaphore:
            cid = _uuid.uuid4().hex
            return await execute_flow(
                flow=flow,
                app_url=app_url,
                run_id=run_id,
                case_id=cid,
                storage_state=storage_state,
                headless=headless,
                max_steps=max_steps,
                run_mode=run_mode,
            )

    results = await asyncio.gather(*[run_one(f) for f in flows], return_exceptions=True)

    cases: list[FailureCase] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            cases.append(FailureCase(
                run_id=run_id,
                flow_id=flows[i].flow_id if i < len(flows) else "unknown",
                flow_name=flows[i].flow_name if i < len(flows) else "unknown",
                status="error",
                raw_result=str(result),
                replay_mode="goal_fallback",
            ))
        else:
            cases.append(result)

    return cases
