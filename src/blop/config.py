import logging
import os
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

# Suppress all logging before any imports
logging.disable(logging.CRITICAL)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "CRITICAL")

# Load .env from the repo root (two levels up from this file: src/blop/config.py → repo root)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)  # override=False: explicit env vars take precedence
except Exception:
    pass

GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
APP_BASE_URL: str = os.getenv("APP_BASE_URL", "")
LOGIN_URL: str = os.getenv("LOGIN_URL", "")

TEST_USERNAME: str = os.getenv("TEST_USERNAME", "")
TEST_PASSWORD: str = os.getenv("TEST_PASSWORD", "")
STORAGE_STATE_PATH: str = os.getenv("STORAGE_STATE_PATH", "")
COOKIE_JSON_PATH: str = os.getenv("COOKIE_JSON_PATH", "")


class ExplorationTuning(TypedDict):
    network_idle_wait_secs: float
    spa_settle_ms: int
    agent_max_failures: int
    agent_max_actions_per_step: int
    discover_max_pages: int


_EXPLORATION_PRESETS: dict[str, ExplorationTuning] = {
    "default": {
        "network_idle_wait_secs": 2.0,
        "spa_settle_ms": 1500,
        "agent_max_failures": 5,
        "agent_max_actions_per_step": 6,
        "discover_max_pages": 10,
    },
    "saas_marketing": {
        # Better for tool-heavy SPAs with async rendering and cross-route transitions.
        "network_idle_wait_secs": 3.5,
        "spa_settle_ms": 2200,
        "agent_max_failures": 8,
        "agent_max_actions_per_step": 8,
        "discover_max_pages": 20,
    },
}


def get_exploration_tuning() -> ExplorationTuning:
    profile_name = os.getenv("BLOP_EXPLORATION_PROFILE", "default").strip().lower()
    preset = _EXPLORATION_PRESETS.get(profile_name, _EXPLORATION_PRESETS["default"]).copy()
    preset["network_idle_wait_secs"] = float(
        os.getenv("BLOP_NETWORK_IDLE_WAIT", str(preset["network_idle_wait_secs"]))
    )
    preset["spa_settle_ms"] = int(
        os.getenv("BLOP_SPA_SETTLE_MS", str(preset["spa_settle_ms"]))
    )
    preset["agent_max_failures"] = int(
        os.getenv("BLOP_AGENT_MAX_FAILURES", str(preset["agent_max_failures"]))
    )
    preset["agent_max_actions_per_step"] = int(
        os.getenv("BLOP_AGENT_MAX_ACTIONS_PER_STEP", str(preset["agent_max_actions_per_step"]))
    )
    preset["discover_max_pages"] = int(
        os.getenv("BLOP_DISCOVERY_MAX_PAGES", str(preset["discover_max_pages"]))
    )
    return preset

# Resolve DB path: if relative, anchor to repo root so the server works from any CWD
_raw_db_path = os.getenv("BLOP_DB_PATH", ".blop/runs.db")
if not os.path.isabs(_raw_db_path):
    _REPO_ROOT = Path(__file__).parent.parent.parent
    BLOP_DB_PATH: str = str(_REPO_ROOT / _raw_db_path)
else:
    BLOP_DB_PATH: str = _raw_db_path
BLOP_HEADLESS: bool = os.getenv("BLOP_HEADLESS", "true").lower() == "true"
BLOP_MAX_STEPS: int = int(os.getenv("BLOP_MAX_STEPS", "50"))

# Multi-LLM provider support
BLOP_LLM_PROVIDER: str = os.getenv("BLOP_LLM_PROVIDER", "google")
BLOP_LLM_MODEL: str = os.getenv("BLOP_LLM_MODEL", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# Extended thinking budget (0 = disabled)
BLOP_THINKING_BUDGET: int = int(os.getenv("BLOP_THINKING_BUDGET", "0"))

# Storage archival thresholds (days)
BLOP_ARCHIVE_RUNS_AFTER_DAYS: int = int(os.getenv("BLOP_ARCHIVE_RUNS_AFTER_DAYS", "30"))
BLOP_ARCHIVE_TELEMETRY_AFTER_DAYS: int = int(os.getenv("BLOP_ARCHIVE_TELEMETRY_AFTER_DAYS", "90"))


def check_llm_api_key() -> tuple[bool, str]:
    """Return (has_key, key_name) based on the configured LLM provider."""
    provider = BLOP_LLM_PROVIDER.lower()
    if provider == "anthropic":
        return (bool(ANTHROPIC_API_KEY), "ANTHROPIC_API_KEY")
    if provider == "openai":
        return (bool(OPENAI_API_KEY), "OPENAI_API_KEY")
    return (bool(GOOGLE_API_KEY), "GOOGLE_API_KEY")


def validate_app_url(url: str) -> str | None:
    """Return an error message if *url* is not a valid HTTP(S) URL, else None."""
    if not url or not url.strip():
        return "app_url is required"
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return f"app_url must use http or https scheme, got '{parsed.scheme or '(none)'}'"
    if not parsed.netloc:
        return "app_url must include a host (e.g. https://example.com)"
    return None


# SPA / complex workflow tuning
_EXPLORATION_TUNING = get_exploration_tuning()
# How long to wait for network idle after page load (seconds).
BLOP_NETWORK_IDLE_WAIT: float = _EXPLORATION_TUNING["network_idle_wait_secs"]
# Default settle wait after SPA navigation (ms) — used by wait_for_spa_ready.
BLOP_SPA_SETTLE_MS: int = _EXPLORATION_TUNING["spa_settle_ms"]
BLOP_AGENT_MAX_FAILURES: int = _EXPLORATION_TUNING["agent_max_failures"]
BLOP_AGENT_MAX_ACTIONS_PER_STEP: int = _EXPLORATION_TUNING["agent_max_actions_per_step"]
BLOP_DISCOVERY_MAX_PAGES: int = _EXPLORATION_TUNING["discover_max_pages"]
