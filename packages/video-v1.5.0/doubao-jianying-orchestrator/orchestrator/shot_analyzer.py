"""Task-local material analysis and sentence-to-shot matching.

This module deliberately keeps the first version library-free.  It scans only
the current task's source videos, detects candidate shot boundaries with the
bundled ffmpeg, extracts auditable representative frames, and writes a JSON
candidate file for the storyboard analysis step to describe.  It never writes
to a reusable material library.  Once descriptions/tags/action status are
present, ``match_manifest`` ranks the best three shots for each spoken claim
and returns a build-ready manifest only for candidates that pass the evidence
requirements.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from . import demo_actions
from . import semantic_gate
from . import timing_contract


_PTS_TIME_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+")

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _skill_version() -> str:
    """产物上的版本戳：直接读 skill 根目录的 VERSION。

    原来这里是写死的 "v1.3.18"，1.3.19 和 1.3.20 都没跟着改，
    导致「这份产物是哪个版本跑出来的」从产物本身答不出来 —— 排查
    线上问题时这点很要命。改成派生，就不会再漂。
    """
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return f"v{value}" if value else "unknown"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 选镜阶段的配音时长估算（v1.3.24，用户 2026-09-16 定案）。
#
# 背景：原先选镜只看语义相关性、完全不看时长，于是出现「41 字的句子配 5.76s 素材、
# 19 字的句子配 16.92s 素材」。引擎在 TTS 合成之后才做音画对齐，那时镜头已经选完、
# 配音已经烧完，只能整支拦住（AUDIO_VIDEO_MISMATCH），出不了草稿。
#
# 所以在选镜这一步先用字数把配音时长估出来，优先挑**装得下**的素材。
# 语速取实测偏慢的一档：估长了大不了少几个候选镜头，估短了才会真的撞上硬阻断。
TTS_CHARS_PER_SECOND = 5.0
# 与 engine.TAIL_PAD 一致：每句口播尾部留白，避免配音贴边。
NARRATION_TAIL_PAD = 0.12
# 与 engine 的 head_trim_s / tail_trim_s 默认值一致：素材首尾各裁掉一段。
DEFAULT_HEAD_TRIM_S = 0.3
DEFAULT_TAIL_TRIM_S = 0.2
# 没有细粒度证据区间时，用「已审核帧前后各多少秒」作为证据区间（v1.3.24）。
# 取值偏小是故意的：区间越窄，引擎越容易把配音长度的切片套在上面，
# 同时保证切片里包含 AI 真正看过的那一帧。
EVIDENCE_HALF_WINDOW = 0.6
# 一句话最多跨几个镜头（定案第 6 条的多镜组合）。
# 上限存在的目的是**防走捷径**：不设限的话，任何长句都能靠「把素材库切碎拼上去」
# 过关，成片会变成一堆闪帧。超过这个数仍无解就如实报 MATERIAL_GAP，让运营补素材。
SPAN_MAX_PARTS = 3


def estimated_narration_seconds(text: str) -> float:
    """Estimate how long this sentence's TTS will run, without synthesizing it."""
    chars = len(re.sub(r"\s+", "", str(text or "")))
    return round(chars / TTS_CHARS_PER_SECOND, 3) if chars else 0.0


def shot_usable_seconds(shot: dict[str, Any], manifest: dict[str, Any] | None = None) -> float:
    """Seconds of clean footage this shot's source can actually contribute."""
    source = manifest or {}
    duration = _number(shot.get("duration") or shot.get("source_end"), 0.0)
    head = max(0.0, _number(source.get("head_trim_s"), DEFAULT_HEAD_TRIM_S))
    tail = max(0.0, _number(source.get("tail_trim_s"), DEFAULT_TAIL_TRIM_S))
    return max(0.0, duration - head - tail)


def narration_fits(shot: dict[str, Any], text: str,
                   manifest: dict[str, Any] | None = None) -> bool:
    """已退出选镜关键路径（v1.3.27），只保留给历史报告/离线核对使用。

    新口径见 `candidate_window`：用真实音频时长 + 可行窗口求解判定，而不是
    「字符数估算 vs 素材时长 − 0.5」。这个函数**不得**再进入选镜决策 ——
    字符数估算只能作为显式降级（``timing_certainty="estimated"``）出现。
    """
    return shot_usable_seconds(shot, manifest) + 1e-3 >= estimated_narration_seconds(text) + NARRATION_TAIL_PAD


def candidate_evidence_interval(shot: dict[str, Any]) -> tuple[float, float, float]:
    """候选镜头的证据区间（与最终落进段落的 `evidence_intervals` 同一口径）。

    求解器必须在**选镜时**就知道证据区间落在哪，否则会求出「窗口合法但没罩住
    已审核帧」的解。所以这里和下面写证据区间的地方共用同一套算法 —— 两处口径
    一旦分叉，求解结果就与最终写入的证据不一致，而写入前的实例校验会因此
    **在最后一步**才炸，正是本次要消灭的那种失败模式。

    证据区间有两个来源，这里必须都覆盖（顺序与写段落时一致）：

    1. 分析阶段（``vision_analyzer._evidence_intervals``）已经从 AI 那里拿到了
       可用区间 —— 那就**原样取并集跨度**，因为写段落时用的就是这一份；
    2. 分析阶段没给（``evidence_intervals`` 为空）—— 用与写段落处完全相同的
       「已审核帧邻域 ±``EVIDENCE_HALF_WINDOW``，并夹在场景窗口内」算法。
    """
    spans: list[tuple[float, float]] = []
    for interval in shot.get("evidence_intervals") or []:
        if not isinstance(interval, dict):
            continue
        left = max(0.0, _number(interval.get("start"), 0.0))
        right = _number(interval.get("end"), 0.0)
        if right > left:
            spans.append((left, right))
    if spans:
        return (round(min(s for s, _ in spans), 3),
                round(max(e for _, e in spans), 3),
                round(_number(shot.get("frame_time"), (spans[0][0] + spans[0][1]) / 2.0), 3))

    span_start = _number(shot.get("source_start"))
    span_end = _number(shot.get("source_end"), span_start)
    frame_ts = _number(shot.get("frame_time"), (span_start + span_end) / 2.0)
    return (round(max(span_start, frame_ts - EVIDENCE_HALF_WINDOW), 3),
            round(min(span_end, frame_ts + EVIDENCE_HALF_WINDOW), 3),
            round(frame_ts, 3))


def _us(seconds: Any, default: float = 0.0) -> int:
    """秒 → 整数微秒（时序契约内部一律 µs）。"""
    return int(round(_number(seconds, default) * timing_contract.US))


def candidate_window(shot: dict[str, Any], *, audio_duration_us: int,
                     manifest: dict[str, Any] | None = None,
                     time_sensitive: bool = False):
    """对**单个候选镜头**求解可行窗口（v1.3.27 结构修复的核心）。

    用户定案（2026-09-16）：
    - 每个候选都要跑求解，不能再用「2 秒占位探针 / 字符数估算」短路；
    - 变速范围由画面角色决定（``speed_range_for_role``），与时间相关的直接证据
      锁 1.0 —— 不允许用变速把装不下的证据镜头塞进配音长度（等于伪造证据）；
    - 无解返回结构化原因码，**引擎不得静默换镜**，由规划层决定换镜/多镜/缺口。

    返回 `timing_contract.WindowSolution`；`audio_duration_us<=0` 时同样返回失败解
    （reason=NO_AUDIO），由调用方按显式降级处理。
    """
    source = manifest or {}
    evidence_start, evidence_end, _frame = candidate_evidence_interval(shot)
    speed_min, speed_max = timing_contract.speed_range_for_role(
        demo_actions.visual_role(shot), time_sensitive=time_sensitive)
    frm = _number(source.get("fps"), 0) or 0
    # 素材总长（1.3.27.1）：analyze_manifest 新产物自带 material_duration_us，与
    # 写入层同一把尺；旧产物没有时退回 source_end 的换算值。
    material_us = _number(shot.get("material_duration_us"))
    if material_us <= 1000:
        material_us = _us(shot.get("source_end"), _number(shot.get("source_start")))
    return timing_contract.solve_window(timing_contract.WindowRequest(
        source_in_us=_us(shot.get("source_start")),
        source_out_us=_us(shot.get("source_end"), _number(shot.get("source_start"))),
        audio_duration_us=int(audio_duration_us or 0),
        tail_pad_us=_us(source.get("tail_pad_s"), NARRATION_TAIL_PAD),
        head_guard_us=_us(source.get("head_trim_s"), DEFAULT_HEAD_TRIM_S),
        tail_guard_us=_us(source.get("tail_trim_s"), DEFAULT_TAIL_TRIM_S),
        evidence_start_us=_us(evidence_start), evidence_end_us=_us(evidence_end),
        speed_min=speed_min, speed_max=speed_max,
        video_duration_us=int(round(material_us)),
        fps=(frm or timing_contract.DEFAULT_FPS)))


def span_segment_for(shot: dict[str, Any], *, manifest: dict[str, Any] | None = None,
                     time_sensitive: bool = False, handles: tuple[int, int] = (0, 0),
                     video: str = "", shot_id: str = ""):
    """把一个候选镜头摊成组合求解需要的一段（口径与 ``candidate_window`` 完全一致）。

    两处口径必须同源，否则会出现「单镜判死、组合却按另一套保护区收下」的裂缝 ——
    那等于绕开用户定案第 5/6 条。所以保护区、变速范围、证据区间全部走同样的取值。
    """
    source = manifest or {}
    evidence_start, evidence_end, _frame = candidate_evidence_interval(shot)
    speed_min, speed_max = timing_contract.speed_range_for_role(
        demo_actions.visual_role(shot), time_sensitive=time_sensitive)
    frm = _number(source.get("fps"), 0) or 0
    return timing_contract.SpanSegment(
        source_in_us=_us(shot.get("source_start")),
        source_out_us=_us(shot.get("source_end"), _number(shot.get("source_start"))),
        head_guard_us=_us(source.get("head_trim_s"), DEFAULT_HEAD_TRIM_S),
        tail_guard_us=_us(source.get("tail_trim_s"), DEFAULT_TAIL_TRIM_S),
        transition_in_handle_us=int(handles[0]), transition_out_handle_us=int(handles[1]),
        evidence_start_us=_us(evidence_start), evidence_end_us=_us(evidence_end),
        speed_min=speed_min, speed_max=speed_max,
        video=str(video), shot_id=str(shot_id))


def plan_span(shots: list[dict[str, Any]], *, audio_duration_us: int,
              manifest: dict[str, Any] | None = None,
              time_sensitive: bool = False) -> dict[str, Any]:
    """为一句装不下的口播挑一组镜头，跑组合求解（定案第 6 条）。

    **只选不执行**：这里只决定「这句话由哪几镜、各承担多长」，把解如实带回报告；
    真正落到草稿段上由引擎的装配步骤做（定案第 7 条：引擎只执行与校验）。
    挑镜用的是**同一套** ``candidate_window`` 求解能力：一个镜头能进组合，当且仅当
    它自己拿到的份额在它自己的允许窗口内有解。

    为什么不在这里挑「语义第 2、第 3 好」之外的镜头：一句话跨多镜仍然是这句话的
    画面，跨到语义完全不沾的镜头去凑长度，就是定案要消灭的「静默换成语义更弱的
    镜头」。所以只在**已通过闸门的候选**里做几何择优，不做语义降级。
    """
    source = manifest or {}
    tail_pad_us = _us(source.get("tail_pad_s"), NARRATION_TAIL_PAD)
    frm = _number(source.get("fps"), 0) or 0
    fps = frm or timing_contract.DEFAULT_FPS

    # ① 逐个候选摊成段，并算出它「在允许的最慢速度下最多能盖多长」。
    #    容量而不是语义分决定取用顺序：语义分在进入这里之前已经过关（能进组合池的
    #    都是过了闸门的候选），再按它排只会让组合更偏向同一个镜头，与「多镜」的
    #    用意相反；容量大的先进来，能用更少的镜头装下这句话。
    pool: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for shot in shots:
        shot_id = str(shot.get("shot_id") or "")
        if shot_id and shot_id in seen_ids:
            continue
        seen_ids.add(shot_id)
        seg = span_segment_for(shot, manifest=manifest, time_sensitive=time_sensitive,
                               video=str(shot.get("video") or ""), shot_id=shot_id)
        if seg.usable_us <= 0:
            continue
        probe = timing_contract.solve_window(seg.request(
            audio_duration_us=max(1, int(audio_duration_us)),
            tail_pad_us=int(tail_pad_us), fps=fps))
        speed_min = min(float(seg.speed_min), float(seg.speed_max))
        capacity_us = int(seg.usable_us / speed_min) if speed_min > 0 else 0
        pool.append({"shot": shot, "segment": seg, "window": probe,
                     "capacity_us": capacity_us})
    if not pool:
        return {"ok": False, "reason": timing_contract.REASON_SOURCE_TOO_SHORT,
                "detail": {"scope": "span", "message": "没有任何候选镜头有可用素材"},
                "segments": [], "picked": []}
    candidates = sorted(pool, key=lambda i: (-i["capacity_us"], i["shot"].get("shot_id", "")))

    # ② 每镜「单独就够」时优先：那是 A 步的合格单镜路径，组合只负责补它补不了的。
    #    `solution` 留的是**求解器对象本身**（不是 to_dict 的结果）：引擎装配那一步
    #    要读它的 speed / detail，字典冒充不了；`part` 才是给报告看的可序列化副本。
    for item in candidates:
        if item["window"].ok:
            return {"ok": True, "segments": [item["segment"]],
                    "parts": [{"part": item["window"].to_dict(), "solution": item["window"],
                               "shot": item["shot"], "segment": item["segment"]}],
                    "picked": [item], "solution": item["window"],
                    "detail": {"parts": 1, "scope": "single",
                               "message": "有单镜能独立承担，不需要拼多镜"}}

    # ③ 单镜都不行 → 按容量从大到小逐条收进组合，每收一条重跑一次分配。
    #    预算耗尽仍无解，就如实报**所有候选合起来**也不够 —— 那是 MATERIAL_GAP
    #    的依据，而不是编一个「勉强能过」的解去骗写入前的闸门。
    max_parts = max(2, int(source.get("span_max_parts", SPAN_MAX_PARTS)))
    chosen: list[dict[str, Any]] = []
    last = None
    for item in candidates[:max_parts]:
        chosen.append(item)
        req = timing_contract.SpanRequest(
            segments=tuple(i["segment"] for i in chosen),
            audio_duration_us=int(audio_duration_us), tail_pad_us=int(tail_pad_us),
            fps=fps)
        last = timing_contract.solve_span(req)
        if last.ok:
            items = list(zip(chosen, last.parts))
            return {"ok": True, "segments": [i["segment"] for i in chosen],
                    "parts": [dict(part=p.to_dict(), solution=p, shot=item["shot"],
                                   segment=item["segment"])
                              for item, p in items],
                    "solution": last,
                    "detail": {"parts": len(chosen), "scope": "span",
                               "allocation_us": list(last.detail.get("allocated_us", [])),
                               "message": f"单镜无解，已拼 {len(chosen)} 镜"}}
    detail = dict(last.detail) if last is not None else {"scope": "span"}
    detail["tried_parts"] = len(chosen)
    detail["candidate_count"] = len(pool)
    return {"ok": False,
            "reason": (last.reason if last is not None
                       else timing_contract.REASON_SOURCE_TOO_SHORT),
            "detail": detail, "segments": [], "parts": [], "picked": []}


def _commit_span(span: dict[str, Any],
                 rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """把 `plan_span` 选中的镜头映射回排序表的行（定案第 7 条：规划层只选，引擎只执行）。

    组合解里每一段的窗口都是求解器按**那一段自己的素材边界**算出来的，所以这里
    必须逐段取回该段对应的行与解，不能拿整句的解套给所有段 —— 那会让第二、三段
    按第一段的素材边界切片，画面直接切到别的镜头上去。

    任何一段映射不回去、或该镜头的分析还没做完（缺完整描述 / 动作确认），就整体
    放弃组合（返回 `None`），交回上面如实报 `SHOT_TIMING_INFEASIBLE` —— 拼一半
    的多镜比不拼更糟：报告会说「已组合」而成片只有一段是那句口播的画面。
    """
    parts = span.get("parts") or []
    segments = span.get("segments") or []
    if not parts or len(parts) != len(segments):
        return None
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        shot_id = str(row.get("shot_id") or "")
        if shot_id and shot_id not in index:
            index[shot_id] = row
    picked: list[dict[str, Any]] = []
    for item in parts:
        segment = item.get("segment")
        shot_id = str(getattr(segment, "shot_id", "") or "")
        row = index.get(shot_id)
        if row is None:
            return None
        shot = row.get("shot") or {}
        if shot.get("status") != "ready_for_matching" or shot.get("action_complete") is not True:
            return None
        picked.append({**row, "window": item.get("solution"),
                       "window_ok": True, "window_reason": "",
                       "span_segment": segment})
    return picked


def _span_evidence_intervals(shot: dict[str, Any], claim: str,
                             segment: Any) -> list[dict[str, Any]]:
    """组合里**某一段**这一帧该带的证据区间（与 `candidate_evidence_interval` 同源）。

    多镜时区间必须**逐段**取，不能把整句的并集跨度发给每一段：求解器是按段求窗口、
    按段判「窗口罩住证据」的，段与段之间隔着别的画面，中间那几段既罩不住头也罩不住
    尾，按并集判就恒不通过 —— 两边口径必须同源，否则会在写入前才炸（本次要消灭的
    正是这种失败模式）。

    区间取值直接复用 `span_segment_for` 存进段落的那一份（它又来自
    `candidate_evidence_interval`），保证「求解器看到的」与「写进段落的」是同一个数。
    """
    spans = [dict(item) for item in (shot.get("evidence_intervals") or [])
             if isinstance(item, dict)]
    if spans:
        # 分析阶段给了区间：与单镜路径一样原样带上，只补 claim。
        return [{**item, "claim": claim} for item in spans]
    start_us = getattr(segment, "evidence_start_us", None)
    end_us = getattr(segment, "evidence_end_us", None)
    if start_us is None or end_us is None:
        # 兜底：段里没留区间时按同一算法就地重算，绝不返回空（空区间会被下游
        # 当成「没有证据要求」而放行一段根本没被审核过的画面）。
        ev_start, ev_end, frame_ts = candidate_evidence_interval(shot)
    else:
        ev_start = round(int(start_us) / timing_contract.US, 3)
        ev_end = round(int(end_us) / timing_contract.US, 3)
        frame_ts = round(_number(shot.get("frame_time"), (ev_start + ev_end) / 2.0), 3)
    return [{
        "start": ev_start, "end": ev_end,
        "frame_path": shot.get("frame_path"), "claim": claim,
        "evidence_source": "audited_frame_neighborhood",
        "frame_time": frame_ts,
    }]


def _unique_paths(values: Iterable[Any], base_dir: Path) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        path = Path(str(value)).expanduser()
        path = (path if path.is_absolute() else base_dir / path).resolve()
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def manifest_sources(manifest: dict[str, Any], base_dir: Path) -> list[Path]:
    """Return only this task's unique video sources."""
    if manifest.get("material_sources"):
        values = manifest.get("material_sources", [])
    elif manifest.get("videos"):
        values = manifest.get("videos", [])
    elif manifest.get("segments"):
        values = [item.get("video") for item in manifest.get("segments", [])
                  if isinstance(item, dict)]
    else:
        values = []
    return _unique_paths(values, base_dir)


def probe_duration(path: Path, *, ffprobe: str | None = None,
                   ffmpeg: str | None = None) -> float:
    """Read duration using ffprobe, falling back to ffmpeg stderr parsing."""
    if ffprobe:
        try:
            proc = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=True)
            duration = _number(proc.stdout.strip())
            if duration > 0:
                return duration
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    command = str(ffmpeg or "ffmpeg")
    try:
        proc = subprocess.run([command, "-hide_banner", "-i", str(path)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0.0


def detect_scene_boundaries(path: Path, *, ffmpeg: str | None = None,
                            threshold: float = 0.30) -> list[float]:
    """Return scene-change timestamps from ffmpeg's showinfo output."""
    command = str(ffmpeg or "ffmpeg")
    filter_expr = f"select='gt(scene,{float(threshold):.3f})',showinfo"
    try:
        proc = subprocess.run(
            [command, "-hide_banner", "-nostdin", "-i", str(path), "-vf", filter_expr,
             "-an", "-f", "null", "-"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({round(float(value), 3) for value in _PTS_TIME_RE.findall(proc.stderr or "")
                   if float(value) > 0})


def extract_frame(path: Path, timestamp: float, output: Path, *,
                  ffmpeg: str | None = None) -> bool:
    command = str(ffmpeg or "ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([command, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{max(0.0, timestamp):.3f}", "-i", str(path),
                        "-frames:v", "1", "-q:v", "2", str(output)],
                       capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return output.is_file() and output.stat().st_size > 0


def _boundaries_us(duration_us: int, scene_times: Iterable[float], *,
                   min_shot_s: float) -> list[tuple[int, int]]:
    """场景窗口切分（整数 µs，**只向下取整，绝不上舍**）。

    1.3.27.1：旧的 `_boundaries` 用 `round(x, 3)` 取 3 位小数秒 —— round 是四舍
    五入，素材尾边界可能被**向上**舍入（4.9835 → 4.984），求解器按它摆出点就会
    越过写入层用 pymediainfo 视频轨量到的真实总长（写入层零容差 → 直接 raise）。
    改动：入点 0、各转场时间戳 `int(t*1e6)`（截断）、末点**直接用实测
    ``duration_us`` 本身**（来自 `probe_material_duration_us`，全程无浮点舍入），
    于是任何一个窗口的 ``end ≤ 素材总长`` 恒成立。
    """
    min_us = max(1, int(float(min_shot_s) * timing_contract.US))
    points = [0]
    for t in scene_times:
        point_us = int(float(t) * timing_contract.US)   # 截断，绝不放大
        if min_us <= point_us < duration_us - min_us:
            points.append(point_us)
    points.append(duration_us)
    points = sorted(set(points))
    windows: list[tuple[int, int]] = []
    for start_us, end_us in zip(points, points[1:]):
        if end_us - start_us >= min_us:
            windows.append((start_us, end_us))
    if not windows and duration_us > 0:
        windows = [(0, duration_us)]
    return windows


def _boundaries(duration: float, scene_times: Iterable[float], *, min_shot_s: float) -> list[tuple[float, float]]:
    """保留的旧接口（秒、3 位小数），仅诊断用途；生产路径走 ``_boundaries_us``。"""
    points = [0.0] + [t for t in scene_times if min_shot_s <= t < duration - min_shot_s] + [duration]
    points = sorted(set(round(max(0.0, min(duration, p)), 3) for p in points))
    windows: list[tuple[float, float]] = []
    for start, end in zip(points, points[1:]):
        if end - start >= min_shot_s:
            windows.append((start, end))
    if not windows and duration > 0:
        windows = [(0.0, duration)]
    return windows


def _existing_shot_map(payload: Any) -> dict[tuple[str, int], dict[str, Any]]:
    rows = payload.get("shots", []) if isinstance(payload, dict) else payload
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for idx, row in enumerate(rows or []):
        if isinstance(row, dict):
            result[(str(row.get("video", "")), idx)] = row
    return result


def analyze_manifest(manifest: dict[str, Any], base_dir: Path, report_dir: Path, *,
                     ffmpeg: str | None = None, ffprobe: str | None = None,
                     threshold: float = 0.30, min_shot_s: float = 0.45) -> dict[str, Any]:
    """Create task-local shot candidates and representative frame artifacts.

    ``analyze_manifest`` only performs deterministic cutting/extraction.  The
    caller may pass the resulting rows to ``vision_analyzer.enrich_with_vision``
    for the automatic semantic step.  Keeping these phases separate makes the
    raw media inventory auditable and prevents a missing model response from
    being mistaken for a valid description.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    frame_dir = report_dir / "temporary_shot_frames"
    existing = manifest.get("shot_analysis") if isinstance(manifest.get("shot_analysis"), (dict, list)) else None
    old = _existing_shot_map(existing)
    shots: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    sources = manifest_sources(manifest, base_dir)
    for source_index, video in enumerate(sources, 1):
        if not video.is_file():
            issues.append({"type": "source_missing", "video": str(video)})
            continue
        # 1.3.27.1：素材总长统一由 probe_material_duration_us 给出（优先使用与
        # 写入层一致的视频素材时长；只有容器时长时进入保守降级并标记 LOW）。
        probe = timing_contract.probe_material_duration_us(
            video, ffprobe=ffprobe, ffmpeg=ffmpeg)
        duration_us = int(probe.get("duration_us") or 0)
        if duration_us <= 0:
            issues.append({"type": "source_probe_failed", "video": str(video)})
            continue
        # 探测来源/置信度必须跟着每一行 shot 落盘：LOW（容器降级）的值不得
        # 参与生成「已认证」的候选窗口，下游认证闸门靠这两个字段看见它。
        duration_source = str(probe.get("source") or "probe_failed")
        duration_confidence = str(probe.get("confidence") or "LOW")
        windows = _boundaries_us(duration_us,
                                 detect_scene_boundaries(video, ffmpeg=ffmpeg, threshold=threshold),
                                 min_shot_s=min_shot_s)
        for shot_index, (start_us, end_us) in enumerate(windows, 1):
            start = start_us / timing_contract.US
            end = end_us / timing_contract.US
            frame = frame_dir / f"source_{source_index:03d}_shot_{shot_index:03d}.jpg"
            frame_mid_ok = extract_frame(video, (start + end) / 2.0, frame, ffmpeg=ffmpeg)
            frame_end = frame_dir / f"source_{source_index:03d}_shot_{shot_index:03d}_end.jpg"
            frame_end_ok = extract_frame(video, max(start, end - 0.15), frame_end, ffmpeg=ffmpeg)
            frame_ok = frame_mid_ok
            prior = old.get((str(video), shot_index), {})
            tags = semantic_gate.normalize_tags(prior.get("visual_tags") or prior.get("frame_tags"))
            description = str(prior.get("description") or prior.get("visual_description") or "").strip()
            row = {
                "shot_id": f"source_{source_index:03d}_shot_{shot_index:03d}",
                "video": str(video), "source_start": start, "source_end": end,
                # 素材总长（µs，真值），写入层同一把尺 —— 供不变量校验与下游复用，
                # 避免任何调用方再拿 `source_end` 或 3 位小数近似去当素材总长。
                "material_duration_us": duration_us,
                "material_duration_source": duration_source,
                "material_duration_confidence": duration_confidence,
                "duration": round((end_us - start_us) / timing_contract.US, 6),
                "frame_path": str(frame),
                # 审核帧的时间戳。AI 描述的就是这一帧，证据区间以它为中心。
                "frame_time": round((start + end) / 2.0, 6),
                "frame_exists": frame_ok, "frame_paths": [str(p) for p, ok in ((frame, frame_mid_ok), (frame_end, frame_end_ok)) if ok],
                "description": description,
                "visual_tags": tags, "evidence_tags": tags,
                "action_complete": prior.get("action_complete"),
                "head_waste": _number(prior.get("head_waste")),
                "tail_waste": _number(prior.get("tail_waste")),
                "reuse_scope": "本商品", "status": "ready_for_matching" if description and tags and frame_ok else "pending_analysis",
                "persist_to_library": False,
            }
            # DEMO_ACTIONS V0.2 素材侧字段：从**上一轮已落盘的视觉结果**里继承，
            # 否则每次重跑 analyze 都会把闸门赖以工作的 role/has_subject 丢掉，
            # 闸门悄悄退回 ungated。键不存在时不写（见 vision_analyzer 同名注释）。
            for key in ("action", "object", "result", "role", "evidence_strength",
                        "action_phase", "setup_id", "has_subject"):
                if key in prior:
                    row[key] = prior[key]
            shots.append(row)
            if not frame_ok:
                issues.append({"type": "frame_extract_failed", "shot_id": row["shot_id"], "video": str(video)})
    prompt = build_analysis_prompt(manifest, shots)
    result = {
        "version": _skill_version(), "scope": "task_local", "sources": [str(p) for p in sources],
        "shots": shots, "issues": issues, "pending_items": issues,
        "analysis_prompt": prompt, "library_write": [],
        "ok": bool(shots) and not issues and all(s["frame_exists"] for s in shots),
    }
    (report_dir / "shot_candidates.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "shot_analysis_prompt.md").write_text(prompt, encoding="utf-8")
    return result


def build_analysis_prompt(manifest: dict[str, Any], shots: list[dict[str, Any]]) -> str:
    rows = "\n".join(f"- {s['shot_id']}: {s['frame_path']} ({s['video']} {s['source_start']}-{s['source_end']}s)" for s in shots)
    return ("请只分析本次商品任务的临时镜头候选，不读取历史素材库。\n"
            "对每个抽帧文件输出一句客观画面描述、visual_tags、可证明的卖点 evidence_tags、"
            "action_complete、head_waste、tail_waste。没有看到的卖点不要填写。\n"
            "若画面有手指/手势明确指向包装上的可读卖点文字或标识，额外输出"
            "readable_claims（逐字记录可读文字）、pointing_to_text=true、"
            "pointing_action/gesture；没有明确指向或文字不可读时必须为 false/空。"
            "包装文字只能支持同一条包装卖点，不能证明产品真实性或吸收/柔软等功能。\n"
            "对于 CTA/促销/划算/囤货/数量话术，记录 multi_pack、row_display、carton、"
            "stack、visible_quantity 等真实量感证据；单包静态特写不得填写量感证据。\n"
            "这一阶段不要读取或猜测脚本文案，先独立完成素材理解。\n\n"
            f"候选抽帧：\n{rows}\n\n"
            "把结果回填到 shot_candidates.json 的 shots 数组；不能编造视频、时间码或 speaker。")


def _sentences(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """切句：**唯一实现**在 `timing_contract.split_sentences`。

    pre-TTS 真值表与选镜必须切出**逐字一致**的同一批句子，否则真值表的键
    对不上段落、真时长会套到别的句子上。所以这里只做委托，不再自己切一遍。
    """
    return timing_contract.split_sentences(manifest)


def _candidate_score(claim: str, shot: dict[str, Any], requirements: Any = None) -> tuple[int, list[str]]:
    required = semantic_gate.inferred_claim_tags(claim)
    for tag in semantic_gate.normalize_tags(requirements):
        if tag not in required:
            required.append(tag)
    blob = " ".join([str(shot.get("description", "")),
                      *semantic_gate.normalize_tags(shot.get("visual_tags")),
                      *semantic_gate.normalize_tags(shot.get("evidence_tags"))])
    matched = [tag for tag in required if semantic_gate._matches(tag, blob)]
    score = len(matched) * 100
    if shot.get("description"):
        score += 10
    if shot.get("frame_exists"):
        score += 5
    if shot.get("action_complete") is True:
        score += 5
    if _number(shot.get("head_waste")) > 0.8 or _number(shot.get("tail_waste")) > 0.8:
        score -= 50
    return score, matched


def _variant_tiebreak(rows: list[dict[str, Any]], priority: Callable[[dict[str, Any]], tuple],
                      *, variant_index: int, score_tolerance: int) -> dict[str, Any] | None:
    """Rotate only near-tied candidates with the same evidence/match tier.

    This gives same-script drafts a visible, deterministic difference without
    allowing a weaker evidence tier or a score outside the declared tolerance
    to displace the best candidate.
    """
    if not rows:
        return None
    ordered = sorted(rows, key=priority, reverse=True)
    best = ordered[0]
    best_priority = priority(best)
    if len(best_priority) < 2:
        return best
    quality_key = best_priority[:-1]
    best_score = _number(best_priority[-1])
    tolerance = max(0, int(score_tolerance))
    pool = [row for row in ordered
            if priority(row)[:-1] == quality_key
            and best_score - _number(priority(row)[-1]) <= tolerance]
    if len(pool) < 2:
        return best
    pool.sort(key=lambda row: (
        str(row.get("source_video") or ""),
        str((row.get("shot") or {}).get("setup_id") or ""),
        str((row.get("shot") or {}).get("action_phase") or ""),
        _number((row.get("shot") or {}).get("source_start")),
        str((row.get("shot") or {}).get("role") or ""),
        str(row.get("shot_id") or ""),
    ))
    return pool[int(variant_index) % len(pool)]


def match_manifest(manifest: dict[str, Any], analysis: dict[str, Any], *, topk: int = 3,
                   auto_accept_top1: bool = True,
                   narration_timing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recommend Top3 shots per sentence and automatically choose eligible Top1.

    A task may contain many spoken claims but the same source clip should not
    dominate the finished video.  The exact temporary shot is single-use and
    a source video may be reused at most twice by default.  The limit can be
    tightened per manifest with ``max_source_reuse``; if no eligible evidence
    remains, matching is blocked instead of silently repeating a clip.

    ``narration_timing`` 是 pre-TTS 逐句真值表（`timing_contract.narration_timing`
    的产物）。有它就用**真实音频时长**求解每个候选的可行窗口；没有它（老调用方
    或独立调用）退化为按段落自带 ``audio_duration_us`` / 字符数估算，并在报告里
    标 ``timing_certainty="estimated"``，绝不冒充真值。
    """
    shots = [s for s in analysis.get("shots", []) if isinstance(s, dict)]
    recommendations: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    used_shot_ids: set[str] = set()
    source_use_counts: dict[str, int] = {}
    max_final_materials = max(1, int(manifest.get("max_final_materials", 24) or 24))
    selected_material_count = 0
    reuse_events: list[dict[str, Any]] = []
    degraded_matches: list[dict[str, Any]] = []
    # DEMO_ACTIONS V0.2 §7：素材缺口。与 degraded_matches 的区别是——
    # 降级是「画面将就了」，缺口是「**这个卖点在当前素材类型下无法被证明**」，
    # 只能补拍或改脚本，匹配层修不掉。
    material_gaps: list[dict[str, Any]] = []
    # 预览稿里未解出可行窗口的段落（定案第 9 条）。单独记账，不并进 degraded_matches：
    # 那个列表驱动成片的「素材待补」标注，而这些段要打的是「未认证」标注。
    preview_unresolved_matches: list[dict[str, Any]] = []
    # 预览阶段没有满足严格匹配/复用约束的可审计画面时，保留音频与字幕，
    # 画面交给引擎生成黑色视频占位；这里的列表是运营可执行的补素材清单。
    visual_missing_matches: list[dict[str, Any]] = []
    ai_matches: dict[int, dict[str, dict[str, Any]]] = {}
    for item in analysis.get("semantic_matches", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        rows = item.get("top3") or item.get("candidates") or []
        if isinstance(rows, list):
            ai_matches[index] = {
                str(row.get("shot_id")): row for row in rows
                if isinstance(row, dict) and str(row.get("shot_id", "")).strip()
            }
    max_source_reuse = max(1, int(manifest.get("max_source_reuse", 2) or 2))
    avoid_adjacent_source_repeat = bool(manifest.get("avoid_adjacent_source_repeat", True))
    sentence_rows = _sentences(manifest)
    # Reserve direct candidates before the sequential allocator starts.  Without
    # this pre-pass, an earlier weak/degraded segment can consume the only direct
    # shot needed by a later segment; the later segment then gets a false gap.
    # Reservation is only a tie-breaking guard for degraded fallback: it never
    # overrides a valid direct selection and never lowers a direct candidate's
    # score to manufacture diversity.
    direct_reservations: dict[int, set[str]] = {}
    variant_index = max(0, int(manifest.get("variant_index", 0) or 0))
    variant_tolerance = max(0, int(manifest.get("variant_score_tolerance", 8) or 0))
    for reserve_index, reserve_segment in enumerate(sentence_rows, 1):
        reserve_text = str(reserve_segment.get("text", reserve_segment.get("caption", "")) or "").strip()
        reserve_claim = str(reserve_segment.get("claim_text") or reserve_text).strip()
        reserve_claims = demo_actions.requirements_for(reserve_text, reserve_claim)
        reserve_rows: set[str] = set()
        reserve_minimum = 70 if reserve_index in ai_matches else 100
        for reserve_shot in shots:
            reserve_score, reserve_matched = _candidate_score(
                reserve_claim, reserve_shot,
                reserve_segment.get("visual_requirements", reserve_segment.get("required_visuals")))
            reserve_id = str(reserve_shot.get("shot_id") or "")
            ai_row = ai_matches.get(reserve_index, {}).get(reserve_id)
            if ai_row is not None:
                ai_claims = semantic_gate.normalize_tags(ai_row.get("matched_claims"))
                reserve_score = (max(0, min(100, int(_number(ai_row.get("score"), 0))))
                                 if ai_claims else 0)
                reserve_matched = ai_claims
            packaging = demo_actions.packaging_claim_direct(reserve_shot, reserve_claim)
            if packaging.get("ok"):
                reserve_score = max(reserve_score, 100)
                if "PACKAGING_CLAIM_DIRECT" not in reserve_matched:
                    reserve_matched = [*reserve_matched, "PACKAGING_CLAIM_DIRECT"]
            gate = demo_actions.judge(reserve_shot, reserve_claims)
            reserve_cta_ok, _reserve_cta_reason = demo_actions.cta_quantity_eligible(
                reserve_shot, reserve_text)
            if packaging.get("ok") and not reserve_claims:
                gate = {"status": "ok", "ok": True, "reason": "PACKAGING_CLAIM_DIRECT",
                        "packaging_claim": packaging}
            if gate.get("ok") and reserve_cta_ok and reserve_score >= reserve_minimum:
                reserve_rows.add(reserve_id)
        direct_reservations[reserve_index] = reserve_rows
    previous_source = ""
    # 上一段采用的镜头本体（反重复新判据要读它的 setup_id / action_phase，
    # 光有 source 名不够 —— 同一条素材里不同机位属于不同 setup）。
    previous_shot: dict[str, Any] | None = None
    for index, source_segment in enumerate(sentence_rows, 1):
        text = str(source_segment.get("text", source_segment.get("caption", "")) or "").strip()
        claim = str(source_segment.get("claim_text") or text).strip()
        intents = demo_actions.infer_intents(text, claim)
        primary_intent = intents[0] if intents else "unknown"
        # DEMO_ACTIONS V0.2 闸门：这句话要求哪些原子卖点、各自要求什么动作/对象/
        # 结果/角色。旧 CLAIM_SYNONYMS 做的是名词性描述匹配，参考片的匹配单位是
        # **动词**，两者并存：卖点表命中时以卖点表为准（它带动作与结果约束），
        # 没命中时退回旧词表，行为与 v1.3.25 一致。
        required_claim_objs = demo_actions.requirements_for(text, claim)
        # 逐句配音时长**真值**：pre-TTS 真值表 → 段落自带 → 字符数估算（显式降级）。
        # 用户定案第 3 条：规划层与引擎读同一串字节，不再各估各的。
        audio_duration_us, timing_certainty = timing_contract.duration_for(
            narration_timing, index,
            key=timing_contract.sentence_key(source_segment, index),
            segment=source_segment)
        # 与时间相关的直接证据不得通过变速伪造（定案第 6 条）：这句话只要有任何
        # 一条 claim 要求直接证据，该句全部候选一律锁 1.0 倍速。
        time_sensitive = any(c.evidence_required for c in required_claim_objs)
        ranked = []
        for shot in shots:
            score, matched = _candidate_score(
                claim, shot,
                source_segment.get("visual_requirements", source_segment.get("required_visuals")))
            shot_id = str(shot.get("shot_id", ""))
            # The visual model ranks against the observed descriptions.  A
            # deterministic claim-tag score remains the fallback for older
            # analysis files, but a real AI result takes precedence so product
            # identity/usage claims that are not in the small canonical
            # vocabulary can still be matched without accepting arbitrary B-roll.
            ai_row = ai_matches.get(index, {}).get(shot_id)
            if ai_row is not None:
                ai_claims = semantic_gate.normalize_tags(ai_row.get("matched_claims"))
                # A numeric score without an observed fact is not an
                # auditable match.  Treat malformed/empty AI candidates as
                # zero instead of allowing a high score to bypass the hard
                # visual-evidence gate for non-canonical claims.
                score = (max(0, min(100, int(_number(ai_row.get("score"), 0))))
                         if ai_claims else 0)
                matched = ai_claims
            packaging_claim = demo_actions.packaging_claim_direct(shot, claim)
            if packaging_claim.get("ok"):
                score = max(score, 100)
                if "PACKAGING_CLAIM_DIRECT" not in matched:
                    matched = [*matched, "PACKAGING_CLAIM_DIRECT"]
            source_video = str(shot.get("video", ""))
            reuse_count = source_use_counts.get(source_video, 0)
            reuse_reasons: list[str] = []
            if shot_id in used_shot_ids:
                reuse_reasons.append("exact_shot_already_used")
            if reuse_count >= max_source_reuse:
                reuse_reasons.append("source_video_reuse_limit")
            eligible = not reuse_reasons
            # 每个候选都跑一次可行窗口求解（用户定案第 5 条：删掉 2 秒占位短路）。
            # 求解口径与引擎最终执行完全一致：同一份音频真值、同一组保护区、同一
            # 个变速范围、同一条证据覆盖约束。规划阶段判死的镜头，引擎不会在
            # write_draft 阶段才翻案 —— 那正是 AUDIO_VIDEO_MISMATCH 的来源。
            window = candidate_window(shot, audio_duration_us=audio_duration_us,
                                      manifest=manifest, time_sensitive=time_sensitive)
            claim_gate = demo_actions.judge(shot, required_claim_objs)
            if packaging_claim.get("ok") and not required_claim_objs:
                claim_gate = {"status": "ok", "ok": True,
                              "reason": "PACKAGING_CLAIM_DIRECT",
                              "packaging_claim": packaging_claim}
            cta_ok, cta_reason = demo_actions.cta_quantity_eligible(shot, text)
            ranked.append({
                "score": score, "shot_id": shot_id, "shot": shot, "matched": matched,
                "source_video": source_video, "reuse_count_before": reuse_count,
                "eligible": eligible, "reuse_block_reason": reuse_reasons,
                "claim_gate": claim_gate,
                "packaging_claim": packaging_claim,
                "cta_visual_ok": cta_ok,
                "cta_visual_reason": cta_reason,
                # 求解结果（可行解或结构化失败原因）。这是选镜的硬闸门之一。
                "window": window,
                "window_ok": bool(window.ok),
                "window_reason": str(window.reason or ""),
                # 历史字段：字符数估算的「装不装得下」。已退出决策路径，
                # 只留在报告里供人工核对求解结果与旧口径的差异。
                "fits_narration": narration_fits(shot, text, manifest),
            })

        # 相邻不得同源 —— v1.3.25 改为**硬约束**。用户定案：「重复可以两次，
        # 但两个片段连在一起就不行」。
        #
        # 旧实现是排序偏好，且门槛写死 `row["score"] >= 100`，而 AI 匹配走
        # 70 分制（见下方 `minimum_match_score = 70 if index in ai_matches`），
        # 条件对 AI 匹配的句子恒为假 —— 等于这个开关从来没生效过。实测
        # 2026-09-15 的「萌睡裤AI分镜测试」：18 段只用了 10 个素材，其中一个
        # 源连用 4 次，且段 12→15 连着四段都是它。
        #
        # 现在的语义是「有替代就禁止相邻」：只有当同源之外不存在任何可用镜头
        # 时才放行，避免把整句逼成 SHOT_NO_MATCH（那属于素材不足，应由
        # SHOT_NO_MATCH 如实报告，而不是靠相邻重复凑数）。
        adjacent_blocked = 0
        if avoid_adjacent_source_repeat and previous_source:
            has_other_source = any(r["eligible"] and r["source_video"] != previous_source
                                   for r in ranked)
            if has_other_source:
                for r in ranked:
                    if r["eligible"] and r["source_video"] == previous_source:
                        r["eligible"] = False
                        r["reuse_block_reason"] = [*r["reuse_block_reason"],
                                                   "adjacent_source_repeat"]
                        adjacent_blocked += 1
        # DEMO_ACTIONS V0.2 §4（用户定案）：反重复判据从「同一素材」升级为
        # 「同一机位 + 同一构图 + 同一动作阶段」。这样参考片 ref1#13→ref1#14
        # 连续两镜拉伸（机位或阶段不同，属于「一个卖点配 2–4 个不同角度」的
        # 正确做法）不会被误杀，而我方 12/21 镜的「木台排排包装 + 手推」
        # （setup 相同、阶段同为 static）会被拦住。
        #
        # 与相邻同源同一条口径：**有替代才禁**，否则会为了这条规则把整句逼成
        # SHOT_NO_MATCH —— 那属于素材不足，应如实上报而不是靠重复凑数。
        setup_blocked = 0
        if previous_shot is not None:
            def _conflict(row: dict[str, Any]) -> bool:
                return demo_actions.repeat_conflict(previous_shot, row["shot"])
            if any(r["eligible"] and _conflict(r) for r in ranked) and \
                    any(r["eligible"] and not _conflict(r) for r in ranked):
                for r in ranked:
                    if r["eligible"] and _conflict(r):
                        r["eligible"] = False
                        r["reuse_block_reason"] = [*r["reuse_block_reason"],
                                                   "same_setup_same_phase"]
                        setup_blocked += 1
        ranked.sort(key=lambda row: (not row["eligible"], -row["score"], row["shot_id"]))
        # DEMO_ACTIONS V0.2 §7：素材缺口按 **claim 粒度**在这里就登记，不等到
        # 「没选中镜头」才报。一句话常常同时要求多条卖点，其中品牌/形态那条容易
        # 满足、超薄那条可能根本无法证明 —— 整句一起判会让易满足的短路掉难的，
        # 缺口就报不出来（回归 R7b）。
        #
        # 缺口照报（不静默），但**不因此把整句判死**：v1.3.24 曾硬阻断到零成片，
        # 用户当时已否决。真正禁止的是拿包装展示/空镜去糊（见下方兜底池收紧）。
        for gap_claim in demo_actions.unmet_claims(shots, required_claim_objs):
            material_gaps.append(
                demo_actions.material_gap(index, text, claim, [gap_claim], shots))
        top = []
        for row in ranked[:max(1, int(topk))]:
            shot = row["shot"]
            top.append({"shot_id": row["shot_id"], "score": row["score"], "matched_claims": row["matched"],
                        "video": shot.get("video"), "source_start": shot.get("source_start"),
                        "duration": shot.get("duration"), "description": shot.get("description"),
                        "frame_path": shot.get("frame_path"), "status": shot.get("status"),
                        "eligible": row["eligible"],
                        "reuse_count_before": row["reuse_count_before"],
                        "reuse_block_reason": row["reuse_block_reason"]})
        recommendations.append({"index": index, "text": text, "claim_text": claim,
                                "intent": primary_intent, "intents": intents,
                                "top3": top, "adjacent_source_blocked": bool(adjacent_blocked),
                                "same_setup_same_phase_blocked": int(setup_blocked),
                                "required_claims": [c.as_dict() for c in required_claim_objs]})
        # Scores from the text matcher are 0-100; deterministic tag scores are
        # intentionally kept on the old 0/100 scale.  Treat an AI score of 70
        # or more as a qualifying semantic match, then still require the
        # evidence/action fields below before accepting it.
        minimum_match_score = 70 if index in ai_matches else 100
        required_claims = list(dict.fromkeys(
            semantic_gate.inferred_claim_tags(text)
            + semantic_gate.inferred_claim_tags(claim)))

        def _tier(row: dict[str, Any]) -> tuple[int, str]:
            """匹配档位（用户定案）：

            2 = 精准：画面直接拍到了该卖点（如「湿水不破」真有洗手/过水场景）；
            1 = 模糊：没有该场景，退而用产品展示类画面的关联支撑（材质特写等）；
            0 = 无关：不得采用，硬阻断交运营补素材。

            句子推不出任何卖点时**不再白送精准档**（v1.3.25 修正）。原先这里
            是 `return 2`，等于对该句完全不做相关性限制；而 `usable` 的判据里
            `tier > 0` 单独一条即可成立，于是任何一句不在卖点词表里的口播
            （「悬挂抽」「挂钩」「一箱4提」这类词都不在 CLAIM_SYNONYMS 的
            9 个词里）都能被语义分只有 20 的镜头接走，报告里还标成 exact。
            现在退回 tier 0：这类句子只剩语义分一条路，必须达到
            minimum_match_score 才可用。
            """
            if not required_claims:
                return 0, ""
            blob = semantic_gate.shot_evidence_text(row["shot"])
            for tag in required_claims:
                if semantic_gate._matches(tag, blob):
                    return 2, tag
            for tag in required_claims:
                hit = semantic_gate._associative_hit(tag, blob)
                if hit:
                    return 1, f"{tag}←{hit}"
            return 0, ""

        def _priority(row: dict[str, Any]) -> tuple:
            """选镜优先级（v1.3.27 结构修复后的口径）。

            **硬闸门**（必要条件，任一不满足即不可用）：

            - ``claim_ok``：DEMO_ACTIONS 闸门（空镜一票否决、证据要求必须满足）；
            - ``window.ok``：可行窗口求解有解（真实音频时长 + 保护区 + 转场把手 +
              变速范围 + 证据覆盖，全部同时满足）。

            在闸门之内按「语义达标 / 画面沾边」排优劣：语义达标优先，其次沾边。
            与 v1.3.24 的关键差别：长度判据从**字符数估算**换成**真实音频求解**，
            而且是硬闸门 —— 求解无解的镜头不再「先取最相关的、交引擎裁决」，
            因为引擎那边的裁决结果必然也是装不下（两边同一口径），拖到最后只会
            变成 ``AUDIO_VIDEO_MISMATCH`` 整支拦截。
            """
            tier, _basis = _tier(row)
            semantic_ok = row["score"] >= minimum_match_score
            window_ok = bool(row["window_ok"])
            # DEMO_ACTIONS V0.2 闸门是**必要条件**，不参与打分：
            # 空镜一票否决；证据要求为真的句子必须拿到 role>=要求 且 strength=A
            # 的动作。旧分析结果缺字段时 judge() 返回 status="ungated"/ok=True，
            # 行为与旧版一致，但覆盖率会写进报告。
            claim_ok = bool(row["claim_gate"].get("ok"))
            cta_ok = bool(row.get("cta_visual_ok", True))
            usable = claim_ok and cta_ok and window_ok and (semantic_ok or tier > 0)
            role_fit = demo_actions.intent_role_score(row["shot"], intents)
            # 多样性只做同等匹配候选的最后一级 tie-break：优先换源、换机位/构图
            # 和换动作阶段，但永远排在 claim/window/semantic/tier 之后，不能为了
            # “看起来丰富”牺牲脚本匹配度。
            diversity_fit = 0
            if previous_source and row["source_video"] != previous_source:
                diversity_fit += 2
            if previous_shot is not None:
                prev_setup = str(previous_shot.get("setup_id") or "")
                cur_setup = str(row["shot"].get("setup_id") or "")
                prev_phase = str(previous_shot.get("action_phase") or "")
                cur_phase = str(row["shot"].get("action_phase") or "")
                if prev_setup and cur_setup and prev_setup != cur_setup:
                    diversity_fit += 1
                if prev_phase and cur_phase and prev_phase != cur_phase:
                    diversity_fit += 1
            return (int(usable), int(semantic_ok and window_ok), int(window_ok and tier > 0),
                    int(semantic_ok), int(tier > 0), int(role_fit), int(diversity_fit),
                    row["score"])

        eligible_good = _variant_tiebreak(
            [row for row in ranked if row["eligible"]], _priority,
            variant_index=variant_index, score_tolerance=variant_tolerance)
        duration_degraded = False
        if eligible_good is not None:
            if not _priority(eligible_good)[0]:
                eligible_good = None          # 闸门不通过 → 交下面的兜底/缺口逻辑
            elif not (eligible_good["window_ok"]
                      and (eligible_good["score"] >= minimum_match_score
                           or _tier(eligible_good)[0] > 0)):
                duration_degraded = True      # 兜底采用的镜头，窗口未必最优
        # 多镜解（定案第 6 条）：这一段由**几镜共同承担**，各段窗口解在 span_parts 里。
        # 单镜路径下它是 None，段落结构与 v1.3.26 完全一致（下游无需兼容分支）。
        span_parts: list[dict[str, Any]] | None = None
        span_solution = None
        span_rows: list[dict[str, Any]] | None = None
        multi_shot = False
        degraded_fallback: dict[str, Any] | None = None
        unmatched_fallback = False
        # 预览模式（定案第 9 条）：这一句的真实音频时长在所有候选里都无解，
        # 但预览稿仍然采用语义最合适的那一条让画面出得来。**这不是降级采用**
        # （不是「语义不够只能将就」），是「长度暂时算不出来先摆上」——
        # 因此走独立的 `preview_unresolved` 记账，不混进 `degraded_no_match`，
        # 免得成片上打错标注（把「没解出来」说成「素材待补」）。
        preview_unresolved = False
        preview_reason = ""
        # preview 判定每句段统一一次口径（与 cli.cmd_build 归一化的一致）。v1.3.18
        # 起 preview 语义为「带缺口出稿、缺口如实上报」（2026-09-18 用户定案）：
        # 有关联展示镜头（product_display/数量/陈列/使用类，关键字级别也行）直接
        # 采用；确实没有才按语义分最高的候选降级兜底 —— 两条都只在 preview 生效。
        preview_mode = str(manifest.get("delivery_mode") or "").lower() in (
            "preview", "preview_only", "预览")

        def _append_visual_missing(reason: str, reason_code: str = "VISUAL_MISSING") -> None:
            """预览缺画面时留黑片段，不借用不相似或已被禁用的镜头。"""
            estimated_s = max(
                1.0,
                _number(audio_duration_us) / timing_contract.US + NARRATION_TAIL_PAD,
                estimated_narration_seconds(text) + NARRATION_TAIL_PAD,
            )
            operator_note = (
                "该句没有满足严格画面匹配、证据或复用约束的可审计素材；"
                "已留黑画面，保留完整旁白/配音、字幕和文案。补充相似素材后重跑该句。"
            )
            segment = {
                **{k: v for k, v in source_segment.items()
                   if k not in {"video", "source_start", "source_end", "duration", "in"}},
                "video": None,
                "source_start": 0.0,
                "source_end": 0.0,
                "duration": round(estimated_s, 6),
                "text": text,
                "claim_text": claim,
                "visual_requirements": semantic_gate.inferred_claim_tags(claim),
                "evidence_intervals": [],
                "action_complete": False,
                "temporary_shot_id": None,
                "selection_mode": "visual_missing",
                "visual_missing": True,
                "visual_missing_reason": str(reason),
                "visual_missing_reason_code": str(reason_code),
                "operator_note": operator_note,
                "degraded_no_match": False,
                "audio_duration_us": int(audio_duration_us),
                "timing_certainty": timing_certainty,
                "video_speed": 1.0,
                "window_solution": None,
                "window_reason": "VISUAL_MISSING",
                "preview_unresolved": True,
                "preview_reason": str(reason),
                "multi_shot": False,
                "span_parts": None,
                "span_solution": None,
            }
            segments.append(segment)
            visual_missing_matches.append({
                "index": index,
                "text": text,
                "claim_text": claim,
                "reason": str(reason),
                "reason_code": str(reason_code),
                "operator_note": operator_note,
                "audio_duration_us": int(audio_duration_us),
                "timing_certainty": timing_certainty,
            })
        if not eligible_good:
            # 先区分两种「没有合格镜头」，因为处置完全不同：
            #
            #   · **语义够但窗口无解** —— 素材本身没问题，是**长度**装不下这句话。
            #     这类必须如实报 SHOT_TIMING_INFEASIBLE 并带结构化原因码，交规划层
            #     换镜 / 多镜组合 / 报素材缺口。**不允许静默换成语义更弱的镜头**
            #     （用户定案第 7 条：引擎不得静默选择语义更弱的镜头，降级由规划层定）。
            #     注意这一步排在复用上限判定**之前**：一个镜头同时「够相关」且
            #     「装不下」时，先报长度问题才是用户定案第 9 条要的验收行为。
            #   · 语义不够 —— 保持 v1.3.25 的兜底 + 「素材待补」标注机制。
            evidence_reqs = [c for c in required_claim_objs if c.evidence_required]
            had_good = next((row for row in ranked if row["score"] >= minimum_match_score), None)
            # 复用上限在 preview/formal 都是硬约束。preview 不能因为要出整体草稿
            # 就绕过精确镜头、同源相邻和同机位同阶段规则；没有合资格画面时留黑。
            if had_good and all(
                    not row["eligible"] for row in ranked if row["score"] >= minimum_match_score):
                block_reasons = sorted({reason for row in ranked if row["score"] >= 100
                                        for reason in row["reuse_block_reason"]})
                if preview_mode:
                    _append_visual_missing(
                        "语义相关镜头均已被精确镜头/来源复用/相邻同源/同机位同阶段约束排除："
                        + ("、".join(block_reasons) or "reuse_guard"),
                        "SHOT_REUSE_LIMIT",
                    )
                    continue
                pending.append({"index": index, "type": "SHOT_REUSE_LIMIT",
                                "message": "可证明该句口播的镜头已达到重复使用上限，禁止继续重复展示",
                                "claim_text": claim, "max_source_reuse": max_source_reuse,
                                "block_reasons": block_reasons, "candidates": top})
                continue
            semantic_ok_rows = [row for row in ranked
                                if row["eligible"] and row["score"] >= minimum_match_score]
            if semantic_ok_rows and not any(r["window_ok"] for r in semantic_ok_rows):
                # 语义够、窗口全无解 —— 这就是本次定案要根治的场景。
                #
                # 处置顺序（用户定案第 6 条）：
                #   合格单镜（已排除）→ **多镜组合** → 允许范围内变速/尾帧（求解器已含）
                #   → MATERIAL_GAP
                # 所以在报无解之前必须**真的试过**拼多镜。以前这里直接报
                # SHOT_TIMING_INFEASIBLE，等于把「单镜不够」当成「素材不够」，
                # 素材充足的库里长句也会被误报成缺口。
                span = plan_span([r["shot"] for r in semantic_ok_rows],
                                 audio_duration_us=audio_duration_us, manifest=manifest,
                                 time_sensitive=time_sensitive)
                picked_rows = _commit_span(span, semantic_ok_rows) if span["ok"] else None
                if picked_rows:
                    # 多镜组合成功：把它当成**一句话被拆成多段画面**的正常采用，
                    # 不是降级。但报告里必须留痕（`span` 字段），否则人工复核看不到
                    # 这一段为什么是三镜拼的。
                    eligible_good = picked_rows[0]
                    span_rows = picked_rows
                    span_parts = [dict(item["part"], **{
                        "shot_id": str(item["shot"].get("shot_id") or ""),
                        "video": item["shot"].get("video"),
                        "scene_source_start": item["shot"].get("source_start"),
                        "scene_source_end": item["shot"].get("source_end"),
                        "evidence_intervals": _span_evidence_intervals(
                            item["shot"], claim, item["segment"]),
                    }) for item in span["parts"]]
                    multi_shot = len(span_parts) > 1
                    span_solution = span["solution"]
                else:
                    # 拼多镜也救不回来 → 如实报缺口。带**组合**的失败细节，
                    # 让运营看到的是「这些镜头加起来还差多少秒」，而不是模棱两可的
                    # 「装不下」。
                    #
                    # 预览模式（定案第 9 条）：单镜与多镜都无解，但预览就是要看
                    # 「除了这一句，整条片子长什么样」。所以**仍然采纳**这一句
                    # 语义最合适的镜头，让草稿能出图，同时把它记成未认证 —— 引擎
                    # 会在成片上打 UNCERTIFIED 标注、结果里带逐条理由。正式交付
                    # 走不到这一支（`delivery_mode` 由 CLI 归一化后注入）。
                    if preview_mode:
                        eligible_good = max(semantic_ok_rows, key=_priority)
                        preview_unresolved = True
                        preview_reason = str(span.get("reason") or "")
                    else:
                        detail = dict(span.get("detail") or {})
                        rows = sorted(semantic_ok_rows,
                                      key=lambda r: (_number((r["window"].detail or {}).get("required_us"), 0)
                                                     - _number((r["window"].detail or {}).get("usable_us"), 0),
                                                     -r["score"]))
                        best = rows[0]
                        first_reason = str(detail.get("first_reason")
                                           or (best["window"].detail or {}).get("first_reason")
                                           or span.get("reason") or best["window_reason"])
                        pending.append({
                            "index": index, "type": "SHOT_TIMING_INFEASIBLE",
                            "reason": first_reason,
                            "message": "该句口播的**真实音频时长**在单镜窗口与多镜组合内均无解；"
                                       "需规划层换镜或如实上报素材缺口，禁止静默改用语义更弱的镜头",
                            "claim_text": claim,
                            "text": text,
                            "audio_duration_us": int(audio_duration_us),
                            "timing_certainty": timing_certainty,
                            "time_sensitive": bool(time_sensitive),
                            "span_attempt": {"reason": str(span.get("reason") or ""),
                                             "ok": bool(span.get("ok")),
                                             "tried_parts": int(detail.get("tried_parts") or 0),
                                             "candidate_count": int(detail.get("candidate_count") or 0),
                                             "capacity_us": detail.get("capacity_us"),
                                             "shortfall_us": detail.get("shortfall_us"),
                                             "detail": detail},
                            "infeasible": [{"shot_id": r["shot_id"], "video": r["source_video"],
                                            "score": r["score"], "reason": r["window_reason"],
                                            "detail": r["window"].detail} for r in rows[:5]],
                            "candidates": top})
                        continue
            # 走到这里说明：没有任何镜头能满足「语义达标」或「画面沾边」中的任意
            # 一条 —— 素材库里确实没有能证明这句口播的画面。
            #
            # v1.3.18 在这里「随便拿一条已分析镜头兜底」，但既不标注也不设限，
            # 于是成了「包装镜头配纸质卖点」的张冠李戴；v1.3.24 改成硬阻断
            # （宁可不生成也不充数），代价是整条任务零成片。
            #
            # v1.3.25（用户定案 2026-09-16）：「缺句用降级画面兜底，但成片里标出来」。
            # 所以兜底恢复，但必须满足两个条件，缺一不算通过：
            #   ① `degraded_no_match=true` + `match_tier=fallback` 落进报告，
            #      `degraded_matches` 里能查到这句为什么被迫采用；
            #   ② 引擎据此在成片画面上打可见标注（engine.py 的 Degraded_Marks 轨），
            #      不允许降级画面冒充正常镜头混过人工复核。
            # 只有在连一条**未被复用约束卡住**的镜头都不剩时才硬阻断 —— 那说明
            # 镜头池真的被用光了，继续兜底只会让同一镜头反复出现。
            # 兜底池里也没有装得下的镜头 —— 这是**素材不够**，不是选镜没选好。
            #
            # 这里**不接多镜组合**：兜底本来就是语义将就（语义达标那条路径在上面
            # 已经试过多镜了），再拿几个语义不沾的镜头去拼长度，正是定案要消灭的
            # 张冠李戴。如实报长度无解 + 结构化原因码，交规划层换镜或上报缺口。
            #
            # v1.3.27 修复：兜底池此前只存在于 v1.3.26 的 `fallback_pool` 局部变量，
            # 1.3.27 重写这一段时忘了重建它（编译与 87 条测试都没走到这条分支，
            # 上线后第一次 preview 就 `NameError`）。现在显式构造：
            #   · 只收**未被复用约束卡住**的镜头（`eligible` 为真，含相邻同源 /
            #     同机位同阶段的排除结果）—— 池子空了就是素材用光，应报
            #     SHOT_NO_MATCH，而不是靠重复凑数；
            #   · 句子**全部**卖点都要求直接证据时再收紧：不拿包装展示 / 场景 /
            #     隐喻 / 空镜充数，且闸门已判 blocked 的镜头也不捡回来（否则会
            #     出现「闸门说它证明不了，兜底又把它们捡回来」）。
            fallback_pool = [row for row in ranked if row["eligible"]]
            if "cta" in intents or "quantity" in intents:
                fallback_pool = [row for row in fallback_pool
                                 if row.get("cta_visual_ok", True)]
                if not fallback_pool:
                    material_gaps.append({
                        "index": index,
                        "type": "MATERIAL_GAP",
                        "reason": "CTA_REQUIRES_MULTI_PACK_QUANTITY_SHOT",
                        "text": text,
                        "claim_text": claim,
                        "message": "CTA/数量话术没有多包、成排、整箱或可见数量关系；保留旁白字幕，不硬插单包特写",
                    })
            if preview_mode:
                # 预览的降级也必须是“相似的可用画面”：无卖点要求的句子不能
                # 因为池里有任意包装镜头就被硬塞进去。真正没有关联镜头时走
                # visual_missing，避免把不相干画面配到旁白上。
                fallback_pool = [row for row in fallback_pool
                                 if _tier(row)[0] > 0
                                 or row["score"] >= minimum_match_score]
        # 收紧池只在 formal 生效（2026-09-18 用户定案）：preview 下保留
            # product_display/context 等“关键字级别关联”的展示类镜头参与兜底，
            # 画面能展示卖点的镜头直接用 —— 不再因为「要求直接证据」就把池子
            # 掏空（那正是「100抽大容量→C-ABSORB-CAPACITY」张冠李戴的场景）。
            restrict_fallback = bool(evidence_reqs) and not preview_mode and all(
                c.evidence_required for c in required_claim_objs)
            if restrict_fallback:
                def _fallback_ok(row: dict[str, Any]) -> bool:
                    # 相关产品特写是明确的降级通道：它的 claim_gate 仍可为
                    # blocked（因为没有直接动作/结果），但必须把降级原因留在
                    # row 里，不能让它看起来像精准证据。
                    related_reason = demo_actions.fallback_reason(
                        row["shot"], required_claim_objs)
                    if related_reason:
                        row["fallback_reason"] = related_reason
                        return True
                    return (demo_actions.fallback_allowed(row["shot"])
                            and row["claim_gate"].get("status") != "blocked")

                fallback_pool = [row for row in fallback_pool if _fallback_ok(row)]
            # A degraded candidate may not consume a shot that a later sentence
            # can use as direct evidence.  Direct candidates were reserved in a
            # pre-pass because this decision is otherwise order-dependent.
            future_reserved = set().union(
                *(direct_reservations.get(future_index, set())
                  for future_index in range(index + 1, len(sentence_rows) + 1))
            )
            if future_reserved:
                fallback_pool = [row for row in fallback_pool
                                 if row["shot_id"] not in future_reserved]
            eligible_good = max(fallback_pool, key=_priority, default=None)
            if eligible_good is not None and not eligible_good["window_ok"]:
                detail = eligible_good["window"].detail or {}
                if preview_mode:
                    # 预览：兜底镜头也装不下 —— 照样采用，让预览出图，同时记账。
                    preview_unresolved = True
                    preview_reason = str(eligible_good["window_reason"])
                else:
                    pending.append({
                        "index": index, "type": "SHOT_TIMING_INFEASIBLE",
                        "reason": str(eligible_good["window_reason"]),
                        "message": "该句口播的真实音频时长在**全部可选镜头**的允许窗口内都无解；"
                                   "需规划层换镜或如实上报素材缺口",
                        "claim_text": claim, "text": text,
                        "audio_duration_us": int(audio_duration_us),
                        "timing_certainty": timing_certainty,
                        "time_sensitive": bool(time_sensitive),
                        "detail": detail,
                        "candidates": top})
                    continue
            if eligible_good is None:
                if preview_mode:
                    # 不能再用 max(ranked) 绕过 eligible。没有相似且可审计的可用
                    # 镜头时，留黑并把处理动作写入报告；这比拿不相关画面配卖点更安全。
                    _append_visual_missing(
                        "素材库中没有同时满足画面相关性、证据完整性和复用约束的可用镜头",
                        "SHOT_NO_MATCH",
                    )
                    continue
                else:
                    pending.append({
                        "index": index, "type": "SHOT_NO_MATCH",
                        "message": "当前商品素材没有任何可用的镜头（可选的都已被重复使用上限占用）"
                                   if not evidence_reqs else
                                   "该句要求直接证据，但素材库里没有可承担证明的镜头，"
                                   "且包装展示/空镜已被禁止兜底",
                        "claim_text": claim, "candidates": top,
                        "material_gap": bool(evidence_reqs)})
                    continue
            unmatched_fallback = True
        # 只要不是「语义达标 + 窗口有解」这个理想组合，就在报告里留痕：
        # 人工复核要能一眼看出这一句是靠什么过的、哪里将就了。
        window_solution = eligible_good["window"]
        if degraded_fallback is None and not preview_unresolved:
            tier_rank, tier_basis = _tier(eligible_good)
            # 兜底即使语义分数达标，也不是直接证据通过：它是因为 claim
            # gate 未通过才被选中，必须显式记成降级，尤其是产品特写承接卖点时。
            if (unmatched_fallback
                    or not (eligible_good["score"] >= minimum_match_score
                            and eligible_good["window_ok"])):
                reasons: list[str] = []
                if eligible_good["score"] < minimum_match_score:
                    reasons.append(f"语义分 {eligible_good['score']} 未达 {minimum_match_score}")
                if not eligible_good["window_ok"]:
                    reasons.append(
                        f"该素材装不下这句配音（求解失败：{eligible_good['window_reason']}；"
                        f"真实音频 {audio_duration_us / timing_contract.US:.2f}s，"
                        f"窗口求解口径的可用 {_number((window_solution.detail or {}).get('usable_us'), 0) / timing_contract.US:.2f}s）")
                if unmatched_fallback:
                    fallback_reason = str(eligible_good.get("fallback_reason") or "")
                    if fallback_reason == "PRODUCT_SELLING_POINT_CLOSEUP_DEGRADED":
                        reasons.append("没有直接动作/结果证据，使用与卖点相关的产品特写降级承接")
                    else:
                        reasons.append("素材库里没有能证明该句口播的镜头（AI 候选为空或全部低于阈值），"
                                       "本段为兜底采用，成片画面已打「素材待补」标注")
                if tier_rank == 1:
                    reasons.append(f"靠产品展示类画面关联证明（{tier_basis}）")
                elif tier_rank == 0:
                    reasons.append("句中没有可识别的卖点关键词（CLAIM_SYNONYMS 未覆盖），"
                                   "画面相关性无法用标签核验"
                                   if not required_claims else "画面与该句卖点无直接关系")
                degraded_fallback = {
                    "reason": "SHOT_NO_MATCH_DEGRADED",
                    "score": eligible_good["score"],
                    "minimum_match_score": minimum_match_score,
                    "unmatched_fallback": unmatched_fallback,
                    "match_tier": "fallback" if unmatched_fallback
                    else {2: "exact", 1: "fuzzy"}.get(tier_rank, "semantic_only"),
                    "match_basis": tier_basis,
                    "fallback_reason": str(eligible_good.get("fallback_reason") or ""),
                    "audio_duration_us": int(audio_duration_us),
                    "timing_certainty": timing_certainty,
                    "window_reason": str(eligible_good["window_reason"]),
                    "duration_degraded": duration_degraded,
                    "message": "该句为降级采用：" + "；".join(reasons)}
        # 多镜组合**不写进 degraded_fallback**（定案第 6 条把它列为正常处置，
        # 不是降级）：能进 `semantic_ok_rows` 的镜头语义分已达标，`_commit_span`
        # 又把各段窗口标成有解，所以上面这段留痕逻辑对多镜天然不成立 ——
        # `degraded_no_match` 保持 False，成片画面上也就不会多出一堆「素材待补」
        # 标注（那些标注只该给兜底段）。
        #
        # 多镜的可见性由段落自己的 `multi_shot` / `span_parts` / `span_solution`
        # 承担：人工复核要能看到「这句话是三镜拼的、各段多长、各段解在哪」。
        selected = eligible_good["shot"]
        material_cost = len(span_parts) if multi_shot and span_parts else 1
        if selected_material_count + material_cost > max_final_materials:
            material_gaps.append({
                "index": index,
                "type": "MATERIAL_GAP",
                "reason": "MAX_FINAL_MATERIALS_REACHED",
                "text": text,
                "claim_text": claim,
                "max_final_materials": max_final_materials,
                "selected_material_count": selected_material_count,
                "requested_material_count": material_cost,
                "message": "已达到24条素材上限；保留本段口播与字幕，不为满足素材数强行插镜头",
            })
            _append_visual_missing(
                "达到单条成片素材上限；本段保留口播字幕并留空",
                "MAX_FINAL_MATERIALS_REACHED",
            )
            continue
        selected_top = next((item for item in top if item["shot_id"] == eligible_good["shot_id"]), None)
        if selected_top is None:
            # 时长偏好可能挑中 top3 之外的镜头，这时不能拿 top[0] 的分数冒充——
            # 否则报告里会同时出现「选分 83」和「语义分 20 未达标」这种自相矛盾。
            selected_top = {"shot_id": eligible_good["shot_id"], "score": eligible_good["score"],
                            "matched_claims": eligible_good["matched"],
                            "video": selected.get("video"),
                            "source_start": selected.get("source_start"),
                            "duration": selected.get("duration"),
                            "description": selected.get("description"),
                            "frame_path": selected.get("frame_path"),
                            "status": selected.get("status"),
                            "eligible": True,
                            "reuse_count_before": eligible_good["reuse_count_before"],
                            "reuse_block_reason": []}
        if not auto_accept_top1:
            pending.append({"index": index, "type": "SHOT_MATCH_MANUAL_DISABLED",
                            "message": "当前策略要求自动采用 Top1；未启用人工选镜头", "claim_text": claim,
                            "selected": top[0]})
            continue
        if not selected or selected.get("status") != "ready_for_matching" or selected.get("action_complete") is not True:
            if preview_mode:
                _append_visual_missing(
                    "候选镜头尚未完成画面描述/证据标签/动作确认，不能作为可审计画面使用",
                    "SHOT_ANALYSIS_PENDING",
                )
                continue
            pending.append({"index": index, "type": "SHOT_ANALYSIS_PENDING",
                            "message": "Top1 镜头缺少完整画面描述、证据标签或动作完成确认", "claim_text": claim,
                            "selected": selected_top})
            continue
        selected_id = str(selected.get("shot_id", ""))
        selected_source = str(selected.get("video", ""))
        source_use_counts[selected_source] = source_use_counts.get(selected_source, 0) + 1
        used_shot_ids.add(selected_id)
        if source_use_counts[selected_source] > 1:
            reuse_events.append({"index": index, "shot_id": selected_id,
                                 "video": selected_source,
                                 "source_reuse_count": source_use_counts[selected_source],
                                 "reason": "distinct_evidence_required"})
        # 多镜组合（定案第 6 条）：这一段由**多镜共同承担**，第 2 段起的镜头也要
        # 逐个登记复用计数与「已用镜头」集合。只登记第一段等于给后面的段开后门 ——
        # 同一素材会在不同句子里被反复取用却不触发 reuse guard，正是「重复展示」
        # 那条硬约束要防的事。
        if multi_shot and span_rows:
            for part_index, extra in enumerate(span_rows[1:], start=2):
                extra_shot = extra.get("shot") or {}
                extra_id = str(extra_shot.get("shot_id") or "")
                extra_source = str(extra_shot.get("video") or "")
                if extra_id:
                    used_shot_ids.add(extra_id)
                if extra_source:
                    source_use_counts[extra_source] = source_use_counts.get(extra_source, 0) + 1
                    reuse_events.append({"index": index, "shot_id": extra_id,
                                         "video": extra_source,
                                         "source_reuse_count": source_use_counts[extra_source],
                                         "reason": "span_multi_shot",
                                         "span_part": part_index})
        # 多镜时本段就由**多镜共同承担**：证据区间必须逐段给（见
        # `_span_evidence_intervals`），否则第 2 段起会带着一段它既没罩住、
        # 也不是它自己那一帧的区间，引擎的「切片必须罩住证据」闸会整支拦掉。
        evidence = []
        if multi_shot and span_parts:
            evidence = [dict(item) for item in (span_parts[0].get("evidence_intervals") or [])]
        else:
            for interval in selected.get("evidence_intervals") or []:
                if isinstance(interval, dict):
                    evidence.append({**interval, "claim": claim})
        if not evidence:
            # v1.3.24（用户定案）：没有细粒度证据区间时，**不能拿整条场景窗口冒充证据**。
            # 成片切片是按配音长度裁的，永远盖不住整条场景，于是引擎那道
            # 「最终切片必须覆盖证据区间」的闸在正常工况下恒不通过（实测 8/8 全拦）。
            # 真正被审核过的只有抽帧那一刻（分析阶段取的是窗口中点帧，AI 也只看了它），
            # 所以证据区间取**已审核帧的邻域**：切片只要包含这一小段，就保证包含了
            # AI 实际看过并打标签的那一帧。区间来源记在 evidence_source 里，可审计。
            #
            # 这条区间**必须**与 `candidate_evidence_interval` 完全一致：求解器是按
            # 它算窗口的，写进段落的是另一条的话，引擎写入前的不变量校验会在
            # 「证据未覆盖」上把整支拦掉 —— 而那时草稿已经组装了一半。所以这里
            # 直接复用同一个函数，不另写一遍算法。
            ev_start, ev_end, frame_ts = candidate_evidence_interval(selected)
            evidence = [{
                "start": ev_start, "end": ev_end,
                "frame_path": selected.get("frame_path"), "claim": claim,
                "evidence_source": "audited_frame_neighborhood",
                # 区间只是用来让引擎把切片**居中**；真正被审核的是这一帧，
                # 校验也按这一帧判（区间宽度可以大于切片，帧不会）。
                "frame_time": frame_ts,
            }]
        next_best_score = max((row["score"] for row in ranked
                               if row["shot_id"] != selected_id and row["eligible"]), default=0)
        margin = selected_top["score"] - next_best_score
        segments.append({
            **{k: v for k, v in source_segment.items() if k not in {"video", "source_start", "duration", "in"}},
            "video": selected["video"], "source_start": selected["source_start"],
            "material_duration_us": selected.get("material_duration_us"),
            "duration": selected["duration"], "text": text, "claim_text": claim,
            "intent": primary_intent, "intents": intents,
            "visual_requirements": semantic_gate.inferred_claim_tags(claim),
            "evidence_tags": selected.get("evidence_tags") or selected.get("visual_tags"),
            "visual_tags": selected.get("visual_tags"), "visual_description": selected.get("description"),
            "evidence_intervals": evidence, "action_complete": True,
            # 被分析过的场景窗口边界。引擎据此校验成片切片没有越界到
            # 「未经分析」的画面上去（v1.3.24 用户定案）。
            "scene_source_start": selected.get("source_start"),
            "scene_source_end": selected.get("source_end"),
            "head_waste": selected.get("head_waste", 0.0), "tail_waste": selected.get("tail_waste", 0.0),
            "temporary_shot_id": selected_id, "persist_to_library": False,
            "selection_mode": (f"auto_top1_degraded_{(degraded_fallback or {}).get('match_tier', 'none')}"
                               if degraded_fallback else "auto_top1_with_reuse_guard"),
            "degraded_no_match": bool(degraded_fallback), "selection_score": selected_top["score"],
            "selection_margin": margin,
            "source_reuse_count": source_use_counts[selected_source],
            # DEMO_ACTIONS V0.2：这一段是靠哪条 claim 要求、通过什么证据档选中的。
            "claim_gate": eligible_good.get("claim_gate"),
            "packaging_claim": eligible_good.get("packaging_claim"),
            "cta_visual_evidence": {
                "ok": eligible_good.get("cta_visual_ok", True),
                "reason": eligible_good.get("cta_visual_reason", "NOT_CTA_OR_QUANTITY"),
            },
            "required_claims": [c.as_dict() for c in required_claim_objs],
            # ── 时序契约（v1.3.27）────────────────────────────────────────
            # 规划阶段这里就把**真实音频时长**对应的可行窗口解出来，引擎只执行、
            # 只校验，不再自己另算一套（用户定案第 3 条：统一口径）。
            "source_end": round(_number(selected.get("source_end"),
                                        _number(selected.get("source_start"))
                                        + _number(selected.get("duration"))), 6),
            "audio_duration_us": int(audio_duration_us),
            "timing_certainty": timing_certainty,
            "video_speed": float(window_solution.speed),
            "window_solution": window_solution.to_dict(),
            "window_reason": str(eligible_good["window_reason"]),
            # 画面角色：引擎据此取变速范围（定案第 6 条：速度范围由 visual_role 决定）。
            # 不写这一行的话，引擎那边读不到角色就只能按最保守的 1.0 锁死，
            # 明明允许微调的展示镜头也会变成无解。
            "visual_role": demo_actions.visual_role(selected),
            "time_sensitive": bool(time_sensitive),
            # ─ 多镜组合（v1.3.27 B 步 / 定案第 6 条）──────────────────────
            # 这一段由**几镜共同承担**时，完整解在 `span_parts`（每段：素材、窗口解、
            # 证据区间、速度各一份）。单镜路径下 `multi_shot=False`、`span_parts=None`，
            # 段落结构与 v1.3.26 完全一致 —— 下游不需要为多镜写兼容分支，
            # 只需在 `multi_shot` 为真时改读 `span_parts`。
            #
            # 上面那一组单值字段（`video` / `source_start` / `source_end` / `video_speed`
            # / `window_solution` / `evidence_intervals`）在多镜时**取第一段的值**：
            # 它们的历史含义是「这一段的画面」，保留成第一段可以让人工复核与旧工具
            # 仍然读得通；但**引擎装配必须按 `span_parts` 逐段落**，不能只落第一段，
            # 否则成片只有第一镜、后面几段的配音会压在静止画面上。
            "multi_shot": bool(multi_shot),
            "span_parts": span_parts,
            "span_solution": (span_solution.to_dict() if span_solution is not None else None),
            # 预览未解出（定案第 9 条）：这一句在单镜与多镜里都没解出可行窗口，
            # 预览稿按语义最合适的镜头先摆上。**判据与 `degraded_no_match` 分开** ——
            # 那是「素材库里没有能证明这句的镜头」，这是「有镜头、长度暂时无解」，
            # 成片上要打的标注不同，不能混为一谈。
            "preview_unresolved": bool(preview_unresolved),
            "preview_reason": str(preview_reason),
        })
        selected_material_count += material_cost
        if degraded_fallback:
            degraded_matches.append({"index": index, "text": text, "claim_text": claim,
                                     "shot_id": selected_id, **degraded_fallback})
        if preview_unresolved:
            preview_unresolved_matches.append({
                "index": index, "text": text, "claim_text": claim,
                "shot_id": selected_id, "reason": str(preview_reason),
                "audio_duration_us": int(audio_duration_us),
                "timing_certainty": timing_certainty,
                "message": "预览稿：该句真实音频时长在单镜与多镜组合内均无解，已按语义最合适的"
                           "镜头出占位画面，未认证、不得用于正式交付。"})
        previous_source = selected_source
        previous_shot = selected
    # 闸门认证状态必须**同时**进 output_manifest —— 这是引擎真正读的那份
    # （`cli._temporary_match` 只把 `result` 里的键写进 material_gaps.json，
    # 回给引擎的是 `matched["manifest"]`）。此前只挂在上面的 result 上，
    # 于是「本批闸门未认证」这条状态在报告里看得见、在引擎里看不见，
    # 成片也就从来没带过 UNCERTIFIED 标注 —— 报告和交付物对不上。
    gate_coverage = demo_actions.gate_coverage(shots)
    output_manifest = dict(manifest)
    output_manifest["segments"] = segments
    output_manifest["temporary_material_analysis"] = True
    output_manifest["temporary_shot_ids"] = [s.get("temporary_shot_id") for s in segments]
    output_manifest["max_source_reuse"] = max_source_reuse
    output_manifest["avoid_adjacent_source_repeat"] = avoid_adjacent_source_repeat
    output_manifest["claim_gate_coverage"] = gate_coverage
    output_manifest["visual_matching_governance"] = {
        "intent_classifier": "demo_actions.intent_v1",
        "intent_role_tiebreak_only": True,
        "related_product_closeup_degraded": True,
        "material_understanding_cache": "engine.probe_cache_v1",
        "direct_candidate_reservations": {
            str(key): sorted(value) for key, value in direct_reservations.items()
        },
        "degraded_never_steals_direct": True,
        "cta_quantity_gate": "multi_pack_or_visible_quantity_required",
        "max_final_materials": max_final_materials,
        "selected_material_count": selected_material_count,
        "intentional_gap_count": sum(1 for item in segments if item.get("visual_missing")),
        "intentional_gap_target": 3,
        "gap_count_over_target_requires_audit": sum(
            1 for item in segments if item.get("visual_missing")
        ) > 3,
    }
    # 预览稿里未解出的段落也要传到引擎（定案第 9 条）：引擎自己能发现的只是
    # 「时序求解失败」，而选镜层在这里**提前**把整句判成无解并采用了镜头，
    # 引擎那边看到的只是一段求解失败的普通片段 —— 少了这份名单，成片上就只剩
    # 闸门未认证这一条理由，运营看不出「哪几句是没算出来的」。
    output_manifest["preview_unresolved"] = preview_unresolved_matches
    output_manifest["visual_missing_matches"] = visual_missing_matches
    result = {"version": _skill_version(), "ok": not pending and bool(segments),
              "selection_policy": "auto_top1_no_human_gate_with_reuse_guard",
              "duplicate_policy": {"exact_shot_max_reuse": 1,
                                    "source_video_max_reuse": max_source_reuse,
                                    "avoid_adjacent_source_repeat": avoid_adjacent_source_repeat},
              "recommendations": recommendations, "segments": segments,
              "pending_items": pending, "accepted_shots": [s.get("temporary_shot_id") for s in segments],
              "reuse_counts": source_use_counts, "reuse_events": reuse_events,
              "degraded_matches": degraded_matches,
              # 交付稿里有多少段是降级采用的（含兜底）。>0 时成片画面上会带
              # 「素材待补」标注，人工复核必须看得见，不能靠翻报告才发现。
              "degraded_count": len(degraded_matches),
              "degraded_fallback_count": sum(
                  1 for item in degraded_matches if item.get("unmatched_fallback")),
              # DEMO_ACTIONS V0.2 §7（用户定案）：无合格动作的句子必须报缺口，
              # 禁止回退到包装展示或空镜。缺口不静默 —— 落盘 + 交付前可见。
              "material_gaps": material_gaps,
              "material_gap_count": len(material_gaps),
              "material_gap_claims": sorted({g["claim_id"] for g in material_gaps if g.get("claim_id")}),
              # 预览稿未解出的段落（定案第 9 条）：有内容时必须让编排层看得见 ——
              # 预览稿能出图不等于能交付，这份名单就是「哪些句还被挂着」。
              "preview_unresolved": preview_unresolved_matches,
              "preview_unresolved_count": len(preview_unresolved_matches),
              "visual_missing_matches": visual_missing_matches,
              "visual_missing_count": len(visual_missing_matches),
              # 闸门实际生效比例：旧分析结果缺 role/has_subject/action/setup_id
              # 字段时 judge() 会放行并标 ungated，这里把覆盖情况写出来，
              # 避免「以为上了闸门其实没生效」这种不可审计状态。
              "claim_gate_coverage": gate_coverage,
              "claim_gate_policy": {
                  "gate": "DEMO_ACTIONS_V0.2",
                  "direct_evidence_requires_strength": "A",
                  "forbid_fallback_roles": ["context", "CTA", "visual_metaphor"],
                  "forbid_generic_product_display_fallback": True,
                  "related_product_closeup_degraded": True,
                  "related_product_closeup_reason": "PRODUCT_SELLING_POINT_CLOSEUP_DEGRADED",
                  "related_product_closeup_requires_claim_overlap": True,
                  "forbid_empty_shot": True,
                  "repeat_rule": "same_setup_id AND same_action_phase",
              },
              "library_write": [], "manifest": output_manifest}
    return result
