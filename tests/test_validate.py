"""Tests for tools/validate.py — validate_setup tool."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_validate_all_pass_returns_ready():
    """All checks pass → status='ready'."""
    from blop.tools.validate import validate_setup

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    mock_resp = MagicMock()
    mock_resp.status = 200

    mock_urlopen = MagicMock()
    mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
    mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                result = await validate_setup()

    assert result["status"] == "ready"
    assert result["blockers"] == []
    assert result["warnings"] == []
    assert any(c["name"] == "GOOGLE_API_KEY" and c["passed"] for c in result["checks"])
    assert any(c["name"] == "chromium_installed" and c["passed"] for c in result["checks"])
    assert any(c["name"] == "sqlite_db" and c["passed"] for c in result["checks"])


@pytest.mark.asyncio
async def test_validate_missing_api_key_blocked():
    """Missing GOOGLE_API_KEY → status='blocked'."""
    from blop.tools.validate import validate_setup

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    with patch.dict(os.environ, {}, clear=True):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                result = await validate_setup()

    assert result["status"] == "blocked"
    assert any("GOOGLE_API_KEY" in b for b in result["blockers"])
    api_check = next(c for c in result["checks"] if c["name"] == "GOOGLE_API_KEY")
    assert not api_check["passed"]


@pytest.mark.asyncio
async def test_validate_chromium_not_installed_blocked():
    """Playwright launch raises → chromium_installed check fails → status='blocked'."""
    from blop.tools.validate import validate_setup

    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.side_effect = Exception("Chromium executable not found")

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                result = await validate_setup()

    assert result["status"] == "blocked"
    assert any("Chromium" in b for b in result["blockers"])
    chrom_check = next(c for c in result["checks"] if c["name"] == "chromium_installed")
    assert not chrom_check["passed"]


@pytest.mark.asyncio
async def test_validate_db_init_fails_blocked():
    """init_db raises → sqlite_db check fails → status='blocked'."""
    from blop.tools.validate import validate_setup

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", side_effect=Exception("disk I/O error")):
                result = await validate_setup()

    assert result["status"] == "blocked"
    assert any("SQLite" in b or "disk" in b for b in result["blockers"])
    db_check = next(c for c in result["checks"] if c["name"] == "sqlite_db")
    assert not db_check["passed"]


@pytest.mark.asyncio
async def test_validate_app_url_unreachable_warnings():
    """app_url provided but urlopen raises → status='warnings', not 'blocked'."""
    from blop.tools.validate import validate_setup

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
                    result = await validate_setup(app_url="https://unreachable.example.com")

    assert result["status"] == "warnings"
    assert result["blockers"] == []
    assert any("unreachable" in w or "not reachable" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_validate_app_url_reachable_adds_check():
    """app_url provided and reachable → app_url_reachable check added and passed."""
    from blop.tools.validate import validate_setup

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    mock_resp = MagicMock()
    mock_resp.status = 200

    mock_urlopen_ctx = MagicMock()
    mock_urlopen_ctx.__enter__ = MagicMock(return_value=mock_resp)
    mock_urlopen_ctx.__exit__ = MagicMock(return_value=False)

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                with patch("urllib.request.urlopen", return_value=mock_urlopen_ctx):
                    result = await validate_setup(app_url="https://example.com")

    url_check = next((c for c in result["checks"] if c["name"] == "app_url_reachable"), None)
    assert url_check is not None
    assert url_check["passed"]


@pytest.mark.asyncio
async def test_validate_profile_not_found_warning():
    """profile_name provided but not in DB → warning, not blocker."""
    from blop.tools.validate import validate_setup

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                with patch("blop.storage.sqlite.get_auth_profile", new_callable=AsyncMock, return_value=None):
                    result = await validate_setup(profile_name="missing_profile")

    assert result["status"] == "warnings"
    assert any("missing_profile" in w for w in result["warnings"])
    auth_check = next((c for c in result["checks"] if c["name"] == "auth_profile"), None)
    assert auth_check is not None
    assert not auth_check["passed"]


@pytest.mark.asyncio
async def test_validate_profile_valid_passes():
    """profile_name provided and resolves successfully → auth_profile check passes."""
    from blop.tools.validate import validate_setup
    from blop.schemas import AuthProfile

    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=False)
    mock_playwright.chromium.launch.return_value = mock_browser

    mock_profile = AuthProfile(
        profile_name="prod",
        auth_type="env_login",
        login_url="https://example.com/login",
    )

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
            with patch("blop.storage.sqlite.init_db", new_callable=AsyncMock):
                with patch("blop.storage.sqlite.get_auth_profile", new_callable=AsyncMock, return_value=mock_profile):
                    with patch("blop.engine.auth.resolve_storage_state", new_callable=AsyncMock, return_value='{"cookies":[]}'):
                        result = await validate_setup(profile_name="prod")

    assert result["status"] == "ready"
    auth_check = next((c for c in result["checks"] if c["name"] == "auth_profile"), None)
    assert auth_check is not None
    assert auth_check["passed"]
