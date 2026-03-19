from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from blop.engine import auth as auth_engine
from blop.schemas import AuthProfile, AuthProfileResult
from blop.storage import sqlite

_VALID_AUTH_TYPES = ("env_login", "storage_state", "cookie_json")


async def save_auth_profile(
    profile_name: str,
    auth_type: str,
    login_url: Optional[str] = None,
    username_env: Optional[str] = "TEST_USERNAME",
    password_env: Optional[str] = "TEST_PASSWORD",
    storage_state_path: Optional[str] = None,
    cookie_json_path: Optional[str] = None,
    user_data_dir: Optional[str] = None,
) -> dict:
    try:
        profile = AuthProfile(
            profile_name=profile_name,
            auth_type=auth_type,
            login_url=login_url,
            username_env=username_env,
            password_env=password_env,
            storage_state_path=storage_state_path,
            cookie_json_path=cookie_json_path,
            user_data_dir=user_data_dir,
        )
    except ValidationError:
        return {
            "error": f"Invalid auth_type '{auth_type}'. Must be one of: {', '.join(_VALID_AUTH_TYPES)}"
        }

    storage_path: Optional[str] = None
    try:
        storage_path = await auth_engine.resolve_storage_state(profile)
    except Exception:
        pass

    await sqlite.save_auth_profile(profile, storage_path)

    note = "Credentials are read from environment variables at run time"
    if auth_type != "env_login":
        note = f"Auth profile saved with type '{auth_type}'"

    return AuthProfileResult(
        profile_name=profile_name,
        auth_type=auth_type,
        status="saved",
        note=note,
    ).model_dump()
