import os
import time
from typing import Optional

from .models import AuthConfig

_auth_cache: dict[str, dict] = {}


async def resolve_storage_state(config: AuthConfig) -> Optional[str]:
    """Returns path to a valid Playwright storage_state.json, or None."""
    if config.auth_type == "env_login":
        return await _env_login(config)
    elif config.auth_type == "storage_state":
        return _storage_state(config)
    elif config.auth_type == "cookie_json":
        return await _cookie_json(config)
    return None


async def _env_login(config: AuthConfig) -> Optional[str]:
    """Log in using credentials from env vars and save storage state."""
    cache_entry = _auth_cache.get(config.auth_config_id)
    if cache_entry and time.time() < cache_entry["expires"] and os.path.exists(cache_entry["path"]):
        return cache_entry["path"]

    username = os.getenv(config.username_env)
    password = os.getenv(config.password_env)
    login_url = config.login_url or os.getenv("TEST_AUTH_URL")

    if not (username and password and login_url):
        return None

    from playwright.async_api import async_playwright

    os.makedirs(".vibetest", exist_ok=True)
    state_path = os.path.join(".vibetest", f"auth_state_{config.auth_config_id}.json")
    username_selector = os.getenv("TEST_USERNAME_SELECTOR", "#email")
    password_selector = os.getenv("TEST_PASSWORD_SELECTOR", "#password")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(login_url)
        await page.fill(username_selector, username)
        await page.fill(password_selector, password)
        await page.press(password_selector, "Enter")
        await page.wait_for_load_state("networkidle")
        await context.storage_state(path=state_path)
        await browser.close()

    _auth_cache[config.auth_config_id] = {
        "path": state_path,
        "expires": time.time() + 3600,
    }
    return state_path


def _storage_state(config: AuthConfig) -> Optional[str]:
    """Return storage_state_path if it exists."""
    if config.storage_state_path and os.path.exists(config.storage_state_path):
        return config.storage_state_path
    return None


async def _cookie_json(config: AuthConfig) -> Optional[str]:
    """Load cookies from JSON file, inject into Playwright context, save storage state."""
    if not config.cookie_json_path or not os.path.exists(config.cookie_json_path):
        return None

    import json
    from playwright.async_api import async_playwright

    with open(config.cookie_json_path) as f:
        cookies = json.load(f)

    os.makedirs(".vibetest", exist_ok=True)
    state_path = os.path.join(".vibetest", f"auth_state_{config.auth_config_id}.json")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        await context.storage_state(path=state_path)
        await browser.close()

    _auth_cache[config.auth_config_id] = {
        "path": state_path,
        "expires": time.time() + 3600,
    }
    return state_path
