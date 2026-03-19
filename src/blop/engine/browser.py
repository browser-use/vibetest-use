"""BrowserProfile factory — ported from vibetest/agents.py."""
from __future__ import annotations

from browser_use import BrowserProfile
from blop.config import BLOP_NETWORK_IDLE_WAIT

# How long to wait for network idle after page load before the agent starts acting.
# Heavy SPA/WebGL apps (video editors, design tools) need more time to initialize.
# Controlled by exploration tuning profile / env override.
_NETWORK_IDLE_WAIT = BLOP_NETWORK_IDLE_WAIT


def make_browser_profile(headless: bool, storage_state: str | None = None, user_data_dir: str | None = None) -> BrowserProfile:
    browser_args = [
        # Use software WebGL so canvas/WebGL apps (e.g. video editors) render correctly.
        # --disable-gpu breaks WebGL; swiftshader keeps it working without real GPU.
        "--use-gl=swiftshader",
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
        args=browser_args,
        ignore_default_args=["--enable-automation"],
        wait_for_network_idle_page_load_time=_NETWORK_IDLE_WAIT,
        wait_between_actions=0.5,  # slightly more breathing room for SPA renders
    )
    if user_data_dir:
        kwargs["user_data_dir"] = user_data_dir
    if storage_state:
        kwargs["storage_state"] = storage_state
    return BrowserProfile(**kwargs)
