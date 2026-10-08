#!/usr/bin/env python3
"""Fail-closed checks for the Windows v1.5.0 deployment package."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def run_preflight(package_root: Path, *, require_windows: bool = True,
                  draft_qc_script: Path | None = None) -> dict:
    root = Path(package_root)
    blockers: list[str] = []
    warnings: list[str] = []
    try:
        metadata = json.loads((root / "SKILL_PACKAGE.json").read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise ValueError("package metadata must be an object")
    except (OSError, ValueError, TypeError):
        metadata = {}
        blockers.append("PACKAGE_METADATA_UNREADABLE")
    try:
        version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        version = ""
        blockers.append("PACKAGE_VERSION_UNREADABLE")
    if version != "1.5.0" or metadata.get("version") != "v1.5.0":
        blockers.append("PACKAGE_VERSION_MISMATCH")
    if str(metadata.get("runtime") or "").lower() != "windows":
        blockers.append("PACKAGE_RUNTIME_NOT_WINDOWS")
    for relative in (
        "jy_poll.ps1", "run_task.template.ps1", "batch_worker.py",
        "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json", "tools/closed_loop.py",
        "tools/load-runtime-config.ps1", "tools/migrate_legacy_state.py",
        "tools/package_preflight.py",
        "doubao-jianying-orchestrator/VERSION", "jianying-editor/VERSION",
    ):
        if not (root / relative).is_file():
            blockers.append("PACKAGE_FILE_MISSING:" + relative)
    for relative in ("doubao-jianying-orchestrator/VERSION",
                     "doubao-jianying-orchestrator/orchestrator/VERSION",
                     "jianying-editor/VERSION"):
        try:
            component_version = (root / relative).read_text(encoding="utf-8-sig").strip()
        except (OSError, ValueError):
            blockers.append("COMPONENT_VERSION_UNREADABLE:" + relative)
            continue
        if component_version != "1.5.0":
            blockers.append("COMPONENT_VERSION_MISMATCH:" + relative)
    qc = Path(draft_qc_script) if draft_qc_script else Path(
        os.environ.get("JY_DRAFT_QC_SCRIPT", str(root / "doubao-jianying-orchestrator" /
                                                   "orchestrator" / "draft_visual_qc.py")))
    if not qc.is_file():
        blockers.append("DRAFT_VISUAL_QC_MISSING:" + str(qc))
    if require_windows and os.name != "nt":
        blockers.append("WINDOWS_RENDERER_REQUIRED")
    if not os.environ.get("JY_VOICE_CATALOG_ROOT", "").strip():
        warnings.append("EXTERNAL_VOICE_CATALOG_NOT_CONFIGURED")
    return {
        "schema_version": 1,
        "status": "READY" if not blockers else "BLOCKED",
        "ok": not blockers,
        "package_root": str(root),
        "package_version": version or None,
        "runtime": metadata.get("runtime") if metadata else None,
        "draft_qc_script": str(qc),
        "blockers": blockers,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--allow-non-windows", action="store_true",
                        help="for offline package tests only; does not validate renderer readiness")
    parser.add_argument("--draft-qc-script", type=Path)
    args = parser.parse_args()
    result = run_preflight(args.package_root, require_windows=not args.allow_non_windows,
                           draft_qc_script=args.draft_qc_script)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
