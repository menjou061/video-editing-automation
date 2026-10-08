# -*- coding: utf-8 -*-
"""批量任务 worker（渲染机侧常驻）。

逐条消费 `work/batch_queue.json`：
  enumerate materials -> manifest -> build(preview管线, 1.3.29 已无画面标注) x count
  -> ship(unс 分发) -> 进度写 `work/batch_progress.jsonl` -> 结果写
  `work/<record_id>/done.json`。
Mac 侧负责最终人工检查和成功交付回写；失败/部分失败可由 worker 在有
`LARK_BASE_TOKEN` 时即时回写表格，缺少凭据则落盘回执供监控侧重试。

原则：
- 任务之间互不影响：单条失败记 `error` + 证据（stderr 尾部/退出码），继续下一条。
- 幂等：任何阶段重跑不产生半成品（report dir 按 task 复用，TTS/素材缓存命中）。
- 凭据不出渲染机：NAS 挂载复用 `jy_pipeline/probe_mount2.ps1`（内部从 jy_poll.ps1
  内存解析），不在本文件出现。
"""
from __future__ import annotations

import json
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from tools import production_eval, material_index
from tools.delivery_identity import tree_identity

try:
    # Windows OpenSSH sessions may expose a GBK console even though the
    # pipeline and provider responses are UTF-8.  A replacement character in
    # a model/error message must never crash the worker while it is logging.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PIPE_ROOT = pathlib.Path(os.environ.get("JY_PIPE_ROOT", pathlib.Path(__file__).resolve().parent))
WORK = pathlib.Path(os.environ.get("JY_WORK_ROOT", str(PIPE_ROOT / "work")))
SKILL = pathlib.Path(os.environ.get("JY_SKILL_ROOT", str(PIPE_ROOT / "doubao-jianying-orchestrator")))
DRAFTS = pathlib.Path(os.environ.get("JY_DRAFT_ROOT", str(PIPE_ROOT / "drafts")))
PY = os.environ.get("JY_PYTHON_EXE", sys.executable)
POST_DRAFT_QC = SKILL / "orchestrator" / "draft_visual_qc.py"
CAPACITY_PROFILE = pathlib.Path(os.environ.get("JY_CAPACITY_PROFILE", str(PIPE_ROOT / "host_capacity.json")))
PROGRESS = WORK / "batch_progress.jsonl"
STATUS_SNAPSHOT = WORK / "task_status_snapshot.json"
LOG = WORK / "batch_worker.log"
NAS_PROBE = os.environ.get("JY_NAS_PROBE", str(PIPE_ROOT / "probe_mount2.ps1"))
NAS_TEST = os.environ.get("JY_NAS_TEST_ROOT", "").strip()
VIDEO_EXTS = (".mp4", ".mov", ".m4v")
SKIP_SHIP = os.environ.get("JY_SKIP_SHIP", "").strip() == "1"
_OUTPUT_LOCK = threading.Lock()
ACTIVE_RECORD_ID = ""
PIPE = WORK.parent
CLOSED_LOOP_TOOL = PIPE / "tools" / "closed_loop.py"
CLOSED_LOOP_POLICY = PIPE / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"
MAC_REVIEW_REQUIRED = os.environ.get("JY_MAC_REVIEW_REQUIRED", "1").strip() != "0"


def _pipeline_version() -> str:
    try:
        return (SKILL / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _runtime_facts() -> dict:
    """Capture the execution surface that produced a batch receipt."""
    vision_profile = os.environ.get("JY_VISION_PROFILE", "volc").strip()
    vision_model = os.environ.get("JY_VISION_MODEL", "").strip()
    return {
        "python": str(PY),
        "skill_root": str(SKILL),
        "worker_path": str(pathlib.Path(__file__).resolve()),
        "pipeline_version": _pipeline_version(),
        "ffmpeg": shutil.which("ffmpeg") or "",
        "ffprobe": shutil.which("ffprobe") or "",
        "vision_profile": vision_profile,
        "vision_model": vision_model,
    }


def _payload_sha256(payload: object) -> str:
    """Hash a JSON payload with stable ordering for run-to-input correlation."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _batch_result_contract(status: str, *, run_id: str, failures: list[dict] | None = None,
                           error: str = "", parent_run_id: str | None = None,
                           task_id: str | None = None,
                           manifest_sha256: str | None = None,
                           runtime: dict | None = None,
                           task_log: str | None = None) -> dict:
    """Return the monitor contract used to decide whether another task is safe."""
    rows = failures or []
    text = " ".join(str(row) for row in rows) + " " + str(error)
    lower = text.lower()
    first = rows[0] if rows else {}
    failure_type = str(first.get("type") or "").lower()
    if any(marker in lower for marker in ("vision_call_failed", "vision_json_invalid")):
        failure_class, failure_code = "vision", "VISION_CALL_FAILED"
    elif any(marker in lower for marker in ("visual_match_blocked", "shot_match_blocked", "material gap", "visual-missing")):
        failure_class, failure_code = "semantic", "VISUAL_MATCH_BLOCKED"
    elif "audio_video_mismatch" in lower or "shot_timing" in lower:
        failure_class, failure_code = "timing", "AUDIO_VIDEO_MISMATCH"
    elif "invalid argument" in lower or "task_budget" in lower or "budget" in lower:
        failure_class, failure_code = "environment", "INVALID_ARGUMENT" if "invalid argument" in lower else "TASK_BUDGET_EXHAUSTED"
    elif "task_already_running" in lower or "task_lock" in lower:
        failure_class, failure_code = "environment", "TASK_ALREADY_RUNNING"
    elif failure_type in {"environment", "preflight"} or "environment_precheck" in lower:
        failure_class, failure_code = "environment", "ENVIRONMENT_PRECHECK_FAILED"
    elif any(marker in lower for marker in (
        "nas mount", "material_dir", "no video files", "script empty",
        "draft root", "pipeline version",
    )):
        failure_class, failure_code = "environment", "ENVIRONMENT_PRECHECK_FAILED"
    elif "qc" in lower or "draft" in lower:
        failure_class, failure_code = "draft", "DRAFT_INVALID"
    elif failure_type == "transient":
        failure_class, failure_code = "provider", "TRANSIENT_BUILD_FAILURE"
    elif rows or error:
        failure_class, failure_code = "unknown", "TASK_FAILED"
    else:
        failure_class, failure_code = None, None

    retryable = bool(
        failure_class in {"vision", "provider"}
        and any(token in lower for token in (
            "timeout", "timed out", "429", "502", "503", "network",
            "connection reset", "temporarily unavailable",
        ))
    )
    if status == "OK":
        terminal_state, next_action = "SUCCESS", "complete"
    elif status == "DEFERRED":
        terminal_state, next_action = "DEFERRED", "skip_task"
    elif status in {"PARTIAL", "PREVIEW_READY"}:
        terminal_state, next_action = "UNCERTIFIED", "manual_review"
    elif failure_class in {"environment", "semantic", "timing", "draft"}:
        terminal_state, next_action = "BLOCKED", "fix_environment" if failure_class == "environment" else "manual_review"
    else:
        terminal_state, next_action = "ERROR", "retry_stage" if retryable else "manual_review"
    return {
        "task_id": task_id,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "pipeline_version": _pipeline_version(),
        "terminal_state": terminal_state,
        "failure_class": failure_class,
        "failure_code": failure_code,
        "retryable": retryable,
        "safe_to_continue": terminal_state == "SUCCESS",
        "next_action": next_action,
        "manifest_sha256": manifest_sha256,
        "runtime": runtime,
        "task_log": task_log,
    }


def _nas_path(name: str) -> str:
    if not NAS_TEST:
        raise RuntimeError("JY_NAS_TEST_ROOT_MISSING")
    return NAS_TEST.rstrip("\\/") + "\\" + name

def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# A single build attempt must fit the calibrated per-host timeout, but a
# transient provider failure (402/502/503/429/timeout) must never stop the
# queue nor fake an OK.  The per-draft loop retries a bounded number of times
# and escalates strategy each attempt (fresh vision -> reuse analysis ->
# larger timeout).  When attempts are exhausted the task is recorded as a
# real ERROR/PARTIAL with evidence, never as OK.
BUILD_TIMEOUT_SECONDS = _env_int("JY_BUILD_TIMEOUT_S", 1500, 60)
MAX_DRAFT_ATTEMPTS = _env_int("JY_MAX_DRAFT_ATTEMPTS", 3, 1)
# Per-task wall-clock budget so an oversized material pool (e.g. 526 fresh
# vision sources) cannot stall the whole queue: once the budget is spent the
# worker stops retrying that task and records an explicit BLOCKED failure,
# then continues.  Default 3600s; raise via JY_TASK_BUDGET_S to allow a
# genuinely large but healthy task to finish.
TASK_BUDGET_SECONDS = _env_int("JY_TASK_BUDGET_S", 1200, 60)
TIMEOUT_ESCALATION_MULT = _env_int("JY_TIMEOUT_ESCALATION", 2, 1)
TRANSIENT_BUILD_MARKERS = (
    "402", "502", "503", "429", "Too Many Requests", "temporarily unavailable",
    "Insufficient Balance", "timed out", "SHOT_ANALYSIS_PENDING",
    "VISION_CALL_FAILED", "SHOT_MATCH_BLOCKED", "Connection reset",
    "VISION_JSON_INVALID",
    "Connection refused", "could not resolve host", "network",
    "vision analysis incomplete", "empty description",
)


def log(*args):
    line = " ".join(str(a) for a in args)
    with _OUTPUT_LOCK:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {line}\n")
        try:
            print(line, flush=True)
        except (OSError, UnicodeError):
            # A redirected Windows/SSH console may reject a GBK byte sequence
            # or close while the build is still running. Logging must never
            # replace the real build/validation failure with worker ERROR.
            try:
                sys.stdout.write(line.encode("unicode_escape", errors="backslashreplace").decode("ascii") + "\n")
                sys.stdout.flush()
            except (OSError, UnicodeError):
                pass


def progress(entry: dict):
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with _OUTPUT_LOCK:
        with PROGRESS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        _write_status_snapshot()
    except Exception as exc:  # noqa: BLE001
        # Status publication must never stop the production worker.
        log("STATUS_SNAPSHOT_WARN %s" % exc)


def _read_json(path: pathlib.Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _acquire_record_lock(task_dir: pathlib.Path, run_id: str) -> pathlib.Path:
    """Prevent two worker processes from touching one record at once."""
    lock_path = task_dir / ".task_run.lock"
    payload = {"pid": os.getpid(), "run_id": run_id,
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "pipeline_version": _pipeline_version()}
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            return lock_path
        except FileExistsError:
            current = _read_json(lock_path)
            pid = current.get("pid")
            alive = False
            try:
                if pid and int(pid) == os.getpid():
                    alive = True
                elif pid:
                    os.kill(int(pid), 0)
                    alive = True
            except (OSError, TypeError, ValueError, ProcessLookupError):
                alive = False
            if alive:
                raise RuntimeError("TASK_ALREADY_RUNNING: record_id=%s run_id=%s pid=%s" % (
                    task_dir.name, current.get("run_id") or "unknown", pid))
            try:
                lock_path.unlink()
            except OSError:
                raise RuntimeError("TASK_LOCK_STALE_UNCLEAR: record_id=%s" % task_dir.name)
    raise RuntimeError("TASK_ALREADY_RUNNING: record_id=%s" % task_dir.name)


def _release_record_lock(lock_path: pathlib.Path | None, run_id: str) -> None:
    if not lock_path:
        return
    current = _read_json(lock_path)
    if current.get("run_id") == run_id:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _qc_state(task_dir: pathlib.Path, drafts: list[str], done: dict) -> tuple[str, int, list[str]]:
    """Summarize QC without allowing missing reports to look like RUNNING."""
    reports = []
    qc_refs = done.get("qc_reports") or []
    for index, draft in enumerate(drafts, 1):
        report_path = None
        if index <= len(qc_refs) and isinstance(qc_refs[index - 1], dict):
            report_ref = str(qc_refs[index - 1].get("report") or "")
            parts = pathlib.PureWindowsPath(report_ref).parts
            lowered = [part.lower() for part in parts]
            try:
                work_index = lowered.index("work")
                if work_index + 1 < len(parts) and parts[work_index + 1] == task_dir.name:
                    report_path = task_dir.joinpath(*parts[work_index + 2:])
            except ValueError:
                pass
        if report_path is None:
            report_dir = task_dir / "report" / (str(draft) if index == 1 else "report_%d" % index)
            report_path = report_dir / "draft_visual_qc.json"
        payload = _read_json(report_path)
        reports.append(str(payload.get("status") or "UNCERTIFIED").upper() if payload else "MISSING")
    if not reports:
        return "UNCERTIFIED", 0, reports
    if all(item == "PASS" for item in reports):
        return "CERTIFIED", len(reports), reports
    if any(item == "FAIL" for item in reports):
        return "FAIL", len(reports), reports
    return "UNCERTIFIED", len(reports), reports


def _write_status_snapshot() -> None:
    """Publish authoritative status; terminal done.json beats old start events."""
    latest: dict[str, dict] = {}
    if PROGRESS.is_file():
        for raw in PROGRESS.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except (ValueError, TypeError):
                continue
            rid = str(row.get("record_id") or "").strip()
            if rid:
                latest[rid] = row
    tasks = {}
    for task_dir in sorted(WORK.iterdir() if WORK.exists() else []):
        if not task_dir.is_dir():
            continue
        rid = task_dir.name
        event = latest.get(rid, {})
        done_path = task_dir / "done.json"
        done = _read_json(done_path) if done_path.is_file() else {}
        drafts = [str(item) for item in (done.get("drafts") or [])]
        qc_status, qc_count, qc_rows = _qc_state(task_dir, drafts, done)
        terminal = str(done.get("status") or "").upper()
        done_finished_at = str(done.get("finished_at") or "")
        new_attempt = (
            rid == ACTIVE_RECORD_ID
            and str(event.get("event") or "").lower() == "start"
            and str(event.get("ts") or "") > done_finished_at
        )
        if new_attempt:
            state, source = "RUNNING", "live_worker_new_attempt"
        elif done_path.is_file() and terminal in {"OK", "PARTIAL", "PREVIEW_READY", "ERROR", "DEFERRED"}:
            state, source = terminal, "done.json"
        elif (str(event.get("event") or "").lower() == "start"
              and rid == ACTIVE_RECORD_ID):
            state, source = "RUNNING", "live_worker_event"
        elif event:
            state, source = "STALE_ORPHANED", "historical_progress_only"
        else:
            state, source = "NOT_STARTED", "no_evidence"
        tasks[rid] = {
            "record_id": rid,
            "state": state,
            "terminal_status": terminal or None,
            "terminal_state": done.get("terminal_state"),
            "run_id": done.get("run_id") or event.get("run_id"),
            "parent_run_id": done.get("parent_run_id") or event.get("parent_run_id"),
            "failure_class": done.get("failure_class") or event.get("failure_class"),
            "failure_code": done.get("failure_code") or event.get("failure_code"),
            "retryable": bool(done.get("retryable", event.get("retryable", False))),
            "safe_to_continue": bool(done.get("safe_to_continue", event.get("safe_to_continue", False))),
            "next_action": done.get("next_action") or event.get("next_action"),
            "certification": qc_status,
            "qc_report_count": qc_count,
            "qc_reports": qc_rows,
            "done_path": str(done_path) if done_path.is_file() else None,
            "source": source,
            "last_event": event,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    payload = {
        "version": "status-reducer-v1",
        "rule": "done.json > live process > historical start",
        "worker_pid": os.getpid(),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
    }
    tmp = STATUS_SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_SNAPSHOT)


def mount_nas() -> bool:
    if os.path.exists(NAS_TEST):
        return True
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
         "-File", NAS_PROBE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    ok = out.returncode == 0 and os.path.exists(NAS_TEST)
    if not ok:
        log("MOUNT_FAIL rc=%s %s" % (out.returncode, (out.stdout or "")[-200:]))
    return ok


def video_files(material_dir: str) -> list[str]:
    d = pathlib.Path(material_dir)
    if not d.is_dir():
        return []
    hits = []
    # 素材目录可能是「父目录/日期子目录/文件」两级结构（实测：闪闪洗脸巾\2026.09.15），
    # 只列顶层会漏子目录；递归但限制深度为 3，避免把无关内容卷进来。
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        try:
            depth = len(p.relative_to(d).parts)
        except ValueError:
            continue
        if depth <= 3 and p.suffix.lower() in VIDEO_EXTS:
            hits.append(str(p))
    return hits


def calibrated_source_limit() -> int | None:
    """Read the render host's last verified source limit, if available."""
    try:
        data = json.loads(CAPACITY_PROFILE.read_text(encoding="utf-8"))
        limit = int(data.get("safe_source_limit") or 0)
        return limit if limit > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def validate_visual_match(report_dir: pathlib.Path) -> tuple[bool, str]:
    """Fail closed unless every selected script claim has real visual evidence."""
    analysis_path = report_dir / "shot_candidates.json"
    match_path = report_dir / "shot_match_report.json"
    if not analysis_path.is_file():
        return False, "VISUAL_MATCH_BLOCKED: shot_candidates.json missing"
    if not match_path.is_file():
        return False, "VISUAL_MATCH_BLOCKED: shot_match_report.json missing"
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        match = json.loads(match_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, "VISUAL_MATCH_BLOCKED: report unreadable: %s" % exc

    vision = analysis.get("vision_analysis") or {}
    errors = vision.get("errors") or []
    shots = analysis.get("shots") or []
    ready = [row for row in shots
             if isinstance(row, dict) and row.get("status") == "ready_for_matching"]
    if errors:
        first = errors[0] if isinstance(errors, (list, tuple)) else errors
        return False, "VISUAL_MATCH_BLOCKED: vision errors: %s" % str(first)[:600]
    if not shots:
        return False, "VISUAL_MATCH_BLOCKED: no candidate shots analysed"
    empty_desc = [row for row in shots if not isinstance(row, dict)
                  or not str(row.get("description") or "").strip()]
    if empty_desc:
        return False, "VISUAL_MATCH_BLOCKED: %d/%d candidate shots lack visual descriptions" % (len(empty_desc), len(shots))
    if not ready:
        return False, "VISUAL_MATCH_BLOCKED: no shots are ready_for_matching"

    delivery_manifest = match.get("manifest") or {}
    preview_mode = str(delivery_manifest.get("delivery_mode") or "").lower() in (
        "preview", "preview_only", "预览")
    preview_degraded_counters = {
        "visual_missing_count", "material_gap_count", "degraded_count",
        "degraded_fallback_count", "preview_unresolved_count"
    }
    for counter, label in (
        ("visual_missing_count", "visual-missing segments"),
        ("material_gap_count", "material gaps"),
        ("preview_unresolved_count", "unresolved previews"),
        ("degraded_count", "degraded matches"),
        ("degraded_fallback_count", "degraded fallbacks"),
    ):
        try:
            count = int(match.get(counter) or 0)
        except (TypeError, ValueError):
            return False, "VISUAL_MATCH_BLOCKED: invalid %s" % counter
        if count and not (preview_mode and counter in preview_degraded_counters):
            return False, "VISUAL_MATCH_BLOCKED: %s reported (%d)" % (label, count)
    for key, label in (
        ("visual_missing_matches", "visual-missing matches"),
        ("material_gaps", "material gaps"),
        ("material_gap_claims", "uncovered script claims"),
        ("preview_unresolved", "unresolved previews"),
        ("degraded_matches", "degraded matches"),
    ):
        rows = match.get(key) or []
        if rows and not (preview_mode and key in {
                "visual_missing_matches", "material_gaps", "material_gap_claims",
                "preview_unresolved", "degraded_matches"}):
            return False, "VISUAL_MATCH_BLOCKED: %s reported (%d)" % (label, len(rows))

    if match.get("ok") is not True:
        return False, "VISUAL_MATCH_BLOCKED: shot match report not ok"
    coverage = match.get("claim_gate_coverage") or {}
    if (coverage.get("certification") != "CERTIFIED"
            or coverage.get("validation_passed") is not True):
        # Preview delivery is explicitly allowed to carry a related,
        # lower-confidence shot.  The selected segment/path/description
        # checks below still reject broken or empty media; this branch only
        # stops the claim-gate certification from blocking the whole queue.
        if not preview_mode:
            return False, "VISUAL_MATCH_BLOCKED: claim gate not certified (%s)" % str(
                coverage.get("uncertified_reasons") or coverage.get("coverage_status") or "missing")
    segments = match.get("segments") or []
    if not segments:
        return False, "VISUAL_MATCH_BLOCKED: shot match report has no segments"
    accepted = match.get("accepted_shots") or []
    if not isinstance(accepted, list):
        return False, "VISUAL_MATCH_BLOCKED: accepted_shots is not a list"
    accepted_ids = set()
    for item in accepted:
        if isinstance(item, str):
            accepted_ids.add(item.strip())
        elif isinstance(item, dict):
            shot_id = item.get("temporary_shot_id") or item.get("shot_id")
            if shot_id:
                accepted_ids.add(str(shot_id).strip())
    required_segments = [row for row in segments
                         if isinstance(row, dict) and row.get("visual_missing") is not True]
    if len(accepted_ids) < len(required_segments):
        return False, "VISUAL_MATCH_BLOCKED: accepted shots fewer than non-missing script segments"

    from math import isfinite
    seen_ids = set()
    placeholders = {"", "n/a", "na", "none", "null", "unknown", "tbd",
                    "placeholder", "visual_missing", "unmatched"}
    for index, row in enumerate(segments, 1):
        if not isinstance(row, dict):
            return False, "VISUAL_MATCH_BLOCKED: segment %d is malformed" % index
        if (row.get("preview_unresolved") is True
                and row.get("visual_missing") is not True
                and not preview_mode):
            return False, "VISUAL_MATCH_BLOCKED: segment %d has missing/unresolved visuals" % index
        claim = str(row.get("claim_text") or row.get("text") or "").strip()
        if row.get("visual_missing") is True:
            if not preview_mode or not claim or str(row.get("selection_mode") or "") != "visual_missing":
                return False, "VISUAL_MATCH_BLOCKED: segment %d invalid intentional visual gap" % index
            duration = row.get("duration")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not isfinite(duration) or duration <= 0:
                return False, "VISUAL_MATCH_BLOCKED: segment %d invalid visual-gap timing" % index
            continue
        shot_id = str(row.get("temporary_shot_id") or "").strip()
        video = str(row.get("video") or "").strip()
        visual = str(row.get("visual_description") or "").strip()
        if not claim or not shot_id or not video or not visual or visual.casefold() in placeholders:
            return False, "VISUAL_MATCH_BLOCKED: segment %d lacks claim, shot id, video path, or visual description" % index
        if shot_id not in accepted_ids:
            return False, "VISUAL_MATCH_BLOCKED: segment %d shot is not in accepted_shots" % index
        if shot_id in seen_ids:
            return False, "VISUAL_MATCH_BLOCKED: temporary shot reused across segments"
        seen_ids.add(shot_id)
        if not os.path.isabs(video) or not os.path.isfile(video):
            return False, "VISUAL_MATCH_BLOCKED: segment %d video path missing/unreachable" % index
        source_start = row.get("source_start")
        duration = row.get("duration")
        if (not isinstance(source_start, (int, float)) or isinstance(source_start, bool)
                or not isfinite(source_start) or source_start < 0
                or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not isfinite(duration) or duration <= 0):
            return False, "VISUAL_MATCH_BLOCKED: segment %d lacks valid placement/timing" % index
        mode = str(row.get("selection_mode") or "").casefold()
        if ((row.get("degraded_no_match") is True or "degraded" in mode or "fallback" in mode)
                and not preview_mode):
            return False, "VISUAL_MATCH_BLOCKED: segment %d uses degraded/fallback matching" % index
        gate = row.get("claim_gate") or {}
        gate_required = gate.get("required") if isinstance(gate, dict) else {}
        claim_requirements = row.get("required_claims") or []
        gate_blocked = (isinstance(gate, dict)
                and (gate.get("ok") is False or str(gate.get("status") or "").casefold() in {"blocked", "failed", "error"})
                or isinstance(gate_required, dict) and gate_required.get("material_gap") is True
                or any(isinstance(req, dict) and req.get("material_gap") is True for req in claim_requirements))
        if gate_blocked and not (preview_mode and row.get("degraded_no_match") is True):
            return False, "VISUAL_MATCH_BLOCKED: segment %d claim gate blocked/uncovered" % index
    return True, "visual_match_valid"

def safe_manifest_stem(name: str) -> str:
    """Make a Windows-safe manifest filename without changing the draft name.

    Only the on-disk manifest file is sanitized; the Jianying draft_name inside
    the manifest keeps its original title, so drafts and their display names are
    never altered or renamed.
    """
    stem = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(name)).strip(" .")
    return stem or "draft"


def classify_build_failure(text: str) -> dict:
    """Auto-classify a build failure so the loop can pick a repair strategy.

    Transient = retryable with a different strategy (fresh vision, analysis
    reuse, or a longer timeout).  Hard = a genuine visual-analysis blocker that
    must be recorded, never faked as OK.
    """
    blob = text or ""
    lower_blob = blob.lower()
    hits = [m for m in TRANSIENT_BUILD_MARKERS if m.lower() in lower_blob]
    transient_signals = (
        "502", "503", "429", "too many requests", "temporarily unavailable",
        "timed out", "timeout", "connection reset", "connection refused",
        "could not resolve host", "network", "provider unavailable",
    )
    transient = any(marker in lower_blob for marker in transient_signals)
    credential_or_input_block = any(marker in lower_blob for marker in (
        "credential", "unauthorized", "forbidden", "invalid api key",
        "insufficient balance", "401", "403", "invalid path", "path not found",
        "file not found", "no such file", "encoding error", "unicode decode",
    ))
    deterministic_block = any(marker.lower() in lower_blob for marker in (
        "VISUAL_MATCH_BLOCKED", "SHOT_MATCH_BLOCKED", "AUDIO_VIDEO_MISMATCH",
        "TASK_BUDGET", "Invalid argument", "INVALID_ARGUMENT",
        "DESKTOP_IMPORT_BLOCKED", "DRAFT_INVALID",
    )) or credential_or_input_block or ("vision_call_failed" in lower_blob and not transient)
    vision_hard = ("draft_visual_qc_blocked" in lower_blob) or (
        "no auditable visual top1" in lower_blob) or (
        "visual_match_blocked" in lower_blob and not hits)
    return {
        "transient": bool(hits and transient and not credential_or_input_block),
        "vision_hard": vision_hard,
        "deterministic_block": deterministic_block,
        "markers": hits,
    }


def _is_reusable_analysis_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if not (str(row.get("description") or "").strip()
            and row.get("visual_tags") and row.get("evidence_tags")):
        return False
    try:
        return float(row.get("analysis_confidence") or 0) > 0
    except (TypeError, ValueError):
        return False


def run_build(manifest: dict, report_dir: pathlib.Path, draft_name: str, *,
              timeout: int | None = None, reuse_analysis: str | None = None,
              attempt: int = 1) -> dict:
    manifest = dict(manifest)
    manifest["draft_name"] = draft_name
    if reuse_analysis:
        try:
            cached_payload = json.loads(pathlib.Path(reuse_analysis).read_text(
                encoding="utf-8"))
            cached_shots = (cached_payload.get("shots") or []
                            if isinstance(cached_payload, dict) else [])
            reusable = [row for row in cached_shots
                        if _is_reusable_analysis_row(row)]
            if reusable:
                # The orchestrator consumes inline `shot_analysis`; the old
                # `shot_analysis_file` manifest key was ignored, so retries
                # re-ran vision despite a persisted candidate report.
                manifest["shot_analysis"] = {"shots": cached_shots}
                log("reuse partial shot analysis rows=%d/%d from %s" % (
                    len(reusable), len(cached_shots), reuse_analysis))
        except (OSError, ValueError, TypeError, AttributeError):
            log("cached shot analysis unavailable; continue with material cache")
    report_dir.mkdir(parents=True, exist_ok=True)
    mpath = report_dir.parent / (f"manifest.{safe_manifest_stem(draft_name)}"
                                 f".a{attempt}.json")
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    cmd = [PY, "-m", "orchestrator.cli", "build",
           "--input", str(mpath), "--report-dir", str(report_dir),
           "--delivery-mode", "preview"]
    env = dict(os.environ, PYTHONUTF8="1")
    # 视觉分析画像可通过 JY_VISION_PROFILE 切换。默认使用火山
    # CodingPlan 的 OpenAI-compatible profile，避免旧的 CC Switch
    # Anthropic Messages 路由返回空响应；需要回滚时显式传入其他 profile。
    vp = os.environ.get("JY_VISION_PROFILE", "volc").strip().strip('"').strip("'").strip()
    vm = os.environ.get("JY_VISION_MODEL", "").strip().strip('"').strip("'").strip()
    if vp:
        env["JY_VISION_PROFILE"] = vp
    if not vm and vp == "aijws":
        vm = "qwen3.8-flash"
    if vm:
        env["JY_VISION_MODEL"] = vm
    timeout = timeout or BUILD_TIMEOUT_SECONDS
    log("build draft=%s vision_profile=%s vision_model=%s timeout=%ss reuse=%s attempt=%s" % (
        draft_name, vp or "volc(default)", vm or "profile-default", timeout,
        "yes" if reuse_analysis else "no", attempt))
    p = subprocess.run(cmd, cwd=str(SKILL), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    return {"rc": p.returncode, "stdout": (p.stdout or "")[-20000:],
            "stderr": (p.stderr or "")[-20000:], "manifest": str(mpath),
            "combined": out}


def run_post_draft_qc(draft_dir: pathlib.Path, report_dir: pathlib.Path) -> dict:
    """Run the independent ChatCut-style visual acceptance after build.

    The build/match gate checks the plan. This second gate reads the generated
    JianYing draft and fresh frames from the actual placed source ranges, then
    runs an independent visual judgment. It is deliberately after build and
    before NAS shipping.
    """
    output = report_dir / "draft_visual_qc.json"
    if not POST_DRAFT_QC.is_file():
        return {"rc": 2, "status": "UNCERTIFIED",
                "message": "DRAFT_VISUAL_QC_BLOCKED: script missing",
                "output": str(output)}
    cmd = [PY, str(POST_DRAFT_QC), "--draft", str(draft_dir),
           "--report-dir", str(report_dir), "--output", str(output)]
    qc_timeout = _env_int("JY_POST_QC_TIMEOUT_S", 90, 30)
    env = dict(os.environ, PYTHONUTF8="1", JY_POST_QC_TIMEOUT_S=str(qc_timeout))
    try:
        p = subprocess.run(cmd, cwd=str(SKILL), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=qc_timeout + 30)
    except subprocess.TimeoutExpired as exc:
        # A provider timeout is not the same as a visual mismatch.  The draft
        # has already passed the build/match gate; archive it as an explicitly
        # uncertified preview so one slow QC call cannot rebuild the same draft
        # and block the queue.  Other QC failures remain hard blockers.
        return {"rc": 2, "status": "UNCERTIFIED_TIMEOUT",
                "message": "POST_DRAFT_VISION_QC_TIMEOUT: post-draft visual QC timeout (%ss); draft retained for deferred QC" % exc.timeout,
                "output": str(output), "nonblocking_timeout": True}
    payload = {}
    if output.is_file():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
    status = str(payload.get("status") or "UNCERTIFIED")
    issues_text = " ".join(str(item) for item in (payload.get("issues") or []))
    if p.returncode != 0 or status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
        if "timed out" in issues_text.lower() or "timeout" in issues_text.lower():
            return {"rc": 2, "status": "UNCERTIFIED_TIMEOUT",
                    "message": "POST_DRAFT_VISION_QC_TIMEOUT: internal visual call timed out; draft retained for deferred QC",
                    "output": str(output), "report": payload,
                    "nonblocking_timeout": True}
        issues = payload.get("issues") or ((p.stdout or "") + (p.stderr or ""))[-1200:]
        return {"rc": p.returncode or 2, "status": status,
                "message": "DRAFT_VISUAL_QC_BLOCKED: %s" % str(issues)[-1600:],
                "output": str(output), "report": payload}
    return {"rc": 0, "status": status, "output": str(output),
            "report": payload}


def ship_one(draft_dir: pathlib.Path, task_dir: pathlib.Path | None = None,
             frozen: dict | None = None) -> dict:
    """Ship a verified copy so path rewriting never mutates the reviewed draft."""
    source_identity = production_eval.output_identity([draft_dir])
    task_dir = task_dir or (WORK / '.manual-ship-stage')
    staging = task_dir / 'shipping-stage' / (uuid.uuid4().hex) / draft_dir.name
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(draft_dir, staging)
    if production_eval.output_identity([staging]) != source_identity:
        raise RuntimeError('SHIP_STAGING_SOURCE_HASH_MISMATCH')
    cmd = [PY, str(SKILL / "orchestrator" / "ship_distribute.py"),
           "--draft", str(staging), "--style", "unc"]
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run(cmd, cwd=str(SKILL), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    raw = (p.stdout or "").strip().splitlines()
    payload = None
    for line in reversed(raw):
        try:
            candidate = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if (p.returncode != 0 or not payload or payload.get('status') != 'OK'
            or payload.get('root_copied') is not True
            or payload.get('nas_root_verified') is not True
            or payload.get('distribution_verified') is not True
            or not payload.get('dist_target')):
        return {"rc": p.returncode or 2, "out": (p.stdout or p.stderr or "")[-1200:],
                "staging_path": str(staging), "source_output_sha256": source_identity['sha256'],
                "distribution": payload or {"status": "UNCERTIFIED"}}
    receipt = {
        'schema_version': 1, 'status': 'VERIFIED',
        'draft_name': draft_dir.name,
        'task_id': (frozen or {}).get('task_id'), 'run_id': (frozen or {}).get('run_id'),
        'contract_hash': (frozen or {}).get('contract_hash'),
        'source_output_sha256': source_identity['sha256'],
        'staging_path': str(staging),
        'nas_target': payload['dist_target'],
        'transformed_tree_sha256': payload['staged_tree_sha256'],
        'transformed_file_count': payload['staged_file_count'],
        'nas_root_verified': payload['nas_root_verified'],
        'distribution_verified': payload['distribution_verified'],
    }
    return {"rc": 0, "out": (p.stdout or "")[-600:], "receipt": receipt,
            "distribution": payload}


def prepare_mac_review(task_dir: pathlib.Path, task: dict, draft_names: list[str]) -> pathlib.Path:
    """Persist an exact Mac review handoff before NAS distribution."""
    outputs = [DRAFTS / name for name in draft_names]
    output_sha256 = production_eval.output_identity(outputs)["sha256"]
    frozen_path = task_dir / "eval" / "active_contract.json"
    frozen = production_eval.read(frozen_path) if frozen_path.is_file() else {}
    package = task_dir / ("mac_review_package-" + output_sha256 + ".json")
    payload = {
        "record_id": task.get("record_id"),
        "task_id": task.get("task_id"),
        "run_id": frozen.get("run_id"),
        "contract_hash": frozen.get("contract_hash"),
        "output_sha256": output_sha256,
        "draft_ids": production_eval.output_draft_ids(outputs),
        "title": task.get("title"),
        "drafts": [str(DRAFTS / name) for name in draft_names],
        "review_required": True,
        "approval_file": str(_mac_approval_path(task_dir, draft_names, frozen)),
        "instruction": "Mac 检查通过后，由授权检查流程写入 mac_review_approved.json；未批准不得 NAS 分发或表格回写。",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    production_eval.write_once(package, payload)
    return package


def _mac_approval_path(task_dir: pathlib.Path, draft_names: list[str], frozen: dict | None) -> pathlib.Path:
    if not frozen or not frozen.get('run_id'):
        return task_dir / 'mac_review_approved.json'
    output_sha256 = production_eval.output_identity([DRAFTS / name for name in draft_names])['sha256']
    return task_dir / ('mac_review_approved-' + output_sha256 + '.json')


def finalize_closed_loop(task_dir: pathlib.Path, task: dict, drafts: list[str],
                         qc_reports: list[dict],
                         delivery_receipts: list[dict] | None = None) -> dict:
    """Evaluate first, then ship hash-verified copies and write back the table."""
    done_path = task_dir / "done.json"
    existing_done = _read_json(done_path)
    provisional = dict(existing_done)
    provisional.update({"record_id": task.get("record_id"), "status": "PREVIEW_READY",
        "drafts": list(drafts), "local_only_drafts": list(drafts),
        "qc_reports": qc_reports, "nas_links": [],
        "distribution_status": "PENDING_EVAL_OR_REVIEW",
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    frozen_path = task_dir / "eval" / "active_contract.json"
    frozen = None
    eval_result = None
    if frozen_path.is_file():
        frozen = production_eval.read(frozen_path)
        result = evaluate_task(task_dir, task, drafts, qc_reports, frozen)
        eval_result = result
        provisional["eval_receipt"] = result
        mac_status = next((row.get('status') for row in result.get('criteria', [])
                           if row.get('id') == 'MAC-REVIEW'), 'UNCERTIFIED')
        if MAC_REVIEW_REQUIRED and mac_status != 'PASS':
            done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"ok": False, "status": "BLOCKED", "reason": "MAC_REVIEW_EVAL_NOT_PASS", "eval": result}
        if frozen["mode"] == "enforce" and result["status"] != "PASS":
            done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"ok": False, "status": "BLOCKED", "reason": "EVAL_DELIVERY_GATE", "eval": result}
    elif os.environ.get('JY_EVAL_MODE') == 'enforce':
        done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": False, "status": "BLOCKED", "reason": "EVAL_CONTRACT_MISSING"}
    if MAC_REVIEW_REQUIRED and not _mac_approval_path(task_dir, drafts, frozen).is_file():
        done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": False, "status": "BLOCKED", "reason": "MAC_REVIEW_PENDING"}

    receipts = list(delivery_receipts or existing_done.get('distribution_receipts') or [])
    by_name = {str(row.get('draft_name') or ''): row for row in receipts if isinstance(row, dict)}
    if frozen is None:
        frozen = {}
    provisional['eval_receipt'] = eval_result or provisional.get('eval_receipt')
    provisional['distribution_receipts'] = []
    done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in drafts:
        source = DRAFTS / name
        current_identity = production_eval.output_identity([source])
        receipt = by_name.get(name)
        if receipt:
            if (receipt.get('status') != 'VERIFIED'
                    or receipt.get('source_output_sha256') != current_identity['sha256']
                    or receipt.get('draft_name') != name
                    or (frozen.get('task_id') and receipt.get('task_id') != frozen['task_id'])
                    or (frozen.get('run_id') and receipt.get('run_id') != frozen['run_id'])
                    or (frozen.get('contract_hash')
                        and receipt.get('contract_hash') != frozen['contract_hash'])):
                return {"ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_SOURCE_DRIFT", "draft": name}
            target = Path(str(receipt.get('nas_target') or ''))
            try:
                target_identity = tree_identity(target)
            except (OSError, ValueError):
                return {"ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_READBACK_MISSING", "draft": name}
            if target_identity['sha256'] != receipt.get('transformed_tree_sha256'):
                return {"ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_READBACK_DRIFT", "draft": name}
        else:
            shipped = ship_one(source, task_dir=task_dir, frozen=frozen)
            if shipped.get('rc') != 0:
                provisional['distribution_status'] = 'PENDING_SHIP_VERIFICATION'
                done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
                return {"ok": False, "status": "BLOCKED", "reason": "SHIP_OR_READBACK_FAILED",
                        "draft": name, "detail": shipped.get('out')}
            receipt = shipped['receipt']
        if frozen.get('contract_hash'):
            receipt['eval_receipt_sha256'] = (eval_result or {}).get('receipt_sha256')
            receipt['eval_status'] = (eval_result or {}).get('status', 'UNCERTIFIED')
        receipt['status'] = 'VERIFIED'
        receipt_body = dict(receipt)
        receipt_body.pop('receipt_sha256', None)
        receipt['receipt_sha256'] = production_eval.digest(receipt_body)
        receipt_dir = task_dir / 'eval' / (frozen.get('run_id') or 'legacy')
        receipt_path = receipt_dir / ('distribution-' + receipt['receipt_sha256'] + '.json')
        production_eval.write_once(receipt_path, receipt)
        bound = dict(receipt, receipt_path=str(receipt_path))
        provisional['distribution_receipts'].append(bound)
        provisional['nas_links'].append(receipt['nas_target'])
        provisional['distribution_status'] = 'SHIPPED_VERIFIED'
        done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
    if len(provisional['distribution_receipts']) != len(drafts):
        return {"ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_RECEIPT_COUNT_MISMATCH"}
    provisional['status'] = 'OK'
    provisional['local_only_drafts'] = []
    done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
    if not CLOSED_LOOP_TOOL.is_file() or not CLOSED_LOOP_POLICY.is_file():
        return {"ok": False, "status": "BLOCKED", "reason": "CLOSED_LOOP_TOOL_OR_POLICY_MISSING"}
    common = [PY, str(CLOSED_LOOP_TOOL), "--task-json", str(task_dir / "task.json"), "--done-json", str(done_path), "--policy", str(CLOSED_LOOP_POLICY)]
    archive = subprocess.run(common[:2] + ["archive-parent"] + common[2:], cwd=str(PIPE), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if archive.returncode != 0:
        return {"ok": False, "status": "BLOCKED", "reason": "ARCHIVE_PARENT_FAILED", "output": (archive.stdout or archive.stderr)[-2000:]}
    update = subprocess.run(common[:2] + ["table-update"] + common[2:], cwd=str(PIPE), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if update.returncode != 0:
        return {"ok": False, "status": "BLOCKED", "reason": "TABLE_UPDATE_FAILED", "output": (update.stdout or update.stderr)[-2000:]}
    try:
        receipt = json.loads(update.stdout)
    except (ValueError, TypeError):
        receipt = {"raw": update.stdout[-2000:]}
    return {"ok": True, "status": "CLOSED_LOOP_COMPLETE", "archive": archive.stdout[-2000:], "table": receipt}


def evaluate_task(task_dir: pathlib.Path, task: dict, drafts: list[str],
                  qc_reports: list[dict], frozen: dict) -> dict:
    """Persist current-output evidence before any final distribution/table gate."""
    outputs = [DRAFTS / name for name in drafts]
    eval_dir = task_dir / "eval" / frozen["run_id"]
    eval_dir.mkdir(parents=True, exist_ok=True)
    identity = production_eval.output_identity(outputs)
    binding = {"task_id": frozen["task_id"], "run_id": frozen["run_id"],
               "contract_hash": frozen["contract_hash"], "output_sha256": identity["sha256"]}
    checks = [validate_draft_package(path) for path in outputs]
    package = dict(binding, status="PASS" if checks and all(c["ok"] for c in checks) else "FAIL",
                   files_verified=bool(checks) and all(c["ok"] for c in checks), checks=checks)
    package_path = eval_dir / ("package-" + identity["sha256"] + ".json")
    production_eval.write_once(package_path, package)
    qc_files = [pathlib.Path(str(row.get("report") or "")) for row in qc_reports]
    qc_payloads = [_read_json(path) if path.is_file() else {} for path in qc_files]
    statuses = [str(row.get("status") or "UNCERTIFIED") for row in qc_payloads]
    qc_status = ("FAIL" if "FAIL" in statuses else
                 "PASS_WITH_DEGRADED" if statuses and len(statuses) == len(drafts)
                 and all(s in {"PASS", "PASS_WITH_DEGRADED"} for s in statuses)
                 and "PASS_WITH_DEGRADED" in statuses else
                 "PASS" if len(statuses) == len(drafts) and statuses and all(s == "PASS" for s in statuses)
                 else "UNCERTIFIED")
    qc = dict(binding, qc_status=qc_status,
              status="FAIL" if qc_status == "FAIL" else "PASS" if qc_status in {"PASS", "PASS_WITH_DEGRADED"} else "UNCERTIFIED",
              reports=[{"path": str(p), "sha256": production_eval.file_hash(p)} for p in qc_files if p.is_file()])
    qc_path = eval_dir / ("qc-" + production_eval.digest(qc) + ".json")
    production_eval.write_once(qc_path, qc)
    proof_map = {"DRAFT-PACKAGE": package_path, "VISUAL-QC": qc_path}
    selection_path = eval_dir / ("review-selection-" + identity["sha256"] + ".json")
    try:
        selection = production_eval.read(selection_path)
        selection_hash = selection.pop('selection_sha256', None)
        if selection_hash != production_eval.digest(selection):
            raise ValueError('REVIEW_SELECTION_HASH_MISMATCH')
        for key, criterion in (('gold_quality_review', 'GOLD-QUALITY'), ('mac_review', 'MAC-REVIEW')):
            ref = selection.get(key) or {}
            path = pathlib.Path(str(ref.get('path') or ''))
            if production_eval.file_hash(path) != ref.get('sha256'):
                raise ValueError('REVIEW_SELECTION_EVIDENCE_HASH_MISMATCH:' + key)
            proof_map[criterion] = path
    except (OSError, ValueError, KeyError, TypeError):
        proof_map['GOLD-QUALITY'] = eval_dir / 'missing-gold-quality-review.json'
        proof_map['MAC-REVIEW'] = eval_dir / 'missing-mac-review.json'
    runtime_root = pathlib.Path(os.environ.get("JY_EVAL_RUNTIME_ROOT", str(PIPE_ROOT)))
    try:
        runtime = production_eval.runtime_identity(runtime_root)
    except (OSError, ValueError, KeyError) as exc:
        runtime = {"verification_error": str(exc)}
    result = production_eval.evaluate(frozen, task, runtime, outputs, proof_map)
    path = eval_dir / ("receipt-" + result["receipt_sha256"] + ".json")
    production_eval.write_once(path, result)
    return dict(result, receipt_path=str(path))


def publish_failure_notification(task_dir: pathlib.Path) -> dict:
    """Best-effort failure status/feedback; never blocks the production queue."""
    task_path = task_dir / "task.json"
    done_path = task_dir / "done.json"
    if not CLOSED_LOOP_TOOL.is_file() or not CLOSED_LOOP_POLICY.is_file():
        return {"ok": False, "reason": "CLOSED_LOOP_TOOL_OR_POLICY_MISSING"}
    command = [PY, str(CLOSED_LOOP_TOOL), "failure-update",
               "--task-json", str(task_path), "--done-json", str(done_path),
               "--policy", str(CLOSED_LOOP_POLICY)]
    try:
        result = subprocess.run(command, cwd=str(PIPE), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=150)
        raw = (result.stdout or result.stderr or "")[-6000:]
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {"ok": False, "reason": raw}
        payload.setdefault("returncode", result.returncode)
        log("FAILURE_NOTIFICATION record=%s ok=%s feedback_only=%s" % (
            task_dir.name, payload.get("ok"), payload.get("feedback_only_ok")))
        return payload
    except Exception as exc:  # noqa: BLE001
        # The task is already durably failed in done.json.  A notification
        # outage must not hold the next queue item.
        log("FAILURE_NOTIFICATION_DEFERRED record=%s reason=%s" % (task_dir.name, exc))
        return {"ok": False, "reason": str(exc)}


def validate_draft_package(draft_dir: pathlib.Path) -> dict:
    """Hard gate the package before NAS shipping.

    A successful build or a timed-out visual model call is not evidence that
    JianYing contains usable video.  Verify the draft graph and the resolved
    local media paths first; otherwise shipping would turn an unverified
    audio-only/empty draft into a false delivery.
    """
    info_path = next((p for p in (draft_dir / "draft_info.json",
                                  draft_dir / "draft_content.json")
                      if p.is_file()), None)
    if info_path is None:
        return {"ok": False, "issues": ["DRAFT_INFO_MISSING"]}
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "issues": ["DRAFT_INFO_INVALID:%s" % exc]}
    tracks = [t for t in (info.get("tracks") or []) if isinstance(t, dict)]
    preferred = [t for t in tracks if str(t.get("name") or "").casefold() in {
        "video_broll", "video", "main video", "主视频", "视频"
    }]
    video_tracks = preferred or [t for t in tracks if t.get("segments")]
    segments = [s for t in video_tracks for s in (t.get("segments") or [])
                if isinstance(s, dict)]
    if not video_tracks:
        return {"ok": False, "issues": ["VIDEO_TRACK_MISSING"]}
    if not segments:
        return {"ok": False, "issues": ["VIDEO_SEGMENTS_EMPTY"]}
    materials = {}
    for item in (info.get("materials") or {}).get("videos") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("material_id") or "").strip()
        if key:
            materials[key] = item
    issues = []
    resolved = 0
    real_segments = 0
    for idx, segment in enumerate(segments, 1):
        material_id = str(segment.get("material_id") or "").strip()
        item = materials.get(material_id)
        if not item:
            issues.append("SEGMENT_%d_MATERIAL_MISSING:%s" % (idx, material_id))
            continue
        raw = str(item.get("path") or item.get("media_path") or "").strip()
        source = pathlib.Path(raw) if raw else pathlib.Path()
        if not source.is_file():
            source = draft_dir / "media" / pathlib.Path(raw).name
        if not source.is_file():
            issues.append("SEGMENT_%d_MEDIA_MISSING:%s" % (idx, raw or material_id))
            continue
        resolved += 1
        if source.suffix.lower() in VIDEO_EXTS:
            real_segments += 1
    if real_segments <= 0:
        issues.append("VIDEO_MEDIA_RESOLVED_ZERO")
    return {"ok": not issues, "issues": issues,
            "video_tracks": len(video_tracks), "video_segments": len(segments),
            "resolved_video_media": resolved, "real_video_segments": real_segments}


def reusable_analysis_rows(candidate_report: pathlib.Path) -> tuple[int, int]:
    """Count auditable rows in an incremental shot report for retry reuse."""
    try:
        payload = json.loads(candidate_report.read_text(encoding="utf-8"))
        shots = payload.get("shots") or [] if isinstance(payload, dict) else []
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0
    ready = sum(1 for row in shots if _is_reusable_analysis_row(row))
    return ready, len(shots)


def existing_task_has_nonpass_qc(task_dir: pathlib.Path) -> bool:
    """Detect stale OK markers whose persisted visual QC is not PASS.

    A resumed task must not trust ``done.json.status == OK`` by itself:
    older workers could retain a draft after a post-draft timeout and then
    incorrectly promote the task to OK on the next single-step run.
    Missing QC files are kept backward-compatible; an existing non-PASS file
    is an explicit reason to re-check the draft.
    """
    done = _read_json(task_dir / "done.json")
    drafts = [str(item) for item in (done.get("drafts") or [])]
    if not drafts:
        return False
    for index, draft in enumerate(drafts, 1):
        report_dir = task_dir / "report" / (draft if index == 1 else "report_%d" % index)
        path = report_dir / "draft_visual_qc.json"
        if not path.is_file():
            return True
        payload = _read_json(path)
        if str(payload.get("status") or "").upper() != "PASS":
            return True
    return False


def prior_draft_requires_recheck(task_dir: pathlib.Path, index: int,
                                 base_draft: str, previous: dict) -> bool:
    """Return whether a retained draft has unresolved acceptance evidence."""
    deferred = set()
    for raw in (previous.get("deferred_draft_indices") or []):
        try:
            deferred.add(int(raw))
        except (TypeError, ValueError):
            continue
    if index in deferred:
        return True
    for failure in (previous.get("failures") or []):
        if not isinstance(failure, dict):
            continue
        try:
            failure_index = int(failure.get("index") or 0)
        except (TypeError, ValueError):
            failure_index = 0
        if failure_index == index:
            return True
    report_dir = task_dir / "report" / (
        base_draft if index == 1 else "report_%d" % index)
    qc_path = report_dir / "draft_visual_qc.json"
    if not qc_path.is_file():
        return False
    try:
        payload = json.loads(qc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return True
    return str(payload.get("status") or "").upper() != "PASS"


def process_task(task: dict, *, retry_partial: bool = False,
                 retry_error: bool = False) -> None:
    global ACTIVE_RECORD_ID
    rid = task["record_id"]
    run_id = "%s-%s-%s" % (rid, time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    parent_run_id = str(task.get("parent_run_id") or "").strip() or None
    ACTIVE_RECORD_ID = str(rid)
    force_rebuild = bool(task.get("force_rebuild"))
    task_id = str(task.get("task_id") or rid)
    manifest_sha256 = _payload_sha256(task)
    runtime = _runtime_facts()
    log("=" * 60)
    log("TASK %s title=%r count=%s voice=%s" % (rid, task.get("title"),
                                                task.get("count", 1),
                                                task.get("voice")))
    progress({"record_id": rid, "event": "start", "run_id": run_id,
              "parent_run_id": parent_run_id, "pipeline_version": _pipeline_version()})
    tdir = WORK / rid
    tdir.mkdir(parents=True, exist_ok=True)
    report_root = tdir / "report"
    report_root.mkdir(parents=True, exist_ok=True)

    def contract(status: str, *, failures: list[dict] | None = None,
                 error: str = "") -> dict:
        return _batch_result_contract(
            status, run_id=run_id, failures=failures, error=error,
            parent_run_id=parent_run_id, task_id=task_id,
            manifest_sha256=manifest_sha256, runtime=runtime,
            task_log=str(report_root))

    try:
        record_lock = _acquire_record_lock(tdir, run_id)
    except RuntimeError as exc:
        receipt = contract("ERROR", error=str(exc))
        progress({"record_id": rid, "event": "blocked",
                  "error": str(exc), **receipt})
        log("TASK BLOCKED %s: %s" % (rid, exc))
        return
    done = tdir / "done.json"
    previous_payload = {}
    previous_drafts = []
    previous_deferred_indices = []
    if done.exists():
        try:
            prev = json.loads(done.read_text(encoding="utf-8"))
            previous_payload = prev if isinstance(prev, dict) else {}
            previous_drafts = [str(name) for name in (prev.get("drafts") or [])]
            previous_deferred_indices = list(prev.get("deferred_draft_indices") or [])
            if not parent_run_id:
                parent_run_id = str(prev.get("run_id") or "").strip() or None
            if force_rebuild:
                log("force_rebuild=true; prior drafts are retained but will not be reused")
                previous_drafts = []
                previous_deferred_indices = []
            if prev.get("status") == "OK" and not force_rebuild:
                if not existing_task_has_nonpass_qc(tdir):
                    log("already done, skip")
                    _release_record_lock(record_lock, run_id)
                    return
                log("previous OK has non-PASS visual QC evidence; rechecking")
            if prev.get("status") == "PARTIAL" and not force_rebuild and not (
                retry_partial or bool(task.get("retry_deferred"))
            ):
                log("partial task deferred, skip; explicit recovery required; stop this run")
                progress({"record_id": rid, "event": "deferred_skip",
                          "drafts": previous_drafts,
                          "run_id": run_id,
                          **contract("PARTIAL", failures=prev.get("failures") or []),
                          "deferred_draft_indices": prev.get(
                              "deferred_draft_indices", [])})
                _release_record_lock(record_lock, run_id)
                return
            if prev.get("status") == "ERROR" and not force_rebuild and not (
                retry_error or bool(task.get("retry_error"))
            ):
                log("previous error deferred, skip; explicit recovery required; stop this run")
                progress({"record_id": rid, "event": "error_deferred_skip",
                          "error": prev.get("error", ""),
                          "run_id": run_id,
                          **contract("ERROR", failures=prev.get("failures") or [],
                                     error=prev.get("error", ""))})
                _release_record_lock(record_lock, run_id)
                return
            log("previous attempt was %s (%s), retrying" % (prev.get("status"),
                                                            prev.get("error", "")[:80]))
        except (OSError, ValueError):
            pass
    try:
        if not mount_nas():
            raise RuntimeError("NAS mount failed")
        mdir = str(task.get("material_dir") or "").strip()
        if not mdir:
            raise RuntimeError("material_dir empty")
        sources = video_files(mdir)
        if not sources:
            raise RuntimeError("no video files under material_dir: %s" % mdir)
        log("candidate_pool=%d" % len(sources))
        safe_limit = calibrated_source_limit()
        requested_limit = int(task.get("source_limit") or 0)
        effective_limit = requested_limit or safe_limit or 0
        selection_info = {
            "original_count": len(sources),
            "selected_count": None,
            "limit": effective_limit or None,
            "strategy": "full_pool_then_script_driven_semantic_match",
            "selection_stage": "orchestrator_temporary_match",
            "final_unique_source_limit": effective_limit or None,
        }
        log("candidate_pool=%d; script_match_required=true; final_unique_source_limit=%s" % (
            len(sources), effective_limit or "none"))
        count = max(1, int(task.get("count") or 1))
        base_draft = str(task.get("title") or rid).strip() or rid
        manifest = {
            "mode": "standard",
            "task_id": str(task.get("task_id") or rid),
            "record_id": rid,
            "parent_run_id": parent_run_id,
            "full_script": str(task.get("script") or "").strip(),
            "voice": str(task.get("voice") or "").strip(),
            "bgm": {"auto": True},
            # 音色/BGM 已由飞书表格给定（运营已选定），等于运营侧确认，必须显式
            # 声明 —— 否则引擎会进 audio_selection_gate，要求可播放的试听候选，
            # 批量环境下会直接 BGM_PREVIEW_UNAVAILABLE。与 R3 可跑通稿口径一致。
            "audio_selection_confirmed": True,
            "material_sources": sources,
            "source_selection": selection_info,
            "clip_selection_policy": {
                "mode": "script_driven_semantic_match",
                "derive_clip_count_from_script": True,
                "candidate_pool_is_not_final_clip_count": True,
                "max_final_unique_sources": effective_limit or None,
            },
            "temporary_material_analysis": True,
            "max_source_reuse": 2,
            "head_trim_s": 0.3,
            "tail_trim_s": 0,
        }
        task_id = str(manifest["task_id"])
        manifest_sha256 = _payload_sha256(manifest)
        if not manifest["full_script"]:
            raise RuntimeError("script empty, skip (nbz_test-like row)")
        index_file = str(task.get("material_index_file") or os.environ.get("JY_MATERIAL_INDEX_FILE") or "").strip()
        if index_file:
            cached_index = production_eval.read(pathlib.Path(index_file))
            policy_hash = production_eval.file_hash(CLOSED_LOOP_POLICY)
            approved_rows = material_index.select(cached_index.get("materials", []),
                category=str(task.get("category") or ""), sku=str(task.get("sku") or ""),
                tags=task.get("shot_tags") or [], policy_hash=policy_hash,
                tagger_version=cached_index.get("tagger_version", ""))
            if not approved_rows:
                raise RuntimeError("MATERIAL_INDEX_NO_APPROVED_SAME_SKU")
            cached_analysis = tdir / "material_index_analysis.json"
            cached_analysis.write_text(json.dumps(material_index.shot_analysis(approved_rows), ensure_ascii=False, indent=2), encoding="utf-8")
            manifest["shot_analysis_file"] = str(cached_analysis)
            manifest["material_sources"] = list(dict.fromkeys(row["source_path"] for row in approved_rows))
            manifest["material_index_sha256"] = production_eval.file_hash(pathlib.Path(index_file))
            manifest["material_cache_reused"] = True
            sources = manifest["material_sources"]
        configured_mode = os.environ.get("JY_EVAL_MODE", "observe")
        eval_mode = "enforce" if configured_mode == "enforce" else str(task.get("eval_mode") or configured_mode)
        runtime_root = pathlib.Path(os.environ.get("JY_EVAL_RUNTIME_ROOT", str(PIPE_ROOT)))
        runtime_identity = production_eval.runtime_identity(runtime_root)
        if eval_mode == "enforce":
            observations = os.environ.get("JY_EVAL_OBSERVATIONS", "").strip()
            if not observations or not production_eval.verify_observations(pathlib.Path(observations), runtime_identity)["ready"]:
                raise RuntimeError("EVAL_ROLLOUT_REQUIRES_TWO_VERIFIED_REAL_TASKS")
        gold_path = pathlib.Path(os.environ.get("JY_GOLD_REFERENCE_MANIFEST", str(PIPE_ROOT / "gold-reference.json")))
        gold = production_eval.read(gold_path)
        # A run-specific freeze precedes TTS, vision and draft generation. It
        # does not alter any retained historical contract or approval.
        frozen = production_eval.freeze(task, run_id, [pathlib.Path(p) for p in sources],
                                         runtime_identity, gold, eval_mode)
        frozen_path = tdir / "eval" / run_id / "contract.json"
        production_eval.write_once(frozen_path, frozen)
        active_contract = tdir / "eval" / "active_contract.json"
        active_contract.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
        drafts = []
        local_only_drafts = []
        failures = []
        qc_reports = []
        distribution_receipts = []
        qc_gaps = []
        review_pending = False
        repair_visual_missing_indices = set()
        analysis_cache = None
        # A partial task may already have shipped draft 1 before the queue
        # moved on.  Recover its validated candidate report so draft 2/3 do
        # not re-run the entire visual scan from scratch.
        existing_report = tdir / "report" / base_draft / "shot_candidates.json"
        if existing_report.exists():
            reusable_count, shot_count = reusable_analysis_rows(existing_report)
            if reusable_count:
                analysis_cache = str(existing_report)
                log("reuse existing shot analysis rows=%d/%d for remaining drafts: %s" % (
                    reusable_count, shot_count, analysis_cache))
        shared_analysis = os.environ.get("JY_SHARED_ANALYSIS_FILE", "").strip()
        if shared_analysis:
            shared_path = pathlib.Path(shared_analysis)
            if shared_path.is_file():
                reusable_count, shot_count = reusable_analysis_rows(shared_path)
                if reusable_count and reusable_count > (reusable_analysis_rows(pathlib.Path(analysis_cache))[0] if analysis_cache else 0):
                    analysis_cache = str(shared_path)
                    log("reuse shared shot analysis rows=%d/%d: %s" % (
                        reusable_count, shot_count, analysis_cache))
        # A large candidate pool is scanned in full, but it must not monopolise
        # the only queue worker. Keep a shorter wall-clock slice for oversized
        # pools; the retained material cache/report makes the next pass cheap.
        task_budget_seconds = TASK_BUDGET_SECONDS
        if os.environ.get("JY_TASK_BUDGET_OVERRIDE", "").strip() != "1":
            if len(sources) >= 400:
                task_budget_seconds = min(task_budget_seconds, 900)
            elif len(sources) >= 200:
                task_budget_seconds = min(task_budget_seconds, 1200)
        log("task_wall_clock_budget=%ss candidate_pool=%d" % (
            task_budget_seconds, len(sources)))
        task_deadline = time.time() + task_budget_seconds
        for i in range(1, count + 1):
            name = base_draft if i == 1 else f"{base_draft}-{i}"

            # A partial task is resumable: keep already shipped drafts and only
            # retry the missing slot.  Without this, a timeout on draft 3 would
            # rebuild/re-upload drafts 1 and 2 on the next queue pass.
            if i <= len(previous_drafts):
                prior = previous_drafts[i - 1]
                needs_recheck = prior_draft_requires_recheck(
                    tdir, i, base_draft, previous_payload)
                if (DRAFTS / prior).exists() and not needs_recheck:
                    drafts.append(prior)
                    log("already shipped draft %d/%d, keep: %s" % (i, count, prior))
                    continue
                if needs_recheck:
                    log("retained draft %d/%d has unresolved QC evidence; recheck: %s" % (
                        i, count, prior))
                    retained_report_dir = tdir / "report" / (
                        base_draft if i == 1 else "report_%d" % i)
                    retained_qc = run_post_draft_qc(
                        DRAFTS / prior, retained_report_dir)
                    retained_status = str(retained_qc.get("status") or "UNCERTIFIED")
                    if retained_status in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS", "UNCERTIFIED_TIMEOUT"}:
                        drafts.append(prior)
                        qc_reports.append({"draft": prior, "status": retained_status,
                                           "report": retained_qc.get("output")})
                        if retained_status in {"PASS_WITH_GAPS", "UNCERTIFIED_TIMEOUT"}:
                            qc_gaps.append({
                                "index": i, "draft": prior,
                                "phase": "post_draft_qc",
                                "type": "uncertified_preview" if retained_status == "PASS_WITH_GAPS" else "uncertified_timeout",
                                "message": retained_qc.get("message") or "retained draft is not formally certified",
                                "qc_report": retained_qc.get("output"),
                            })
                        log("retained draft %d/%d QC=%s; no duplicate rebuild: %s" % (
                            i, count, retained_status, prior))
                        continue
                    log("retained draft %d/%d QC=%s; rebuild requested: %s" % (
                        i, count, retained_status, prior))

            rd = tdir / "report" / (name if i == 1 else "report_%d" % i)
            draft_ok = False
            last_failure = None
            for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
                # Escalating strategies so one broken build never repeats
                # identically.  The first draft may scan the pool once; all
                # later drafts reuse that validated analysis, and retries
                # keep reusing it.  This skips repeated relay stalls and
                # makes three variants share one material-understanding pass.
                # Hard visual blockers are recorded, never faked as OK.
                #
                # Each attempt writes to a brand-new draft name so a partially
                # created draft from a failed attempt can never collide with the
                # next attempt (剪映 rejects "target draft already exists").
                a_name = name if attempt == 1 else f"{name}-{time.strftime('%H%M%S')}"
                guard = 0
                while (DRAFTS / a_name).exists():
                    guard += 1
                    a_name = f"{name}-{time.strftime('%H%M%S')}-{guard}"
                if a_name != name:
                    log("draft name bump (attempt %d): %s -> %s" % (
                        attempt, name, a_name))
                # Once a candidate analysis has been validated, reuse it for
                # every later draft in this task.  Re-running the same pool
                # for draft 2/3 was the main source of queue-wide delays.
                reuse = analysis_cache
                timeout = BUILD_TIMEOUT_SECONDS
                if attempt >= 2 and (not reuse or attempt >= 3):
                    timeout = BUILD_TIMEOUT_SECONDS * TIMEOUT_ESCALATION_MULT
                # Never exceed the per-task wall-clock budget; a task that
                # cannot finish inside it is recorded as BLOCKED and the queue
                # moves on instead of being stalled by an oversized pool.
                remaining = int(task_deadline - time.time())
                if remaining < 120:
                    last_failure = {
                        "index": i, "draft": a_name, "phase": "task_budget",
                        "type": "blocked",
                        "message": "素材池过大，任务墙钟预算(%ss)已用尽，延后单独处理；已保留既有草稿，未伪造 OK" % task_budget_seconds,
                    }
                    log("TASK BUDGET EXHAUSTED draft %d/%d after %s attempt(s); stop current task" % (i, count, attempt))
                    break
                timeout = min(timeout, remaining)
                try:
                    build_manifest = dict(manifest)
                    # Same-script variants must share one analysis cache but
                    # receive a deterministic rotation index. The allocator
                    # may rotate only within a near-tied, gate-valid pool.
                    build_manifest["variant_index"] = i - 1
                    build_manifest["variant_count"] = count
                    build_manifest["variant_pool_size"] = min(5, max(3, count))
                    build_manifest["variant_score_tolerance"] = 8
                    if repair_visual_missing_indices:
                        build_manifest["forced_visual_missing_indices"] = sorted(
                            repair_visual_missing_indices)
                        log("post-draft visual repair: force blank segments=%s" %
                            sorted(repair_visual_missing_indices))
                    res = run_build(build_manifest, rd, a_name, timeout=timeout,
                                    reuse_analysis=reuse, attempt=attempt)
                    candidate_report = rd / "shot_candidates.json"
                    if analysis_cache is None and candidate_report.exists():
                        reusable_count, shot_count = reusable_analysis_rows(candidate_report)
                        if reusable_count:
                            analysis_cache = str(candidate_report)
                            log("cache partial shot analysis rows=%d/%d before match gate: %s" % (
                                reusable_count, shot_count, analysis_cache))
                    failure_text = ""
                    qc_result = None
                    if res["rc"] != 0:
                        fb = tdir / ("build_fail_%d_a%d.log" % (i, attempt))
                        fb.write_text(
                            "RC=%s\n----STDOUT(20k)----\n%s\n----STDERR(20k)----\n%s\n"
                            % (res["rc"], res["stdout"], res["stderr"]),
                            encoding="utf-8")
                        failure_text = (res.get("combined") or res["stderr"])
                        failure_text += "\n[build rc=%s log=%s]" % (
                            res["rc"], fb.name)
                    else:
                        visual_ok, visual_message = validate_visual_match(rd)
                        if not visual_ok:
                            failure_text = visual_message
                        else:
                            qc_result = run_post_draft_qc(DRAFTS / a_name, rd)
                            if (qc_result.get("status") not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}
                                    and qc_result.get("status") != "UNCERTIFIED_TIMEOUT"):
                                failure_text = qc_result.get("message") or (
                                    "DRAFT_VISUAL_QC_BLOCKED: status=%s" % qc_result.get("status"))
                            elif qc_result.get("status") == "UNCERTIFIED_TIMEOUT":
                                log("DRAFT %d/%d retained as UNCERTIFIED_TIMEOUT; defer post-draft visual QC: %s" % (
                                    i, count, a_name))
                    if failure_text:
                        cls = classify_build_failure(failure_text)
                        last_failure = {
                            "index": i, "draft": name, "phase": "build_or_validate",
                            "type": "vision_hard_block" if cls["vision_hard"]
                            else "transient",
                            "attempt": attempt, "markers": cls["markers"],
                            "message": failure_text[-800:],
                        }
                        if qc_result is not None:
                            last_failure["qc_report"] = qc_result.get("output")
                            last_failure["qc_status"] = qc_result.get("status")
                            for row in (qc_result.get("report") or {}).get("model_review") or []:
                                if isinstance(row, dict) and row.get("visual_ok") is False:
                                    try:
                                        repair_visual_missing_indices.add(int(row.get("index")))
                                    except (TypeError, ValueError):
                                        pass
                            for issue in (qc_result.get("report") or {}).get("issues") or []:
                                match = re.search(r"POST_DRAFT_VISION_MISSING:S(\d+)", str(issue))
                                if match:
                                    repair_visual_missing_indices.add(int(match.group(1)))
                        log("DRAFT %d/%d attempt %d failed (%s): %s" % (
                            i, count, attempt,
                            "hard" if cls["vision_hard"] else "transient",
                            failure_text[:160].replace("\n", " ")))
                        # A post-draft visual mismatch is actionable: the QC
                        # report has already identified the bad sentence(s),
                        # and the next build will turn those exact segments
                        # into intentional gaps. Allow one final repair pass;
                        # do not keep retrying deterministic match failures.
                        qc_repairable = bool(qc_result and repair_visual_missing_indices)
                        # `vision_analyzer` has already performed its single
                        # bounded provider retry.  Re-running the complete
                        # build here would turn one service retry into a
                        # hidden second full vision pass.
                        vision_stage_failed = any(marker in failure_text.upper()
                                                  for marker in ("VISION_CALL_FAILED", "VISION_JSON_INVALID"))
                        if vision_stage_failed and not qc_repairable:
                            log("vision stage exhausted its bounded retry; stop current task")
                            break
                        # Deterministic semantic/timing/environment/UI failures
                        # are not provider retries.  Re-running the full draft
                        # only repeats the same blocker and used to consume the
                        # whole task budget.
                        if cls.get("deterministic_block") and not qc_repairable:
                            break
                        # A temporary vision/provider outage gets at most one
                        # bounded retry.  Later attempts must be a directed
                        # stage recovery, not another full build.
                        if cls["transient"] and attempt >= 2 and not qc_repairable:
                            break
                        if cls["vision_hard"] and attempt >= 2 and not qc_repairable:
                            break
                        if attempt >= MAX_DRAFT_ATTEMPTS:
                            break
                        continue
                    # A timeout is an explicit lack of visual certification,
                    # not a shippable preview. Keep the local draft for a
                    # later QC pass, record the deferment, and advance the
                    # queue without calling ship_distribute.
                    if qc_result and qc_result.get("status") == "UNCERTIFIED_TIMEOUT":
                        qc_reports.append({"draft": a_name,
                                           "status": "UNCERTIFIED_TIMEOUT",
                                           "report": qc_result.get("output")})
                        last_failure = {
                            "index": i, "draft": a_name,
                            "phase": "post_draft_qc",
                            "type": "uncertified_timeout",
                            "attempt": attempt,
                            "message": qc_result.get("message") or
                                       "独立视觉验收超时；保留本地草稿，未写入 NAS",
                            "qc_report": qc_result.get("output"),
                        }
                        log("DRAFT %d/%d not shipped: UNCERTIFIED_TIMEOUT; local-only deferment: %s" % (
                            i, count, a_name))
                        break
                    package_gate = validate_draft_package(DRAFTS / a_name)
                    if not package_gate.get("ok"):
                        failure_text = "PRE_SHIP_PACKAGE_BLOCKED: " + "; ".join(
                            str(item) for item in (package_gate.get("issues") or []))
                        last_failure = {
                            "index": i, "draft": a_name,
                            "phase": "pre_ship_package_gate",
                            "type": "draft_structure_block",
                            "attempt": attempt,
                            "message": failure_text,
                            "package_gate": package_gate,
                        }
                        log("DRAFT %d/%d not shipped: %s" % (i, count, failure_text))
                        if attempt >= MAX_DRAFT_ATTEMPTS:
                            break
                        continue
                    log("PRE_SHIP_PACKAGE_OK draft=%s video_tracks=%s video_segments=%s resolved_media=%s" % (
                        a_name, package_gate.get("video_tracks"),
                        package_gate.get("video_segments"),
                        package_gate.get("resolved_video_media")))
                    # Degraded/gapped visual QC is a usable preview, never a
                    # formal delivery. Retain the verified local draft and
                    # release the queue; do not send it to NAS or table write.
                    if qc_result and qc_result.get("status") in {"PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
                        preview_status = qc_result.get("status")
                        drafts.append(a_name)
                        local_only_drafts.append(a_name)
                        qc_reports.append({"draft": a_name,
                                           "status": preview_status,
                                           "report": qc_result.get("output")})
                        qc_gaps.append({
                            "index": i, "draft": a_name,
                            "phase": "post_draft_qc",
                            "type": "degraded_preview" if preview_status == "PASS_WITH_DEGRADED" else "uncertified_preview",
                            "message": "视觉验收为%s；保留本地预览，不进入 NAS/表格正式回写" % preview_status,
                            "qc_report": qc_result.get("output"),
                        })
                        draft_ok = True
                        log("DRAFT %d/%d retained as %s preview; NAS shipping blocked: %s" % (
                            i, count, preview_status, a_name))
                        break
                    current_review_approval = _mac_approval_path(
                        tdir, drafts + [a_name], frozen)
                    if (MAC_REVIEW_REQUIRED and frozen["mode"] != "enforce"
                            and not current_review_approval.is_file()):
                        package = prepare_mac_review(tdir, task, drafts + [a_name])
                        drafts.append(a_name)
                        local_only_drafts.append(a_name)
                        if qc_result is not None:
                            qc_reports.append({"draft": a_name,
                                               "status": qc_result.get("status"),
                                               "report": qc_result.get("output")})
                        review_pending = True
                        draft_ok = True
                        log("DRAFT %d/%d pending Mac review; NAS shipping blocked: %s" % (
                            i, count, a_name))
                        break
                    if SKIP_SHIP or frozen["mode"] == "enforce":
                        drafts.append(a_name)
                        local_only_drafts.append(a_name)
                        if qc_result is not None:
                            qc_reports.append({"draft": a_name,
                                               "status": qc_result.get("status"),
                                               "report": qc_result.get("output")})
                        draft_ok = True
                        log("DRAFT %d/%d ready locally; NAS shipping skipped pending Mac/Eval review: %s" % (
                            i, count, a_name))
                        break
                    sh = ship_one(DRAFTS / a_name, task_dir=tdir, frozen=frozen)
                    if sh["rc"] != 0:
                        last_failure = {
                            "index": i, "draft": a_name, "phase": "ship",
                            "type": "transient", "attempt": attempt,
                            "message": "ship rc=%s out=%s" % (
                                sh["rc"], sh["out"][-300:]),
                        }
                        log("SHIP %d/%d attempt %d failed: %s" % (
                            i, count, attempt, sh["out"][-160:]))
                        if attempt >= MAX_DRAFT_ATTEMPTS:
                            break
                        continue
                    drafts.append(a_name)
                    distribution_receipts.append(sh["receipt"])
                    if qc_result is not None:
                        qc_reports.append({"draft": a_name,
                                           "status": qc_result.get("status"),
                                           "report": qc_result.get("output")})
                        if qc_result.get("status") == "PASS_WITH_GAPS":
                            qc_gaps.append({
                                "index": i, "draft": a_name,
                                "phase": "post_draft_qc", "type": "uncertified_preview",
                                "message": "预览稿已通过结构与独立视觉核验，但保留明确的留空段；补素材/改文案后再转正式交付",
                                "qc_report": qc_result.get("output"),
                            })
                        elif qc_result.get("status") == "UNCERTIFIED_TIMEOUT":
                            qc_gaps.append({
                                "index": i, "draft": a_name,
                                "phase": "post_draft_qc", "type": "uncertified_timeout",
                                "message": qc_result.get("message") or "独立视觉验收超时；草稿已归档，待后续单独补验",
                                "qc_report": qc_result.get("output"),
                            })
                    draft_ok = True
                    log("draft %d/%d shipped: %s" % (i, count, a_name))
                    break
                except subprocess.TimeoutExpired as exc:
                    if analysis_cache is None:
                        candidate_report = rd / "shot_candidates.json"
                        reusable_count, shot_count = reusable_analysis_rows(candidate_report)
                        if reusable_count:
                            analysis_cache = str(candidate_report)
                            log("cache partial shot analysis rows=%d/%d after timeout: %s" % (
                                reusable_count, shot_count, analysis_cache))
                    last_failure = {
                        "index": i, "draft": name, "phase": "build",
                        "type": "transient", "attempt": attempt,
                        "timeout_s": exc.timeout,
                        "message": "单稿构建超时（attempt %s, timeout %ss）；下次改用更长超时或复用素材分析" % (attempt, exc.timeout),
                    }
                    (tdir / ("build_timeout_%d_a%d.log" % (i, attempt))).write_text(
                        json.dumps(last_failure, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    log("DRAFT TIMEOUT %d/%d attempt %d: %s (timeout %ss)" % (
                        i, count, attempt, name, exc.timeout))
                    if attempt >= MAX_DRAFT_ATTEMPTS:
                        break
                    continue
                except Exception as exc:  # noqa: BLE001
                    if analysis_cache is None:
                        candidate_report = rd / "shot_candidates.json"
                        reusable_count, shot_count = reusable_analysis_rows(candidate_report)
                        if reusable_count:
                            analysis_cache = str(candidate_report)
                            log("cache partial shot analysis rows=%d/%d after error: %s" % (
                                reusable_count, shot_count, analysis_cache))
                    last_failure = {
                        "index": i, "draft": name, "phase": "build_or_ship",
                        "type": "error", "attempt": attempt,
                        "message": str(exc)[:800],
                    }
                    log("DRAFT ERROR %d/%d attempt %d: %s" % (
                        i, count, attempt, exc))
                    if attempt >= MAX_DRAFT_ATTEMPTS:
                        break
                    continue
            if not draft_ok and last_failure is not None:
                failures.append(last_failure)
                log("draft %d/%d unresolved after %d attempt(s); stop current task" % (i, count, MAX_DRAFT_ATTEMPTS))
                break

        if frozen.get("mode") == "enforce" and drafts and MAC_REVIEW_REQUIRED:
            package = prepare_mac_review(tdir, task, drafts)
            log("MAC_REVIEW_PACKAGE output_count=%d path=%s" % (len(drafts), package))

        closed_loop_result = None
        if not failures and not qc_gaps and not SKIP_SHIP:
            closed = finalize_closed_loop(tdir, task, drafts, qc_reports,
                                          delivery_receipts=distribution_receipts)
            closed_loop_result = closed
            if not closed.get("ok") and closed.get("reason") in {
                    "MAC_REVIEW_EVAL_NOT_PASS", "MAC_REVIEW_PENDING"}:
                review_pending = True
                log("MAC_REVIEW_PENDING: %s" % closed)
            elif not closed.get("ok"):
                failures.append({
                    "phase": "closed_loop",
                    "type": "delivery_or_table_gate",
                    "message": str(closed),
                })
                log("CLOSED_LOOP_BLOCKED: %s" % closed)

        if failures or qc_gaps or SKIP_SHIP or review_pending:
            status = ("PREVIEW_READY" if drafts and not failures else
                      "PARTIAL" if drafts else "ERROR")
            all_issues = [*failures, *qc_gaps]
            if SKIP_SHIP and drafts:
                all_issues.append({
                    "phase": "distribution",
                    "type": "pending_mac_review",
                    "message": "草稿已生成并保留在 Windows，待 Mac 检查确认后再分发 NAS",
                })
            persisted_done = _read_json(done)
            distribution_receipts = (persisted_done.get("distribution_receipts")
                                     or distribution_receipts)
            verified_by_name = {row.get("draft_name"): row for row in distribution_receipts}
            shipped_drafts = [name for name in drafts if name in verified_by_name]
            if review_pending:
                distribution_status = "AWAITING_MAC_REVIEW"
            elif SKIP_SHIP:
                distribution_status = "PENDING_MAC_REVIEW"
            elif len(shipped_drafts) == len(drafts) and drafts:
                distribution_status = "SHIPPED_VERIFIED"
            elif frozen.get("mode") == "enforce" and shipped_drafts:
                distribution_status = "SHIPPED_PARTIAL"
            elif frozen.get("mode") == "enforce":
                distribution_status = "PENDING_EVAL_REVIEW"
            elif local_only_drafts:
                distribution_status = "PENDING_MAC_REVIEW"
            elif shipped_drafts:
                distribution_status = "SHIPPED_VERIFIED"
            else:
                distribution_status = "NOT_SHIPPED"
            payload = {
                "record_id": rid, "status": status, "drafts": drafts,
                "nas_links": [verified_by_name[name]["nas_target"] for name in shipped_drafts],
                "local_only_drafts": local_only_drafts,
                "distribution_status": distribution_status,
                "distribution_receipts": distribution_receipts,
                "qc_reports": qc_reports,
                "failures": all_issues,
                "deferred_draft_indices": (list(range(
                    failures[0]["index"], count + 1)) if failures and "index" in failures[0] else []),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            payload.update(contract(status, failures=all_issues))
            if drafts:
                payload["eval_receipt"] = evaluate_task(tdir, task, drafts, qc_reports, frozen)
            done.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            if drafts and status == "PREVIEW_READY" and MAC_REVIEW_REQUIRED:
                try:
                    from tools import eval_stage, mac_review_handoff
                    eval_stage.prepare(tdir, DRAFTS)
                    review_stage = os.environ.get("JY_MAC_REVIEW_STAGE", "").strip()
                    mac_root = os.environ.get("JY_MAC_DRAFT_ROOT", "").strip()
                    if not review_stage or not mac_root:
                        raise ValueError("MAC_REVIEW_STAGE_CONFIG_MISSING")
                    handoff = mac_review_handoff.stage(
                        tdir, DRAFTS, pathlib.Path(review_stage), mac_root)
                    payload["mac_review_handoff"] = handoff
                    payload["distribution_status"] = "AWAITING_MAC_REVIEW"
                except (OSError, ValueError, RuntimeError) as exc:
                    payload["distribution_status"] = "PENDING_REVIEW_TRANSFER"
                    payload["review_transfer_reason"] = str(exc)
                done.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            if status in {"ERROR", "PARTIAL"}:
                notification = publish_failure_notification(tdir)
                payload["failure_notification"] = notification
                done.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            progress({"record_id": rid, "event": status.lower(),
                      "drafts": drafts, "failures": all_issues,
                      **contract(status, failures=all_issues)})
            log("TASK %s: completed=%s deferred=%s; terminal=%s safe_to_continue=%s" % (
                status, ", ".join(drafts) or "none",
                ", ".join(str(f.get("index", "gate")) for f in failures),
                payload.get("terminal_state"), payload.get("safe_to_continue")))
            _release_record_lock(record_lock, run_id)
            return

        # finalize_closed_loop writes the verified grouped path and table receipt
        # into done.json. Preserve those fields instead of overwriting them with
        # the older flat-list success payload.
        existing_done = {}
        try:
            existing_done = json.loads(done.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing_done = {}
        done_payload = {
            "record_id": rid, "status": "OK",
            "drafts": drafts,
            "qc_reports": qc_reports,
            "nas_links": existing_done.get("nas_links", []),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        done_payload.update(contract("OK"))
        for key in ("nas_parent_folder", "distribution_status", "distribution_receipt",
                    "table_update_receipt", "closed_loop_receipts", "eval_receipt",
                    "distribution_receipts"):
            if key in existing_done:
                done_payload[key] = existing_done[key]
        done.write_text(json.dumps(done_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        progress({"record_id": rid, "event": "ok", "drafts": drafts, **contract("OK")})
        log("TASK OK: %s" % ", ".join(drafts))
        _release_record_lock(record_lock, run_id)
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        # A preflight error can happen before the per-draft loop. Never erase
        # drafts already shipped by an earlier PARTIAL attempt.
        preserved_status = "PARTIAL" if previous_drafts else "ERROR"
        failure_rows = [{"phase": "preflight", "type": "error", "message": str(exc)}]
        error_payload = {
            "record_id": rid, "status": preserved_status,
            "drafts": previous_drafts,
            "nas_links": _read_json(done).get("nas_links", []),
            "error": str(exc), "trace": tb,
            "failures": failure_rows,
            "deferred_draft_indices": (previous_deferred_indices
                                        if previous_drafts else []),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        error_payload.update(contract(preserved_status, failures=failure_rows, error=str(exc)))
        done.write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        notification = publish_failure_notification(tdir)
        done_payload = _read_json(done)
        if done_payload:
            done_payload["failure_notification"] = notification
            done.write_text(json.dumps(done_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        progress({"record_id": rid, "event": preserved_status.lower(),
                  "error": str(exc), "drafts": previous_drafts,
                  **contract(preserved_status, failures=failure_rows, error=str(exc))})
        log("TASK %s %s: %s" % (preserved_status, rid, exc))
        _release_record_lock(record_lock, run_id)


def _parallel_eligible(task: dict) -> bool:
    """Allow safe retry rows into the bounded two-task trial.

    A retry is safe to overlap only when `_parallel_pair_safe` proves that the
    title and material pool are distinct. Each task still owns its own report,
    cache keys and draft name; the worker remains capped at two processes.
    """
    rid = str(task.get("record_id") or "").strip()
    if not rid:
        return False
    done = WORK / rid / "done.json"
    if not done.exists():
        return True
    try:
        status = json.loads(done.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, TypeError):
        status = None
    return bool(task.get("force_rebuild") or task.get("retry_error")
                or task.get("retry_deferred") or status in {"ERROR", "PARTIAL"})


def _parallel_pair_safe(left: dict, right: dict) -> bool:
    """Avoid same-title draft collisions and same-pool cache contention."""
    left_title = str(left.get("title") or left.get("record_id") or "").strip().casefold()
    right_title = str(right.get("title") or right.get("record_id") or "").strip().casefold()
    left_material = str(left.get("material_dir") or "").strip().casefold()
    right_material = str(right.get("material_dir") or "").strip().casefold()
    return bool(left_title and right_title and left_title != right_title
                and left_material and right_material
                and left_material != right_material)


def _run_task(task: dict) -> None:
    process_task(task,
                 retry_partial=bool(task.get("retry_deferred")),
                 retry_error=bool(task.get("retry_error")))


def select_queue_for_single_run(queue: list[dict], *,
                                record_id: str = "",
                                force_rebuild: bool = False) -> list[dict]:
    """Select exactly one task; a targeted run must never drain the queue."""
    target = str(record_id or "").strip()
    if force_rebuild and not target:
        raise ValueError("force_rebuild requires an exact record_id")
    if target:
        matches = [task for task in queue
                   if str(task.get("record_id") or "").strip() == target]
        if len(matches) != 1:
            raise ValueError("expected exactly one queued task for record_id=%s; found %d"
                             % (target, len(matches)))
        selected = dict(matches[0])
        if force_rebuild:
            selected["force_rebuild"] = True
        if os.environ.get("JY_TASK_RESUME_PARTIAL", "").strip() == "1":
            selected["force_rebuild"] = False
            selected["retry_deferred"] = True
            selected["retry_error"] = False
        return [selected]
    return [dict(queue[0])] if queue else []


def _queue_preflight(task: dict) -> dict:
    """Check static queue inputs before starting the selected task."""
    issues = []
    rid = str(task.get("record_id") or "").strip()
    if not rid:
        issues.append("record_id missing")
    version_path = SKILL / "VERSION"
    if _pipeline_version() == "unknown" or not version_path.is_file():
        issues.append("pipeline VERSION missing or unreadable")
    material_dir = str(task.get("material_dir") or "").strip()
    sources = []
    if not material_dir:
        issues.append("material_dir empty")
    elif not pathlib.Path(material_dir).is_dir():
        issues.append("material_dir unavailable: %s" % material_dir)
    else:
        sources = video_files(material_dir)
        if not sources:
            issues.append("no video files under material_dir: %s" % material_dir)
    if not DRAFTS.parent.is_dir():
        issues.append("draft root parent unavailable: %s" % DRAFTS.parent)
    if not str(task.get("script") or "").strip():
        issues.append("script empty")
    return {"ok": not issues, "issues": issues, "source_count": len(sources)}


def _record_queue_preflight_block(task: dict, prior: dict, preflight: dict) -> dict:
    """Persist a blocking receipt without entering generation or vision."""
    rid = str(task.get("record_id") or "").strip()
    task_dir = WORK / rid
    task_dir.mkdir(parents=True, exist_ok=True)
    report_root = task_dir / "report"
    report_root.mkdir(parents=True, exist_ok=True)
    run_id = "%s-preflight-%s" % (rid, uuid.uuid4().hex[:8])
    parent_run_id = str(prior.get("run_id") or task.get("parent_run_id") or "").strip() or None
    failures = [{"phase": "queue_preflight", "type": "environment",
                 "message": str(item)} for item in (preflight.get("issues") or [])]
    error = "ENVIRONMENT_PRECHECK_FAILED: " + "; ".join(
        str(item) for item in (preflight.get("issues") or []))
    contract = _batch_result_contract(
        "ERROR", run_id=run_id, failures=failures, error=error,
        parent_run_id=parent_run_id, task_id=str(task.get("task_id") or rid),
        manifest_sha256=_payload_sha256(task), runtime=_runtime_facts(),
        task_log=str(report_root))
    payload = {
        "record_id": rid, "status": "ERROR", "drafts": [],
        "error": error, "failures": failures,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **contract,
    }
    (task_dir / "done.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    progress({"record_id": rid, "event": "queue_preflight_blocked",
              "error": error, "source_count": preflight.get("source_count", 0),
              **contract})
    log("QUEUE PREFLIGHT BLOCKED record_id=%s: %s" % (rid, error))
    return payload


def _task_receipt_complete(task_log: str) -> bool:
    """Accept a direct orchestrator report dir or its batch report root."""
    root = pathlib.Path(task_log)
    if not root.is_dir():
        return False
    candidates = [root]
    try:
        candidates.extend(path for path in root.rglob("result.json")
                         if path.is_file())
    except OSError:
        return False
    for candidate in candidates:
        result_path = candidate if candidate.name == "result.json" else candidate / "result.json"
        receipt_dir = result_path.parent
        events_path = receipt_dir / "events.jsonl"
        if not result_path.is_file() or not events_path.is_file():
            continue
        try:
            receipt = _read_json(result_path)
            finished = any(
                str(json.loads(line).get("event") or "") == "task_finished"
                for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip())
        except (OSError, ValueError, TypeError):
            continue
        if receipt and finished:
            return True
    return False


def _continuation_allowed(task: dict) -> tuple[bool, dict]:
    """Stop implicit recovery after a non-success terminal result.

    A retry/skip flag is an explicit operator decision.  Without it, the worker
    publishes a hold event and does not touch the failed task or the next queue
    item.
    """
    rid = str(task.get("record_id") or "").strip()
    done_path = WORK / rid / "done.json"
    if not done_path.is_file():
        return True, {}
    try:
        payload = json.loads(done_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, {"reason": "DONE_JSON_INVALID"}
    if bool(payload.get("safe_to_continue")) and payload.get("terminal_state") == "SUCCESS":
        task_log = str(payload.get("task_log") or "").strip()
        if task_log:
            if not _task_receipt_complete(task_log):
                return False, {**payload, "reason": "TASK_RECEIPT_INCOMPLETE"}
        elif payload.get("pipeline_version") == _pipeline_version():
            return False, {**payload, "reason": "TASK_RECEIPT_MISSING"}
        elif not payload.get("finished_at"):
            return False, {**payload, "reason": "TASK_FINISH_EVENT_MISSING"}
        if payload.get("pipeline_version") == _pipeline_version():
            missing = [key for key in ("task_id", "manifest_sha256", "runtime")
                       if not payload.get(key)]
            if missing:
                return False, {**payload, "reason": "RUN_RECEIPT_FIELDS_MISSING",
                               "missing_fields": missing}
        return True, payload
    explicit = bool(
        task.get("force_rebuild") or task.get("retry_error") or
        task.get("retry_deferred") or
        os.environ.get("JY_TASK_SKIP_CURRENT", "").strip() == "1"
    )
    return explicit, payload


def _mark_deferred(task: dict, prior: dict) -> dict:
    """Record an explicit operator skip before selecting the next task."""
    rid = str(task.get("record_id") or "").strip()
    task_dir = WORK / rid
    task_dir.mkdir(parents=True, exist_ok=True)
    report_root = task_dir / "report"
    report_root.mkdir(parents=True, exist_ok=True)
    old_run_id = str(prior.get("run_id") or "").strip() or None
    run_id = "%s-deferred-%s" % (rid, uuid.uuid4().hex[:8])
    contract = _batch_result_contract(
        "DEFERRED", run_id=run_id, parent_run_id=old_run_id,
        task_id=str(task.get("task_id") or rid),
        manifest_sha256=_payload_sha256(task), runtime=_runtime_facts(),
        task_log=str(report_root))
    payload = dict(prior or {})
    payload.update({
        "record_id": rid,
        "status": "DEFERRED",
        "run_id": run_id,
        "parent_run_id": old_run_id,
        "pipeline_version": _pipeline_version(),
        "deferred_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "deferred_reason": "operator_skip_current",
        **contract,
    })
    (task_dir / "done.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    progress({"record_id": rid, "event": "deferred",
              "reason": "operator_skip_current", **contract})
    return payload


def main():
    queue_path = WORK / "batch_queue.json"
    if not queue_path.exists():
        log("queue missing: %s" % queue_path)
        sys.exit(2)
    all_tasks = json.loads(queue_path.read_text(encoding="utf-8"))
    target_id = os.environ.get("JY_TASK_RECORD_ID", "").strip()
    force_rebuild = os.environ.get("JY_TASK_FORCE_REBUILD", "").strip() == "1"
    skip_requested = os.environ.get("JY_TASK_SKIP_CURRENT", "").strip() == "1"
    if skip_requested and target_id:
        log("QUEUE_SELECTION_ERROR JY_TASK_SKIP_CURRENT cannot be combined with JY_TASK_RECORD_ID")
        sys.exit(2)
    try:
        queue = select_queue_for_single_run(
            all_tasks, record_id=target_id, force_rebuild=force_rebuild)
    except ValueError as exc:
        log("QUEUE_SELECTION_ERROR %s" % exc)
        sys.exit(2)
    if not queue:
        log("QUEUE empty; no task selected")
        return
    # User-facing acceptance is required between tasks: each invocation is
    # single-task and sequential. The old parallel trial is removed so stale
    # environment settings cannot re-enable cross-task execution.
    log("QUEUE single-step total=%d selected=%s" % (
        len(all_tasks), queue[0].get("record_id")))
    allowed, prior = _continuation_allowed(queue[0])
    if skip_requested and not target_id:
        _mark_deferred(queue[0], prior)
        remaining = [dict(item) for item in all_tasks
                     if str(item.get("record_id") or "").strip() !=
                     str(queue[0].get("record_id") or "").strip()]
        queue = select_queue_for_single_run(remaining)
        if not queue:
            log("QUEUE skip recorded; no next task remains")
            return
        allowed, prior = _continuation_allowed(queue[0])
    if not allowed:
        rid = str(queue[0].get("record_id") or "")
        hold = _batch_result_contract(
            str(prior.get("status") or "ERROR"),
            run_id=str(prior.get("run_id") or (rid + "-hold")),
            failures=prior.get("failures") or [],
            error=str(prior.get("error") or prior.get("reason") or ""),
            parent_run_id=prior.get("parent_run_id"),
        )
        # A persisted done.json is the authoritative terminal receipt.  Keep
        # its classification in the hold event even when the compact failure
        # rows were omitted by an older worker.
        for key in ("terminal_state", "failure_class", "failure_code", "retryable"):
            if key in prior and prior.get(key) is not None:
                hold[key] = prior[key]
        progress({"record_id": rid, "event": "queue_hold",
                  "reason": "previous task is not safe_to_continue",
                  **hold, "safe_to_continue": False,
                  "next_action": "manual_review"})
        log("QUEUE HOLD record_id=%s terminal_state=%s failure_class=%s; explicit retry/skip required" % (
            rid, hold.get("terminal_state"), hold.get("failure_class")))
        return
    preflight = _queue_preflight(queue[0])
    if not preflight.get("ok"):
        _record_queue_preflight_block(queue[0], prior, preflight)
        return
    _run_task(queue[0])
    log("BATCH FINISHED")


if __name__ == "__main__":
    main()
