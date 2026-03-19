"""Artifact path helpers."""
from __future__ import annotations

import os


def artifacts_base(run_id: str) -> str:
    return os.path.abspath(os.path.join("runs", run_id))


def list_screenshots(run_id: str) -> list[str]:
    base = os.path.join("runs", "screenshots", run_id)
    if not os.path.exists(base):
        return []
    results = []
    for root, _, files in os.walk(base):
        for f in sorted(files):
            if f.endswith(".png"):
                results.append(os.path.join(root, f))
    return results
