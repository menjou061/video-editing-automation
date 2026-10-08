"""时序契约（Timing Contract）：全流水线唯一的「够不够长」求解器。

## 为什么要有这个模块

v1.3.26 及以前，同一件事被**三个互相不认识的时钟**各算了一遍：

1. **选镜**（`shot_analyzer.narration_fits`）：拿**字符数估算**的配音时长，和
   「素材时长 − 0.5s」比大小，只用来排优先级；
2. **引擎第一次切窗**（`engine.stabilize` → `stable_window_duration`）：配音还没合成，
   先用 **2 秒占位探针**找一个「稳定窗」；
3. **引擎最终裁决**（`engine.align_audio_video`）：真实配音 ffprobe 时长 vs
   「素材时长 − 0.2 − 入点」。

只有 ③ 是真的。①②算出来的数一旦和 ③ 不一致，就会出现「选镜时认为装得下、
切窗时按 2 秒摆位、最终裁决时发现装不下」——实测 2026-09-16 的
`recvvfR4QKW8Ky`：段 2「心相印也太会了…」选中 `波点挂抽_IMG_7950.MOV`（全库最短，
3.945s），`sound_dur = 4.16s`，`avail = 3.945 − 0.2 − 0.0 = 3.745s`，差 0.415s，
整支被 `AUDIO_VIDEO_MISMATCH` 拦死，一个草稿都出不来。

用户定案（2026-09-16）：「批准做结构性修复」，并要求
**不要只抽 usable_seconds() 减常数，改成统一的可行窗口求解** —— 输入源素材区间、
证据动作区间、真实音频时长、转场把手、允许变速；输出可行入出点**或结构化失败原因**。

## 本模块的口径

- **唯一真值**：`audio_duration_us` 来自最终音频文件的 ffprobe，不是估算。
- **全部整数微秒**：秒只在模块边界进出，内部一律 `int` µs，避免浮点累积误差。
- **容差 1 帧**：`check_invariants` 的默认容差，帧率取草稿 fps（`create_draft` 默认 30）。
- **不替规划层做决定**：求解器只回答「这个镜头在允许范围内有没有解」，没有解就返回
  结构化原因码。换不换镜头、要不要多镜拼、报不报 MATERIAL_GAP，由规划层定
  （用户定案：「引擎不得静默选择语义更弱镜头。降级由规划层决定，引擎只执行和验证」）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

US = 1_000_000

# 音频处理版本：TTS 的后处理/编码链（采样率、容器、重采样、响度处理等）一变就 +1。
# 它进 TTS 缓存键，保证「同样文本+音色但后处理变了」不会误复用旧音频字节。
AUDIO_PIPELINE_VERSION = "1"

# 字符数估算语速（字/秒）。**只在 TTS 挂掉时**用作显式降级值（见
# `narration_duration_us`），正常关键路径一律以最终音频 ffprobe 真值为准。
# 取实测偏慢的一档（实测 5.726 字/秒），估长一点比估短安全。
FALLBACK_CHARS_PER_SECOND = 5.0

DEFAULT_FPS = 30.0          # 与 pyJianYingDraft DraftFolder.create_draft 默认值一致
DEFAULT_TAIL_PAD_US = 120_000    # 每句口播尾部留白，避免贴边（原 engine.TAIL_PAD=0.12）
DEFAULT_HEAD_GUARD_US = 300_000  # 素材头废料保护区（手入画、曝光未稳）
DEFAULT_TAIL_GUARD_US = 200_000  # 素材尾废料保护区（手撤出、镜头甩走）

# 变速范围按画面角色分级（用户 2026-09-16 定案第 6 条：speed 范围由 visual_role 决定）。
#
# 关键约束：**时间相关的直接证据原则上不得通过变速伪造证据**。一段「湿水不破」的
# 洗手镜头被拉慢 0.8 倍，画面看起来仍在「过水」，但动作节奏已经不是拍摄时的真实
# 节奏了 —— 用变速把装不下的证据镜头塞进配音长度，等于用剪辑手法伪造证明。
# 所以 `direct_evidence` 锁死 1.0：装不下就如实报缺素材，交运营补拍。
SPEED_RANGE_BY_ROLE: dict[str, tuple[float, float]] = {
    "direct_evidence": (1.0, 1.0),
    "usage_demo": (0.92, 1.15),
    "product_display": (0.85, 1.20),
    "context": (0.85, 1.20),
    "CTA": (0.85, 1.20),
    "visual_metaphor": (0.92, 1.15),
}
# 角色缺失/不认识时锁 1.0：宁可不生成，也不在没有依据的情况下改时间轴。
DEFAULT_SPEED_RANGE: tuple[float, float] = (1.0, 1.0)

# 求解器搜索用的速度栅格步长（在 [speed_min, speed_max] 上遍历）。
SPEED_GRID_STEP = 0.01


def frame_us(fps: float = DEFAULT_FPS) -> int:
    """一帧对应的微秒数（整数）。fps 非法时退回 30。"""
    try:
        value = float(fps)
    except (TypeError, ValueError):
        value = DEFAULT_FPS
    if value <= 0:
        value = DEFAULT_FPS
    return int(round(US / value))


# 探测置信度分级（1.3.27.1 用户修正第 2 条）：调用方**必须**把这个分级带到
# 报告里，并且只有 HIGH 才允许参与「已认证」候选窗口的生成。
PROBE_CONFIDENCE_HIGH = "HIGH"
PROBE_CONFIDENCE_LOW = "LOW"


def probe_material_duration_us(path: Any, *, ffprobe: str | None = None,
                               ffmpeg: str | None = None) -> dict:
    """素材**总时长**的唯一探测口：返回 ``{"duration_us", "source",
    "confidence"}``（整数 µs + 探测来源 + 置信度）。

    为什么必须有这个函数（1.3.27.1 修复的根因）：iPhone 实拍的 MOV 音轨通常比
    视频轨长，容器时长（ffprobe ``format=duration``）于是**大于**视频轨时长 ——
    实测 `波点挂抽_IMG_7942.MOV`：容器 4.990000s，视频轨 4.983333s（写入层
    `draft.VideoMaterial` = pymediainfo 视频轨 = 4.984s）。规划层拿容器时长把
    出点摆到 4.99，写入层零容差必然 raise「变速切片越出素材范围」。

    策略（用户修正第 2 条的措辞）：**优先使用与写入层一致的视频素材时长；
    只有容器时长时进入保守降级**。分级表：

    ========  ====================  ==========
    置信度    source                说明
    ========  ====================  ==========
    HIGH      pymediainfo_video_track  写入层 VideoMaterial 同一代码路径，逐位一致
    HIGH      ffprobe_video_stream     ``-select_streams v:0`` 视频轨时长
    HIGH      last_video_pts           最后一个视频 packet 的 PTS（末帧起播点，
                                       只代表「末帧从这开始」，天然不大于真值）
    LOW       container_duration       容器时长 − 一帧安全边界，保守降级
    LOW       ffmpeg_container         ffmpeg stderr Duration − 一帧，保守降级
    ========  ====================  ==========

    容器降级路径（只有容器时长时）：先尝试解析最后一个视频 packet 的 PTS
    （升回 HIGH）；解析不了则至少减去一帧安全边界并标记 LOW。**LOW 的值不得
    用于生成「已认证」的候选窗口** —— 调用方（engine/shot_analyzer）必须把
    confidence 带进产物，让认证闸门看见它。

    注意：不要写「所有回退绝不会报告更长」这种话 —— 单纯向下取整不构成这个
    保证；保证来自「探测口径与写入层一致」＋「降级路径减帧」，所以每个返回值
    都必须带 source/confidence，由调用方决定能不能当真值用。

    返回 ``duration_us=0``（source="probe_failed"）表示连保守降级都做不了，
    调用方按既有逻辑报 source_probe_failed —— 这是「仍不能确认时保守失败」。
    """
    target = str(path)

    def result(duration_us: int, source: str, confidence: str) -> dict:
        return {"duration_us": int(duration_us), "source": source,
                "confidence": confidence}

    # 1) pymediainfo 视频轨 —— 写入层的同一把尺（HIGH）
    try:
        import pymediainfo
        info = pymediainfo.MediaInfo.parse(
            target, mediainfo_options={"File_TestContinuousFileNames": "0"})
        if info and info.video_tracks:
            ms = info.video_tracks[0].duration
            if ms and isinstance(ms, (int, float)) and float(ms) > 0:
                return result(int(float(ms) * 1e3),       # 与 VideoMaterial 逐位一致
                              "pymediainfo_video_track", PROBE_CONFIDENCE_HIGH)
    except Exception:
        pass

    if ffprobe:
        # 2) ffprobe 视频轨（HIGH）
        try:
            out = subprocess.run(
                [str(ffprobe), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=duration", "-of",
                 "default=noprint_wrappers=1:nokey=1", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=True)
            seconds = float(out.stdout.strip() or 0.0)
            if seconds > 0:
                return result(int(seconds * US), "ffprobe_video_stream",
                              PROBE_CONFIDENCE_HIGH)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        # 3) 最后一个视频 packet 的 PTS（HIGH）。容器时长可用而视频轨时长取不到
        #    时的第一选择：末帧 PTS 天然 ≤ 真值（它不含末帧自身的显示时长），
        #    不会把窗口摆到写入层拒绝的位置。
        try:
            out = subprocess.run(
                [str(ffprobe), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=pts_time", "-of",
                 "csv=p=0", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, check=True)
            pts_lines = [ln for ln in (out.stdout or "").splitlines()
                         if ln.strip() and ln.strip() != "N/A"]
            if pts_lines:
                last_pts = float(pts_lines[-1].strip())
                if last_pts > 0:
                    return result(int(last_pts * US), "last_video_pts",
                                  PROBE_CONFIDENCE_HIGH)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        # 4) ffprobe 容器（LOW，保守降级：至少减去一帧安全边界）
        try:
            out = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=True)
            seconds = float(out.stdout.strip() or 0.0)
            if seconds > 0:
                # 一帧安全边界按默认帧率取（此处拿不到真实 fps；真实漂移 5~19ms
                # < 30fps 一帧 33.3ms，减一帧必然落到视频轨之下）。
                return result(max(0, int(seconds * US) - frame_us()),
                              "container_duration", PROBE_CONFIDENCE_LOW)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    command = str(ffmpeg or "ffmpeg")
    # 5) ffmpeg stderr 容器（LOW，同上减一帧）
    try:
        proc = subprocess.run([command, "-hide_banner", "-i", target],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
                          proc.stderr or "")
        if match:
            hours, minutes, seconds = match.groups()
            total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            if total > 0:
                return result(max(0, int(total * US) - frame_us()),
                              "ffmpeg_container", PROBE_CONFIDENCE_LOW)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return result(0, "probe_failed", PROBE_CONFIDENCE_LOW)


def speed_range_for_role(role: Any, *, time_sensitive: bool = False) -> tuple[float, float]:
    """该画面角色允许的变速范围。

    ``time_sensitive=True`` 表示这一段承载的是**与时间相关的直接证据**
    （claim 的 ``evidence_required=True``），一律锁 1.0，不允许用变速凑长度。
    """
    if time_sensitive:
        return (1.0, 1.0)
    return SPEED_RANGE_BY_ROLE.get(str(role or "").strip(), DEFAULT_SPEED_RANGE)


def us_of(seconds: Any, default_us: int) -> int:
    """秒 → 整数微秒；``None``/空/非法值一律退回 ``default_us``（整数微秒）。

    存在的意义是**别让默认值再以秒的形式散落在各模块里**：保护区、尾留白这些
    数值一旦在 engine 里写成 ``0.3``、在 shot_analyzer 里写成 ``300_000``，
    两处口径迟早会漂。统一从这里取，单位也统一成 µs（定案第 3 条）。
    """
    if seconds is None or seconds == "":
        return int(default_us)
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return int(default_us)
    if value < 0:
        return int(default_us)
    return int(round(value * US))


def frame_speed(speed: Any, lo: Any = None, hi: Any = None, *,
                fps: float = DEFAULT_FPS) -> float:
    """把求解出的速度吸附到 1 帧的**整数微秒**档位上（容差 ≤1 帧，定案第 3 条）。

    为什么必须吸附：剪映的 ``VideoSegment`` 自己会用
    ``round(source_timerange.duration / speed)`` **回算**输出时长。写进草稿后真正
    参与渲染的是那个回算值，和求解器给的 ``segment_us`` 差一点点，音画就对不上。

    吸附精度取「1 帧 = 33333µs 的倒数」，而不是简单四舍五入到两位小数：后者
    在 ``round(material / speed)`` 里会放大成几毫秒的误差（实测 1.4 倍速下差 12ms，
    约 0.4 帧）。整数微秒档位把回程误差压到 **≤ 1µs**（对 0.85/0.92/1.0/1.15/1.2
    以及 0.5~2.0 全栅格实测：最大 1µs，即 1/33333 帧），远在 1 帧容差之内。

    ``lo``/``hi`` 是该画面角色允许的变速区间（``speed_range_for_role``）。**必须传**
    才能保证吸附后的值仍在范围内：栅格点一般落在区间端点内侧一丁点（``frame_speed(0.85)
    = 0.8499985``，比下界小 1.5e-6），而不变量校验是按区间卡的，不夹紧就会在写入前
    误报 ``SPEED_OUT_OF_RANGE``。夹紧方向朝**区间内**取最近的栅格点。
    """
    try:
        value = float(speed)
    except (TypeError, ValueError):
        value = 1.0
    if not math.isfinite(value) or value <= 0:
        value = 1.0
    step = 1.0 / frame_us(fps)        # 1µs 是 1 帧的 frame_us 分之一
    snapped = round(max(1, int(round(value / step))) * step, 9)

    if lo is None or hi is None:
        return snapped
    try:
        low, high = float(lo), float(hi)
    except (TypeError, ValueError):
        return snapped
    if not (math.isfinite(low) and math.isfinite(high)):
        return snapped
    if high < low:
        low, high = high, low
    if snapped < low:
        n = math.ceil(low / step)          # 区间内最小的栅格点
        return round(n * step, 9) if n * step <= high + 1e-12 else snapped
    if snapped > high:
        n = math.floor(high / step)        # 区间内最大的栅格点
        return round(n * step, 9) if n * step >= low - 1e-12 else snapped
    return snapped


@dataclass(frozen=True)
class WindowRequest:
    """一次可行窗口求解的全部输入。

    素材侧与输出侧刻意分开，避免重复扣减（用户定案第 5 条）：

    - **素材侧**：``source_in_us``/``source_out_us`` 圈定可用区间，
      ``head_guard_us``/``tail_guard_us`` 是区间内的首尾废料保护，
      ``transition_in_handle_us``/``transition_out_handle_us`` 是前后转场把手。
      这五者共同决定「实际能消耗多少素材」。
    - **输出侧**：``tail_pad_us`` 只加在输出时间轴上（旁白播完再留白），
      **不从素材侧扣**。旧实现把 tail_pad 混进素材边界判断，是「同一个数被扣两次」
      的来源之一。
    """

    source_in_us: int
    source_out_us: int
    audio_duration_us: int               # ★ 最终音频 ffprobe 真值
    tail_pad_us: int = DEFAULT_TAIL_PAD_US
    head_guard_us: int = DEFAULT_HEAD_GUARD_US
    tail_guard_us: int = DEFAULT_TAIL_GUARD_US
    transition_in_handle_us: int = 0
    transition_out_handle_us: int = 0
    evidence_start_us: Optional[int] = None
    evidence_end_us: Optional[int] = None
    speed_min: float = 1.0
    speed_max: float = 1.0
    preferred_start_us: Optional[int] = None
    video_duration_us: Optional[int] = None
    fps: float = DEFAULT_FPS

    @property
    def timeline_us(self) -> int:
        """输出时间线占位长度 = 音频时长 + 尾部留白。与音频的差恒为 tail_pad。"""
        return max(0, int(self.audio_duration_us)) + max(0, int(self.tail_pad_us))

    @property
    def lo_bound_us(self) -> int:
        """素材侧最早可入点。"""
        return (int(self.source_in_us) + max(0, int(self.head_guard_us))
                + max(0, int(self.transition_in_handle_us)))

    @property
    def hi_bound_us(self) -> int:
        """素材侧最晚可出点。"""
        return (int(self.source_out_us) - max(0, int(self.tail_guard_us))
                - max(0, int(self.transition_out_handle_us)))

    @property
    def usable_us(self) -> int:
        """素材侧实际可消耗量（已扣 guards 与 handles）。"""
        return self.hi_bound_us - self.lo_bound_us

    @property
    def evidence_span_us(self) -> Optional[int]:
        if self.evidence_start_us is None or self.evidence_end_us is None:
            return None
        start = int(self.evidence_start_us)
        end = int(self.evidence_end_us)
        return end - start if end > start else None


@dataclass(frozen=True)
class WindowSolution:
    """可行解，或结构化失败原因。"""

    ok: bool
    speed: float = 1.0
    source_start_us: int = 0
    source_end_us: int = 0
    segment_us: int = 0          # 时间线占位（含 tail_pad），恒等于 request.timeline_us
    material_us: int = 0         # 实际消耗素材长度 == source_end_us - source_start_us
    handle_room_us: int = 0      # 未被吃掉、仍可让转场重叠的素材余量
    reason: str = ""             # ok=False 时的结构化原因码
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "speed": round(self.speed, 6),
            "source_start": round(self.source_start_us / US, 6),
            "source_end": round(self.source_end_us / US, 6),
            "segment_s": round(self.segment_us / US, 6),
            "material_s": round(self.material_us / US, 6),
            "handle_room_s": round(self.handle_room_us / US, 6),
            "reason": self.reason, "detail": self.detail,
        }


# ---- 结构化失败原因码（规划层据此决定换镜 / 多镜 / MATERIAL_GAP）----
REASON_SOURCE_TOO_SHORT = "SOURCE_TOO_SHORT"              # 允许的最慢速度也装不下
REASON_EVIDENCE_OUT_OF_RANGE = "EVIDENCE_OUT_OF_RANGE"    # 证据区间落在保护区/素材外
REASON_EVIDENCE_SPAN_TOO_LONG = "EVIDENCE_SPAN_TOO_LONG"  # 证据跨度超过最大可用切片
REASON_EVIDENCE_SPEED_CONFLICT = "EVIDENCE_SPEED_CONFLICT"  # 证据在范围内但放不下
REASON_SPEED_OUT_OF_RANGE = "SPEED_OUT_OF_RANGE"          # 其余无解（防御性）
REASON_NO_AUDIO = "NO_AUDIO"                              # 音频时长为 0，无从求解

FAIL_REASONS = (
    REASON_NO_AUDIO, REASON_SOURCE_TOO_SHORT, REASON_EVIDENCE_OUT_OF_RANGE,
    REASON_EVIDENCE_SPAN_TOO_LONG, REASON_EVIDENCE_SPEED_CONFLICT,
    REASON_SPEED_OUT_OF_RANGE,
)


def _fail(reason: str, **detail) -> WindowSolution:
    return WindowSolution(ok=False, reason=reason, detail=detail)


def _candidate_speeds(req: WindowRequest, usable_us: int, timeline_us: int,
                      evidence_span_us: Optional[int]) -> list[float]:
    """候选速度集合。

    用户定案第 4 条明确**不要**把 ``s = (A − handles) / T`` 当作唯一公式：
    那个式子只表达了「恰好铺满可用素材」一种意图。一旦证据跨度把窗口下限抬高，
    或 ``speed_min`` 把下界卡住，它给出的值可能根本放不下（窗口放不进边界），
    而真正可行的速度在别处。所以这里枚举一批有意义的候选，再在**可行集**里挑
    最接近 1.0 的那个 —— 「不必变速」因此天然优先。
    """
    lo = float(req.speed_min)
    hi = float(req.speed_max)
    if hi < lo:
        lo, hi = hi, lo
    frame = frame_us(req.fps)
    values: set[float] = {1.0, lo, hi}

    if timeline_us > 0:
        values.add(usable_us / timeline_us)                     # 恰好铺满可用素材
        if evidence_span_us:
            # 恰好罩住证据跨度（两端各留一帧余量，避免四舍五入后卡边）
            values.add((evidence_span_us + 2 * frame) / timeline_us)

    steps = int(round((hi - lo) / SPEED_GRID_STEP))
    for i in range(steps + 1):
        values.add(round(lo + i * SPEED_GRID_STEP, 6))

    return sorted(v for v in values if lo - 1e-9 <= v <= hi + 1e-9)


def solve_window(req: WindowRequest) -> WindowSolution:
    """求 ``source_start / source_end / video_speed`` 的可行组合。

    约束（必须同时满足）：

    1. **源边界**：窗口完全落在 ``[source_in + head_guard + in_handle,
       source_out − tail_guard − out_handle]`` 内；
    2. **证据覆盖**：窗口必须罩住 ``[evidence_start, evidence_end]``
       （已审核帧邻域，剪掉它等于把 AI 实际看过并打标签的那一帧剪掉）；
    3. **速度范围**：``speed_min ≤ v ≤ speed_max``（由画面角色决定，见
       ``speed_range_for_role``）；
    4. **时长严格**：时间线占位恒等于 ``音频 + tail_pad``，与音频误差恒为 tail_pad。

    目标：在可行集里选 ``|v − 1.0|`` 最小者；并列时选窗口更小的（少动素材）。
    """
    timeline_us = req.timeline_us
    if timeline_us <= 0:
        return _fail(REASON_NO_AUDIO, audio_duration_us=req.audio_duration_us)

    lo_bound = req.lo_bound_us
    hi_bound = req.hi_bound_us
    usable_us = hi_bound - lo_bound

    ev_start = req.evidence_start_us
    ev_end = req.evidence_end_us
    span = req.evidence_span_us
    if span is not None:
        ev_start, ev_end = int(ev_start), int(ev_end)
        # 证据区间必须落在保护区之内：落在头尾废料里说明分析阶段给出的帧
        # 本身就不可用，这不是「窗口摆放」能解决的，交规划层换镜。
        if ev_start < lo_bound or ev_end > hi_bound:
            return _fail(REASON_EVIDENCE_OUT_OF_RANGE, lo_bound_us=lo_bound,
                         hi_bound_us=hi_bound, evidence_start_us=ev_start,
                         evidence_end_us=ev_end)

    speed_min = float(req.speed_min)
    speed_max = float(req.speed_max)
    if speed_max < speed_min:
        speed_min, speed_max = speed_max, speed_min

    if usable_us < int(round(speed_min * timeline_us)):
        return _fail(REASON_SOURCE_TOO_SHORT, usable_us=usable_us,
                     required_us=int(round(speed_min * timeline_us)),
                     speed_min=speed_min, timeline_us=timeline_us)

    feasible: list[tuple[float, int, float, int]] = []

    def _snapped_speed_that_fits(speed: float) -> Optional[float]:
        """Return the speed the writer will use, still inside this window.

        The writer snaps speeds to a one-frame reciprocal grid.  A raw boundary
        candidate such as ``usable / timeline`` can therefore snap *up* by a
        few microseconds and become illegal at write time.  Choose the nearest
        grid point that does not cross the already-proved material boundary.
        """
        snapped = frame_speed(speed, speed_min, speed_max, fps=req.fps)
        if int(round(snapped * timeline_us)) <= usable_us:
            return snapped

        step = 1.0 / frame_us(req.fps)
        cap_n = int(math.floor((usable_us / timeline_us) / step + 1e-12))
        low_n = int(math.ceil(speed_min / step - 1e-12))
        for n in range(cap_n, low_n - 1, -1):
            candidate = round(n * step, 9)
            if candidate < speed_min - 1e-9 or candidate > speed_max + 1e-9:
                continue
            if int(round(candidate * timeline_us)) <= usable_us:
                return candidate
        return None

    for raw_speed in _candidate_speeds(req, usable_us, timeline_us, span):
        speed = _snapped_speed_that_fits(raw_speed)
        if speed is None:
            continue
        window_us = int(round(speed * timeline_us))
        place_lo = lo_bound
        place_hi = hi_bound - window_us
        if span is not None:
            # 窗口罩住证据 ⇔ start ≤ ev_start 且 start + window ≥ ev_end
            place_lo = max(place_lo, ev_end - window_us)
            place_hi = min(place_hi, ev_start)
        if place_lo > place_hi:
            continue
        if span is not None:
            center = int(round((ev_start + ev_end) / 2 - window_us / 2))
            start = max(place_lo, min(center, place_hi))
        else:
            preferred = req.preferred_start_us
            preferred = place_lo if preferred is None else int(preferred)
            start = max(place_lo, min(preferred, place_hi))
        feasible.append((abs(speed - 1.0), window_us, speed, start))

    if not feasible:
        if span is not None and span > int(round(speed_max * timeline_us)):
            return _fail(REASON_EVIDENCE_SPAN_TOO_LONG, evidence_span_us=span,
                         max_window_us=int(round(speed_max * timeline_us)),
                         speed_max=speed_max, timeline_us=timeline_us)
        if span is not None:
            return _fail(REASON_EVIDENCE_SPEED_CONFLICT, evidence_span_us=span,
                         usable_us=usable_us, timeline_us=timeline_us,
                         speed_min=speed_min, speed_max=speed_max)
        # 无证据时的理论兜底：A ≥ speed_min·T 已经保证 v=speed_min 可行，
        # 走到这里说明边界退化了，如实报出来而不是硬塞一个解。
        return _fail(REASON_SPEED_OUT_OF_RANGE, usable_us=usable_us,
                     timeline_us=timeline_us, speed_min=speed_min, speed_max=speed_max)

    feasible.sort(key=lambda item: (item[0], item[1], item[3]))
    _dist, window_us, speed, start = feasible[0]
    return WindowSolution(
        ok=True, speed=round(speed, 9), source_start_us=int(start),
        source_end_us=int(start) + int(window_us), segment_us=int(timeline_us),
        material_us=int(window_us), handle_room_us=int(usable_us - window_us),
        detail={"candidate_count": len(feasible), "usable_us": int(usable_us),
                "timeline_us": int(timeline_us)},
    )


# ---- 多镜组合（用户定案第 6 条：一句话跨多个镜头）---------------------------
#
# 单镜装不下时的下一步不是「换一个更弱的镜头」，也不是「变速硬塞」，而是
# **把这句话按画面切成几段、每段用一个镜头**（拍板第 6 条的处置顺序：
# 合格单镜 → 多镜组合 → 允许范围内变速/尾帧 → MATERIAL_GAP）。
#
# 与单镜求解的关系：组合求解**不引入新的时间口径**，它只是把同一份音频时长
# 按各段的能力拆开，再对每段调用同一个 `solve_window`。因此
# 「每段自己的窗口合法」+「各段输出时长之和恒等于音频+尾留白」两条合起来，
# 就等价于单镜那条不变量，写入前的 `check_invariants` 不需要为多镜放宽任何阈值。
MIN_SPAN_PART_US = 300_000   # 单个镜头在组合里至少要贡献 0.3s，否则是闪帧不是镜头


@dataclass(frozen=True)
class SpanSegment:
    """组合里的一个镜头（一段画面）。字段口径与 `WindowRequest` 完全一致。"""

    source_in_us: int
    source_out_us: int
    head_guard_us: int = DEFAULT_HEAD_GUARD_US
    tail_guard_us: int = DEFAULT_TAIL_GUARD_US
    transition_in_handle_us: int = 0
    transition_out_handle_us: int = 0
    evidence_start_us: Optional[int] = None
    evidence_end_us: Optional[int] = None
    speed_min: float = 1.0
    speed_max: float = 1.0
    video: str = ""
    shot_id: str = ""

    @property
    def lo_bound_us(self) -> int:
        return (int(self.source_in_us) + max(0, int(self.head_guard_us))
                + max(0, int(self.transition_in_handle_us)))

    @property
    def hi_bound_us(self) -> int:
        return (int(self.source_out_us) - max(0, int(self.tail_guard_us))
                - max(0, int(self.transition_out_handle_us)))

    @property
    def usable_us(self) -> int:
        return self.hi_bound_us - self.lo_bound_us

    @property
    def evidence_span_us(self) -> Optional[int]:
        if self.evidence_start_us is None or self.evidence_end_us is None:
            return None
        start, end = int(self.evidence_start_us), int(self.evidence_end_us)
        return end - start if end > start else None

    def request(self, *, audio_duration_us: int, tail_pad_us: int,
                fps: float = DEFAULT_FPS) -> WindowRequest:
        """把这个镜头摊成一次单镜求解请求（组合求解内部就调它）。"""
        return WindowRequest(
            source_in_us=int(self.source_in_us), source_out_us=int(self.source_out_us),
            audio_duration_us=int(audio_duration_us), tail_pad_us=int(tail_pad_us),
            head_guard_us=int(self.head_guard_us), tail_guard_us=int(self.tail_guard_us),
            transition_in_handle_us=int(self.transition_in_handle_us),
            transition_out_handle_us=int(self.transition_out_handle_us),
            evidence_start_us=self.evidence_start_us, evidence_end_us=self.evidence_end_us,
            speed_min=float(self.speed_min), speed_max=float(self.speed_max), fps=fps)


@dataclass(frozen=True)
class SpanRequest:
    segments: tuple = ()                  # 顺序即成片顺序
    audio_duration_us: int = 0
    tail_pad_us: int = DEFAULT_TAIL_PAD_US
    fps: float = DEFAULT_FPS

    @property
    def timeline_us(self) -> int:
        """整句在输出时间线上的总长度 = 音频 + 尾留白（与单镜同一口径）。"""
        return max(0, int(self.audio_duration_us)) + max(0, int(self.tail_pad_us))


@dataclass(frozen=True)
class SpanSolution:
    """组合解：`parts` 与 `segments` 一一对应，或结构化失败原因。"""

    ok: bool
    parts: tuple = ()
    reason: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def segment_us(self) -> int:
        return sum(int(p.segment_us) for p in self.parts)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason, "detail": self.detail,
            "segment_s": round(self.segment_us / US, 6),
            "parts": [p.to_dict() for p in self.parts],
        }


def solve_span(req: SpanRequest) -> SpanSolution:
    """把一句话的输出时间线分摊给多个镜头，逐段求可行窗口。

    分摊规则必须**可审计且不偏袒**，否则「多镜」会变成「随便把剩下的时间塞给
    最后一个镜头」：

    1. 每段先拿一个下限 —— ``MIN_SPAN_PART_US``，带证据的段还要多拿够罩住证据
       的最小时长（``证据跨度 / speed_max + 1帧``）；下限还得被该段自身能力夹住，
       否则一条很短的镜头会被分到它吃不下的份额；
    2. 剩余时间按各段**余量**（``usable/speed_min − 下限``）等比分配。等比意味着
       「谁更能吃谁多担」，且天然保证每段分到的时长不超过它自己的上限；
    3. 整数微秒取整丢掉的那几 µs 补给余量最大的一段，保证**各段之和恰好等于**
       ``音频 + 尾留白`` —— 差一 µs 都会让写入前的不变量校验判 DURATION_MISMATCH。

    分配完成后每段单独跑 ``solve_window``。因为分配时已保证
    ``分到时长 × speed_min ≤ usable``，所以 ``speed_min`` 一定可行；
    但真实求解仍要走一遍，因为**证据覆盖**与**落点夹紧**只有求解器知道。
    """
    timeline_us = req.timeline_us
    if timeline_us <= 0:
        return SpanSolution(ok=False, reason=REASON_NO_AUDIO,
                            detail={"audio_duration_us": int(req.audio_duration_us)})
    segs = list(req.segments)
    if not segs:
        return SpanSolution(ok=False, reason=REASON_SOURCE_TOO_SHORT,
                            detail={"scope": "span", "segments": 0,
                                    "message": "组合里没有任何镜头"})
    if len(segs) == 1:
        only = segs[0]
        part = solve_window(only.request(
            audio_duration_us=int(req.audio_duration_us),
            tail_pad_us=int(req.tail_pad_us), fps=req.fps))
        if not part.ok:
            return SpanSolution(ok=False, reason=part.reason, detail=part.detail)
        return SpanSolution(ok=True, parts=(part,),
                            detail={"parts": 1, "timeline_us": timeline_us})

    frame = frame_us(req.fps)
    caps: list[int] = []
    mins: list[int] = []
    for index, seg in enumerate(segs):
        lo, hi = seg.lo_bound_us, seg.hi_bound_us
        if hi <= lo:
            return SpanSolution(ok=False, reason=REASON_SOURCE_TOO_SHORT, detail={
                "scope": "segment", "segment_index": index, "shot_id": seg.shot_id,
                "lo_bound_us": lo, "hi_bound_us": hi,
                "message": "该镜头扣除保护区/转场把手后没有可用素材"})
        span = seg.evidence_span_us
        if span is not None and (int(seg.evidence_start_us) < lo
                                or int(seg.evidence_end_us) > hi):
            return SpanSolution(ok=False, reason=REASON_EVIDENCE_OUT_OF_RANGE, detail={
                "scope": "segment", "segment_index": index, "shot_id": seg.shot_id,
                "lo_bound_us": lo, "hi_bound_us": hi,
                "evidence_start_us": int(seg.evidence_start_us),
                "evidence_end_us": int(seg.evidence_end_us)})
        speed_min = min(float(seg.speed_min), float(seg.speed_max))
        speed_max = max(float(seg.speed_min), float(seg.speed_max))
        # 该段在**输出时间线**上最多能盖多长：素材消耗 = 输出时长 × 速度，
        # 所以最慢速度给出最大覆盖（慢放把同样的素材摊得更长）。
        caps.append(int(math.floor(seg.usable_us / speed_min)) if speed_min > 0 else 0)
        need = int(MIN_SPAN_PART_US)
        if span is not None:
            need = max(need, int(math.ceil(span / speed_max)) + frame if speed_max > 0 else need)
        mins.append(max(0, min(need, caps[-1])))

    cap_total = sum(caps)
    if cap_total < timeline_us:
        return SpanSolution(ok=False, reason=REASON_SOURCE_TOO_SHORT, detail={
            "scope": "span", "segments": len(segs),
            "timeline_us": timeline_us, "capacity_us": cap_total, "shortfall_us": timeline_us - cap_total,
            "per_segment": [{"shot_id": s.shot_id, "usable_us": c, "speed_min": s.speed_min}
                            for s, c in zip(segs, caps)],
            "message": "全部候选镜头在各自允许的最慢速度下加起来也铺不满这句配音"})
    if sum(mins) > timeline_us:
        return SpanSolution(ok=False, reason=REASON_SOURCE_TOO_SHORT, detail={
            "scope": "span", "segments": len(segs), "timeline_us": timeline_us,
            "minimum_us": sum(mins), "min_part_us": int(MIN_SPAN_PART_US),
            "message": "每段至少要有 0.3s（带证据的段还要罩住证据），这句话太短，"
                       "拆给这么多镜头会变成闪帧"})

    alloc = list(mins)
    rooms = [max(0, c - m) for c, m in zip(caps, mins)]
    total_room = sum(rooms)
    remaining = timeline_us - sum(mins)
    if remaining > 0 and total_room > 0:
        for i, room in enumerate(rooms):
            alloc[i] += int(remaining * room / total_room)
    # 取整残差补给余量最大的一段：各段之和必须**恰好**等于 timeline_us。
    residual = timeline_us - sum(alloc)
    order = sorted(range(len(alloc)), key=lambda i: -rooms[i])
    guard = 0
    while residual > 0 and guard < len(alloc) * 8:
        moved = False
        for i in order:
            if residual <= 0:
                break
            if alloc[i] < caps[i]:
                alloc[i] += 1
                residual -= 1
                moved = True
        if not moved:
            break
        guard += 1
    if sum(alloc) != timeline_us:
        return SpanSolution(ok=False, reason=REASON_SPEED_OUT_OF_RANGE, detail={
            "scope": "span", "timeline_us": timeline_us, "allocated_us": sum(alloc),
            "message": "分配取整后无法精确铺满时间线（边界退化），如实报出而不是硬塞一个解"})

    parts: list = []
    pad = max(0, int(req.tail_pad_us))
    for index, (seg, span_us) in enumerate(zip(segs, alloc)):
        last = index == len(segs) - 1
        part_pad = pad if last else 0
        part_audio = int(span_us) - part_pad
        if part_audio <= 0:
            return SpanSolution(ok=False, reason=REASON_SOURCE_TOO_SHORT, detail={
                "scope": "segment", "segment_index": index, "shot_id": seg.shot_id,
                "assigned_us": int(span_us), "tail_pad_us": part_pad,
                "message": "分给该段的时长还不够放尾留白"})
        part = solve_window(seg.request(audio_duration_us=part_audio,
                                        tail_pad_us=part_pad, fps=req.fps))
        if not part.ok:
            return SpanSolution(ok=False, reason=part.reason, detail={
                **part.detail, "scope": "segment", "segment_index": index,
                "shot_id": seg.shot_id, "assigned_us": int(span_us)})
        if int(part.segment_us) != int(span_us):
            # 求解器给的段长与分配不符 —— 这是求解器自身的问题，不能就此放过：
            # 放过的话各段之和就不等于音频+尾留白，写入前必然 DURATION_MISMATCH。
            return SpanSolution(ok=False, reason=REASON_SPEED_OUT_OF_RANGE, detail={
                "scope": "segment", "segment_index": index, "shot_id": seg.shot_id,
                "assigned_us": int(span_us), "solved_us": int(part.segment_us)})
        parts.append(part)
    return SpanSolution(ok=True, parts=tuple(parts), detail={
        "parts": len(parts), "timeline_us": timeline_us,
        "allocated_us": [int(a) for a in alloc],
        "capacity_us": [int(c) for c in caps]})


# ---- 显式不变量检查（用户定案第 8 条：不用 Python assert 代替防线）----
INV_WINDOW_OUT_OF_BOUNDS = "WINDOW_OUT_OF_BOUNDS"
INV_DURATION_MISMATCH = "DURATION_MISMATCH"
INV_EVIDENCE_NOT_COVERED = "EVIDENCE_NOT_COVERED"
INV_SPEED_OUT_OF_RANGE = "SPEED_OUT_OF_RANGE"
INV_APERTURE_MISMATCH = "APERTURE_MISMATCH"

INVARIANT_CODES = (INV_WINDOW_OUT_OF_BOUNDS, INV_DURATION_MISMATCH,
                   INV_EVIDENCE_NOT_COVERED, INV_SPEED_OUT_OF_RANGE,
                   INV_APERTURE_MISMATCH)


# ---- 逐句配音时长真值表（规划层与引擎共用同一份）----------------------------
#
# 用户定案第 3 条：timing_contract 统一真实音频时长与视频窗口求解口径。
# 所以「一句口播到底多长」只在这里算一次：规划层选镜之前先跑 TTS（成本已被
# `voice_tts._cache_name` 的增量缓存吸收，引擎后续 synthesize 命中同一缓存、
# 不额外花钱），ffprobe 出真实时长落盘；规划层读这份真值选镜，引擎再读同一份
# 对齐音画。两边读的是同一串字节，不再各估各的。
#
# 合成失败时**显式降级**：`timing_certainty="estimated"`，时长按字符数估，
# 并把它一路带进成片报告。绝不在正常路径上悄悄用估算值冒充真值。

TIMING_CERTAINTY_AUDIO = "audio"          # 真值：最终音频 ffprobe
TIMING_CERTAINTY_ESTIMATED = "estimated"  # 显式降级：TTS 失败，按字符数估
TIMING_CERTAINTY_SEGMENT = "segment"      # 段内已有音频参数，先于本次合成

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+")


def split_sentences(manifest: Any) -> list[dict[str, Any]]:
    """全流水线**唯一**的切句口径（规划层 `_sentences` 与 pre-TTS 共用）。

    两边各切一次必然会出现「规划层按 A 切、TTS 按 B 切」的错位，于是真值表
    的键对不上段落、估出来的时长套到了别的句子上。所以切句只留这一份实现。
    """
    m = manifest if isinstance(manifest, dict) else {}
    if m.get("segments"):
        return [dict(s) for s in m.get("segments", [])
                if isinstance(s, dict)
                and str(s.get("text", s.get("caption", ""))).strip()]
    text = str(m.get("full_script") or m.get("script") or "")
    return [{"text": part.strip()} for part in _SENTENCE_SPLIT_RE.split(text)
            if part.strip()]


def sentence_key(item: Any, index: int) -> str:
    """一句口播在真值表里的键：段内已有 key 优先，否则用 `s<序号>`。"""
    if isinstance(item, dict):
        declared = str(item.get("key") or "").strip()
        if declared:
            return declared
    return f"s{int(index)}"


def sentence_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text", item.get("caption", "")) or "").strip()
    return str(item or "").strip()


def estimated_duration_us(text: str,
                          chars_per_second: float = FALLBACK_CHARS_PER_SECOND) -> int:
    """字符数估算时长（**降级专用**）。"""
    chars = len(re.sub(r"\s+", "", str(text or "")))
    if not chars:
        return 0
    cps = float(chars_per_second) or FALLBACK_CHARS_PER_SECOND
    return int(round(chars / cps * US))


def narration_timing(items: Any, *, ffprobe: Optional[Callable[[str], Any]] = None,
                     manifest: Any = None) -> dict[str, Any]:
    """把「句子 / 音频路径」列表解析成逐句时长真值表。

    ``items`` 每项：``{"key"?, "text", "tts_path"?}``。有 ``tts_path`` 且
    ``ffprobe`` 可读出正时长 → ``timing_certainty="audio"``；否则按字符数估算
    并标 ``"estimated"``。**估算只在读不到真值时才出现**，不会伪装成真值。
    """
    rows: list[dict[str, Any]] = []
    for i, it in enumerate(items or [], 1):
        key = sentence_key(it, i)
        text = sentence_text(it)
        path = ""
        if isinstance(it, dict):
            path = str(it.get("tts_path") or it.get("audio") or "").strip()
        duration_us = 0
        certainty = TIMING_CERTAINTY_ESTIMATED
        error = ""
        if path and ffprobe is not None:
            try:
                result = ffprobe(path)
                raw = result.get("duration") if isinstance(result, dict) else result
                if raw is not None and float(raw) > 0:
                    duration_us = int(round(float(raw) * US))
                    certainty = TIMING_CERTAINTY_AUDIO
                else:
                    error = "ffprobe 未返回正时长"
            except Exception as exc:  # noqa: BLE001 —— 探测失败只降级，不阻断
                error = f"{type(exc).__name__}: {exc}"
        elif not path:
            error = "该句没有音频路径"
        if not duration_us:
            declared = it.get("audio_duration_us") if isinstance(it, dict) else None
            if declared is not None and int(declared) > 0:
                duration_us = int(declared)
                certainty = str(it.get("timing_certainty") or TIMING_CERTAINTY_SEGMENT)
            else:
                duration_us = estimated_duration_us(text)
                certainty = TIMING_CERTAINTY_ESTIMATED
        rows.append({
            "key": key, "index": i, "text": text, "tts_path": path,
            "audio_duration_us": int(duration_us),
            "timing_certainty": certainty,
            "estimated": certainty != TIMING_CERTAINTY_AUDIO,
            "probe_error": error,
        })
    by_key = {row["key"]: row for row in rows}
    return {
        "version": AUDIO_PIPELINE_VERSION,
        "fps": DEFAULT_FPS,
        "rows": rows,
        "by_key": by_key,
        "estimated_count": sum(1 for r in rows if r["estimated"]),
        "audio_count": sum(1 for r in rows if r["timing_certainty"] == TIMING_CERTAINTY_AUDIO),
        "chars_per_second_fallback": FALLBACK_CHARS_PER_SECOND,
        "note": ("时长真值来自最终音频 ffprobe；timing_certainty=estimated 的句子是"
                 "TTS 不可用时的显式降级，必须随段落带进成片报告。"),
    }


def load_narration_timing(path: Any) -> dict[str, Any]:
    """读回真值表；缺失/损坏返回空表（引擎按段落自带字段降级，不炸）。"""
    try:
        data = json.loads(open(str(path), "r", encoding="utf-8-sig").read())
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    rows = data.get("rows")
    if not isinstance(rows, list):
        return {}
    data["by_key"] = {str(r.get("key")): r for r in rows if isinstance(r, dict)}
    return data


def duration_for(timing: Any, index: int, key: str = "",
                 segment: Any = None) -> tuple[int, str]:
    """取第 ``index`` 句（1 起）的 ``(时长µs, certainty)``。

    优先级：真值表 → 段落自带 ``audio_duration_us`` → 字符数估算。
    返回的是**显式**来源，调用方必须把它带到报告里，不允许吞掉。
    """
    tbl = timing if isinstance(timing, dict) else {}
    row = (tbl.get("by_key") or {}).get(str(key)) if key else None
    if row is None:
        rows = tbl.get("rows") if isinstance(tbl.get("rows"), list) else []
        row = rows[index - 1] if 0 <= index - 1 < len(rows) else None
    if isinstance(row, dict) and int(row.get("audio_duration_us") or 0) > 0:
        return int(row["audio_duration_us"]), str(row.get("timing_certainty")
                                                  or TIMING_CERTAINTY_AUDIO)
    seg = segment if isinstance(segment, dict) else {}
    declared = seg.get("audio_duration_us")
    if declared is not None and int(declared or 0) > 0:
        return int(declared), str(seg.get("timing_certainty") or TIMING_CERTAINTY_SEGMENT)
    if seg.get("duration") is not None and float(seg.get("duration") or 0) > 0:
        return int(round(float(seg["duration"]) * US)), TIMING_CERTAINTY_SEGMENT
    return estimated_duration_us(sentence_text(seg)), TIMING_CERTAINTY_ESTIMATED


def check_invariants(items: Iterable[dict], *, fps: float = DEFAULT_FPS,
                     tol_frames: int = 1) -> list[dict]:
    """逐段校验时序不变量，返回违规清单（空 = 全部通过）。

    ``items`` 每项支持的键：``index`` / ``video`` / ``audio_mode`` /
    ``source_start_us`` / ``source_end_us`` / ``segment_us`` / ``audio_duration_us`` /
    ``tail_pad_us`` / ``video_duration_us`` / ``material_duration_us``（1.3.27.1：
    素材总长硬边界，零容差）/ ``video_speed`` / ``speed_range`` /
    ``evidence_start_us`` / ``evidence_end_us`` /
    ``actual_video_us``（草稿写入后回读才有）。

    这是**防线不是断言**：``native`` 段由现场原声精确锚定，时长不参与
    「音频+tail_pad」的等式校验，故整体豁免 ``DURATION_MISMATCH``。
    """
    tol_us = max(0, int(tol_frames)) * frame_us(fps)
    issues: list[dict] = []

    def add(code: str, item: dict, message: str, expected=None, actual=None):
        delta = None
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            delta = int(actual) - int(expected)
        issues.append({
            "code": code, "index": item.get("index"), "video": item.get("video"),
            "audio_mode": item.get("audio_mode"),
            "expected_us": expected, "actual_us": actual, "delta_us": delta,
            "delta_frames": (round(delta / frame_us(fps), 2) if delta is not None else None),
            "message": message,
        })

    for item in items:
        index = item.get("index")
        video = item.get("video")
        mode = str(item.get("audio_mode") or "tts")
        start = int(item.get("source_start_us") or 0)
        end = int(item.get("source_end_us") or 0)
        segment = int(item.get("segment_us") or 0)
        speed = float(item.get("video_speed") or 1.0)

        # ── 源窗口几何链（1.3.27.1 用户修正第 3 条）─────────────────────
        # 必须成立：0 <= source_start_us < source_end_us <= material_duration_us。
        # material 边界**零容差**：写入层 `_add_video_safe` 对
        # source_start + 素材时长 > 物理时长是零容差 raise，这里若容 1 帧就会把
        # 6ms 级的越界放进草稿，最后一步照样炸（1 帧容差只属于下面的换算等式）。
        if start < 0:
            add(INV_WINDOW_OUT_OF_BOUNDS, item,
                f"{video}：入点 {start/US:.3f}s 为负（须 0 <= source_start_us）",
                expected=0, actual=start)
        if end <= start:
            add(INV_WINDOW_OUT_OF_BOUNDS, item,
                f"{video}：源窗口为空或倒挂（start={start}µs, end={end}µs）",
                expected="0 <= source_start_us < source_end_us",
                actual=f"start={start}, end={end}")
        material_duration = item.get("material_duration_us")
        if material_duration is not None:
            material_us = int(material_duration)
            if end > material_us:
                add(INV_WINDOW_OUT_OF_BOUNDS, item,
                    f"{video}：出点 {end/US:.3f}s 超出素材总长 "
                    f"{material_us/US:.3f}s（material_duration_us，与写入层同一把尺，"
                    f"零容差）",
                    expected=material_us, actual=end)
        else:
            # 旧形态条目（只带 video_duration_us）保持原容差语义兜底；
            # 新产物一律带 material_duration_us，走上面的零容差硬边界。
            video_duration = item.get("video_duration_us")
            if video_duration is not None and end > int(video_duration) + tol_us:
                add(INV_WINDOW_OUT_OF_BOUNDS, item,
                    f"{video}：出点 {end/US:.3f}s 超出素材时长 "
                    f"{int(video_duration)/US:.3f}s",
                    expected=int(video_duration), actual=end)
        # ── 源窗口 ⇄ 时间线段长的速度换算（用户修正第 3 条）──────────────
        # 按当前速度语义：segment_us ≈ (source_end_us - source_start_us) / video_speed，
        # 容差 1 帧。剪映写入层用 round(source/speed) 回算段长，这条不闭合 =
        # 音画错位；极短素材（窗口不足一帧）连一个可用切片都成不了，直接拦。
        window_us = end - start
        if window_us > 0:
            if window_us < frame_us(fps):
                add(INV_WINDOW_OUT_OF_BOUNDS, item,
                    f"{video}：源窗口 {window_us}µs 不足一帧（{frame_us(fps)}µs），"
                    f"无法成为可用切片", expected=frame_us(fps), actual=window_us)
            elif segment > 0 and speed > 0:
                timeline_us = window_us / speed
                if abs(timeline_us - segment) > tol_us:
                    add(INV_DURATION_MISMATCH, item,
                        f"{video}：源窗口 {window_us/US:.3f}s ÷ 变速 {speed:.3f} ≈ "
                        f"{timeline_us/US:.3f}s，与段时长 {segment/US:.3f}s 相差 "
                        f"{abs(timeline_us-segment)/US:.3f}s（容差 {tol_frames} 帧）",
                        expected=segment, actual=int(round(timeline_us)))

        if mode != "native":
            expected_segment = (int(item.get("audio_duration_us") or 0)
                                + int(item.get("tail_pad_us") or 0))
            if abs(segment - expected_segment) > tol_us:
                add(INV_DURATION_MISMATCH, item,
                    f"{video}：段时长 {segment/US:.3f}s 与「音频+尾部留白」"
                    f"{expected_segment/US:.3f}s 相差 "
                    f"{abs(segment-expected_segment)/US:.3f}s（容差 {tol_frames} 帧）",
                    expected=expected_segment, actual=segment)

        ev_start = item.get("evidence_start_us")
        ev_end = item.get("evidence_end_us")
        if ev_start is not None and ev_end is not None and int(ev_end) > int(ev_start):
            if start > int(ev_start) + tol_us or end < int(ev_end) - tol_us:
                add(INV_EVIDENCE_NOT_COVERED, item,
                    f"{video}：切片 [{start/US:.3f},{end/US:.3f}]s 未覆盖证据区间 "
                    f"[{int(ev_start)/US:.3f},{int(ev_end)/US:.3f}]s",
                    expected=int(ev_end) - int(ev_start), actual=end - start)

        allowed = item.get("speed_range")
        if allowed:
            lo, hi = float(allowed[0]), float(allowed[1])
            if speed < lo - 1e-6 or speed > hi + 1e-6:
                issues.append({
                    "code": INV_SPEED_OUT_OF_RANGE, "index": index, "video": video,
                    "audio_mode": mode, "expected_us": None, "actual_us": None,
                    "delta_us": None, "delta_frames": None,
                    "message": f"{video}：变速 {speed:.3f} 超出该画面角色允许范围 "
                               f"[{lo:.3f},{hi:.3f}]",
                })

        actual_video = item.get("actual_video_us")
        if actual_video is not None and abs(int(actual_video) - segment) > tol_us:
            add(INV_APERTURE_MISMATCH, item,
                f"{video}：草稿回读时长 {int(actual_video)/US:.3f}s 与请求片段 "
                f"{segment/US:.3f}s 相差 {abs(int(actual_video)-segment)/US:.3f}s",
                expected=segment, actual=int(actual_video))

    return issues
