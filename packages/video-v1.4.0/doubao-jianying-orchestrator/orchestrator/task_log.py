"""Structured, append-only task logs for troubleshooting."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_SENSITIVE = re.compile(r"(token|secret|password|api[_-]?key|authorization|cookie)", re.I)


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

    def __init__(self, report_dir: Path, manifest: dict, *, version: str = "v1.3.13"):
        self.report_dir = Path(report_dir)
        self.task_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.task_dir = self.report_dir / "tasks" / self.task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.task_dir / "events.jsonl"
        self.started = time.time()
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str).encode()
        self.input_chars = len(raw.decode("utf-8", errors="replace"))
        self.output_chars = 0
        self.llm_calls = 0
        self.peak_rss_bytes = 0
        self.cpu_seconds = 0.0
        summary = {
            "task_id": self.task_id,
            "version": version,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "manifest": _safe(manifest),
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
        self.event("task_started", version=version)

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

    def finish(self, result: dict) -> Path:
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
        return path

    def _sample_process(self) -> None:
        try:
            import psutil  # type: ignore
            proc = psutil.Process(os.getpid())
            self.peak_rss_bytes = max(self.peak_rss_bytes, int(proc.memory_info().rss))
            self.cpu_seconds = max(self.cpu_seconds,
                                   float(sum(proc.cpu_times()[:2])))
        except Exception:
            return
