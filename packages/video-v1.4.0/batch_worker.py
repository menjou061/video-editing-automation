# -*- coding: utf-8 -*-
"""批量任务 worker（渲染机侧常驻）。

逐条消费 `work/batch_queue.json`：
  enumerate materials -> manifest -> build(preview管线, 1.3.29 已无画面标注) x count
  -> ship(unс 分发) -> 进度写 `work/batch_progress.jsonl` -> 结果写
  `work/<record_id>/done.json`。
Mac 侧监控进度并负责飞书回写（Windows lark-cli 不参与，回写口径由 Mac 侧
经实测的 `lark-cli --profile personal` 完成）。

原则：
- 任务之间互不影响：单条失败记 `error` + 证据（stderr 尾部/退出码），继续下一条。
- 幂等：任何阶段重跑不产生半成品（report dir 按 task 复用，TTS/素材缓存命中）。
- 凭据不出渲染机：NAS 挂载复用 `jy_pipeline/probe_mount2.ps1`（内部从 jy_poll.ps1
  内存解析），不在本文件出现。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        elif done_path.is_file() and terminal in {"OK", "PARTIAL", "ERROR"}:
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
    hits = [m for m in TRANSIENT_BUILD_MARKERS if m.lower() in blob.lower()]
    vision_hard = ("DRAFT_VISUAL_QC_BLOCKED" in blob) or (
        "no auditable visual Top1" in blob) or (
        "VISUAL_MATCH_BLOCKED" in blob and not hits)
    return {
        "transient": bool(hits),
        "vision_hard": vision_hard,
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
    # 视觉分析画像可通过 JY_VISION_PROFILE 切换（flash=官方 DeepSeek 直连，
    # aijws=供应商中转）。高峰期限流时可切 aijws 跑批量。
    # Default to the validated Qwen visual route (aijws + qwen3.8-flash). The
    # old DeepSeek direct route (402 Insufficient Balance) stays opt-in only via
    # JY_VISION_PROFILE=flash.
    vp = os.environ.get("JY_VISION_PROFILE", "aijws").strip().strip('"').strip("'").strip()
    vm = os.environ.get("JY_VISION_MODEL", "").strip().strip('"').strip("'").strip()
    if vp:
        env["JY_VISION_PROFILE"] = vp
    if not vm and vp == "aijws":
        vm = "qwen3.8-flash"
    if vm:
        env["JY_VISION_MODEL"] = vm
    timeout = timeout or BUILD_TIMEOUT_SECONDS
    log("build draft=%s vision_profile=%s vision_model=%s timeout=%ss reuse=%s attempt=%s" % (
        draft_name, vp or "flash(default)", vm or "profile-default", timeout,
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


def ship_one(draft_dir: pathlib.Path) -> dict:
    cmd = [PY, str(SKILL / "orchestrator" / "ship_distribute.py"),
           "--draft", str(draft_dir), "--style", "unc"]
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run(cmd, cwd=str(SKILL), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    return {"rc": p.returncode, "out": (p.stdout or "")[-600:]}


def prepare_mac_review(task_dir: pathlib.Path, task: dict, draft_names: list[str]) -> pathlib.Path:
    """Persist an exact Mac review handoff before NAS distribution."""
    package = task_dir / "mac_review_package.json"
    payload = {
        "record_id": task.get("record_id"),
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "drafts": [str(DRAFTS / name) for name in draft_names],
        "review_required": True,
        "approval_file": str(task_dir / "mac_review_approved.json"),
        "instruction": "Mac 检查通过后，由授权检查流程写入 mac_review_approved.json；未批准不得 NAS 分发或表格回写。",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    package.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return package


def finalize_closed_loop(task_dir: pathlib.Path, task: dict, drafts: list[str],
                         qc_reports: list[dict]) -> dict:
    """Group, verify, and write the table only after all frozen gates pass."""
    done_path = task_dir / "done.json"
    provisional = {
        "record_id": task.get("record_id"), "status": "OK", "drafts": drafts,
        "qc_reports": qc_reports,
        "nas_links": [_nas_path(name) for name in drafts],
        "distribution_status": "SHIPPED",
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
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
    ACTIVE_RECORD_ID = str(rid)
    force_rebuild = bool(task.get("force_rebuild"))
    log("=" * 60)
    log("TASK %s title=%r count=%s voice=%s" % (rid, task.get("title"),
                                                task.get("count", 1),
                                                task.get("voice")))
    progress({"record_id": rid, "event": "start"})
    tdir = WORK / rid
    tdir.mkdir(parents=True, exist_ok=True)
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
            if force_rebuild:
                log("force_rebuild=true; prior drafts are retained but will not be reused")
                previous_drafts = []
                previous_deferred_indices = []
            if prev.get("status") == "OK" and not force_rebuild:
                if not existing_task_has_nonpass_qc(tdir):
                    log("already done, skip")
                    return
                log("previous OK has non-PASS visual QC evidence; rechecking")
            if prev.get("status") == "PARTIAL" and not force_rebuild and not (
                retry_partial or bool(task.get("retry_deferred"))
            ):
                log("partial task deferred, skip; continue queue")
                progress({"record_id": rid, "event": "deferred_skip",
                          "drafts": previous_drafts,
                          "deferred_draft_indices": prev.get(
                              "deferred_draft_indices", [])})
                return
            if prev.get("status") == "ERROR" and not force_rebuild and not (
                retry_error or bool(task.get("retry_error"))
            ):
                log("previous error deferred, skip; continue queue")
                progress({"record_id": rid, "event": "error_deferred_skip",
                          "error": prev.get("error", "")})
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
        if not manifest["full_script"]:
            raise RuntimeError("script empty, skip (nbz_test-like row)")
        drafts = []
        local_only_drafts = []
        failures = []
        qc_reports = []
        qc_gaps = []
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
                    log("TASK BUDGET EXHAUSTED draft %d/%d after %s attempt(s); continue queue" % (i, count, attempt))
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
                    if MAC_REVIEW_REQUIRED and not (tdir / "mac_review_approved.json").is_file():
                        package = prepare_mac_review(tdir, task, drafts + [a_name])
                        drafts.append(a_name)
                        local_only_drafts.append(a_name)
                        if qc_result is not None:
                            qc_reports.append({"draft": a_name,
                                               "status": qc_result.get("status"),
                                               "report": qc_result.get("output")})
                        last_failure = {
                            "index": i, "draft": a_name, "phase": "mac_review_gate",
                            "type": "pending_mac_review",
                            "message": "Mac 检查尚未批准；草稿已保留本地，未写入 NAS 或表格",
                            "review_package": str(package),
                        }
                        log("DRAFT %d/%d pending Mac review; NAS shipping blocked: %s" % (
                            i, count, a_name))
                        break
                    if SKIP_SHIP:
                        drafts.append(a_name)
                        local_only_drafts.append(a_name)
                        if qc_result is not None:
                            qc_reports.append({"draft": a_name,
                                               "status": qc_result.get("status"),
                                               "report": qc_result.get("output")})
                        draft_ok = True
                        log("DRAFT %d/%d ready locally; NAS shipping skipped pending Mac review: %s" % (
                            i, count, a_name))
                        break
                    sh = ship_one(DRAFTS / a_name)
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
                log("draft %d/%d unresolved after %d attempt(s); continue queue" % (i, count, MAX_DRAFT_ATTEMPTS))
                break

        closed_loop_result = None
        if not failures and not qc_gaps and not SKIP_SHIP:
            closed = finalize_closed_loop(tdir, task, drafts, qc_reports)
            closed_loop_result = closed
            if not closed.get("ok"):
                failures.append({
                    "phase": "closed_loop",
                    "type": "delivery_or_table_gate",
                    "message": str(closed),
                })
                log("CLOSED_LOOP_BLOCKED: %s" % closed)

        if failures or qc_gaps or SKIP_SHIP:
            status = "PREVIEW_READY" if SKIP_SHIP and drafts else ("PARTIAL" if drafts else "ERROR")
            all_issues = [*failures, *qc_gaps]
            if SKIP_SHIP and drafts:
                all_issues.append({
                    "phase": "distribution",
                    "type": "pending_mac_review",
                    "message": "草稿已生成并保留在 Windows，待 Mac 检查确认后再分发 NAS",
                })
            shipped_drafts = [name for name in drafts if name not in local_only_drafts]
            payload = {
                "record_id": rid, "status": status, "drafts": drafts,
                "nas_links": ([] if SKIP_SHIP else
                              [_nas_path(d)
                               for d in shipped_drafts]),
                "local_only_drafts": local_only_drafts,
                "distribution_status": ("PENDING_MAC_REVIEW" if (SKIP_SHIP or local_only_drafts)
                                         else "SHIPPED"),
                "qc_reports": qc_reports,
                "failures": all_issues,
                "deferred_draft_indices": (list(range(
                    failures[0]["index"], count + 1)) if failures else []),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            done.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            progress({"record_id": rid, "event": status.lower(),
                      "drafts": drafts, "failures": all_issues})
            log("TASK %s: completed=%s deferred=%s; continue queue" % (
                status, ", ".join(drafts) or "none",
                ", ".join(str(f["index"]) for f in failures)))
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
            "nas_links": existing_done.get("nas_links") or
                         [_nas_path(d)
                          for d in drafts],
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for key in ("nas_parent_folder", "distribution_status", "distribution_receipt",
                    "table_update_receipt", "closed_loop_receipts"):
            if key in existing_done:
                done_payload[key] = existing_done[key]
        done.write_text(json.dumps(done_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        progress({"record_id": rid, "event": "ok", "drafts": drafts})
        log("TASK OK: %s" % ", ".join(drafts))
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        # A preflight error can happen before the per-draft loop. Never erase
        # drafts already shipped by an earlier PARTIAL attempt.
        preserved_status = "PARTIAL" if previous_drafts else "ERROR"
        done.write_text(json.dumps({
            "record_id": rid, "status": preserved_status,
            "drafts": previous_drafts,
            "nas_links": [_nas_path(d)
                          for d in previous_drafts],
            "error": str(exc), "trace": tb,
            "failures": ([{"phase": "preflight", "type": "error",
                            "message": str(exc)}] if previous_drafts else []),
            "deferred_draft_indices": (previous_deferred_indices
                                        if previous_drafts else []),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        progress({"record_id": rid, "event": preserved_status.lower(),
                  "error": str(exc), "drafts": previous_drafts})
        log("TASK %s %s: %s" % (preserved_status, rid, exc))


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


def main():
    queue_path = WORK / "batch_queue.json"
    if not queue_path.exists():
        log("queue missing: %s" % queue_path)
        sys.exit(2)
    all_tasks = json.loads(queue_path.read_text(encoding="utf-8"))
    target_id = os.environ.get("JY_TASK_RECORD_ID", "").strip()
    force_rebuild = os.environ.get("JY_TASK_FORCE_REBUILD", "").strip() == "1"
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
    # single-task and sequential, regardless of stale parallel env settings.
    task_concurrency = 1
    log("QUEUE single-step total=%d selected=%s task_concurrency=1" % (
        len(all_tasks), queue[0].get("record_id")))
    index = 0
    while index < len(queue):
        task = queue[index]
        if task_concurrency < 2 or index + 1 >= len(queue) \
                or not _parallel_eligible(task) \
                or not _parallel_eligible(queue[index + 1]) \
                or not _parallel_pair_safe(task, queue[index + 1]):
            _run_task(task)
            index += 1
            continue
        pair = [task, queue[index + 1]]
        log("TASK PARALLEL START workers=2 ids=%s,%s" % (
            pair[0].get("record_id"), pair[1].get("record_id")))
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="jy-task") as executor:
            futures = [executor.submit(_run_task, item) for item in pair]
            for future in as_completed(futures):
                future.result()
        log("TASK PARALLEL DONE ids=%s,%s" % (
            pair[0].get("record_id"), pair[1].get("record_id")))
        index += 2
    log("BATCH FINISHED")


if __name__ == "__main__":
    main()
