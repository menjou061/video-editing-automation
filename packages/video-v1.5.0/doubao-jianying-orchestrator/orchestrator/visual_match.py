"""Deterministic final-cut evidence coverage checks.

Semantic labels are produced by the storyboard analysis step.  This module
checks the part the runtime can prove without guessing: every labelled source
evidence interval is valid, has an extracted frame artifact, and remains
inside the final source window after stability/audio trimming.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_intervals(value: Any) -> list[dict[str, Any]]:
    """Normalize evidence_intervals into {start,end,frame_path,claim} records."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            start = _number(item.get("start", item.get("source_start", item.get("in"))))
            end = _number(item.get("end", item.get("source_end", item.get("out"))))
            if not end and item.get("duration") is not None:
                end = start + _number(item.get("duration"))
            rec = dict(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end = _number(item[0]), _number(item[1])
            rec = {}
        else:
            continue
        rec["start"] = start
        rec["end"] = end
        out.append(rec)
    return out


def _frame_path(item: dict[str, Any]) -> str:
    return str(item.get("frame_path") or item.get("evidence_frame") or item.get("frame") or "").strip()


def _extract_frame(ffmpeg: str | None, video: str, timestamp: float, output: Path) -> bool:
    """Extract one evidence frame when the analysis supplied no path."""
    if not ffmpeg:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([str(ffmpeg), "-y", "-ss", f"{max(0.0, timestamp):.3f}",
                        "-i", str(video), "-frames:v", "1", "-q:v", "2", str(output)],
                       check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return output.is_file() and output.stat().st_size > 0


def validate_source_intervals(segments: list[dict[str, Any]], *, probe: Callable[[str], dict],
                              report_dir: Path, ffmpeg: str | None,
                              require_intervals: bool = True) -> dict[str, Any]:
    """Validate source evidence and materialize one frame per evidence interval."""
    issues: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for index, item in enumerate(segments or [], 1):
        text = str(item.get("text", item.get("caption", "")) or "")
        if item.get("visual_missing"):
            records.append({
                "index": index,
                "video": "",
                "text": text,
                "evidence_intervals": [],
                "scene_source_start": None,
                "scene_source_end": None,
                "visual_missing": True,
                "operator_note": item.get("operator_note", ""),
            })
            continue
        if not text:
            continue
        intervals = normalize_intervals(item.get("evidence_intervals"))
        if require_intervals and not intervals:
            issues.append({"index": index, "type": "evidence_intervals_missing",
                           "video": str(item.get("video", "")),
                           "message": "口播分镜缺少原素材证据时间段"})
            continue
        try:
            duration = _number(probe(str(item.get("video"))).get("duration"))
        except Exception as exc:
            issues.append({"index": index, "type": "source_probe_failed",
                           "video": str(item.get("video", "")), "message": str(exc)})
            continue
        normalized: list[dict[str, Any]] = []
        for n, interval in enumerate(intervals, 1):
            start, end = _number(interval.get("start")), _number(interval.get("end"))
            valid = 0 <= start < end <= duration + 0.02
            frame = _frame_path(interval)
            if frame:
                frame_path = Path(frame)
                if not frame_path.is_absolute():
                    frame_path = report_dir / frame_path
                frame_ok = frame_path.is_file() and frame_path.stat().st_size > 0
            else:
                frame_path = report_dir / "visual_evidence_frames" / f"shot_{index:03d}_{n:02d}.jpg"
                frame_ok = _extract_frame(ffmpeg, str(item.get("video")), (start + end) / 2.0, frame_path)
            if not valid:
                issues.append({"index": index, "type": "evidence_interval_invalid",
                               "interval": {"start": start, "end": end},
                               "source_duration": duration,
                               "message": "证据时间段超出原素材范围"})
            if not frame_ok:
                issues.append({"index": index, "type": "evidence_frame_missing",
                               "interval": {"start": start, "end": end},
                               "message": "证据时间段没有可审计抽帧文件"})
            normalized.append({**interval, "start": start, "end": end,
                               "frame_path": str(frame_path), "frame_exists": frame_ok})
        records.append({"index": index, "video": str(item.get("video", "")),
                        "text": text, "evidence_intervals": normalized,
                        # 被分析过的场景窗口边界，供最终切片做「不得越界」校验。
                        "scene_source_start": item.get("scene_source_start"),
                        "scene_source_end": item.get("scene_source_end")})
    return {"ok": not issues, "issues": issues, "records": records}


def validate_final_windows(segments: list[dict[str, Any]], source_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Ensure every evidence interval is contained in the final source window."""
    issues: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    by_index = {int(r.get("index")): r for r in source_records}
    for index, item in enumerate(segments or [], 1):
        if item.get("visual_missing"):
            coverage.append({
                "index": index,
                "video": "",
                "temporary_shot_id": None,
                "final_source_start": 0.0,
                "final_source_end": 0.0,
                "evidence_intervals": [],
                "evidence_covered": False,
                "missed_intervals": [],
                "scene_window": None,
                "inside_scene": False,
                "visual_missing": True,
                "operator_note": item.get("operator_note", ""),
            })
            continue
        start = _number(item.get("source_start"))
        end = start + _number(item.get("duration"))
        rec = by_index.get(index, {"evidence_intervals": []})
        intervals = rec.get("evidence_intervals", [])
        missed = []
        for interval in intervals:
            istart, iend = _number(interval.get("start")), _number(interval.get("end"))
            # v1.3.24：由「已审核帧邻域」派生的区间，真正被审核的只有那一帧，
            # 所以按**帧**判是否被剪掉；区间宽度可能大于切片（短句的配音比邻域还短），
            # 要求覆盖整个区间会把本来正确的切片误判为剪掉了证据。
            # AI/上游明确给出时间段的区间仍然是原样的整段覆盖校验。
            if interval.get("evidence_source") == "audited_frame_neighborhood" and interval.get("frame_time") is not None:
                frame_ts = _number(interval.get("frame_time"))
                if frame_ts < start - 0.02 or frame_ts > end + 0.02:
                    missed.append({"start": istart, "end": iend, "frame_time": frame_ts,
                                   "frame_path": interval.get("frame_path")})
                continue
            if istart < start - 0.02 or iend > end + 0.02:
                missed.append({"start": istart, "end": iend,
                               "frame_path": interval.get("frame_path")})
        # v1.3.24（用户定案）：切片不但要套住证据区间，还不能越出**被分析过的场景窗口**
        # ——不得播到没有抽帧审核过的画面上去。占位兜底仍会校验，缺边界时跳过。
        outside = None
        scene_start = rec.get("scene_source_start")
        scene_end = rec.get("scene_source_end")
        if scene_start is not None and scene_end is not None:
            if start < _number(scene_start) - 0.02 or end > _number(scene_end) + 0.02:
                outside = {"start": _number(scene_start), "end": _number(scene_end)}
        row = {"index": index, "video": str(item.get("video", "")),
               "temporary_shot_id": item.get("temporary_shot_id"),
               "final_source_start": start, "final_source_end": end,
               "evidence_intervals": intervals, "evidence_covered": not missed,
               "missed_intervals": missed,
               "scene_window": outside or {"start": _number(scene_start), "end": _number(scene_end)},
               "inside_scene": outside is None}
        coverage.append(row)
        if missed:
            issues.append({"index": index, "type": "evidence_cut_away",
                           "video": str(item.get("video", "")),
                           "final_source_window": [start, end],
                           "missed_intervals": missed,
                           "message": "最终稳定切片没有覆盖全部卖点证据区间"})
        if outside:
            issues.append({"index": index, "type": "window_outside_scene",
                           "video": str(item.get("video", "")),
                           "final_source_window": [start, end],
                           "scene_window": [outside["start"], outside["end"]],
                           "message": "最终切片越出了被分析的场景窗口，会播到未经审核的画面"})
    return {"ok": not issues, "issues": issues, "coverage": coverage}


def write_report(path: Path, *, semantic: dict[str, Any], source: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    report = {
        "ok": bool(semantic.get("ok") and source.get("ok") and final.get("ok")),
        "semantic": semantic,
        "source": source,
        "final": final,
        "verification": {
            "mode": "source_frame_and_final_window",
            "source_frames_verified": bool(source.get("ok")),
            "final_windows_verified": bool(final.get("ok")),
            "pixel_semantics": "labels supplied by storyboard frame analysis; runtime verifies the labelled frame artifacts and final interval coverage",
        },
    }
    report["issues"] = list(semantic.get("issues", [])) + list(source.get("issues", [])) + list(final.get("issues", []))
    report["pending_items"] = report["issues"]
    report["issue_count"] = len(report["issues"])
    report["coverage"] = final.get("coverage", []) or semantic.get("coverage", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
