#!/usr/bin/env python3
"""Windows video-tool closed-loop gates.

This module is deliberately evidence-first.  It never upgrades a preview or an
uncertified draft to a formal delivery and never edits the source task fields.
It is called by the Windows worker after build/QC and can be run independently
for a read-only audit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
PIPE = HERE.parent
DEFAULT_POLICY = PIPE / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"
DEFAULT_TABLE_ENV = "LARK_TABLE_ID"
DRAFTS = pathlib.Path(os.environ.get("JY_DRAFT_ROOT", str(PIPE / "drafts")))
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def safe_name(value: str) -> str:
    result = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in value).strip(" .")
    return result or "task"


def policy(path: pathlib.Path) -> dict[str, Any]:
    value = read_json(path)
    if not value.get("policy_id"):
        raise RuntimeError("CLOSED_LOOP_POLICY_INVALID")
    return value


def qc_statuses(task_dir: pathlib.Path, done: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    refs = done.get("qc_reports") or []
    drafts = [str(item) for item in (done.get("drafts") or [])]
    for index, draft in enumerate(drafts, 1):
        report = ""
        if index <= len(refs) and isinstance(refs[index - 1], dict):
            report = str(refs[index - 1].get("report") or "")
        report_path = pathlib.Path(report) if report else task_dir / "report" / (draft if index == 1 else "report_%d" % index) / "draft_visual_qc.json"
        payload = read_json(report_path)
        statuses.append(str(payload.get("status") or "UNCERTIFIED").upper())
    return statuses


def package_check(draft: pathlib.Path) -> dict[str, Any]:
    issues: list[str] = []
    if not draft.is_dir():
        return {"ok": False, "issues": ["DRAFT_DIR_MISSING"]}
    for required in ("draft_info.json", "media"):
        if not (draft / required).exists():
            issues.append("MISSING_" + required.upper())
    if not any((draft / name).is_file() for name in ("素材本地化修复.bat", "fix_local.bat")):
        issues.append("LOCALIZE_BAT_MISSING")
    if not (draft / "fix_local.ps1").is_file():
        issues.append("FIX_LOCAL_PS1_MISSING")
    media = [item for item in (draft / "media").rglob("*") if item.is_file()] if (draft / "media").is_dir() else []
    if not media:
        issues.append("MEDIA_EMPTY")
    return {"ok": not issues, "issues": issues, "media_count": len(media)}


def audit(task_path: pathlib.Path, done_path: pathlib.Path, policy_path: pathlib.Path) -> dict[str, Any]:
    rules = policy(policy_path)
    task = read_json(task_path)
    done = read_json(done_path)
    task_dir = done_path.parent
    reasons: list[str] = []
    if str(done.get("status") or "").upper() != "OK":
        reasons.append("DONE_NOT_OK")
    statuses = qc_statuses(task_dir, done)
    required_qc = rules["qc"].get("formal_delivery_requires") or ["PASS"]
    if not statuses:
        reasons.append("QC_REPORT_MISSING")
    if any(status not in required_qc for status in statuses):
        reasons.append("QC_NOT_FORMAL: " + ",".join(statuses or ["UNCERTIFIED"]))
    if any(status in {"FAIL", "UNCERTIFIED", "PASS_WITH_GAPS"} for status in statuses):
        reasons.append("QC_BLOCKED")
    if rules.get("delivery", {}).get("mac_review_gate", {}).get("required_before_nas_and_table_write", True):
        approval = task_dir / "mac_review_approved.json"
        if not approval.is_file():
            reasons.append("MAC_REVIEW_PENDING")
    package_rows = []
    for draft in (done.get("local_only_drafts") or done.get("drafts") or []):
        draft_path = pathlib.Path(str(draft))
        if not draft_path.is_absolute():
            draft_path = DRAFTS / draft_path
        package_rows.append(package_check(draft_path))
    if any(not row.get("ok") for row in package_rows):
        reasons.append("PACKAGE_INCOMPLETE")
    return {
        "policy_id": rules["policy_id"],
        "policy_version": rules["policy_version"],
        "eligible": not reasons,
        "reasons": reasons,
        "done_status": done.get("status"),
        "qc_statuses": statuses,
        "task_id": task.get("task_id"),
        "record_id": task.get("record_id"),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def archive_parent(task_path: pathlib.Path, done_path: pathlib.Path, policy_path: pathlib.Path) -> dict[str, Any]:
    """Group shipped draft directories into one parent folder.

    The operation is additive: it creates and verifies the new parent before
    touching any old directory.  Replacement/deletion is a separate, explicit
    post-verification step and is never inferred from a copy failure.
    """
    rules = policy(policy_path)
    task = read_json(task_path)
    done = read_json(done_path)
    links = [str(item) for item in (done.get("nas_links") or []) if str(item).strip()]
    if not links:
        raise RuntimeError("NAS_DRAFT_LINKS_MISSING")
    if len(links) == 1:
        parent = links[0]
        source = pathlib.Path(parent)
        if not source.is_dir():
            raise RuntimeError("NAS_DRAFT_MISSING:" + parent)
        check = package_check(source)
        if not check.get("ok"):
            raise RuntimeError("SINGLE_PACKAGE_INVALID:%s:%s" % (source, check.get("issues")))
        receipt = {
            "record_id": task.get("record_id"),
            "parent_folder": parent,
            "versions": [parent],
            "source_links": links,
            "policy_id": rules["policy_id"],
            "policy_version": rules["policy_version"],
            "verified": True,
            "already_grouped": True,
            "receipt_hash": digest({"parent_folder": parent, "versions": [parent], "source_links": links}),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_json(done_path.parent / "distribution_receipt.json", receipt)
        done["nas_parent_folder"] = parent
        done["distribution_status"] = "VERIFIED"
        write_json(done_path, done)
        return receipt
    stamp = time.strftime("%Y%m%d-%H%M%S")
    title = safe_name(str(task.get("title") or task.get("record_id") or "task"))
    distribution_root = os.environ.get("JY_NAS_DISTRIBUTION_ROOT", "").strip()
    if not distribution_root:
        raise RuntimeError("JY_NAS_DISTRIBUTION_ROOT_MISSING")
    parent = pathlib.Path(distribution_root) / (title + "-closed-loop-" + stamp)
    parent.mkdir(parents=True, exist_ok=False)
    versions = []
    for index, raw in enumerate(links, 1):
        source = pathlib.Path(raw)
        if not source.is_dir():
            raise RuntimeError("NAS_DRAFT_MISSING:" + raw)
        target = parent / ("版本%d" % index)
        shutil.copytree(source, target)
        check = package_check(target)
        if not check.get("ok"):
            raise RuntimeError("GROUPED_PACKAGE_INVALID:%s:%s" % (target, check.get("issues")))
        versions.append(str(target))
    receipt = {
        "record_id": task.get("record_id"),
        "parent_folder": str(parent),
        "versions": versions,
        "source_links": links,
        "policy_id": rules["policy_id"],
        "policy_version": rules["policy_version"],
        "verified": True,
        "receipt_hash": digest({"parent_folder": str(parent), "versions": versions, "source_links": links}),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json(done_path.parent / "distribution_receipt.json", receipt)
    done["nas_parent_folder"] = str(parent)
    done["nas_links"] = [str(parent)]
    done["distribution_status"] = "GROUPED_VERIFIED"
    write_json(done_path, done)
    return receipt


def table_update(task_path: pathlib.Path, done_path: pathlib.Path, policy_path: pathlib.Path) -> dict[str, Any]:
    rules = policy(policy_path)
    task = read_json(task_path)
    done = read_json(done_path)
    audit_result = audit(task_path, done_path, policy_path)
    if not audit_result.get("eligible"):
        return {"ok": False, "status": "BLOCKED", "audit": audit_result}
    parent = str(done.get("nas_parent_folder") or (done.get("nas_links") or [""])[0]).strip()
    if not parent:
        return {"ok": False, "status": "BLOCKED", "reason": "NAS_PARENT_MISSING"}
    record_id = str(task.get("record_id") or "").strip()
    task_id = str(task.get("task_id") or record_id).strip()
    film_status = rules["table_write"]["revision_status"] if task.get("rev") or task.get("issue") else rules["table_write"]["initial_success_status"]
    patch = {"草稿文件": parent, "任务状态": "已完成", "成片状态": film_status}
    payload = {"record_id_list": [record_id], "patch": patch}
    token_env = str(rules["table_write"].get("base_token_env") or "LARK_BASE_TOKEN")
    base_token = os.environ.get(token_env, "").strip()
    if not base_token:
        return {"ok": False, "status": "BLOCKED", "reason": "LARK_BASE_TOKEN_MISSING", "record_id": record_id, "task_id": task_id}
    table_id = str(rules["table_write"].get("table_id") or os.environ.get(DEFAULT_TABLE_ENV, "")).strip()
    if not table_id:
        return {"ok": False, "status": "BLOCKED", "reason": "LARK_TABLE_ID_MISSING", "record_id": record_id, "task_id": task_id}
    command = ["lark-cli", "base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id, "--as", "bot", "--json", json.dumps(payload, ensure_ascii=False)]
    profile = os.environ.get("LARK_PROFILE", "").strip()
    if profile:
        command[1:1] = ["--profile", profile]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    raw = (result.stdout or "")[-4000:]
    try:
        response = json.loads(raw)
    except (ValueError, TypeError):
        response = {}
    ok = result.returncode == 0 and response.get("ok") is True
    receipt = {"ok": ok, "record_id": record_id, "task_id": task_id, "patch": patch, "returncode": result.returncode, "response": response if response else raw, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    write_json(done_path.parent / "table_update_receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("audit", "archive-parent", "table-update"))
    parser.add_argument("--task-json", required=True, type=pathlib.Path)
    parser.add_argument("--done-json", required=True, type=pathlib.Path)
    parser.add_argument("--policy", type=pathlib.Path, default=DEFAULT_POLICY)
    args = parser.parse_args()
    try:
        if args.command == "audit":
            result = audit(args.task_json, args.done_json, args.policy)
        elif args.command == "archive-parent":
            result = archive_parent(args.task_json, args.done_json, args.policy)
        else:
            result = table_update(args.task_json, args.done_json, args.policy)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("eligible", result.get("ok", True)) else 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
