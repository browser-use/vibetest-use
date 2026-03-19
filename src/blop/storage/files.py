from __future__ import annotations

import os
from pathlib import Path

# Anchor runs/ to the repo root so paths are stable regardless of CWD.
# files.py lives at src/blop/storage/files.py → 4 levels up = repo root
_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _runs_dir() -> Path:
    """Return the absolute path to the runs/ directory."""
    custom = os.environ.get("BLOP_RUNS_DIR", "")
    if custom:
        return Path(custom)
    return _REPO_ROOT / "runs"


def ensure_run_dirs(run_id: str) -> str:
    """Create per-run subdirectories under runs/ and return the base run dir."""
    base = _runs_dir()
    for sub in ("screenshots", "traces", "console"):
        (base / sub / run_id).mkdir(parents=True, exist_ok=True)
    return str(base / run_id)


def screenshot_path(run_id: str, case_id: str, step: int) -> str:
    dir_ = _runs_dir() / "screenshots" / run_id / case_id
    dir_.mkdir(parents=True, exist_ok=True)
    return str(dir_ / f"step_{step:03d}.png")


def trace_path(run_id: str, case_id: str) -> str:
    dir_ = _runs_dir() / "traces" / run_id
    dir_.mkdir(parents=True, exist_ok=True)
    return str(dir_ / f"{case_id}.zip")


def console_log_path(run_id: str, case_id: str) -> str:
    dir_ = _runs_dir() / "console" / run_id
    dir_.mkdir(parents=True, exist_ok=True)
    return str(dir_ / f"{case_id}.log")


def artifacts_dir(run_id: str) -> str:
    return str((_runs_dir() / run_id).resolve())
