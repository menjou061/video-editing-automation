"""Read-only draft/import diagnostics."""
from __future__ import annotations

import shutil
import json
from pathlib import Path

from . import capacity, draft_safety
from .platform_env import environment_report, is_jianying_running


def _agency_facts(draft_path: Path) -> dict:
    """Extract only observable agency metadata; tolerate private/encrypted drafts."""
    paths = [draft_path / "draft_agency_info.json", draft_path / "agency_info.json"]
    raw = None
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            break
        except (OSError, ValueError, TypeError):
            continue
    if not isinstance(raw, dict):
        return {"available": False}
    hits: list[dict] = []

    def walk(value):
        if isinstance(value, dict):
            path = value.get("path") or value.get("file_path") or value.get("material_path")
            if isinstance(path, str) and path.lower().endswith((".mov", ".mp4", ".mkv", ".avi")):
                hits.append({"path": path, "width": value.get("width"), "height": value.get("height")})
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(raw)
    unique = {item["path"]: item for item in hits}
    resolutions = sorted({f"{x.get('width')}x{x.get('height')}" for x in unique.values()
                          if x.get("width") and x.get("height")})
    return {"available": True, "use_converter": raw.get("use_converter"),
            "unique_video_sources": len(unique), "resolutions": resolutions}


def diagnose_timeline(draft_path: Path, *, expect_external_package: bool = False) -> dict:
    draft_path = Path(draft_path).expanduser().resolve()
    issues = draft_safety.post_write_validate(draft_path) if draft_path.exists() else ["草稿目录不存在"]
    media_dir = draft_path / "media"
    media_files = list(media_dir.rglob("*") ) if media_dir.exists() else []
    media_bytes = sum(p.stat().st_size for p in media_files if p.is_file())
    disk = shutil.disk_usage(draft_path if draft_path.exists() else Path.cwd())
    env = environment_report()
    profile = capacity.load_profile()
    state, host = capacity.profile_state(profile, storage_path=Path(env.get("drafts_root") or Path.cwd()))
    package = env.get("active_package", {})
    status = "DESKTOP_IMPORT_BLOCKED" if issues else "DIAGNOSIS_OK"
    if expect_external_package and not package.get("active_external_package"):
        status = "PACKAGE_NOT_ACTIVE"
    return {
        "status": status,
        "draft_path": str(draft_path),
        "draft_exists": draft_path.exists(),
        "issues": issues,
        "media": {"files": len([p for p in media_files if p.is_file()]),
                  "bytes": media_bytes},
        "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
        "jianying_running": is_jianying_running(),
        "agency": _agency_facts(draft_path),
        "capacity": {"profile_state": state, "profile": profile, "host": host},
        "active_package": package,
        "environment": env,
        "note": "诊断结果描述可观测状态；素材数量或分辨率不是已确认根因，需结合日志与复现结果判断。",
    }
