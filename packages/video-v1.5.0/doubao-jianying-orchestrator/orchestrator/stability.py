"""镜头稳定性检测与转场建议（仅依赖 ffmpeg，无需 numpy/OpenCV）。

核心思路：抽低帧率小尺寸灰度帧，用 tblend=difference + signalstats 输出逐帧
帧间差（YAVG），运动越剧烈数值越高。据此为每个分镜挑选“平均运动低、无剧烈
抖动峰值、无黑场”的稳定入点；并按相邻分镜是否同源/连续给出转场建议，避免
大幅晃动镜头与生硬跨场景硬切。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class StableWindow:
    source_start: float
    duration: float
    mean_motion: float
    peak_motion: float
    requested_start: float
    adjusted: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "source_start": round(self.source_start, 3),
            "duration": round(self.duration, 3),
            "mean_motion": round(self.mean_motion, 2),
            "peak_motion": round(self.peak_motion, 2),
            "requested_start": round(self.requested_start, 3),
            "adjusted": self.adjusted,
            "reason": self.reason,
        }


def _ffmpeg(ffmpeg: Optional[str] = None) -> str:
    return ffmpeg or "ffmpeg"


def motion_curve(video: str | Path, *, start: float = 0.0, duration: Optional[float] = None,
                 fps: int = 4, width: int = 160, height: int = 284,
                 ffmpeg: Optional[str] = None) -> list[tuple[float, float]]:
    """返回 [(帧时间s, 帧间差YAVG)]，首帧差值为0。"""
    cmd = [_ffmpeg(ffmpeg), "-nostdin"]
    if duration:
        cmd += ["-ss", str(start), "-t", str(duration)]
    cmd += ["-i", str(video),
            "-vf", (f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"format=gray,tblend=all_mode=difference,signalstats,"
                    f"metadata=print:key=lavfi.signalstats.YAVG"),
            "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = proc.stderr or ""
    points: list[tuple[float, float]] = []
    cur_t = 0.0
    for line in out.splitlines():
        m_t = re.search(r"pts_time:([\d.]+)", line)
        if m_t:
            cur_t = float(m_t.group(1)) + (start if duration else 0.0)
        m_v = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", line)
        if m_v:
            points.append((cur_t, float(m_v.group(1))))
    return points


def _window_score(points: list[tuple[float, float]], w_start: float, w_end: float
                  ) -> tuple[float, float]:
    vals = [v for t, v in points if w_start - 1e-6 <= t <= w_end + 1e-6]
    if not vals:
        return 999.0, 999.0
    # 去掉首帧0值，避免拉低均值
    vals = [v for v in vals if v > 0.01] or vals
    mean = sum(vals) / len(vals)
    peak = max(vals)
    return mean, peak


def has_black(video: str | Path, start: float, duration: float, *,
              ratio_thr: float = 0.985, ffmpeg: Optional[str] = None) -> bool:
    """检测窗口内是否存在黑场（黑帧占比过高）。"""
    cmd = [_ffmpeg(ffmpeg), "-nostdin", "-ss", str(start), "-t", str(duration), "-i", str(video),
           "-vf", f"blackdetect=d=0.1:pix_th=0.10:pic_th={ratio_thr}",
           "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return "black_start" in (proc.stderr or "")


def freeze_ranges(video: str | Path, *, start: float = 0.0,
                  duration: Optional[float] = None, freeze_duration: float = 0.35,
                  ffmpeg: Optional[str] = None) -> list[tuple[float, float]]:
    """Return static/frozen ranges reported by ffmpeg's freezedetect filter.

    This is intentionally a soft selector: a product packshot can be static
    for the whole clip, so callers only avoid frozen candidates when another
    candidate exists.  The ranges are mainly used to skip camera setup and
    end-of-action hold frames.
    """
    cmd = [_ffmpeg(ffmpeg), "-nostdin"]
    if duration:
        cmd += ["-ss", str(start), "-t", str(duration)]
    cmd += ["-i", str(video), "-vf",
            f"freezedetect=n=-60dB:d={max(0.1, float(freeze_duration))}",
            "-an", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    ranges: list[tuple[float, float]] = []
    current: Optional[float] = None
    for line in (proc.stderr or "").splitlines():
        m_start = re.search(r"freezedetect\.freeze_start\s*[:=]\s*([0-9.]+)", line)
        if m_start:
            current = float(m_start.group(1)) + (start if duration else 0.0)
            continue
        m_end = re.search(r"freezedetect\.freeze_end\s*[:=]\s*([0-9.]+)", line)
        if m_end and current is not None:
            end = float(m_end.group(1)) + (start if duration else 0.0)
            if end > current:
                ranges.append((current, end))
            current = None
    if current is not None:
        end = (start + duration) if duration else current + max(0.1, float(freeze_duration))
        if end > current:
            ranges.append((current, end))
    return ranges


def _freeze_overlap(ranges: list[tuple[float, float]], start: float, end: float) -> float:
    return sum(max(0.0, min(end, b) - max(start, a)) for a, b in ranges)


def find_stable_window(video: str | Path, target_duration: float, *,
                       requested_start: float = 0.0, video_duration: Optional[float] = None,
                       search_span: float = 0.8, step: float = 0.1,
                       max_peak: float = 32.0, shaky_mean: float = 26.0,
                       ffmpeg: Optional[str] = None,
                       head_trim: float = 0.3, tail_trim: float = 0.2) -> StableWindow:
    """以 requested_start 为首选，在其前后 search_span 内选相对最稳的等长窗口。

    设计原则（兼顾“稳定”与“画面匹配文案”）：
    - 只在首选入点附近小范围调整（默认 ±0.8s），不为求稳漂移到无关画面；
    - 硬约束：避开运动峰值（peak 超阈值的窗口降权）；
    - 软目标：平均运动更低；均值接近时优先少漂移；
    - 若整片都在手持微晃（最优窗口均值仍高于 shaky_mean），保留结果并在
      reason 标注 shaky，提示上层用叠化/轻防抖弥补，而不是盲目跳帧。
    """
    if video_duration is None:
        video_duration = 1e9
    # The first/last few tenths of many phone clips contain hand placement,
    # exposure settling, or a camera pull-away.  Keep these conservative
    # guards in the candidate range, while falling back to the full source for
    # clips too short to satisfy both guards.
    head_trim = max(0.0, float(head_trim or 0.0))
    tail_trim = max(0.0, float(tail_trim or 0.0))
    usable_end = max(0.0, video_duration - tail_trim)
    guarded = usable_end - head_trim >= target_duration - 1e-6
    min_start = head_trim if guarded else 0.0
    max_end = usable_end if guarded else video_duration
    lo = max(min_start, requested_start - search_span)
    hi = min(max_end, requested_start + search_span + target_duration)
    span = max(0.1, hi - lo)
    points = motion_curve(video, start=lo, duration=span, ffmpeg=ffmpeg)
    try:
        frozen = freeze_ranges(video, start=lo, duration=span, ffmpeg=ffmpeg)
    except Exception:
        frozen = []

    candidates: list[tuple[float, float, float, float]] = []  # mean, peak, drift, start
    s = lo
    while s + target_duration <= max_end + 1e-6 and s <= requested_start + search_span + 1e-6:
        mean, peak = _window_score(points, s, s + target_duration)  # 绝对时间
        drift = abs(s - requested_start)
        candidates.append((mean, peak, drift, s))
        s += step
    if not candidates:
        return StableWindow(requested_start, target_duration, 999.0, 999.0,
                            requested_start, False, "no-search-range")

    # 1) 能避开峰值的窗口集合；若全都超峰值则放宽
    calm = [c for c in candidates if c[1] <= max_peak]
    pool = calm or candidates
    # 静止检测只用于排除可替代的首尾废料，不会阻断整段静态产品展示。
    clean = [c for c in pool if _freeze_overlap(frozen, c[3], c[3] + target_duration) <= 1e-3]
    freeze_pool = bool(frozen and clean)
    if freeze_pool:
        pool = clean
    # 2) 在运动最低的一档（与最优均值差≤3）里，选漂移最小的，兼顾稳定与原意
    min_mean = min(c[0] for c in pool)
    near_best = [c for c in pool if c[0] <= min_mean + 3.0]
    ranked = sorted(near_best, key=lambda c: (c[2], c[0]))
    mean, peak, drift, best = ranked[0]
    black_avoided = False
    # ``has_black`` already uses the same bundled ffmpeg.  Run it only on the
    # best few motion candidates so black/transition frames cannot win merely
    # because their pixel difference is low, without multiplying probe cost
    # across the whole search range.
    if ffmpeg:
        for candidate in ranked[:12]:
            try:
                if not has_black(video, candidate[3], target_duration, ffmpeg=ffmpeg):
                    mean, peak, drift, best = candidate
                    black_avoided = candidate is not ranked[0]
                    break
            except (OSError, subprocess.SubprocessError):
                break

    adjusted = abs(best - requested_start) > step / 2
    if mean >= shaky_mean:
        reason = f"该镜头整体手持微晃(均值{mean:.1f})，已选相对最稳入点，建议叠化/轻防抖"
    elif adjusted:
        reason = f"入点由{requested_start:.2f}s微调到{best:.2f}s以避开运动峰值"
    else:
        reason = "首选入点已足够稳定"
    if black_avoided:
        reason += "；已避开黑帧/转场帧"
    if freeze_pool:
        reason += "；已避开静止废料段"
    if guarded and (head_trim > 0 or tail_trim > 0):
        reason += f"；已避开首尾废料保护区({head_trim:.1f}s/{tail_trim:.1f}s)"
    return StableWindow(best, target_duration, mean, peak, requested_start, adjusted, reason)


def recommend_transition(prev_video: str, cur_video: str, *,
                         prev_source_end: float, cur_source_start: float,
                         same_source_gap: float = 0.35,
                         cur_peak_motion: Optional[float] = None) -> Optional[dict]:
    """建议相邻分镜之间的转场。

    - 同源且时间连续（动作衔接）：None（硬切即可）；
    - 同源但跳点 / 异源跨场景：短“叠化”，入画运动偏大时略加长。
    """
    same = Path(prev_video).name == Path(cur_video).name
    contiguous = same and abs(cur_source_start - prev_source_end) <= same_source_gap
    if contiguous:
        return None
    dur = 0.25
    if cur_peak_motion and cur_peak_motion > 28:
        dur = 0.35
    return {"type": "叠化", "duration": round(dur, 3),
            "reason": "同源连续动作" if False else ("跨场景切换，叠化柔化硬切" if not same
                                            else "同源跳点，短叠化过渡")}
