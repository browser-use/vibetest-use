"""capture_auth_session tool — interactive OAuth capture via headed browser."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

_BLOP_DIR: str = str(Path(__file__).parent.parent.parent.parent / ".blop")


async def capture_auth_session(
    profile_name: str,
    login_url: str,
    success_url_pattern: Optional[str] = None,
    timeout_secs: int = 120,
    user_data_dir: Optional[str] = None,
) -> dict:
    """Launch headed browser, wait for user to complete OAuth/MFA, then save storage state.

    Opens a visible browser window at login_url. Polls the URL every 500ms until:
    - The URL matches success_url_pattern (if provided), or
    - The URL changes away from the login URL (if no pattern given).

    On success, saves Playwright storage state and upserts an auth profile in SQLite.
    """
    from playwright.async_api import async_playwright
    from blop.schemas import AuthProfile
    from blop.storage import sqlite

    from urllib.parse import urlparse

    os.makedirs(_BLOP_DIR, exist_ok=True)
    state_path = os.path.join(_BLOP_DIR, f"auth_state_{profile_name}.json")
    login_domain = urlparse(login_url).netloc  # e.g. "app.rendley.com"

    captured = False

    async with async_playwright() as p:
        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
            )
            browser = None
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()

        try:
            page = await context.new_page()
            await page.goto(login_url, wait_until="networkidle", timeout=15000)
            # Record the settled URL (accounts for redirects like /login → /login)
            settled_login_url = page.url.rstrip("/")

            deadline = asyncio.get_event_loop().time() + timeout_secs
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.5)
                try:
                    current_url = page.url
                    current_domain = urlparse(current_url).netloc
                    # Only consider URLs back on the original app domain — ignore
                    # external OAuth provider pages (accounts.google.com, github.com, etc.)
                    on_app_domain = (
                        current_domain == login_domain
                        or current_domain.endswith("." + login_domain)
                    )
                    if success_url_pattern:
                        if success_url_pattern in current_url and on_app_domain:
                            captured = True
                            break
                    else:
                        # URL changed AND we're back on the app domain (not mid-OAuth)
                        if on_app_domain and current_url.rstrip("/") != settled_login_url:
                            captured = True
                            break
                except Exception:
                    pass

            if captured:
                # Wait for the auth callback to fully settle (session cookies are set
                # after the OAuth callback handler runs — don't capture mid-redirect)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await context.storage_state(path=state_path)
                # Sanitize: Playwright occasionally saves localStorage as undefined
                # for origins that use it; Playwright's restore path requires an array.
                try:
                    import json as _json
                    with open(state_path) as _f:
                        _state = _json.load(_f)
                    for _origin in _state.get("origins", []):
                        if not isinstance(_origin.get("localStorage"), list):
                            _origin["localStorage"] = []
                    with open(state_path, "w") as _f:
                        _json.dump(_state, _f)
                except Exception:
                    pass
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

    if not captured:
        return {
            "status": "timeout",
            "profile_name": profile_name,
            "note": (
                "No successful login detected within timeout. "
                "Check success_url_pattern or increase timeout_secs."
            ),
        }

    # Upsert auth profile in SQLite as storage_state type
    profile = AuthProfile(
        profile_name=profile_name,
        auth_type="storage_state",
        storage_state_path=state_path,
        user_data_dir=user_data_dir,
    )
    await sqlite.save_auth_profile(profile, state_path)

    return {
        "status": "captured",
        "profile_name": profile_name,
        "storage_state_path": state_path,
        "note": "Auth state saved. Pass profile_name to record_test_flow and run_regression_test.",
    }
