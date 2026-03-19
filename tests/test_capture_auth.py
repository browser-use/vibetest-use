"""Tests for tools/capture_auth.py."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


@pytest.mark.asyncio
async def test_capture_auth_session_happy_path(tmp_path):
    """Happy path: URL changes to success, storage state saved, profile created."""
    from blop.tools.capture_auth import capture_auth_session

    login_url = "https://app.example.com/login"
    success_url = "https://app.example.com/dashboard"
    blop_dir = str(tmp_path / ".blop")

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    type(mock_page).url = PropertyMock(side_effect=[login_url, login_url, success_url])
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page
    mock_context.close = AsyncMock()

    async def fake_storage_state(path=None):
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"origins": [{"localStorage": []}]}, f)

    mock_context.storage_state = AsyncMock(side_effect=fake_storage_state)

    mock_browser = AsyncMock()
    mock_browser.new_context.return_value = mock_context
    mock_browser.close = AsyncMock()

    mock_playwright = MagicMock()
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=None)

    mock_save_auth_profile = AsyncMock()

    with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
        with patch("blop.storage.sqlite.save_auth_profile", mock_save_auth_profile):
            with patch("blop.tools.capture_auth._BLOP_DIR", blop_dir):
                result = await capture_auth_session(
                    profile_name="test",
                    login_url=login_url,
                    timeout_secs=2,
                )

    assert result["status"] == "captured"
    assert result["profile_name"] == "test"
    assert "storage_state_path" in result
    mock_save_auth_profile.assert_called_once()


@pytest.mark.asyncio
async def test_capture_auth_session_timeout(tmp_path):
    """Timeout: URL never changes from login, returns status=timeout."""
    from blop.tools.capture_auth import capture_auth_session

    login_url = "https://app.example.com/login"
    blop_dir = str(tmp_path / ".blop")

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    type(mock_page).url = PropertyMock(return_value=login_url)
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page
    mock_context.close = AsyncMock()

    mock_browser = AsyncMock()
    mock_browser.new_context.return_value = mock_context
    mock_browser.close = AsyncMock()

    mock_playwright = MagicMock()
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_playwright.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_playwright.__aexit__ = AsyncMock(return_value=None)

    mock_sleep = AsyncMock()

    with patch("playwright.async_api.async_playwright", return_value=mock_playwright):
        with patch("blop.tools.capture_auth.asyncio.sleep", mock_sleep):
            with patch("blop.tools.capture_auth._BLOP_DIR", blop_dir):
                with patch(
                    "blop.tools.capture_auth.asyncio.get_event_loop"
                ) as mock_loop:
                    loop = MagicMock()
                    loop.time.side_effect = [0, 0, 10]
                    mock_loop.return_value = loop
                    result = await capture_auth_session(
                        profile_name="test",
                        login_url=login_url,
                        timeout_secs=1,
                    )

    assert result["status"] == "timeout"
    assert result["profile_name"] == "test"
    assert "No successful login detected" in result["note"]
