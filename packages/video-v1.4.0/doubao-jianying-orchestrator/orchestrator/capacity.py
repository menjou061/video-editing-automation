"""Per-host material capacity profiles and pre-write admission control."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable

from .platform_env import jianying_installation_report


PROFILE_VERSION = 1
MIN_SAFE_SOURCE_LIMIT = 13
# Resource policy for an interactive JianYing session.  Between the soft and
# critical limits we keep working at the lowest concurrency; only the
# critical limit blocks a task because continuing can make the editor hang or
# leave a partial draft.
SOFT_MEMORY_LIMIT = 1024 * 1024 * 1024
CRITICAL_MEMORY_LIMIT = 256 * 1024 * 1024


def _memory_bytes() -> tuple[int, int]:
    """Return total and currently available physical memory without psutil."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except Exception:
            return 0, 0
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        return int(page * os.sysconf("SC_PHYS_PAGES")), int(page * os.sysconf("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0, 0


def host_fingerprint(storage_path: Path | None = None) -> dict:
    total_memory, available_memory = _memory_bytes()
    install = jianying_installation_report()
    root = Path(storage_path or Path.home()).resolve()
    disk = shutil.disk_usage(root)
    stable = {
        "platform": sys.platform,
        "machine": platform.machine(),
        "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "cpu_count": os.cpu_count() or 0,
        "memory_total_bytes": total_memory,
        "jianying_version": install.get("version"),
        "jianying_executable": install.get("executable"),
        "storage_anchor": str(root.anchor or root),
    }
    stable_hash = hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()
    return {**stable, "fingerprint": stable_hash, "memory_available_bytes": available_memory,
            "disk_free_bytes": disk.free, "disk_total_bytes": disk.total}


def default_profile_path() -> Path:
    override = os.environ.get("JY_CAPACITY_PROFILE", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "Doubao" / "jianying-orchestrator" / "host_capacity.json"
    return Path.home() / ".doubao-jianying" / "host_capacity.json"


def load_profile(path: Path | None = None) -> dict | None:
    target = Path(path or default_profile_path())
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def save_profile(profile: dict, path: Path | None = None) -> Path:
    target = Path(path or default_profile_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def create_profile(*, last_passing_count: int, storage_path: Path, samples: list[dict]) -> dict:
    fingerprint = host_fingerprint(storage_path)
    safe_limit = max(MIN_SAFE_SOURCE_LIMIT, int(last_passing_count * 0.8))
    return {
        "schema": PROFILE_VERSION,
        "host": fingerprint,
        "last_passing_count": int(last_passing_count),
        "safe_source_limit": safe_limit,
        "samples": samples,
    }


def profile_state(profile: dict | None, *, storage_path: Path) -> tuple[str, dict]:
    current = host_fingerprint(storage_path)
    # Do not begin an expensive task merely because a profile is absent. The
    # current machine state is a harder safety bound than calibration history.
    if current["memory_available_bytes"] and current["memory_available_bytes"] < CRITICAL_MEMORY_LIMIT:
        return "resource_pressure", current
    if not profile:
        return "missing", current
    saved = profile.get("host", {})
    if profile.get("schema") != PROFILE_VERSION or saved.get("fingerprint") != current["fingerprint"]:
        return "stale", current
    saved_free = int(saved.get("disk_free_bytes", 0) or 0)
    current_free = int(current.get("disk_free_bytes", 0) or 0)
    # Normal temp files change free space. Recalibrate only after a meaningful
    # reduction, where the prior throughput result no longer represents this
    # machine's available I/O headroom.
    if saved_free and current_free < min(saved_free * 0.6, saved_free - 5 * 1024 ** 3):
        return "stale", current
    return "ready", current


def resource_snapshot(storage_path: Path | None = None) -> dict:
    """A portable, best-effort telemetry sample used by calibration/task logs."""
    total, available = _memory_bytes()
    install = jianying_installation_report()
    root = Path(storage_path or Path.home())
    disk = shutil.disk_usage(root)
    sample = {
        "ts": time.time(),
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "jianying_processes": install.get("running_processes", 0),
        "jianying_version": install.get("version"),
        "cpu_percent": None,
        "process_rss_bytes": None,
    }
    try:
        import psutil  # type: ignore
        proc = psutil.Process(os.getpid())
        sample["process_rss_bytes"] = proc.memory_info().rss
        sample["cpu_percent"] = psutil.cpu_percent(interval=0.05)
    except Exception:
        pass
    return sample


def memory_pressure(storage_path: Path | None = None, *, minimum_bytes: int = CRITICAL_MEMORY_LIMIT) -> dict | None:
    sample = resource_snapshot(storage_path)
    available = sample["memory_available_bytes"]
    if available and available < minimum_bytes:
        return {"status": "SYSTEM_RESOURCE_PRESSURE", "available_memory_bytes": available,
                "minimum_memory_bytes": minimum_bytes,
                "message": "当前可用内存不足 256 MiB，为避免影响剪映编辑，任务已暂停。释放内存后重新运行即可。",
                "sample": sample}
    return None


def summarize_sources(paths: Iterable[str | Path]) -> dict:
    unique: list[Path] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw in paths:
        path = Path(raw).resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
    return {"unique_source_count": len(unique), "total_bytes": total_bytes,
            "paths": [str(p) for p in unique]}


def admission_result(paths: Iterable[str | Path], *, storage_path: Path,
                     profile_path: Path | None = None) -> tuple[dict | None, dict]:
    summary = summarize_sources(paths)
    profile = load_profile(profile_path)
    state, host = profile_state(profile, storage_path=storage_path)
    detail = {"sources": summary, "profile_state": state, "profile_path": str(profile_path or default_profile_path()),
              "host": host, "profile": profile}
    if state in {"missing", "stale"}:
        return ({"status": "CAPACITY_CALIBRATION_REQUIRED",
                 "message": "此电脑尚未完成容量校准，已停止生成以避免影响剪映。请先运行容量校准。",
                 "source_count": summary["unique_source_count"], "profile_state": state}, detail)
    if state == "resource_pressure":
        return ({"status": "SYSTEM_RESOURCE_PRESSURE",
                 "message": "当前可用内存不足 256 MiB，为避免影响剪映编辑，任务已暂停。释放内存后重新运行即可。",
                 "source_count": summary["unique_source_count"]}, detail)
    limit = int(profile.get("safe_source_limit", 0))
    if summary["unique_source_count"] > limit:
        return ({"status": "MATERIAL_BATCH_LIMIT_EXCEEDED",
                 "message": f"本机当前最多处理 {limit} 段素材；本次输入 {summary['unique_source_count']} 段。请保留 {limit} 段以下后重新提交。",
                 "source_count": summary["unique_source_count"], "safe_source_limit": limit}, detail)
    return None, detail
