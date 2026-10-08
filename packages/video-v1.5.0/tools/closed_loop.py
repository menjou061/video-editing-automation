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
import sys
import time
from typing import Any
from tools.delivery_identity import tree_identity

HERE = pathlib.Path(__file__).resolve().parent
PIPE = HERE.parent
if str(PIPE) not in sys.path:
    sys.path.insert(0, str(PIPE))
DEFAULT_POLICY = PIPE / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"
DEFAULT_TABLE_ENV = "LARK_TABLE_ID"
DRAFTS = pathlib.Path(os.environ.get("JY_DRAFT_ROOT", str(PIPE / "drafts")))
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
FAILURE_TASK_STATUS = "生成失败"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        # PowerShell 5.1's UTF-8 writer adds a BOM; accept both producers.
        value = json.loads(path.read_text(encoding="utf-8-sig"))
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


def lark_cli_executable() -> str:
    """Resolve the Windows npm shim explicitly for scheduled-task sessions."""
    configured = os.environ.get("LARK_CLI_PATH", "").strip()
    candidates = [configured] if configured else []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            candidates.append(str(pathlib.Path(appdata) / "npm" / "lark-cli.cmd"))
        candidates.extend(["lark-cli.cmd", "lark-cli"])
    else:
        candidates.append("lark-cli")
    for candidate in candidates:
        if not candidate:
            continue
        if pathlib.Path(candidate).is_file() or shutil.which(candidate):
            return candidate
    return "lark-cli.cmd" if os.name == "nt" else "lark-cli"


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
        frozen_path = task_dir / 'eval' / 'active_contract.json'
        if frozen_path.is_file():
            try:
                from tools import production_eval
                contract = production_eval.read(frozen_path)
                names = done.get('drafts') or []
                outputs = [DRAFTS / str(name) for name in names]
                output_hash = production_eval.output_identity(outputs)['sha256']
                approval = task_dir / ('mac_review_approved-' + output_hash + '.json')
                approval_data = production_eval.read(approval)
                if (approval_data.get('approved') is not True
                        or approval_data.get('task_id') != contract.get('task_id')
                        or approval_data.get('run_id') != contract.get('run_id')
                        or approval_data.get('contract_hash') != contract.get('contract_hash')
                        or approval_data.get('output_sha256') != output_hash
                        or approval_data.get('draft_ids') != production_eval.output_draft_ids(outputs)):
                    reasons.append('MAC_REVIEW_BINDING_MISMATCH')
            except (OSError, ValueError, KeyError, TypeError):
                reasons.append('MAC_REVIEW_PENDING_OR_UNCERTIFIED')
        elif not (task_dir / "mac_review_approved.json").is_file():
            reasons.append("MAC_REVIEW_PENDING")
    package_rows = []
    for draft in (done.get("local_only_drafts") or done.get("drafts") or []):
        draft_path = pathlib.Path(str(draft))
        if not draft_path.is_absolute():
            draft_path = DRAFTS / draft_path
        package_rows.append(package_check(draft_path))
    if any(not row.get("ok") for row in package_rows):
        reasons.append("PACKAGE_INCOMPLETE")
    frozen_path = task_dir / 'eval' / 'active_contract.json'
    if os.environ.get('JY_EVAL_MODE') == 'enforce' and not frozen_path.is_file():
        reasons.append('EVAL_CONTRACT_MISSING')
    if frozen_path.is_file():
        from tools import production_eval
        try:
            contract = production_eval.read(frozen_path)
            if contract['mode'] == 'enforce':
                wrapper = done.get('eval_receipt') or {}
                receipt = production_eval.read(pathlib.Path(wrapper['receipt_path']))
                outputs = [DRAFTS / str(name) for name in done.get('drafts', [])]
                runtime_root = pathlib.Path(os.environ.get('JY_EVAL_RUNTIME_ROOT', str(PIPE)))
                current = production_eval.runtime_identity(runtime_root)
                if receipt.get('status') != 'PASS' or not production_eval.verify_receipt(receipt, contract, task, current, outputs):
                    reasons.append('EVAL_RECEIPT_NOT_VERIFIED')
                elif wrapper.get('receipt_sha256') != receipt.get('receipt_sha256'):
                    reasons.append('EVAL_RECEIPT_WRAPPER_MISMATCH')
                distribution = done.get('distribution_receipts') or []
                by_name = {str(row.get('draft_name') or ''): row
                           for row in distribution if isinstance(row, dict)}
                if set(by_name) != set(str(name) for name in done.get('drafts', [])):
                    reasons.append('DISTRIBUTION_RECEIPTS_MISSING')
                else:
                    for name, row in by_name.items():
                        try:
                            output_hash = production_eval.output_identity([DRAFTS / name])['sha256']
                            target_identity = tree_identity(pathlib.Path(row['nas_target']))
                            receipt_body = dict(row)
                            receipt_body.pop('receipt_sha256', None)
                            receipt_body.pop('receipt_path', None)
                            if (row.get('status') != 'VERIFIED'
                                    or row.get('task_id') != contract.get('task_id')
                                    or row.get('run_id') != contract.get('run_id')
                                    or row.get('contract_hash') != contract.get('contract_hash')
                                    or row.get('source_output_sha256') != output_hash
                                    or row.get('transformed_tree_sha256') != target_identity['sha256']
                                    or row.get('eval_receipt_sha256') != receipt.get('receipt_sha256')
                                    or row.get('eval_status') != 'PASS'
                                    or row.get('receipt_sha256') != production_eval.digest(receipt_body)):
                                reasons.append('DISTRIBUTION_RECEIPT_INVALID:' + name)
                        except (OSError, ValueError, KeyError, TypeError):
                            reasons.append('DISTRIBUTION_READBACK_INVALID:' + name)
        except (OSError, ValueError, KeyError, TypeError):
            reasons.append('EVAL_RECEIPT_MISSING_OR_DRIFTED')
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
            "version_identities": [tree_identity(source)],
            "receipt_hash": digest({"parent_folder": parent, "versions": [parent],
                                    "source_links": links, "version_identities": [tree_identity(source)]}),
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
    version_identities = []
    for index, raw in enumerate(links, 1):
        source = pathlib.Path(raw)
        if not source.is_dir():
            raise RuntimeError("NAS_DRAFT_MISSING:" + raw)
        source_identity = tree_identity(source)
        target = parent / ("版本%d" % index)
        shutil.copytree(source, target)
        copied_identity = tree_identity(target)
        if source_identity['sha256'] != copied_identity['sha256']:
            raise RuntimeError("GROUPED_COPY_HASH_MISMATCH:%s" % target)
        check = package_check(target)
        if not check.get("ok"):
            raise RuntimeError("GROUPED_PACKAGE_INVALID:%s:%s" % (target, check.get("issues")))
        versions.append(str(target))
        version_identities.append(copied_identity)
    receipt = {
        "record_id": task.get("record_id"),
        "parent_folder": str(parent),
        "versions": versions,
        "source_links": links,
        "policy_id": rules["policy_id"],
        "policy_version": rules["policy_version"],
        "verified": True,
        "version_identities": version_identities,
        "receipt_hash": digest({"parent_folder": str(parent), "versions": versions,
                                "source_links": links, "version_identities": version_identities}),
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
    patch = {"草稿文件": parent, "任务状态": ["已完成"], "成片状态": [film_status]}
    payload = {"update_records": {record_id: patch}}
    token_env = str(rules["table_write"].get("base_token_env") or "LARK_BASE_TOKEN")
    base_token = os.environ.get(token_env, "").strip()
    if not base_token:
        return {"ok": False, "status": "BLOCKED", "reason": "LARK_BASE_TOKEN_MISSING", "record_id": record_id, "task_id": task_id}
    table_id = str(rules["table_write"].get("table_id") or os.environ.get(DEFAULT_TABLE_ENV, "")).strip()
    if not table_id:
        return {"ok": False, "status": "BLOCKED", "reason": "LARK_TABLE_ID_MISSING", "record_id": record_id, "task_id": task_id}
    command = [lark_cli_executable(), "base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id, "--as", "bot", "--json", json.dumps(payload, ensure_ascii=False)]
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


def _failure_summary(done: dict[str, Any]) -> str:
    """Collapse a terminal failure into a short operator-facing reason."""
    rows = done.get("failures") or []
    parts: list[str] = []
    if isinstance(rows, list):
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            phase = str(row.get("phase") or row.get("type") or "failure").strip()
            message = " ".join(str(row.get("message") or "").split())
            if message:
                parts.append(f"{phase}: {message[:300]}")
    error = " ".join(str(done.get("error") or "").split())
    if error and not parts:
        parts.append(error[:500])
    return "；".join(parts) or "未提供结构化失败原因，详见任务 work 目录证据"


def failure_update(task_path: pathlib.Path, done_path: pathlib.Path,
                   policy_path: pathlib.Path) -> dict[str, Any]:
    """Publish a failed/partial task without pretending it was delivered.

    This is separate from the successful delivery gate: it only writes the
    failure status and bounded feedback, never a draft path or success status.
    """
    rules = policy(policy_path)
    task = read_json(task_path)
    done = read_json(done_path)
    record_id = str(task.get("record_id") or done.get("record_id") or "").strip()
    task_id = str(task.get("task_id") or record_id).strip()
    status = str(done.get("status") or "ERROR").upper()
    if status in {"OK", "SUCCESS", "PREVIEW_READY"}:
        return {"ok": False, "status": "SKIPPED", "reason": "NOT_A_FAILURE", "record_id": record_id}
    failure_cfg = rules.get("failure_notification") or {}
    task_field = str(failure_cfg.get("task_status_field") or "任务状态")
    feedback_field = str(failure_cfg.get("feedback_field") or "问题反馈")
    failure_status = str(failure_cfg.get("task_status") or FAILURE_TASK_STATUS)
    summary = _failure_summary(done)
    stamp = str(done.get("finished_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
    note = (f"[自动化失败回执] {stamp} | 状态={status} | 任务={task_id} | 原因={summary}")
    original_issue = str(task.get("issue") or "").strip()
    feedback = (original_issue + "\n\n" + note).strip() if original_issue else note
    fingerprint = digest({"record_id": record_id, "status": status,
                          "failures": done.get("failures") or [],
                          "error": done.get("error") or ""})
    receipt: dict[str, Any] = {
        "ok": False, "record_id": record_id, "task_id": task_id,
        "status": status, "notification_id": fingerprint,
        "patch": {task_field: [failure_status], feedback_field: feedback},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    token_env = str(rules["table_write"].get("base_token_env") or "LARK_BASE_TOKEN")
    base_token = os.environ.get(token_env, "").strip()
    table_id = str(rules["table_write"].get("table_id") or os.environ.get(DEFAULT_TABLE_ENV, "")).strip()
    if not base_token:
        receipt["reason"] = "LARK_BASE_TOKEN_MISSING"
        write_json(done_path.parent / "failure_notification_receipt.json", receipt)
        return receipt
    if not table_id:
        receipt["reason"] = "LARK_TABLE_ID_MISSING"
        write_json(done_path.parent / "failure_notification_receipt.json", receipt)
        return receipt
    command = [lark_cli_executable(), "base", "+record-batch-update", "--base-token", base_token,
               "--table-id", table_id, "--as", "bot", "--json",
               json.dumps({"update_records": {record_id: receipt["patch"]}}, ensure_ascii=False)]
    profile = os.environ.get("LARK_PROFILE", "").strip()
    if profile:
        command[1:1] = ["--profile", profile]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        raw = (result.stdout or result.stderr or "")[-4000:]
        try:
            response = json.loads(raw)
        except (ValueError, TypeError):
            response = {}
        receipt["ok"] = result.returncode == 0 and response.get("ok") is True
        receipt["returncode"] = result.returncode
        receipt["response"] = response if response else raw
    except (OSError, subprocess.SubprocessError) as exc:
        receipt["reason"] = str(exc)
    if not receipt["ok"] and failure_cfg.get("feedback_only_fallback", True):
        fallback_payload = {"update_records": {record_id: {feedback_field: feedback}}}
        fallback_cmd = list(command)
        fallback_cmd[-1] = json.dumps(fallback_payload, ensure_ascii=False)
        try:
            fallback = subprocess.run(fallback_cmd, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=60)
            raw = (fallback.stdout or fallback.stderr or "")[-4000:]
            try:
                response = json.loads(raw)
            except (ValueError, TypeError):
                response = {}
            receipt["feedback_only_ok"] = fallback.returncode == 0 and response.get("ok") is True
            receipt["feedback_only_response"] = response if response else raw
        except (OSError, subprocess.SubprocessError) as exc:
            receipt["feedback_only_ok"] = False
            receipt["feedback_only_response"] = str(exc)
    write_json(done_path.parent / "failure_notification_receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("audit", "archive-parent", "table-update", "failure-update"))
    parser.add_argument("--task-json", required=True, type=pathlib.Path)
    parser.add_argument("--done-json", required=True, type=pathlib.Path)
    parser.add_argument("--policy", type=pathlib.Path, default=DEFAULT_POLICY)
    args = parser.parse_args()
    try:
        if args.command == "audit":
            result = audit(args.task_json, args.done_json, args.policy)
        elif args.command == "archive-parent":
            result = archive_parent(args.task_json, args.done_json, args.policy)
        elif args.command == "table-update":
            result = table_update(args.task_json, args.done_json, args.policy)
        else:
            result = failure_update(args.task_json, args.done_json, args.policy)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("eligible", result.get("ok", True)) else 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
