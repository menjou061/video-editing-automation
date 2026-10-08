#!/usr/bin/env python3
"""Inspect or archive 1.4.1 task state into an isolated v1.5.0 migration folder.

The source is never changed. Migrated files are archival only; this tool never
manufactures a v1.5.0 success receipt or makes a task safe to continue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

SCHEMA = "video-v150-legacy-state-migration-v1"
ARCHIVE_NAME = "legacy-v1.4.1"
EVIDENCE_SUFFIXES = {".json", ".jsonl", ".csv", ".txt", ".log", ".md"}
EXCLUDED_DIRS = {"cache", "media", "素材", "__pycache__"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> object:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def _selected_sources(source: Path) -> list[Path]:
    found: set[Path] = set()
    state = source / "state"
    if state.is_symlink():
        raise ValueError("legacy state directory may not be a symlink")
    processed = state / "processed.json"
    if processed.is_file():
        found.add(processed)
    if state.is_dir():
        found.update(path for path in state.glob("*.lock") if path.is_file())
    work = source / "work"
    if work.is_symlink():
        raise ValueError("legacy work directory may not be a symlink")
    if work.is_dir():
        for record_dir in work.iterdir():
            if record_dir.is_symlink():
                raise ValueError("legacy task directory may not be a symlink")
            if not record_dir.is_dir() or record_dir.name in EXCLUDED_DIRS:
                continue
            for path in record_dir.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                if any(part in EXCLUDED_DIRS for part in path.relative_to(record_dir).parts):
                    continue
                if path.name == ".task_run.lock" or path.suffix.lower() in EVIDENCE_SUFFIXES:
                    found.add(path)
    return sorted(found, key=lambda path: path.relative_to(source).as_posix())


def _records(source: Path, files: list[Path]) -> list[dict]:
    processed_path = source / "state" / "processed.json"
    try:
        processed = read_json(processed_path) if processed_path in files else {}
    except (OSError, ValueError, TypeError):
        processed = {}
    if not isinstance(processed, dict):
        processed = {}
    task_dirs = {p.parent for p in files if p.name in {"task.json", "done.json"}}
    keys = {str(key) for key in processed}
    ids = {directory.name for directory in task_dirs}
    rows: list[dict] = []
    global_lock = any(path.parent == source / "state" and path.suffix == ".lock"
                      for path in files)
    for key in sorted(keys | ids):
        record_id = key.split("#rev", 1)[0]
        if (not record_id or record_id in {".", ".."}
                or any(char in record_id for char in ("/", "\\", ":"))):
            raise ValueError("legacy record key must be a local task directory name")
        directory = source / "work" / record_id
        task_path, done_path = directory / "task.json", directory / "done.json"
        try:
            task = read_json(task_path) if task_path.is_file() else {}
        except (OSError, ValueError, TypeError):
            task = {}
        try:
            done = read_json(done_path) if done_path.is_file() else {}
        except (OSError, ValueError, TypeError):
            done = {}
        task = task if isinstance(task, dict) else {}
        done = done if isinstance(done, dict) else {}
        prior = processed.get(key, {})
        prior = prior if isinstance(prior, dict) else {}
        status = str(done.get("status") or prior.get("status") or "PENDING").upper()
        lock = directory / ".task_run.lock"
        if global_lock or lock.exists() or status in {"RUNNING", "IN_PROGRESS", "PENDING_LOCK"}:
            decision, reason = "live_process_check_required", "active_or_unknown_lock_state"
        elif status in {"DEFERRED", "SKIPPED"} or done.get("deferred_reason"):
            decision, reason = "deferred_no_attempt", "preserve_deferred_state_and_recheck_inputs"
        elif status in {"OK", "SUCCESS", "COMPLETED", "DONE"}:
            decision = "history_only_unverified"
            reason = "legacy_terminal_status_is_not_a_v150_success_receipt"
        elif status in {"ERROR", "PARTIAL", "PREVIEW_READY", "FAILED"}:
            decision, reason = "manual_retry_or_skip_required", "preserve_failure_without_automatic_retry"
        else:
            decision, reason = "pending_review", "legacy_state_does_not_prove_safe_continuation"
        rows.append({
            "legacy_key": key,
            "record_id": str(task.get("record_id") or done.get("record_id") or record_id),
            "task_id": task.get("task_id") or done.get("task_id") or prior.get("task_id"),
            "legacy_status": status,
            "attempts": done.get("attempts", prior.get("attempts")),
            "issue_hash_legacy": prior.get("issue_hash"),
            "source_task_json_sha256": sha256(task_path.read_bytes()) if task_path.is_file() else None,
            "source_done_json_sha256": sha256(done_path.read_bytes()) if done_path.is_file() else None,
            "decision": decision,
            "reason": reason,
        })
    return rows


def inspect_source(source: Path) -> dict:
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source root must be an existing directory")
    files = _selected_sources(source)
    hashes = []
    for path in files:
        if path.is_symlink():
            raise ValueError("source evidence may not be a symlink")
        relative = path.relative_to(source).as_posix()
        hashes.append({"path": relative, "sha256": sha256(path.read_bytes()), "size": path.stat().st_size})
    source_digest = sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode())
    locks = [item["path"] for item in hashes if item["path"].endswith(".lock")]
    return {
        "schema": SCHEMA,
        "source_root": str(source.resolve()),
        "source_snapshot_sha256": source_digest,
        "source_files": hashes,
        "active_or_unknown_locks": locks,
        "records": _records(source, files),
        "source_mutation": "none",
        "safe_to_continue": False,
    }


def _verify_existing_archive(target: Path, existing: object, report: dict) -> None:
    """An idempotent repeat verifies the stored bytes, not only its report."""
    if (target.is_symlink() or not isinstance(existing, dict)
            or existing.get("schema") != SCHEMA
            or existing.get("source_root") != report["source_root"]
            or existing.get("source_snapshot_sha256") != report["source_snapshot_sha256"]
            or existing.get("source_files") != report["source_files"]):
        raise FileExistsError("destination archive contains a different source snapshot")
    snapshot = target / "source_snapshot"
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise FileExistsError("destination snapshot is missing or invalid")
    expected = {row["path"]: row for row in report["source_files"]}
    actual = set()
    for path in snapshot.rglob("*"):
        if path.is_symlink():
            raise FileExistsError("destination evidence may not be a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(snapshot).as_posix()
        actual.add(relative)
        row = expected.get(relative)
        if (not row or path.stat().st_size != row["size"]
                or sha256(path.read_bytes()) != row["sha256"]):
            raise FileExistsError("destination evidence hash mismatch: " + relative)
    if actual != set(expected):
        raise FileExistsError("destination snapshot evidence is incomplete")


def migrate(source: Path, destination: Path) -> dict:
    source, destination = Path(source), Path(destination)
    source_real, destination_real = source.resolve(), destination.resolve()
    if destination_real == source_real or source_real in destination_real.parents:
        raise ValueError("destination must be outside the legacy source tree")
    report = inspect_source(source)
    target = destination / ARCHIVE_NAME
    if target.exists():
        existing_path = target / "migration-report.json"
        try:
            existing = read_json(existing_path)
        except (OSError, ValueError, TypeError):
            raise FileExistsError("destination archive exists without a valid migration report")
        _verify_existing_archive(target, existing, report)
        return {"status": "unchanged", "archive": str(target), "report": existing}
    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".legacy-migration-", dir=str(destination)))
    try:
        snapshot = stage / "source_snapshot"
        snapshot.mkdir()
        for row in report["source_files"]:
            relative = Path(row["path"])
            src = source / relative
            if src.is_symlink() or not src.is_file():
                raise ValueError("source evidence changed during migration")
            out = snapshot / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            if sha256(out.read_bytes()) != row["sha256"]:
                raise IOError("copied evidence hash mismatch: " + row["path"])
        report["status"] = "archived"
        report["archive_root"] = str(target)
        (stage / "migration-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(stage, target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"status": "archived", "archive": str(target), "report": report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    inspect = subs.add_parser("inspect", help="read-only compatibility preview")
    inspect.add_argument("--source", required=True, type=Path)
    apply = subs.add_parser("migrate", help="archive source evidence into a new destination")
    apply.add_argument("--source", required=True, type=Path)
    apply.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    result = inspect_source(args.source) if args.command == "inspect" else migrate(args.source, args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
