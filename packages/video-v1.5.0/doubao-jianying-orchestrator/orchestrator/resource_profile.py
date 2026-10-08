"""Bounded resource profiles that reduce contention with an interactive editor."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .platform_env import is_jianying_running


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    analysis_workers: int
    tts_concurrency: int
    staging_workers: int
    process_priority: str


def choose_profile(manifest: dict) -> ResourceProfile:
    requested = str(manifest.get("execution_profile", "")).strip().lower()
    if requested not in {"performance", "background_friendly"}:
        requested = "background_friendly" if is_jianying_running() else "performance"
    if requested == "background_friendly":
        return ResourceProfile(requested, 1, 1, 1, "below_normal")
    return ResourceProfile(requested, 2, 3, 2, "normal")


def apply_process_priority(profile: ResourceProfile) -> bool:
    """Best effort priority lowering; never fails a media task."""
    if profile.process_priority != "below_normal":
        return False
    try:
        if sys.platform.startswith("win"):
            import psutil  # type: ignore
            psutil.Process(os.getpid()).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return True
        if hasattr(os, "nice"):
            os.nice(5)
            return True
    except Exception:
        return False
    return False
