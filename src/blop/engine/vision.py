"""Gemini vision fallback for when selectors fail."""
from __future__ import annotations

import base64
import json
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


def _llm():
    from browser_use.llm import ChatGoogle
    return ChatGoogle(
        model="gemini-2.5-flash",
        temperature=0.1,
        api_key=os.getenv("GOOGLE_API_KEY", ""),
        max_output_tokens=256,
    )


async def _screenshot_b64(page: "Page") -> str:
    """Capture JPEG screenshot at 70% quality — ~5x smaller than PNG."""
    img_bytes = await page.screenshot(type="jpeg", quality=70)
    return base64.b64encode(img_bytes).decode()


async def find_element_coords(page: "Page", description: str) -> tuple[int, int] | None:
    """Take a screenshot and ask Gemini where an element is. Returns (x, y) or None."""
    if not os.getenv("GOOGLE_API_KEY"):
        return None

    try:
        from browser_use.llm.messages import UserMessage

        b64 = await _screenshot_b64(page)
        size = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        width = size.get("w", 1280)
        height = size.get("h", 800)

        prompt = f"""Look at this screenshot ({width}x{height} pixels).
Find the element described as: "{description}"
Return ONLY a JSON object with the pixel coordinates of its center:
{{"x": <integer>, "y": <integer>}}
If not found, return {{"x": null, "y": null}}"""

        llm = _llm()
        response = await llm.ainvoke([UserMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ])])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r'\{"x":\s*(\d+|null),\s*"y":\s*(\d+|null)\}', text)
        if m and m.group(1) != "null":
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    return None


async def click_by_vision(page: "Page", description: str) -> None:
    coords = await find_element_coords(page, description)
    if coords:
        await page.mouse.click(coords[0], coords[1])


async def locate_visual_target(page: "Page", description: str, nearby_text: str | None = None) -> tuple[int, int] | None:
    """Find an element using description plus optional nearby_text hint for disambiguation."""
    full_desc = description
    if nearby_text:
        full_desc = f"{description} (near text: {nearby_text})"
    return await find_element_coords(page, full_desc)


async def assert_by_vision(page: "Page", assertion: str) -> bool:
    """Take a screenshot and ask Gemini whether the assertion is true."""
    results = await assert_all_by_vision(page, [assertion])
    return results[0] if results else False


async def assert_all_by_vision(page: "Page", assertions: list[str]) -> list[bool]:
    """Evaluate multiple assertions in a single Gemini call (one screenshot, one LLM call)."""
    if not os.getenv("GOOGLE_API_KEY") or not assertions:
        return [False] * len(assertions)

    try:
        from browser_use.llm import ChatGoogle
        from browser_use.llm.messages import UserMessage

        b64 = await _screenshot_b64(page)
        numbered = "\n".join(f'{i + 1}. "{a}"' for i, a in enumerate(assertions))
        prompt = f"""Look at this screenshot. For each assertion, return true or false.

Assertions:
{numbered}

Return ONLY a JSON array of booleans in the same order (e.g. [true, false, true]):"""

        llm = ChatGoogle(
            model="gemini-2.5-flash",
            temperature=0.1,
            api_key=os.getenv("GOOGLE_API_KEY", ""),
            max_output_tokens=64,
        )
        response = await llm.ainvoke([UserMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ])])
        text = str(response.content) if hasattr(response, "content") else str(response)
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if m:
            result = json.loads(m.group())
            if isinstance(result, list):
                padded = list(result) + [False] * (len(assertions) - len(result))
                return [bool(v) for v in padded[:len(assertions)]]
    except Exception:
        pass

    return [False] * len(assertions)
