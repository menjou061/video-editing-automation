"""Structured, append-only task logs for troubleshooting."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_SENSITIVE = re.compile(r"(token|secret|password|api[_-]?key|authorization|cookie)", re.I)
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


class TaskAlreadyRunningError(RuntimeError):
    """Raised when the same report directory already has a live run."""


def runtime_version() -> str:
    """Read the version shipped with this orchestrator instead of a stale default."""
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or "unknown"


def runtime_facts() -> dict[str, Any]:
    """Return safe, reproducible runtime identity facts for every task receipt."""
    ffmpeg = os.environ.get("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg")
    ffprobe = os.environ.get("FFPROBE_BIN", "").strip() or shutil.which("ffprobe")
    return {
        "pipeline_version": runtime_version(),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "skill_root": str(_VERSION_FILE.parent),
        "orchestrator_path": str(Path(__file__).resolve()),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "vision_profile": os.environ.get("JY_VISION_PROFILE", "").strip() or None,
        "vision_model": os.environ.get("JY_VISION_MODEL", "").strip() or None,
    }


def _result_text(result: dict[str, Any]) -> str:
    values = []
    for key in ("status", "message", "error", "reason", "problems", "failures", "markers"):
        value = result.get(key)
        if value:
            values.append(str(value))
    return " ".join(values)


def result_contract(result: dict[str, Any], *, task_id: str, run_id: str,
                    task_log: str, parent_run_id: str | None = None,
                    manifest_task_id: str | None = None,
                    version: str | None = None,
                    manifest_sha256: str | None = None,
                    runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach the stable terminal/recovery contract to a task result.

    The original status remains intact for compatibility.  The additional fields
    tell a monitor whether a result is terminal, retryable, and safe to follow
    with another task.
    """
    payload = dict(result or {})
    status = str(payload.get("status") or "ERROR").strip().upper()
    text = _result_text(payload).lower()
    marker_text = text.replace("_", " ")

    failure_code = ""
    failure_class = str(payload.get("failure_class") or "").strip().lower()
    for marker, kind, code in (
        ("vision_call_failed", "vision", "VISION_CALL_FAILED"),
        ("vision_json_invalid", "vision", "VISION_JSON_INVALID"),
        ("visual_match_blocked", "semantic", "VISUAL_MATCH_BLOCKED"),
        ("shot_match_blocked", "semantic", "SHOT_MATCH_BLOCKED"),
        ("audio_video_mismatch", "timing", "AUDIO_VIDEO_MISMATCH"),
        ("shot_timing_infeasible", "timing", "SHOT_TIMING_INFEASIBLE"),
        ("timing_infeasible", "timing", "TIMING_INFEASIBLE"),
        ("timing_invariant_violation", "timing", "TIMING_INVARIANT_VIOLATION"),
        ("desktop_import_blocked", "ui", "DESKTOP_IMPORT_BLOCKED"),
        ("draft_invalid", "draft", "DRAFT_INVALID"),
        ("invalid argument", "environment", "INVALID_ARGUMENT"),
        ("task_budget", "environment", "TASK_BUDGET_EXHAUSTED"),
        ("system_resource_pressure", "environment", "SYSTEM_RESOURCE_PRESSURE"),
        ("env_error", "environment", "ENV_ERROR"),
    ):
        if marker.replace("_", " ") in marker_text:
            # A generic caller may have supplied ``unknown`` before the
            # contract sees the concrete marker.  Known markers must win so
            # VISION_CALL_FAILED/INVALID_ARGUMENT never get flattened into a
            # generic error class.
            if not failure_class or failure_class == "unknown":
                failure_class = kind
            failure_code = code
            break
    if not failure_class and status in {"ENV_ERROR", "SYSTEM_RESOURCE_PRESSURE"}:
        failure_class = "environment"
        failure_code = failure_code or status
    status_codes = {
        "RECOVERY_ANALYSIS_REQUIRED": ("semantic", "RECOVERY_ANALYSIS_REQUIRED"),
        "RECOVERY_MATCHED_MANIFEST_REQUIRED": ("semantic", "RECOVERY_MATCHED_MANIFEST_REQUIRED"),
        "RECOVERY_DRAFT_REQUIRED": ("ui", "RECOVERY_DRAFT_REQUIRED"),
        "RECOVERY_STAGE_INVALID": ("environment", "RECOVERY_STAGE_INVALID"),
        "TASK_ALREADY_RUNNING": ("environment", "TASK_ALREADY_RUNNING"),
        "PACKAGE_NOT_ACTIVE": ("ui", "PACKAGE_NOT_ACTIVE"),
    }
    if status in status_codes and (not failure_class or failure_class == "unknown"):
        failure_class, failure_code = status_codes[status]
    elif status in status_codes:
        failure_code = failure_code or status_codes[status][1]
    if not failure_class and status in {"AUDIO_SELECTION_REQUIRED", "BGM_PREVIEW_UNAVAILABLE"}:
        failure_class = "provider"
        failure_code = failure_code or status
    if not failure_class and any(token in marker_text for token in
                                 ("network", "connection refused", "connection reset")):
        failure_class = "provider"
        failure_code = failure_code or "PROVIDER_UNAVAILABLE"
    if not failure_class and status not in {
        "SUCCESS", "OK", "PREVIEW_READY", "PARTIAL", "DEFERRED",
        "PREFLIGHT_READY", "SHOT_MATCH_READY", "UI_ACCEPTANCE_PENDING",
    }:
        failure_class = "unknown"

    transient = any(token in marker_text for token in (
        "timed out", "timeout", "429", "502", "503", "connection reset",
        "temporarily unavailable", "network", "connection refused",
    ))
    retryable = bool(failure_class in {"vision", "provider"} and transient)

    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    uncertified = bool(
        status in {"PREVIEW_READY", "PARTIAL", "UNCERTIFIED"}
        or str(payload.get("delivery_mode") or "").lower() == "preview"
        or str(coverage.get("certification") or "").upper() not in {"", "CERTIFIED"}
        or coverage.get("uncertified_reasons")
    )
    intermediate_stage = status in {"PREFLIGHT_READY", "SHOT_MATCH_READY", "UI_ACCEPTANCE_PENDING"}
    if status == "DEFERRED":
        terminal_state = "DEFERRED"
    elif intermediate_stage:
        terminal_state = "UNCERTIFIED"
    elif status in {"SUCCESS", "OK"} and not uncertified:
        terminal_state = "SUCCESS"
    elif uncertified:
        terminal_state = "UNCERTIFIED"
    elif status in {"SHOT_MATCH_BLOCKED", "VISUAL_MATCH_BLOCKED", "ENV_ERROR",
                    "SYSTEM_RESOURCE_PRESSURE", "AUDIO_SELECTION_REQUIRED",
                    "BGM_PREVIEW_UNAVAILABLE", "DESKTOP_IMPORT_BLOCKED",
                    "PACKAGE_NOT_ACTIVE", "DRAFT_INVALID"} or failure_class in {"semantic", "timing", "environment", "ui", "draft"}:
        terminal_state = "BLOCKED"
    else:
        terminal_state = "ERROR"

    if status == "DEFERRED":
        next_action = "skip_task"
    elif status == "PREFLIGHT_READY":
        next_action = "complete"
    elif status == "SHOT_MATCH_READY":
        next_action = "draft_write"
    elif terminal_state == "SUCCESS":
        next_action = "complete"
    elif failure_class == "environment":
        next_action = "fix_environment"
    elif retryable:
        next_action = "retry_stage"
    else:
        next_action = "manual_review"

    payload.update({
        "task_id": task_id,
        "manifest_task_id": manifest_task_id,
        "run_id": run_id,
        "pipeline_version": version or runtime_version(),
        "terminal_state": terminal_state,
        "failure_class": failure_class or None,
        "failure_code": failure_code or None,
        "retryable": retryable,
        "safe_to_continue": terminal_state == "SUCCESS" and not intermediate_stage,
        "next_action": next_action,
        "parent_run_id": parent_run_id,
        "manifest_sha256": manifest_sha256,
        "runtime": runtime or runtime_facts(),
        "task_log": task_log,
    })
    return payload


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if _SENSITIVE.search(str(k)) else _safe(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TaskLogger:
    """One task folder containing immutable metadata, events and final result."""

    def __init__(self, report_dir: Path, manifest: dict, *, version: str | None = None,
                 parent_run_id: str | None = None):
        self.report_dir = Path(report_dir)
        self.task_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.run_id = self.task_id
        # ``version`` remains accepted for old callers, but the receipt always
        # reflects the package actually executing.  This closes the historical
        # v1.3.13 default/version-drift path.
        self.version = runtime_version()
        self.parent_run_id = parent_run_id or str(manifest.get("parent_run_id") or "").strip() or None
        self.manifest_task_id = str(manifest.get("task_id") or manifest.get("record_id") or "").strip() or None
        self.lock_path = self.report_dir / ".task_run.lock"
        self._lock_owned = False
        self._acquire_lock()
        self.task_dir = self.report_dir / "tasks" / self.task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.task_dir / "events.jsonl"
        self.started = time.time()
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str).encode()
        self.manifest_sha256 = hashlib.sha256(raw).hexdigest()
        self.runtime = runtime_facts()
        self.input_chars = len(raw.decode("utf-8", errors="replace"))
        self.output_chars = 0
        self.llm_calls = 0
        self.peak_rss_bytes = 0
        self.cpu_seconds = 0.0
        summary = {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "manifest_task_id": self.manifest_task_id,
            "parent_run_id": self.parent_run_id,
            "version": self.version,
            "pipeline_version": self.version,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "manifest_sha256": self.manifest_sha256,
            "manifest": _safe(manifest),
            "runtime": self.runtime,
            "pid": os.getpid(),
            "observability": {"input_chars": self.input_chars,
                               "output_chars": 0,
                               "llm_calls": 0,
                               "estimated_input_tokens": (self.input_chars + 3) // 4,
                               "estimated_output_tokens": 0,
                               "estimated_total_tokens": (self.input_chars + 3) // 4,
                               "token_accounting": "estimate_only"},
        }
        self.task_path = self.task_dir / "task.json"
        self.task_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.event("task_started", version=self.version, run_id=self.run_id,
                   parent_run_id=self.parent_run_id)

    def _acquire_lock(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        lock_payload = {
            "pid": os.getpid(),
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "pipeline_version": self.version,
        }
        for _ in range(2):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(lock_payload, handle, ensure_ascii=False)
                self._lock_owned = True
                return
            except FileExistsError:
                existing = {}
                try:
                    existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    existing = {}
                pid = existing.get("pid")
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
                    raise TaskAlreadyRunningError(
                        "TASK_ALREADY_RUNNING: report_dir=%s run_id=%s pid=%s" %
                        (self.report_dir, existing.get("run_id") or "unknown", pid))
                try:
                    self.lock_path.unlink()
                except OSError:
                    raise TaskAlreadyRunningError(
                        "TASK_LOCK_STALE_UNCLEAR: report_dir=%s" % self.report_dir)

    def release_lock(self) -> None:
        if not self._lock_owned:
            return
        try:
            current = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            current = {}
        if current.get("run_id") == self.run_id:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        self._lock_owned = False

    def event(self, name: str, **fields: Any) -> None:
        row = {"ts": time.time(), "event": name, "task_id": self.task_id}
        row.update(_safe(fields))
        if name.endswith("_llm") or name in {"llm_call", "model_call"}:
            self.llm_calls += 1
        for key in ("output", "output_text", "response", "text"):
            value = fields.get(key)
            if isinstance(value, str):
                self.output_chars += len(value)
                break
        self._sample_process()
        row["process"] = {"rss_bytes": self.peak_rss_bytes,
                           "cpu_seconds": round(self.cpu_seconds, 3)}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    @contextmanager
    def phase(self, name: str, **fields: Any) -> Iterator[None]:
        started = time.perf_counter()
        self.event(name + "_start", **fields)
        try:
            yield
        except Exception as exc:
            self.event(name + "_error", elapsed_s=round(time.perf_counter() - started, 3),
                       error_type=type(exc).__name__, error=str(exc))
            raise
        else:
            self.event(name + "_finish", elapsed_s=round(time.perf_counter() - started, 3))

    def finish(self, result: dict, *, release: bool = True) -> Path:
        self._sample_process()
        payload = _safe(result)
        payload.setdefault("task_id", self.task_id)
        payload.setdefault("elapsed_s", round(time.time() - self.started, 3))
        payload.setdefault("observability", {
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "llm_calls": self.llm_calls,
            "estimated_input_tokens": (self.input_chars + 3) // 4,
            "estimated_output_tokens": (self.output_chars + 3) // 4,
            "estimated_total_tokens": (self.input_chars + self.output_chars + 3) // 4,
            "peak_rss_bytes": self.peak_rss_bytes,
            "cpu_seconds": round(self.cpu_seconds, 3),
            "token_accounting": "estimate_only",
        })
        path = self.task_dir / "result.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.event("task_finished", status=result.get("status"), elapsed_s=payload["elapsed_s"])
        if release:
            self.release_lock()
        return path

    def finalize(self, result: dict) -> dict:
        """Write one normalized terminal result and return the same payload."""
        payload = result_contract(
            result,
            task_id=self.task_id,
            run_id=self.run_id,
            task_log=str(self.task_dir),
            parent_run_id=self.parent_run_id,
            manifest_task_id=self.manifest_task_id,
            version=self.version,
            manifest_sha256=self.manifest_sha256,
            runtime=self.runtime,
        )
        self.finish(payload, release=False)
        last_result = self.report_dir / "last_result.json"
        try:
            last_result.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            self.release_lock()
        return payload

    def _sample_process(self) -> None:
        try:
            import psutil  # type: ignore
            proc = psutil.Process(os.getpid())
            self.peak_rss_bytes = max(self.peak_rss_bytes, int(proc.memory_info().rss))
            self.cpu_seconds = max(self.cpu_seconds,
                                   float(sum(proc.cpu_times()[:2])))
        except Exception:
            return
