"""Safe handoff for JianYing encrypted drafts.

The skill never decrypts, rewrites, or copies private JianYing ciphertext.  This
module only opens the official client and verifies a user-created copy when the
client has finished its own migration.
"""
from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

from .draft_safety import inspect_draft_format
from .platform_env import IS_WIN, environment_report, find_drafts_root


def _tree_snapshot(root: Path) -> dict:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            files.append({"relative": str(path.relative_to(root)),
                          "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        except OSError:
            continue
    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['relative']}\0{item['bytes']}\0{item['mtime_ns']}\n".encode())
    return {"file_count": len(files), "stat_sha256": digest.hexdigest(), "files": files}


def migrate_encrypted(source_draft: Path, new_name: str, *, wait_seconds: int = 0) -> dict:
    """Launch JianYing for an explicitly named official-copy operation.

    A successful result is emitted only when a new plain/migrated directory is
    observed and the source tree's stat snapshot is unchanged.  Without a real
    Windows desktop UI session the command returns an actionable status instead
    of pretending that keyboard automation succeeded.
    """
    source = Path(source_draft).expanduser().resolve()
    if not source.is_dir():
        return {"status": "MIGRATION_SOURCE_NOT_FOUND", "source_draft": str(source)}
    if not new_name.strip() or Path(new_name).name != new_name:
        return {"status": "MIGRATION_NAME_INVALID", "message": "新草稿名必须是单一名称，不能包含目录分隔符。"}
    fmt = inspect_draft_format(source)
    if fmt.get("kind") != "encrypted":
        return {"status": "MIGRATION_NOT_REQUIRED", "source_draft": str(source), "format": fmt}

    env = environment_report()
    root = find_drafts_root()
    target = root / new_name
    if target.exists():
        return {"status": "MIGRATION_TARGET_EXISTS", "source_draft": str(source),
                "target_draft": str(target), "message": "请换一个全新的草稿名，避免覆盖已有工程。"}
    before = _tree_snapshot(source)
    result = {"status": "OFFICIAL_COPY_UI_UNAVAILABLE", "source_draft": str(source),
              "target_draft": str(target), "source_format": fmt,
              "source_snapshot_before": before,
              "instructions": [
                  "在剪映中打开源草稿，等待素材和时间线加载完成。",
                  "使用剪映官方“另存为/创建副本/导入草稿”并填写目标草稿名。",
                  "完成后重新运行本命令验证，不要复制或修改 crypto_key_store.dat。",
              ]}
    if not IS_WIN:
        result["reason"] = "当前迁移验证入口针对 Windows 官方客户端；Mac 通常可直接读取明文或已迁移工程。"
        return result

    executable = ((env.get("jianying") or {}).get("executable"))
    launched = False
    if executable and Path(executable).exists():
        try:
            subprocess.Popen([executable], close_fds=True)
            launched = True
        except OSError as exc:
            result["launch_error"] = str(exc)
    result["client_launched"] = launched
    deadline = time.time() + max(0, min(int(wait_seconds), 300))
    while time.time() < deadline:
        if target.is_dir():
            target_fmt = inspect_draft_format(target)
            after = _tree_snapshot(source)
            if target_fmt.get("kind") in {"plain", "migrated"} and after == before:
                return {"status": "MIGRATION_VERIFIED", "source_draft": str(source),
                        "target_draft": str(target), "target_format": target_fmt,
                        "source_snapshot_before": before, "source_snapshot_after": after,
                        "client_launched": launched}
        time.sleep(1)
    result["wait_seconds"] = max(0, min(int(wait_seconds), 300))
    result["target_observed"] = target.is_dir()
    return result
