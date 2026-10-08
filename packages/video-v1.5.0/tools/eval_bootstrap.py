"""Freeze a production Eval contract before the standalone JyRun creates a draft.

The operator-owned identity file is intentionally outside the release package.
Its ``sources`` map can bind a confirmed single-SKU directory once for reuse;
``records`` supplies exact exceptions. Unknown identity is not a generation failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import material_index, production_eval as pe

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


def resolve_identity(task: dict, mapping: dict) -> dict:
    record_id = str(task.get("record_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", record_id):
        raise ValueError("RECORD_ID_UNSAFE")
    row = mapping.get("records", {}).get(record_id)
    if row is None:
        row = mapping.get("sources", {}).get(str(task.get("material_dir") or ""))
    if not isinstance(row, dict) or row.get("human_confirmed") is not True:
        raise ValueError("PRODUCT_IDENTITY_NOT_CONFIRMED")
    for key in ("product", "category", "sku", "material_dir"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError("PRODUCT_IDENTITY_INCOMPLETE:" + key)
    if (row["product"] != task.get("product")
            or row["material_dir"] != task.get("material_dir")):
        raise ValueError("PRODUCT_OR_SOURCE_BINDING_DRIFT")
    if row["category"] == "待确认" or row["sku"] == "待确认":
        raise ValueError("PRODUCT_IDENTITY_NOT_CONFIRMED")
    return row


def source_files(task: dict, runtime_root: Path, index_path: Path | None) -> list[Path]:
    root = Path(str(task.get("material_dir_z") or ""))
    if not root.is_dir() or root.is_symlink():
        raise ValueError("MATERIAL_DIRECTORY_MISSING")
    if index_path is not None:
        index = pe.read(index_path)
        approved = material_index.select(
            index.get("materials", []), category=task["category"], sku=task["sku"],
            tags=task.get("shot_tags") or [],
            policy_hash=pe.file_hash(runtime_root / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"),
            tagger_version=index.get("tagger_version", ""),
        )
        if not approved:
            raise ValueError("MATERIAL_INDEX_NO_APPROVED_SAME_SKU")
        files = [Path(row["source_path"]) for row in approved]
        for path in files:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("MATERIAL_INDEX_SOURCE_OUTSIDE_BOUND_DIRECTORY")
        return sorted(set(files))
    files = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("SYMLINK_MATERIAL_REJECTED")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            files.append(path)
    if not files:
        raise ValueError("MATERIAL_VIDEO_FILES_MISSING")
    return sorted(files)


def prepare(task_path: Path, runtime_root: Path, identity_path: Path,
            *, index_path: Path | None = None, mode: str = "observe",
            observations_path: Path | None = None) -> dict:
    task = pe.read(task_path)
    identity = resolve_identity(task, pe.read(identity_path))
    if task.get("category") != identity["category"] or task.get("sku") != identity["sku"]:
        raise ValueError("TASK_PRODUCT_IDENTITY_DRIFT")
    if mode not in {"observe", "enforce"}:
        raise ValueError("EVAL_MODE_INVALID")
    runtime = pe.runtime_identity(runtime_root)
    if mode == "enforce":
        if observations_path is None or not pe.verify_observations(observations_path, runtime)["ready"]:
            raise ValueError("EVAL_ROLLOUT_REQUIRES_TWO_VERIFIED_REAL_TASKS")
    inputs = source_files(task, runtime_root, index_path)
    gold = pe.read(runtime_root / "gold-reference.json")
    eval_root = task_path.parent / "eval"
    active = eval_root / "active_contract.json"
    if active.exists():
        contract = pe.read(active)
        pe.verify_contract(contract)
        if (contract["task_sha256"] != pe.digest(task)
                or contract["runtime"] != runtime or contract["mode"] != mode
                or len(contract["inputs"]) != len(inputs)):
            raise ValueError("ACTIVE_EVAL_CONTRACT_DRIFT")
        for frozen, source in zip(contract["inputs"], inputs):
            if frozen["path"] != str(source.absolute()) or frozen["sha256"] != pe.file_hash(source):
                raise ValueError("ACTIVE_EVAL_INPUT_DRIFT")
        return {"status": "FROZEN_REUSED", "run_id": contract["run_id"],
                "contract_hash": contract["contract_hash"]}
    run_id = str(task["record_id"]) + "-" + uuid.uuid4().hex
    contract = pe.freeze(task, run_id, inputs, runtime, gold, mode)
    pe.write_once(eval_root / run_id / "contract.json", contract)
    pe.write_once(active, contract)
    return {"status": "FROZEN", "run_id": run_id,
            "contract_hash": contract["contract_hash"], "input_count": len(inputs)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-json", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--identity-file", required=True, type=Path)
    parser.add_argument("--material-index", type=Path)
    parser.add_argument("--mode", default=os.environ.get("JY_EVAL_MODE", "observe"))
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    result = prepare(args.task_json, args.runtime_root, args.identity_file,
                     index_path=args.material_index, mode=args.mode,
                     observations_path=args.observations)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
