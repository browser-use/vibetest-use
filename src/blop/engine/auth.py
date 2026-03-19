"""Auth engine — ported from vibetest/auth.py, adapted for AuthProfile."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from blop.schemas import AuthProfile

# Anchor .blop/ to the repo root so paths work regardless of the server's CWD.
# auth.py lives at src/blop/engine/auth.py → 4 levels up = repo root.
_BLOP_DIR: str = str(Path(__file__).parent.parent.parent.parent / ".blop")

_auth_cache: dict[str, dict] = {}
# Per-profile lock to prevent concurrent logins racing each other
_login_locks: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _login_locks:
        _login_locks[key] = asyncio.Lock()
    return _login_locks[key]


async def resolve_storage_state(profile: AuthProfile) -> Optional[str]:
    """Return path to a valid Playwright storage_state.json, or None."""
    if profile.auth_type == "env_login":
        return await _env_login(profile)
    elif profile.auth_type == "storage_state":
        return _storage_state(profile)
    elif profile.auth_type == "cookie_json":
        return await _cookie_json(profile)
    return None


async def _env_login(profile: AuthProfile) -> Optional[str]:
    cache_key = profile.profile_name
    os.makedirs(_BLOP_DIR, exist_ok=True)
    state_path = os.path.join(_BLOP_DIR, f"auth_state_{cache_key}.json")

    # Fast path: in-memory cache hit
    entry = _auth_cache.get(cache_key)
    if entry and time.time() < entry["expires"] and os.path.exists(entry["path"]):
        return entry["path"]

    # Serialize concurrent callers — only one login per profile at a time
    async with _get_lock(cache_key):
        # Re-check after acquiring lock (another coroutine may have just finished)
        entry = _auth_cache.get(cache_key)
        if entry and time.time() < entry["expires"] and os.path.exists(entry["path"]):
            return entry["path"]

        username_env = profile.username_env or "TEST_USERNAME"
        password_env = profile.password_env or "TEST_PASSWORD"
        username = os.getenv(username_env)
        password = os.getenv(password_env)
        login_url = profile.login_url or os.getenv("LOGIN_URL") or os.getenv("TEST_AUTH_URL")

        # No credentials — fall back to existing state file if available
        if not (username and password and login_url):
            if os.path.exists(state_path):
                return state_path
            return None

        from playwright.async_api import async_playwright

        username_selector = os.getenv("TEST_USERNAME_SELECTOR", "")
        password_selector = os.getenv("TEST_PASSWORD_SELECTOR", "")

        _user_selectors = [s for s in [username_selector] if s] + [
            "input[name='username']", "input[name='email']",
            "input[type='email']", "#email", "input[placeholder*='email' i]",
            "input[placeholder*='username' i]",
        ]
        _pass_selectors = [s for s in [password_selector] if s] + [
            "input[name='password']", "input[type='password']", "#password",
        ]

        async def _try_fill(page, selectors: list[str], value: str) -> str:
            for sel in selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    if el:
                        await el.fill(value)
                        return sel
                except Exception:
                    continue
            raise RuntimeError(f"Could not find input with any of: {selectors}")

        try:
            async with async_playwright() as p:
                # Use a persistent context when user_data_dir is set — this prevents
                # OAuth providers (Google, GitHub) from detecting a fresh browser context
                # as a bot and blocking automated login.
                if profile.user_data_dir:
                    os.makedirs(profile.user_data_dir, exist_ok=True)
                    context = await p.chromium.launch_persistent_context(
                        profile.user_data_dir, headless=True,
                    )
                    browser = None
                else:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context()

                try:
                    page = await context.new_page()
                    await page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
                    await _try_fill(page, _user_selectors, username)
                    working_pass_sel = await _try_fill(page, _pass_selectors, password)
                    await page.press(working_pass_sel, "Enter")

                    # Wait for navigation away from the login page.
                    # Using a URL-polling loop handles multi-step OAuth redirects
                    # (login → IdP → MFA → callback → app) that fool networkidle.
                    _login_path_kws = ("login", "signin", "sign-in", "oauth", "auth/")
                    for _ in range(40):  # up to 20 seconds in 500ms steps
                        await asyncio.sleep(0.5)
                        current_url = page.url.lower()
                        if not any(kw in current_url for kw in _login_path_kws):
                            break
                    else:
                        # Fallback: just wait for network to settle
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass

                    # Validate login succeeded: check URL and page text
                    current_url = page.url

                    page_text = ""
                    try:
                        page_text = (await page.evaluate("() => document.body.innerText")).lower()
                    except Exception:
                        pass
                    _login_error_kws = (
                        "invalid", "incorrect", "failed", "wrong password",
                        "no account", "not found", "error signing in",
                    )
                    page_has_error = any(kw in page_text for kw in _login_error_kws)

                    login_failed = (
                        login_url.rstrip("/") in current_url.rstrip("/")
                        or "login" in current_url.lower()
                        or "auth" in current_url.lower()
                        or "signin" in current_url.lower()
                        or page_has_error
                    )
                    if login_failed:
                        if os.path.exists(state_path):
                            return state_path
                        return None

                    await context.storage_state(path=state_path)
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    if browser:
                        try:
                            await browser.close()
                        except Exception:
                            pass

            # Cache update inside the lock — guaranteed to run only on successful login
            _auth_cache[cache_key] = {"path": state_path, "expires": time.time() + 3600}
            return state_path

        except Exception:
            # Login threw — fall back to existing state file if available
            if os.path.exists(state_path):
                return state_path
            return None


def _storage_state(profile: AuthProfile) -> Optional[str]:
    path = profile.storage_state_path or os.getenv("STORAGE_STATE_PATH")
    if not path:
        return None
    # Resolve relative paths against the repo root (where .blop/ lives),
    # not the server process's CWD.
    if not os.path.isabs(path):
        path = str(Path(_BLOP_DIR).parent / path)
    if os.path.exists(path):
        return path
    return None


async def validate_auth_session(
    storage_state_path: str,
    app_url: str,
    auth_redirect_patterns: tuple[str, ...] = ("/login", "/signin", "/sign-in", "/auth"),
) -> bool:
    """Open headless browser with storage_state, navigate to app_url, return True if not redirected to auth."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=storage_state_path)
            page = await context.new_page()
            await page.goto(app_url, wait_until="domcontentloaded", timeout=15000)
            current_url = page.url.lower()
            await context.close()
            await browser.close()
            return not any(pat in current_url for pat in auth_redirect_patterns)
    except Exception:
        return False


async def _cookie_json(profile: AuthProfile) -> Optional[str]:
    cookie_path = profile.cookie_json_path or os.getenv("COOKIE_JSON_PATH")
    if not cookie_path or not os.path.exists(cookie_path):
        return None

    from playwright.async_api import async_playwright

    with open(cookie_path) as f:
        cookies = json.load(f)

    os.makedirs(_BLOP_DIR, exist_ok=True)
    state_path = os.path.join(_BLOP_DIR, f"auth_state_{profile.profile_name}.json")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        await context.storage_state(path=state_path)
        await browser.close()

    _auth_cache[profile.profile_name] = {"path": state_path, "expires": time.time() + 3600}
    return state_path
