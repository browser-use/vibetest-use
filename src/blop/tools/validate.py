"""validate_setup tool — checks all preconditions before the user runs anything."""
from __future__ import annotations

import os
from typing import Optional


async def validate_setup(
    app_url: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> dict:
    checks: list[dict] = []
    blockers: list[str] = []
    warnings: list[str] = []

    # 1. LLM API key (provider-aware)
    provider = os.getenv("BLOP_LLM_PROVIDER", "google").lower()
    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        key_name = "ANTHROPIC_API_KEY"
        key_hint = "Anthropic Claude agent and flow planning"
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        key_name = "OPENAI_API_KEY"
        key_hint = "OpenAI agent and flow planning"
    else:
        api_key = os.getenv("GOOGLE_API_KEY", "")
        key_name = "GOOGLE_API_KEY"
        key_hint = "Gemini agent and flow planning"
    if api_key:
        checks.append({"name": key_name, "passed": True, "message": f"Set and non-empty (provider: {provider})"})
    else:
        checks.append({"name": key_name, "passed": False, "message": f"Not set — required for {key_hint}"})
        blockers.append(f"{key_name} environment variable is not set")

    # 2. Chromium installed
    chromium_ok = False
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
        chromium_ok = True
    except Exception as exc:
        chromium_ok = False
        err = str(exc)

    if chromium_ok:
        checks.append({"name": "chromium_installed", "passed": True, "message": "Chromium launches successfully"})
    else:
        checks.append({"name": "chromium_installed", "passed": False, "message": "Chromium not found — run: playwright install chromium"})
        blockers.append("Chromium not installed. Run: playwright install chromium")

    # 3. SQLite DB accessible
    db_ok = False
    try:
        from blop.storage.sqlite import init_db
        await init_db()
        db_ok = True
    except Exception as exc:
        db_ok = False
        db_err = str(exc)

    if db_ok:
        checks.append({"name": "sqlite_db", "passed": True, "message": "Database initialized and accessible"})
    else:
        checks.append({"name": "sqlite_db", "passed": False, "message": f"DB init failed: {db_err}"})
        blockers.append(f"SQLite database initialization failed: {db_err}")

    # 4. app_url reachable (optional)
    if app_url:
        import asyncio
        url_ok = False
        try:
            import urllib.request
            req = urllib.request.Request(app_url, headers={"User-Agent": "blop-validate/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                url_ok = resp.status < 500
            checks.append({"name": "app_url_reachable", "passed": True, "message": f"{app_url} responded with HTTP {resp.status}"})
        except Exception as exc:
            checks.append({"name": "app_url_reachable", "passed": False, "message": f"{app_url} not reachable: {exc}"})
            warnings.append(f"app_url {app_url!r} is not reachable. Ensure the app is running.")

    # 5. Auth profile valid (optional)
    if profile_name:
        try:
            from blop.storage.sqlite import get_auth_profile
            from blop.engine.auth import resolve_storage_state, validate_auth_session
            profile = await get_auth_profile(profile_name)
            if profile is None:
                checks.append({"name": "auth_profile", "passed": False, "message": f"Profile '{profile_name}' not found — run save_auth_profile first"})
                warnings.append(f"Auth profile '{profile_name}' not found")
            else:
                state = await resolve_storage_state(profile)
                if not state:
                    checks.append({"name": "auth_profile", "passed": False, "message": f"Profile '{profile_name}' could not resolve storage state"})
                    warnings.append(f"Auth profile '{profile_name}' exists but storage state could not be resolved")
                elif profile.auth_type == "storage_state" and app_url:
                    # Validate the session is still live, not just that the file exists
                    session_valid = await validate_auth_session(state, app_url)
                    if session_valid:
                        checks.append({"name": "auth_profile", "passed": True, "message": f"Profile '{profile_name}' resolved and session is active"})
                    else:
                        checks.append({"name": "auth_profile", "passed": False, "message": f"Profile '{profile_name}' storage state exists but session has expired — re-run capture_auth_session"})
                        warnings.append(f"Auth session for '{profile_name}' has expired")
                else:
                    checks.append({"name": "auth_profile", "passed": True, "message": f"Profile '{profile_name}' resolved successfully"})
        except Exception as exc:
            checks.append({"name": "auth_profile", "passed": False, "message": f"Auth profile check failed: {exc}"})
            warnings.append(f"Auth profile validation error: {exc}")

    if blockers:
        status = "blocked"
    elif warnings:
        status = "warnings"
    else:
        status = "ready"

    return {
        "status": status,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }
