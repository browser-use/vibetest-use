"""Resilient browser interaction helpers with CSS → text → vision fallback chain."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page


async def wait_for_spa_ready(
    page: "Page",
    wait_for_selector: Optional[str] = None,
    wait_for_shadow_selector: Optional[str] = None,
    settle_ms: int = 1500,
    timeout_ms: int = 15000,
    spa_hints=None,  # SpaHints instance — enables Tier 5 for editor_heavy archetypes
) -> None:
    """Wait for SPA route to settle and render usable content.

    Uses element-visibility and DOM signals rather than networkidle, which
    misfires on WebGL/WASM apps and multi-redirect OAuth flows.
    Falls through each strategy gracefully — never raises.
    """
    # 1. Explicit ready selector
    if wait_for_selector:
        try:
            await page.wait_for_selector(wait_for_selector, state="visible", timeout=timeout_ms)
            return
        except Exception:
            pass  # Fall through to generic checks

    # 2. Shadow DOM ready selector (web components / StencilJS / LitElement)
    if wait_for_shadow_selector:
        escaped = wait_for_shadow_selector.replace("\\", "\\\\").replace("'", "\\'")
        try:
            await page.wait_for_function(
                f"""() => {{
                    function searchShadow(root) {{
                        if (root.querySelector('{escaped}')) return true;
                        for (const el of root.querySelectorAll('*')) {{
                            if (el.shadowRoot && searchShadow(el.shadowRoot)) return true;
                        }}
                        return false;
                    }}
                    return searchShadow(document);
                }}""",
                timeout=timeout_ms,
            )
            return
        except Exception:
            pass

    # 3. Generic loading-indicator disappearance check
    try:
        await page.wait_for_function(
            """() => {
                const els = document.querySelectorAll(
                    '[class*="loading"], [class*="spinner"], [class*="skeleton"], [aria-busy="true"]'
                );
                return els.length === 0 || Array.from(els).every(el => {
                    const s = window.getComputedStyle(el);
                    return s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0';
                });
            }""",
            timeout=min(settle_ms * 3, 8000),
        )
    except Exception:
        pass

    # 4. Unconditional settle pause
    if settle_ms > 0:
        await page.wait_for_timeout(settle_ms)

    # 5. Canvas/WebGL extended wait for editor-heavy archetypes
    if spa_hints and getattr(spa_hints, "is_editor_heavy", False):
        await wait_for_editor_ready(
            page=page,
            editor_ready_js=getattr(spa_hints, "editor_ready_js", None),
            editor_ready_selector=getattr(spa_hints, "editor_ready_selector", None),
            editor_settle_ms=getattr(spa_hints, "editor_settle_ms", 8000),
        )


async def wait_for_editor_ready(
    page: "Page",
    editor_ready_js: Optional[str] = None,
    editor_ready_selector: Optional[str] = None,
    editor_settle_ms: int = 8000,
    timeout_ms: int = 45000,
) -> None:
    """Extended wait for canvas/WebGL-heavy views. Never raises.

    Used when context graph detects an `editor_heavy` archetype — any app whose
    primary UI renders into a canvas rather than DOM (design tools, video editors,
    diagram builders, game engines, etc.). Tries four strategies in order.
    """
    # Phase 1: JS expression polling (e.g. window.__appReady === true)
    if editor_ready_js:
        try:
            await page.wait_for_function(editor_ready_js, timeout=timeout_ms)
            return
        except Exception:
            pass

    # Phase 2: DOM selector for a known landmark element (toolbar, menu bar, etc.)
    if editor_ready_selector:
        try:
            await page.wait_for_selector(editor_ready_selector, state="visible", timeout=timeout_ms)
            return
        except Exception:
            pass

    # Phase 3: Canvas presence + non-zero dimensions + pixel content
    try:
        canvas_ready = await page.evaluate("""() => {
            const canvas = document.querySelector('canvas');
            if (!canvas || canvas.width === 0) return false;
            const rect = canvas.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return false;
            try {
                const ctx = canvas.getContext('2d');
                if (!ctx) return true;  // WebGL context — presence alone is signal
                const w = Math.min(canvas.width, 100);
                const h = Math.min(canvas.height, 100);
                const data = ctx.getImageData(0, 0, w, h).data;
                let nonDark = 0;
                for (let i = 0; i < data.length; i += 4) {
                    if ((data[i] + data[i+1] + data[i+2]) / 3 > 30) nonDark++;
                }
                return (nonDark / (w * h)) > 0.10;
            } catch (e) {
                return true;  // CORS-restricted canvas — presence is enough
            }
        }""")
        if canvas_ready:
            await page.wait_for_timeout(min(editor_settle_ms, 3000))
            return
    except Exception:
        pass

    # Phase 4: Generic loading overlay disappearance
    try:
        await page.wait_for_function(
            """() => {
                const els = document.querySelectorAll(
                    '[class*="loading"], [class*="spinner"], [class*="skeleton"], [aria-busy="true"]'
                );
                return els.length === 0 || Array.from(els).every(el => {
                    const s = window.getComputedStyle(el);
                    return s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0';
                });
            }""",
            timeout=min(editor_settle_ms * 2, 20000),
        )
    except Exception:
        pass

    # Phase 5: Unconditional extended settle
    await page.wait_for_timeout(editor_settle_ms)


async def find_in_shadow_dom(page: "Page", css_selector: str) -> bool:
    """Return True if css_selector matches any element anywhere, including inside shadow roots."""
    try:
        return bool(await page.evaluate(
            """(selector) => {
                function searchShadow(root) {
                    if (root.querySelector(selector)) return true;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot && searchShadow(el.shadowRoot)) return true;
                    }
                    return false;
                }
                return searchShadow(document);
            }""",
            css_selector,
        ))
    except Exception:
        return False


async def validate_selector(page: "Page", selector: str) -> bool:
    try:
        return await page.locator(selector).count() > 0
    except Exception:
        return False


async def scroll_into_view(page: "Page", selector: str) -> None:
    try:
        await page.locator(selector).scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass


async def wait_for_stable(page: "Page", selector: str, timeout: int = 3000) -> None:
    try:
        loc = page.locator(selector)
        await loc.wait_for(state="visible", timeout=timeout)
    except Exception:
        pass


async def click_locator(
    locator: "Locator",
    timeout: int = 5000,
    allow_force: bool = True,
) -> bool:
    """Click a locator with visibility and interception-aware retries."""
    try:
        await locator.scroll_into_view_if_needed(timeout=min(timeout, 3000))
    except Exception:
        pass

    try:
        await locator.click(timeout=timeout)
        return True
    except Exception as exc:
        if not allow_force:
            return False
        err = str(exc).lower()
        if any(
            kw in err
            for kw in (
                "intercept",
                "not visible",
                "outside of the viewport",
                "another element",
                "receives pointer events",
            )
        ):
            try:
                await locator.click(timeout=timeout, force=True)
                return True
            except Exception:
                return False
        return False


async def fill_locator(
    locator: "Locator",
    value: str,
    timeout: int = 5000,
) -> bool:
    """Fill a locator with a scroll-and-retry strategy."""
    try:
        await locator.scroll_into_view_if_needed(timeout=min(timeout, 3000))
    except Exception:
        pass

    try:
        await locator.fill(value, timeout=timeout)
        return True
    except Exception:
        return False


async def safe_click(
    page: "Page",
    selector: str,
    timeout: int = 5000,
    fallback_vision: bool = True,
) -> bool:
    """Try CSS selector click, then text match, then vision fallback."""
    try:
        await page.locator(selector).click(timeout=timeout)
        return True
    except Exception:
        pass

    # Try text match fallback
    try:
        await page.get_by_text(selector).first.click(timeout=timeout)
        return True
    except Exception:
        pass

    if fallback_vision:
        from blop.engine.vision import click_by_vision
        try:
            await click_by_vision(page, selector)
            return True
        except Exception:
            pass

    return False


async def safe_fill(
    page: "Page",
    selector: str,
    value: str,
    timeout: int = 5000,
    fallback_vision: bool = True,
) -> bool:
    """Try CSS selector fill, then vision fallback."""
    try:
        await page.locator(selector).fill(value, timeout=timeout)
        return True
    except Exception:
        pass

    if fallback_vision:
        from blop.engine.vision import find_element_coords
        try:
            coords = await find_element_coords(page, selector)
            if coords:
                await page.mouse.click(coords[0], coords[1])
                await page.keyboard.type(value)
                return True
        except Exception:
            pass

    return False


async def drag_and_drop(page: "Page", source: str, target: str) -> bool:
    """Playwright drag with fallback to JS dispatchEvent."""
    try:
        await page.drag_and_drop(source, target)
        return True
    except Exception:
        pass

    try:
        src = page.locator(source)
        tgt = page.locator(target)
        src_box = await src.bounding_box()
        tgt_box = await tgt.bounding_box()
        if src_box and tgt_box:
            await page.mouse.move(src_box["x"] + src_box["width"] / 2, src_box["y"] + src_box["height"] / 2)
            await page.mouse.down()
            await page.mouse.move(tgt_box["x"] + tgt_box["width"] / 2, tgt_box["y"] + tgt_box["height"] / 2)
            await page.mouse.up()
            return True
    except Exception:
        pass

    return False
