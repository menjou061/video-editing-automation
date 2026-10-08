"""豆包剪映智能编排引擎（跨平台、模块化）。

把素材分析、口播门禁、稳定切片、TTS 选声并发、音画严格对齐、自然转场、
BGM 铺底、草稿安全写入串成一条确定性流水线。所有时间内部统一为整数微秒。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import (audio_loudness, bgm_selector, capacity, draft_safety, script_polish,
               semantic_gate, stability, timing_contract, voice_tts, visual_match)
from .platform_env import (bootstrap_path, environment_report, find_drafts_root,
                           find_ffmpeg, find_jianying_editor_root)
from .resource_profile import apply_process_priority, choose_profile
from .voice_catalog import recommend_voices, resolve_voice
from .timing_contract import frame_speed

US = 1_000_000
TAIL_PAD = 0.12  # 每句口播尾部留白秒数，避免贴边
MAX_SUBTITLE_CHARS = 15  # 用户定案：单个分镜字幕（含标点）不得超过 15 字

# 探测缓存世代戳（v1.3.27.1）：换探针口径时 +1，老缓存重新探测。
PROBE_KIND = "material_us_v1"

# 环境闸的版本下限。这是**地板不是等值**：只有出现真正不兼容的破坏性变更时才上调。
# 1.3.14–1.3.22 期间这里被写成等值比较（`!= "v1.3.13"`），于是每次版本升级都会让
# 环境自检失败——本想强制升级的机制反而把最新版全拦在了门外，且因为 build 会先跑
# 文档匹配、blocker 提前返回，这道错闸整整藏了九个版本没被发现。
MIN_SUPPORTED_PACKAGE_VERSION = "v1.3.13"


def _version_key(value) -> tuple:
    """把 v1.3.22 / 1.3.22 归一成可比较的 (1, 3, 22)；无法解析返回 ()。"""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    return tuple(int(x) for x in m.groups()) if m else ()


def to_us(seconds: float) -> int:
    return int(round(float(seconds) * US))


# v1.3.25：兜底段的成片可见标注。文案刻意短、位置刻意放画面上方（剪映里
# 字幕默认在底部 transform_y=-0.8，这里取正向），避免和字幕叠在一起。
DEGRADED_MARK_TEXT = "【素材待补】"
DEGRADED_MARK_TRACK = "Degraded_Marks"
DEGRADED_MARK_FONT_SIZE = 6.0
DEGRADED_MARK_COLOR = (1.0, 0.85, 0.0)
DEGRADED_MARK_Y = 700.0

# v1.3.27 B 步（用户定案第 10 条）：本批闸门未认证时，成片必须**看得见**。
#
# 背景：7A 规则包（fail-closed 语义 + 悬挂抽 CLAIMS）尚未落地，本批所有镜头的
# role/has_subject/action/setup_id 都缺字段，闸门实际处于「未认证」状态（见
# `demo_actions.gate_coverage` → `certification="UNCERTIFIED"`）。这份状态此前
# 只写在 shot_match_report 里，成片上看不出来 —— 于是「以为质检过了」这件事
# 无法从交付物本身证伪。这里照 `DEGRADED_MARK_*` 的做法，在画面上打一条常驻
# 标注，位置放在**画面下方**且不叫 Subtitles，与底部字幕、上方「素材待补」都错开。
#
# 注意：这是**状态告知**，不是降级标记 —— 每一段都会带上它，因为未认证说的是
# 整批，不是某一句。规则包发布、`certification` 变成已认证后，标注自动消失
# （判据在 `_uncertified_coverage()`，不写死）。
UNCERTIFIED_MARK_TEXT = "【本批未认证】"
UNCERTIFIED_MARK_TRACK = "Uncertified_Marks"
UNCERTIFIED_MARK_FONT_SIZE = 6.0
UNCERTIFIED_MARK_COLOR = (1.0, 0.35, 0.35)
UNCERTIFIED_MARK_Y = -520.0

# 通用画面亮度规则（2026-09-17 用户定案：「保持明亮自然，不能出现过暗或过爆，
# 这些都是通用的生成剪辑处理规则」）。
#
# 口径来源：`DefaultAdjustBundle/brightness_v2` 的着色器是
#     p = 1 + param*5 ;  y = 1 - (1 - x)^p
# 该式**端点保持**（x=0→0、x=1→1），所以它只会抬中间调，结构上不可能把已经
# 到顶的高光推过 1.0 —— 这正好对上「不能过爆」的硬要求。对比 `curves`/
# `contrast` 类算子会拉伸端点，用过爆换取对比度，不合用。
#
# 值取 0.08：对 x=0.5 的中间调，p=1.4 → y=1-0.5^1.4≈0.621（+12%），
# 而 x=0.95 的高光只到 y≈0.977（+2.8%）。即「提亮一档」落在中间调上，
# 高光几乎不动。R3 实测成片 YAVG=144.6，历史已验收稿 144.7 —— 两者本就一致，
# 用户的主观「偏暗」不能靠加对比修（YMIN 已 0、YMAX 已 247~255），只能抬中间调。
BRIGHTNESS_KEYFRAME_VALUE = 0.08
BRIGHTNESS_MIN = -1.0
BRIGHTNESS_MAX = 1.0


@dataclass
class Clip:
    video: str
    source_start: float
    duration: float
    text: str = ""
    claim_text: str = ""
    temporary_shot_id: Optional[str] = None
    audio: Optional[str] = None
    audio_mode: str = "tts"          # tts / native / mute / file
    voice: Any = None
    visual_requirements: Any = None
    evidence_tags: Any = None
    visual_tags: Any = None
    evidence_intervals: Any = None
    rationale: str = ""
    action_complete: Optional[bool] = None
    head_waste: float = 0.0
    tail_waste: float = 0.0
    transition: Optional[dict] = None
    stability: Optional[dict] = None
    tts_path: Optional[str] = None
    start_us: int = 0
    # v1.3.25：该段是「素材库没有能证明这句口播的镜头」时的兜底采用。
    # 成片里必须打可见标注，不允许它冒充正常镜头（用户定案 2026-09-16）。
    degraded: bool = False
    # 预览阶段严格匹配无可用画面时的显式黑片段；只占画面轨，旁白/字幕仍正常写入。
    visual_missing: bool = False
    visual_missing_note: str = ""
    # ── 时序契约（v1.3.27，用户 2026-09-16 定案第 3~6 条）──────────────────
    # 规划阶段（shot_analyzer）已用**真实音频时长**解出可行窗口并写进段落；
    # 引擎只执行这个解、只校验这个解，**不再自己另算一套长度**。
    source_end: float = 0.0            # 段落声明的源窗口右界（秒）
    audio_duration_us: int = 0         # 规划时用的真实音频时长（µs）
    timing_certainty: str = ""         # audio / estimated / segment
    video_speed: float = 1.0           # 求解给出的变速（1.0 = 原速）
    window_solution: Any = None        # 规划阶段的完整解（审计用）
    # 引擎对齐时**自己解出来的**解（`align_audio_video` 用真实音频时长重解一次，
    # 定案第 3 条口径统一）。写入前的不变量校验以此为基准；用吸附后的浮点值反推
    # 等于自证，发现不了求解器本身给错解的情况。
    solved_window: Any = None
    window_reason: str = ""            # 求解失败时的结构化原因码
    # 画面角色 + 是否承载时间相关的直接证据：决定变速范围（定案第 6 条）。
    visual_role: str = ""
    time_sensitive: bool = False
    # 转场把手（定案第 5 条：与首尾废料保护区**分开**扣）。转场会吃掉紧邻切点的
    # 若干帧，这段素材不能被「装配音」用掉。0 表示该侧没有转场，不扣。
    transition_in_handle_us: int = 0
    transition_out_handle_us: int = 0
    # ── 多镜段的展开记账（v1.3.27 B 步）───────────────────────────────────
    # 摊平后每个子段要知道自己「属于哪条原段落、是第几段」，以及**旁白是谁的、
    # 该取哪一段时间片** —— 旁白只有一份，不能每段都从头播一遍。
    parent_index: int = -1     # 所属原段落下标（子段为其父，单镜段为自己）
    span_index: int = 0        # 在原段落里的第几段（单镜恒为 0）
    span_total: int = 1        # 原段落被拆成几段
    audio_clip_index: int = -1  # 旁白归属：哪条原段落的音频（决定 TTS 缓存归属）
    # 父段落的旁白铺在**输出时间线**上的起点（µs），由 ``_mark_audio_origin``
    # 在 ``align_audio_video`` 推进游标时，按父段落**首次出现**建立。
    # 各子段要取的那一片，在配音里的偏移 = 「本段 `start_us`」− 本字段 ——
    # 本字段是**基准**，偏移是老式相减的结果，两者不要搞反。
    # 用「本段起点 − 父段起点」而不是「各兄弟段长累加」，是为了不与游标推进方式
    # 耦合 —— 正确解上两者恒等，但耦合会在游标算法一改就静默错位。
    # 单镜段恒等于自己的 `start_us`（偏移 0，整句从头播）。
    audio_clip_start_us: int = 0
    track_name: str = "Video_BRoll"
    # 运行期
    video_duration: float = 0.0
    # `stabilize` 选出的稳定入点（**提示**，不是边界）。
    #
    # v1.3.27（用户定案第 3 条「统一口径」）：这个值只能当 `solve_window` 的
    # `preferred_start_us` 用。v1.3.24~26 是直接覆写 `c.source_start`，等于把
    # 稳定性结果变成了**素材区间下界** —— 而规划层（`shot_analyzer.candidate_window`）
    # 用的是镜头自己的 `source_start`。两个区间一旦不同，就会出现两种翻案：
    # 稳定入点后移 → 规划批准的解在引擎侧变成 SOURCE_TOO_SHORT（规划说行、交付才炸）；
    # 前移 → 引擎求出规划层判死的解（静默换解）。所以 `source_start` 保持规划值不动，
    # 稳定入点只做提示，由求解器在**同一个可行集**里挑落点。
    stable_start: Optional[float] = None
    # 对齐时 ffprobe 出的**真实音频时长**（µs）。写入前不变量校验必须拿它做
    # 「段长 = 音频 + 尾留白」的比对基准；拿 c.duration 自己比自己等于没校验。
    runtime_audio_us: int = 0
    # ── 多镜组合（v1.3.27 B 步，用户定案第 6 条）──────────────────────────
    # 一句话装不下一个镜头时，规划层（`shot_analyzer.plan_span`）把**同一句配音**
    # 按时间切成 N 段，每段配一个镜头。规划层的原始分段表在**段落字典**的
    # `span_parts` 键里（那是 `raw`，不是 Clip），摊平后**不搬到 Clip 上**：
    # 每段 Clip 自己就带着本段的解（`solved_window` / `window_solution`），
    # 旁白偏移由 `audio_clip_start_us` 相减得出。曾经在这里留过
    # `span_parts` / `span_audio_offsets_us` 两个字段，但从来没有任何生产者
    # 写它们 —— 恒为 None / 0 的字段比没有更糟：读的人会以为拿到的是数据。
    #
    # 口径等价性（这是多镜不必放宽任何阈值的原因）：组合**不引入新的时间口径** ——
    # 只是把同一份音频按时长拆开，再对每段调用**同一个** `solve_window`。
    # 「每段窗口合法」+「各段段长之和恰好等于 audio + tail_pad」两条合起来，
    # 与单镜那条不变量完全等价。所以 `check_invariants` 的每条阈值都不用动，
    # 只要把多镜展开成**每段一条** item 去逐条校验。
    #
    # 单镜路径下 `multi_shot=False`，行为与 v1.3.26 一致。
    multi_shot: bool = False


class OrchestrationEngine:
    def __init__(self, manifest: dict, base_dir: Path, report_dir: Path, *,
                 editor_root: Optional[Path] = None, strict_script: bool = False,
                 logger=None, capacity_guard: bool = True):
        self.m = manifest
        self.base_dir = base_dir
        self.report_dir = report_dir
        self.editor_root = editor_root or find_jianying_editor_root()
        self.strict_script = strict_script
        self.logger = logger
        self.capacity_guard = capacity_guard
        self.profile = choose_profile(manifest)
        self.priority_applied = apply_process_priority(self.profile)
        self._resource_degraded = False
        self._probe_cache: dict[str, dict] = {}
        self._probe_cache_lock = threading.Lock()
        self._probe_inflight: dict[str, threading.Event] = {}
        self._probe_disk_path = self.report_dir / ".cache" / "media_probe.json"
        self._load_probe_cache()
        self.ffmpeg, self.ffprobe = find_ffmpeg()
        bootstrap_path(self.ffmpeg.parent if self.ffmpeg else None)
        self.warnings: list[dict] = []
        self.pending: list[dict] = []
        self.sync_issues: list[dict] = []
        # v1.3.27 B 步：preview 允许带未解决片段出草稿（只打标注、不阻断），
        # formal 不允许静默通过（定案第 9 条）。默认 formal —— 少一次显式
        # 授权就少一次「预览稿被当成交付稿发出去」的机会。
        self.delivery_mode = self._resolve_delivery_mode(manifest)
        # 预览未认证的原因（供结果里如实上报，不静默）
        self.preview_uncertified_reasons: list[str] = []

    @staticmethod
    def _resolve_delivery_mode(manifest: dict) -> str:
        """交付模式：``formal``（默认）或 ``preview``。

        preview 的语义是「带缺口出稿、缺口如实上报」（2026-09-18 用户定案合并
        时序层与证据层两问）：
          · 时序层：未解出的片段照样出草稿（`preview_unresolved`）；
          · 证据层：视觉证据/最终覆盖缺口不再硬阻断，逐条记入
            `preview_uncertified_reasons`（见 `visual_evidence_gate`/
            `final_visual_evidence_gate`）；
          · 选镜层：`shot_analyzer` 保留关键字级别关联镜头直接采用、无关联时
            降级兜底，同样只在 preview 生效。
        放宽的结果一律写进 report/coverage（`formal_delivery_allowed=False`），
        preview 稿**不允许**进正式交付。formal 保持全部严格门禁，不因预览而松开。
        """
        raw = str(manifest.get("delivery_mode") or "").strip().lower()
        return "preview" if raw in {"preview", "preview_only", "预览"} else "formal"

    def _event(self, name: str, **fields) -> None:
        if self.logger:
            self.logger.event(name, **fields)

    def intelligent_mode_authorized(self) -> bool:
        """智能模式只能由用户显式触发，禁止 Agent 根据内容自行升级。"""
        if str(self.m.get("mode", "")).lower() != "intelligent":
            return False
        phrase = str(self.m.get("trigger_phrase", "")).strip()
        return phrase in {"智能编排", "智能生成", "开启智能模式"} or bool(
            self.m.get("mode_user_confirmed") is True)

    def mode_confirmation_gate(self) -> Optional[dict]:
        mode = str(self.m.get("mode", "")).lower()
        if mode == "intelligent" and not self.intelligent_mode_authorized():
            return {
                "status": "MODE_CONFIRMATION_REQUIRED",
                "message": "智能模式只能由用户明确输入“智能编排”“智能生成”或“开启智能模式”触发；Agent 不得自行切换。",
                "guidance": "未收到明确触发时将按普通模式处理；如确需智能模式，请由用户明确发送触发词后重新提交。",
            }
        return None

    def _timed(self, name: str, fn, *args, **kwargs):
        cm = self.logger.phase(name) if self.logger else contextlib.nullcontext()
        with cm:
            return fn(*args, **kwargs)

    def _background_pressure(self, stage: str) -> Optional[dict]:
        """Yield between phases; degrade before stopping under extreme pressure."""
        # Give the interactive editor a scheduling opportunity before another
        # CPU/I/O phase. This is intentionally short and only active when the
        # user has Jianying open or explicitly chose the friendly profile.
        if self.profile.name == "background_friendly":
            time.sleep(0.12)
        sample = capacity.resource_snapshot(self._drafts_root)
        available = int(sample.get("memory_available_bytes") or 0)
        issue = capacity.memory_pressure(self._drafts_root)
        if issue:
            self._event("resource_pause", stage=stage, policy="critical_memory",
                        sample=issue.get("sample", sample))
            return issue
        if self.profile.name == "background_friendly" and available and available < capacity.SOFT_MEMORY_LIMIT:
            if not self._resource_degraded:
                # Keep the task moving, but make every later expensive phase
                # single-flight.  This is reversible for the next task and
                # does not alter output selection or quality rules.
                self.profile = type(self.profile)(
                    self.profile.name, 1, 1, 1, "below_normal")
                self._resource_degraded = True
                self._event("resource_degraded", stage=stage,
                            policy="minimum_concurrency", available_memory_bytes=available,
                            soft_limit_bytes=capacity.SOFT_MEMORY_LIMIT,
                            message="可用内存偏低，已降为最低并发并继续生成。")
            sample["policy"] = "minimum_concurrency"
        if self.profile.name == "background_friendly" or self._resource_degraded:
            self._event("resource_yield", stage=stage, sample=sample,
                        action="continue" if not issue else "pause")
        return None

    # ---------- 基础工具 ----------
    def resolve(self, value: str) -> Path:
        p = Path(str(value)).expanduser()
        return (p if p.is_absolute() else self.base_dir / p).resolve()

    def _load_probe_cache(self) -> None:
        """Load reusable media metadata; entries are invalidated by size/mtime."""
        try:
            payload = json.loads(self._probe_disk_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._probe_cache = payload
        except (OSError, ValueError, TypeError):
            self._probe_cache = {}

    def _persist_probe_cache(self) -> None:
        try:
            self._probe_disk_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._probe_disk_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._probe_cache, ensure_ascii=False,
                                       separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._probe_disk_path)
        except OSError:
            pass

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        st = path.stat()
        return int(st.st_size), int(st.st_mtime_ns)

    def probe(self, path: str | Path) -> dict:
        resolved = Path(path).resolve()
        key = str(resolved)
        try:
            # 缓存键 = absolute_path + file_size + mtime_ns + PROBE_KIND（1.3.27.1
            # 用户修正第 6 条）：文件被替换（大小/mtime 变化）即自动重探；
            # PROBE_KIND 世代戳让旧口径缓存（容器时长）整体失效。
            signature = (key, *self._file_signature(resolved), PROBE_KIND)
        except OSError:
            signature = (key, 0, 0, PROBE_KIND)
        # Reuse both in-memory and cross-task results when the source is unchanged.
        # `probe_kind` 是 1.3.27.1 加的缓存世代戳：老缓存（容器时长口径）签名里
        # 没有它/形态不同，直接重探，绝不把旧的「容器 4.99 / 视频轨 4.984」两把尺混用。
        with self._probe_cache_lock:
            cached = self._probe_cache.get(key)
            if cached and tuple(cached.get("signature", ())) == signature \
                    and cached.get("probe_kind") == PROBE_KIND:
                return cached
            waiter = self._probe_inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                self._probe_inflight[key] = waiter
                owner = True
            else:
                owner = False
        if not owner:
            waiter.wait()
            with self._probe_cache_lock:
                cached = self._probe_cache.get(key)
                if cached and tuple(cached.get("signature", ())) == signature \
                        and cached.get("probe_kind") == PROBE_KIND:
                    return cached
            raise RuntimeError(f"媒体探测失败：{key}")
        try:
            meta = None
            if self.ffprobe:
                try:
                    # 1.3.27.1：素材总长统一走 probe_material_duration_us ——
                    # 优先使用与写入层一致的视频素材时长（pymediainfo/ffprobe
                    # 视频轨、末帧 PTS，HIGH）；只有容器时长时进入保守降级
                    # （减一帧、LOW）。source/confidence 必须随缓存落盘。
                    probe_res = timing_contract.probe_material_duration_us(
                        key, ffprobe=self.ffprobe, ffmpeg=self.ffmpeg)
                    dur_us = int(probe_res.get("duration_us") or 0)
                    if dur_us > 0:
                        meta = {"duration": dur_us / US, "path": key,
                                "signature": list(signature),
                                "probe_kind": PROBE_KIND,
                                "duration_source": str(
                                    probe_res.get("source") or "probe_failed"),
                                "duration_confidence": str(
                                    probe_res.get("confidence") or "LOW")}
                except Exception:
                    pass  # 探测失败则落到 ffmpeg 兜底
            if meta is None:
                meta = self._probe_via_ffmpeg(key)
                meta["signature"] = list(signature)
                meta["probe_kind"] = PROBE_KIND
                meta.setdefault("duration_source", "ffmpeg_container")
                meta.setdefault("duration_confidence", "LOW")
            with self._probe_cache_lock:
                self._probe_cache[key] = meta
            self._persist_probe_cache()
            return meta
        finally:
            with self._probe_cache_lock:
                event = self._probe_inflight.pop(key, None)
                if event:
                    event.set()

    def _probe_via_ffmpeg(self, path: str) -> dict:
        """无 ffprobe 时（如仅复用剪映捆绑的 ffmpeg），用 `ffmpeg -i` 的 stderr 解析时长。"""
        ff = str(self.ffmpeg) if self.ffmpeg else "ffmpeg"
        r = subprocess.run([ff, "-nostdin", "-i", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if not m:
            raise RuntimeError(f"无法解析媒体时长（ffmpeg/ffprobe 均失败）：{path}")
        h, mi, s = m.groups()
        return {"duration": int(h) * 3600 + int(mi) * 60 + float(s), "path": path}

    def _probe_material(self, c: Clip) -> float:
        """素材时长探测的统一入口（片段维度）：除返回时长外，把**低置信度**
        （容器降级）来源如实记账 —— LOW 的值不得参与生成「已认证」的候选窗口
        （1.3.27.1 用户修正第 2 条），认证闸门靠 `preview_uncertified_reasons`
        看见它，正式交付也会因此失去认证资格。
        """
        meta = self.probe(c.video)
        if str(meta.get("duration_confidence") or "").upper() == "LOW" \
                and not getattr(c, "_low_conf_material_flagged", False):
            c._low_conf_material_flagged = True
            name = Path(c.video).name
            self._warn("material_duration_low_confidence",
                       f"{name} 的素材时长只有容器级来源"
                       f"（{meta.get('duration_source')}），已标记低置信度："
                       f"本批不得按「已认证」出稿。", video=name)
            self._event("material_duration_low_confidence",
                        video=name, source=meta.get("duration_source"),
                        message="素材时长为容器降级值（LOW），已整批打未认证标注。")
            self.preview_uncertified_reasons.append(
                f"LOW_CONF_MATERIAL_DURATION@{name}")
        return meta["duration"]

    def _warn(self, kind: str, msg: str, **extra):
        rec = {"type": kind, "message": msg, **extra}
        self.warnings.append(rec)

    # ---------- 环境自检 ----------
    def self_check(self) -> Optional[dict]:
        rep = environment_report()
        problems = []
        if not rep["wrapper_available"]:
            problems.append("未找到 jianying-editor skill（两个 skill 需同级部署，或设 JY_SKILL_ROOT 指向它）")
        if not rep.get("pyjianyingdraft_vendor"):
            problems.append("jianying-editor 缺少 scripts/vendor/pyJianYingDraft（打包时需带上整个 jianying-editor）")
        if not rep["ffmpeg"]:
            problems.append(
                "未找到 ffmpeg。公司禁装时改用免安装方式：把绿色版 ffmpeg.exe/ffprobe.exe 放到本 skill 的 "
                "bin/ 目录，或设 FFMPEG_BIN/FFPROBE_BIN 环境变量，引擎也会自动复用剪映自带 ffmpeg；详见 README_Windows.md")
        missing = rep.get("python_deps", {}).get("missing_required", [])
        if missing:
            problems.append("缺少必需 Python 依赖，请执行：python -m pip install " + " ".join(missing))
        self._dep_hints = rep.get("python_deps", {}).get("missing_recommended", [])
        if self._dep_hints:
            print("[i] 建议安装以获得 TTS 兜底/云曲库能力：python -m pip install "
                  + " ".join(self._dep_hints))
        if not rep["drafts_root_exists"]:
            problems.append(f"剪映草稿目录不存在：{rep['drafts_root']}（请先安装并打开一次剪映让其初始化）")
        else:
            problems.extend(rep.get("drafts_root_access_issues", []))
        package = rep.get("active_package", {})
        pkg_version = package.get("version")
        pkg_key = _version_key(pkg_version)
        if not package.get("active_external_package", False):
            problems.append(
                "当前不是完整的外部交付包或包校验失败，请从标准豆包 Skills 目录重新部署 "
                f"{MIN_SUPPORTED_PACKAGE_VERSION} 或更高版本")
        elif package.get("location") == "private_internal":
            problems.append(
                "当前使用的是随应用内置的 Skill，禁止使用；请部署外部交付包并放入 Skills 标准目录。")
        elif not pkg_key or pkg_key < _version_key(MIN_SUPPORTED_PACKAGE_VERSION):
            problems.append(
                f"当前 Skill 版本 {pkg_version or '未知'} 低于最低支持版本 "
                f"{MIN_SUPPORTED_PACKAGE_VERSION}；请先执行更新，不得回退到内置或旧版 Skill。")
        if problems:
            return {"status": "ENV_ERROR", "problems": problems, "report": rep}
        self.env = rep
        return None

    # ---------- 分镜解析 ----------
    def build_clips(self) -> list[Clip]:
        clips: list[Clip] = []
        raw_segments = self.m.get("segments")
        if raw_segments:
            self._raw_segments = raw_segments
            for i, it in enumerate(raw_segments, 1):
                visual_missing = bool(it.get("visual_missing"))
                if not it.get("video") and not visual_missing:
                    self.pending.append({"index": i, "reason": "missing video"})
                    continue
                mode = str(it.get("audio_mode", "tts")).lower()
                if it.get("audio"):
                    mode = "file"
                video = (self._ensure_visual_missing_placeholder(
                    max(float(it.get("duration", 1.0) or 1.0), 1.0))
                         if visual_missing else self.resolve(it["video"]))
                clip = Clip(
                    video=str(video),
                    source_start=float(it.get("source_start", it.get("in", 0.0))),
                    duration=float(it.get("duration", 0.0)),
                    text=str(it.get("text", it.get("caption", ""))),
                    claim_text=str(it.get("claim_text", "") or ""),
                    temporary_shot_id=it.get("temporary_shot_id"),
                    audio=str(self.resolve(it["audio"])) if it.get("audio") else None,
                    audio_mode=mode, voice=it.get("voice"),
                    visual_requirements=it.get("visual_requirements", it.get("required_visuals")),
                    evidence_tags=it.get("evidence_tags"), visual_tags=it.get("visual_tags"),
                    evidence_intervals=it.get("evidence_intervals"),
                    rationale=str(it.get("rationale", "")),
                    action_complete=it.get("action_complete"),
                    head_waste=float(it.get("head_waste", 0.0) or 0.0),
                    tail_waste=float(it.get("tail_waste", 0.0) or 0.0),
                    degraded=bool(it.get("degraded_no_match")),
                    visual_missing=visual_missing,
                    visual_missing_note=str(it.get("operator_note") or
                                            it.get("visual_missing_reason") or "").strip(),
                    # 规划阶段的时序契约解：引擎只执行、只校验，不再自算长度。
                    source_end=float(it.get("source_end", 0.0) or 0.0),
                    audio_duration_us=int(it.get("audio_duration_us", 0) or 0),
                    timing_certainty=str(it.get("timing_certainty", "") or ""),
                    video_speed=float(it.get("video_speed", 1.0) or 1.0),
                    window_solution=it.get("window_solution"),
                    window_reason=str(it.get("window_reason", "") or ""),
                    visual_role=str(it.get("visual_role", "") or "").strip(),
                    time_sensitive=bool(it.get("time_sensitive")),
                )
                clips.append(clip)
        else:  # sequence 模式：每个视频一段，音频/字幕按下标对齐
            self._raw_segments = []
            videos = [str(self.resolve(v)) for v in self.m.get("videos", [])]
            audios = [str(self.resolve(a)) for a in self.m.get("audios", [])]
            caps = self.m.get("captions", [])
            for i, v in enumerate(videos):
                a = audios[i] if i < len(audios) else None
                clips.append(Clip(
                    video=v, source_start=0.0, duration=0.0,
                    text=caps[i] if i < len(caps) else "",
                    audio=a, audio_mode="file" if a else "tts"))
                self._raw_segments.append({"video": v, "text": clips[-1].text})
        self._autofill_script_voiceover(clips)
        return self._expand_spans(clips)

    def _ensure_visual_missing_placeholder(self, requested_s: float) -> Path:
        """Create a real black video so missing visuals do not drop the timeline slot."""
        requested_s = max(1.0, float(requested_s or 1.0))
        duration_s = max(30.0, requested_s + 5.0)
        path = self.report_dir / "visual_missing_placeholder.mp4"
        cached = float(getattr(self, "_visual_missing_placeholder_s", 0.0) or 0.0)
        if path.is_file() and cached + 0.02 >= duration_s:
            return path
        self.report_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg = str(self.ffmpeg) if self.ffmpeg else "ffmpeg"
        # Jianying's bundled ffmpeg is not guaranteed to include libx264.
        # Prefer the Windows MediaFoundation H.264 encoder, then fall back to
        # the built-in MPEG-4 encoder; a missing optional encoder must not turn
        # an otherwise valid audio/subtitle-only segment into a task failure.
        encoder_attempts = [
            (["-c:v", "h264_mf", "-pix_fmt", "yuv420p"], "h264_mf"),
            (["-c:v", "mpeg4", "-q:v", "5"], "mpeg4"),
        ]
        encoder_errors: list[str] = []
        generated = None
        for codec_args, codec_name in encoder_attempts:
            cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
                   f"color=c=black:s=1080x1920:r={self._fps():g}",
                   "-t", f"{duration_s:.3f}", "-an", *codec_args,
                   "-movflags", "+faststart", str(path)]
            try:
                generated = subprocess.run(
                    cmd, check=True, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180)
                self._event("visual_missing_placeholder_encoder",
                            encoder=codec_name)
                break
            except (OSError, subprocess.SubprocessError) as exc:
                detail = ""
                if isinstance(exc, subprocess.CalledProcessError):
                    detail = (exc.stderr or exc.stdout or "")[-500:]
                encoder_errors.append(f"{codec_name}: {exc} {detail}")
        if generated is None:
            raise RuntimeError(
                "无法生成缺画面黑色视频占位：" + " | ".join(encoder_errors))
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("无法生成缺画面黑色视频占位：输出文件为空")
        self._visual_missing_placeholder_s = duration_s
        self._event("visual_missing_placeholder_created",
                    path=str(path), duration_s=round(duration_s, 3))
        return path

    def _expand_spans(self, clips: list[Clip]) -> list[Clip]:
        """把多镜段摊成**每镜一个 Clip**（定案第 6 条）。

        为什么不是「一个 Clip 带 N 段画面」：引擎下游每一步（构建、求解、
        转场、证据闸、装配、不变量校验）本来就是**按 Clip 逐段线性遍历**的，
        而多镜在时间线上恰恰就是 N 个**先后排列的普通段** —— 每段一条素材、
        一个窗口解、一条证据区间。摊平后：

        · `align_audio_video` 不必为多镜写分支，`solve_window` 也不必放宽阈值
          （口径等价性：各段各自合法 + 各段之和恰等于整句时间线）；
        · 写入前的不变量校验天然变成「每段一条 item」，每条都按**同样的**阈值卡；
        · 字幕、降级标注、BGM 收口全部照常按段走。

        代价（必须显式处理，否则会在错误的地方炸）：
        · 旁白只有一份，N 个 Clip 各分到它的一段时间片，所以每个子段都要带上
          `audio_clip_index` / `audio_slice_us`，`synthesize` 与 `write_draft`
          据此只加**一次**音频、且每段取配音的不同区间；
        · `script_review` / `score_storyboard` / `visual_evidence_gate` 这类
          「按原段落一一对应」的步骤不能看到摊平后的列表，必须拿回原段落，
          故子段上记 `parent_index`（见 `_parent_segments`）。
        """
        expanded: list[Clip] = []
        parents: list[dict] = []
        for parent_index, c in enumerate(clips):
            raw = self._raw_segment_for(c)
            parts = self._span_part_clips(parent_index, c, raw)
            if parts is None:
                c.parent_index = parent_index
                c.audio_clip_index = parent_index
                c.audio_clip_start_us = 0
                expanded.append(c)
                parents.append(raw or {"video": c.video, "text": c.text,
                                       "temporary_shot_id": c.temporary_shot_id})
                continue
            for part_index, p in enumerate(parts):
                sub = self._span_clip(parent_index, part_index, c, p, raw)
                sub.audio_clip_index = parent_index
                expanded.append(sub)
            parents.append(raw or {"video": c.video, "text": c.text,
                                   "temporary_shot_id": c.temporary_shot_id})
            self._event("span_assembled", index=parent_index + 1, parts=len(parts),
                        timeline_us=sum(to_us(p.duration) for p in parts),
                        slots=[{"video": Path(p.video).name,
                                "source_start_us": to_us(p.source_start),
                                "source_end_us": to_us(p.source_end),
                                "speed": p.video_speed, "segment_us": to_us(p.duration)}
                               for p in parts])
        self._span_parents = parents
        return expanded

    def _parent_segments(self) -> list[dict]:
        """原段落表（未摊平）—— 供「按段落一一对应」的步骤使用。

        `visual_evidence_gate` / `script_review` / `score_storyboard` 的输入
        契约是「一条原段落一个条目」。多镜摊平后 Clips 变成 N 段，直接把摊平
        列表喂给它们会错位（而且它们会去读不存在的 evidence_intervals）。
        """
        parents = getattr(self, "_span_parents", None)
        return list(parents) if parents else list(self._raw_segments)

    def _parent_clips(self, clips: list[Clip]) -> list[Clip]:
        """摊平列表 → **每个原段落取第一个子段**（按原段落顺序）。

        与 ``_parent_segments`` 同一个契约的 Clip 版本：`script_review` 要的是
        「这段旁白说了什么」（多镜的 N 个子段共享同一句文本，逐子段取会让同一句
        被数 N 次），`score_storyboard` 要的是「这个原段落值不值得留」（它还会写
        `clip.duration`，对子段写等于事后改分配解）。两者都只能看原段落。
        """
        out: list[Clip] = []
        seen: set[int] = set()
        for index, c in enumerate(clips):
            pid = int(getattr(c, "parent_index", -1) or -1)
            if pid < 0:
                pid = index
            if pid in seen:
                continue
            seen.add(pid)
            out.append(c)
        return out

    def _script_segment_length_gate(self, clips: list[Clip]) -> Optional[dict]:
        """阻止超长口播进入 TTS/写稿，避免一个语义段撑成长镜头。"""
        offenders = []
        for index, clip in enumerate(self._parent_clips(clips), 1):
            text = re.sub(r"\s+", "", str(getattr(clip, "text", "") or ""))
            if text and len(text) > MAX_SUBTITLE_CHARS:
                offenders.append({
                    "index": index,
                    "text": text,
                    "chars": len(text),
                    "max_chars": MAX_SUBTITLE_CHARS,
                    "reason": "完整语义段超过字幕硬上限，需按标点/语义拆分",
                })
        if not offenders:
            return None
        self._event("script_segment_length_blocked",
                    max_chars=MAX_SUBTITLE_CHARS, offenders=offenders)
        return {
            "status": "SCRIPT_SEGMENT_TOO_LONG",
            "message": f"存在 {len(offenders)} 个分镜超过 {MAX_SUBTITLE_CHARS} 字硬上限，未开始上传素材或生成配音。",
            "max_chars": MAX_SUBTITLE_CHARS,
            "segments": offenders,
            "guidance": "保留原文，按完整语义/标点拆成多个 segments；不要缩写、改写或把长文案压在一个镜头里。",
        }

    def _visual_duplicate_gate(self, clips: list[Clip]) -> Optional[dict]:
        """阻止同一草稿复用同一视觉签名或同源同构图。"""
        parents = self._parent_clips(clips)
        records = []

        def tokens(value: Any) -> set[str]:
            return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}",
                                  str(value or "").lower()))

        def role_group(role: str, text: str) -> str:
            value = f"{role} {text}".lower()
            groups = {
                "packaging_quantity": ("包装", "多包", "囤", "福利", "促销", "品牌", "packag", "promotion", "quantity", "stocking", "cta"),
                "absorption": ("吸收", "瞬吸", "液体", "absorb"),
                "breathability_structure": ("透气", "底膜", "闷热", "breath", "film"),
                "surface_handfeel": ("柔软", "面层", "纯棉", "surface", "soft", "cotton"),
                "dryness_result": ("干爽", "dry"),
            }
            for group, keys in groups.items():
                if any(key in value for key in keys):
                    return group
            return "generic"

        for index, clip in enumerate(parents):
            raw = self._raw_segment_for(clip)
            if not raw and index < len(getattr(self, "_raw_segments", []) or []):
                candidate = self._raw_segments[index]
                raw = candidate if isinstance(candidate, dict) else {}
            role = str(raw.get("visual_role") or clip.visual_role or "")
            visual_tokens = " ".join(str(raw.get(key) or "") for key in (
                "visual_description", "visual_requirements", "visual_tags",
                "evidence_tags", "text", "claim_text"))
            records.append({
                "index": index + 1,
                "source": Path(str(raw.get("video") or clip.video or "")).name.casefold(),
                "source_start": float(raw.get("source_start", clip.source_start) or 0.0),
                "duration": float(raw.get("duration", clip.duration) or 0.0),
                "signature": str(raw.get("visual_signature") or "").strip().casefold(),
                "group": role_group(role, ""),
                "tokens": tokens(visual_tokens),
                "text": str(raw.get("text") or clip.text or "").strip(),
            })

        duplicates = []
        for current_index, current in enumerate(records):
            # Exact audited signatures are unique across the full draft; a
            # third copy must not bypass the gate by sitting farther away.
            for previous_index in range(0, current_index):
                previous = records[previous_index]
                same_signature = bool(current["signature"] and current["signature"] == previous["signature"])
                distinct_audited_signatures = bool(
                    current["signature"] and previous["signature"] and
                    current["signature"] != previous["signature"])
                same_source_window = bool(
                    current["source"] and current["source"] == previous["source"] and
                    abs(current["source_start"] - previous["source_start"]) < 1e-6 and
                    abs(current["duration"] - previous["duration"]) < 1e-6)
                same_source_group = bool(current["source"] and current["source"] == previous["source"] and current["group"] == previous["group"])
                token_overlap = len(current["tokens"] & previous["tokens"]) >= 2
                if not (same_signature or same_source_window or
                        (same_source_group and token_overlap and not distinct_audited_signatures)):
                    continue
                duplicates.append({
                    "current_index": current["index"], "previous_index": previous["index"],
                    "current_text": current["text"], "previous_text": previous["text"],
                    "current_source": current["source"], "previous_source": previous["source"],
                    "visual_group": current["group"],
                    "reason": "同一草稿复用同一视觉签名或同源同构图",
                })
                break
        if not duplicates:
            return None
        report = {
            "status": "DUPLICATE_VISUAL_SHOT",
            "message": "检测到同一草稿复用同一视觉画面，已阻止写入草稿。",
            "duplicates": duplicates,
            "guidance": "重新从候选池选择不同动作/构图；只换源时间窗或把重复镜头隔开不算修复。",
        }
        self.pending.extend({"type": "DUPLICATE_VISUAL_SHOT", **item} for item in duplicates)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.report_dir / "visual_duplicate_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._event("visual_duplicate_shot_blocked", duplicates=duplicates,
                    report=str(self.report_dir / "visual_duplicate_report.json"))
        return report

    def _span_part_clips(self, parent_index: int, c: Clip, raw: dict):
        """段落里的多镜解 → N 个子 Clip（单镜 / 无解时返回 None）。

        **每个子段的窗口解必须来自它自己那一段**（`span_parts[i]`），不能拿整句
        的解套给所有子段：那样第二、三段会按第一段的素材边界切片，画面直接切到
        别的镜头上去了。规划阶段的 `_commit_span` 已经把「逐段取回该段的行与解」
        做完，这里只做装配。
        """
        if not raw.get("multi_shot"):
            return None
        parts = raw.get("span_parts") or []
        if len(parts) < 2:
            return None
        out: list[Clip] = []
        for part in parts:
            if not isinstance(part, dict):
                return None
            video = part.get("video") or raw.get("video")
            if not video:
                return None
            role = str(part.get("visual_role") or raw.get("visual_role") or "").strip()
            # `part` **本身就是** `WindowSolution.to_dict()` 的结果（规划层
            # `plan_span` 把 `p.to_dict()` 摊进 `span["parts"]`），键是
            # `ok/speed/source_start/source_end/segment_s/material_s/handle_room_s`。
            # 所以「求解器的原解」就在手边，不必再去找一个叫 `window_solution`
            # 的子键 —— 那个键在这一层**根本不存在**（曾经这么读过，恒取到 None，
            # 于是多镜子段的 `solved_window` 一直是空的）。
            solution = dict(part)
            # 段长同样来自求解器的解。**键名是 `segment_s` 而不是 `duration`**：
            # `part` 是 `WindowSolution.to_dict()`，它只吐
            # `ok/speed/source_start/source_end/segment_s/material_s/handle_room_s`。
            # 这里曾经读 `part["duration"]` —— 恒取到 0，于是每个子段段长为 0：
            # 游标推不动、所有子段叠在同一个起点、旁白轨全是零长片段。写入前的
            # 不变量校验也发现不了（它按 `segment_us` 比对，0 也是「一个数」），
            # 是典型「没报错但全错」。两个键都认，`duration` 留给将来显式传秒的调用方。
            segment_s = part.get("segment_s")
            if segment_s is None:
                segment_s = part.get("duration") or 0.0
            sub = Clip(
                video=str(self.resolve(str(video))),
                source_start=float(part.get("source_start") or 0.0),
                duration=float(segment_s or 0.0),
                text=str(raw.get("text", "") or ""),
                claim_text=str(raw.get("claim_text", "") or ""),
                temporary_shot_id=part.get("shot_id") or raw.get("temporary_shot_id"),
                audio_mode=str(raw.get("audio_mode", "tts")).lower(),
                evidence_intervals=part.get("evidence_intervals") or raw.get("evidence_intervals"),
                rationale=str(raw.get("rationale", "") or ""),
                action_complete=raw.get("action_complete"),
                degraded=bool(raw.get("degraded_no_match")),
                source_end=float(part.get("source_end") or 0.0),
                audio_duration_us=int(raw.get("audio_duration_us", 0) or 0),
                timing_certainty=str(raw.get("timing_certainty", "") or ""),
                video_speed=float(part.get("video_speed", part.get("speed", 1.0)) or 1.0),
                window_solution=solution,
                # 规划层给的解就是本段的**源出点基准**。写入前的不变量校验
                # （`_invariant_items`）与装配闸门（`_align_span_clip`）都优先读
                # 它，拿不到才按「入点 + 段长 × 速度」反推。反推是用**吸附后的
                # 速度**算的：一旦吸附把速度挪出原解，反推值会跟着偏，而校验就
                # 建在被校验对象自己的推导上（定案第 8 条要消灭的正是这种自证）。
                solved_window=solution,
                window_reason="",
                visual_role=role,
                time_sensitive=bool(raw.get("time_sensitive")),
            )
            if sub.audio_mode == "file":
                sub.audio = c.audio
            out.append(sub)
        return out

    def _span_clip(self, parent_index: int, part_index: int, parent: Clip,
                   part: Clip, raw: dict) -> Clip:
        """给多镜的某个子段补上「它属于谁、在哪个位置槽位」的记账字段。"""
        part.parent_index = parent_index
        part.span_index = part_index
        part.span_total = len(raw.get("span_parts") or [])
        # 摊平出来的子段自己也要带 `multi_shot=True`：下游判断「兄弟缝」靠它
        # （`_same_span`），`plan_transitions` 据此不给段落内部塞转场 —— 那是一句
        # 连续的话被拆成几段画面，中间插转场等于让这句话卡一下。
        part.multi_shot = True
        part.voice = parent.voice
        part.track_name = parent.track_name
        return part

    def _autofill_script_voiceover(self, clips: list[Clip]) -> None:
        """识别整稿是否可直接口播，并只填充完全缺失的 TTS 文案。"""
        full_script = str(self.m.get("full_script", "") or self.m.get("script", ""))
        assessment = script_polish.assess_voiceover_eligibility(full_script)
        self.script_assessment = assessment
        if not full_script:
            return
        self._event("script_voiceover_assessed", **assessment)
        targets = [c for c in clips if c.audio_mode == "tts" and not c.text.strip()]
        explicit_text = any(c.audio_mode == "tts" and c.text.strip() for c in clips)
        if explicit_text or not targets:
            return
        if assessment.get("usable"):
            chunks = script_polish.distribute_voiceover(assessment["text"], len(targets))
            for clip, chunk in zip(targets, chunks):
                clip.text = chunk
            self._event("script_voiceover_autofilled", source=assessment["status"],
                        clips=len(chunks), chars=sum(len(x) for x in chunks))
        else:
            self._warn("script_not_direct_voiceover", assessment.get("reason", "输入脚本不能直接作为口播"),
                       assessment=assessment)

    # ---------- 需求4：口播门禁 ----------
    def voiceover_gate(self, clips: list[Clip]) -> Optional[dict]:
        has_voice = any((c.audio_mode in ("tts",) and c.text) or
                        c.audio_mode == "native" or c.audio_mode == "file"
                        for c in clips)
        if has_voice or self.m.get("allow_silent"):
            return None
        return {
            "status": "NEED_VOICEOVER",
            "message": "脚本没有任何口播内容（无 TTS 文案、无现场原声、无配音文件）。",
            "guidance": [
                "请确认视频口播内容：1) 提供整稿/逐分镜文案，由 TTS 配音；",
                "2) 若分镜本身带现场口播，把对应段 audio_mode 设为 native 保留原声；",
                "3) 若确为纯 BGM/字幕无旁白片，在 manifest 设 allow_silent=true。",
            ],
            "script_assessment": getattr(self, "script_assessment", None),
            "segments": [{"video": Path(c.video).name, "text": c.text} for c in clips],
        }

    def audio_selection_gate(self, clips: list[Clip]) -> Optional[dict]:
        """Pause normal-mode builds until missing voice/BGM choices are confirmed.

        Intelligent mode is intentionally one-click. Normal mode must expose
        choices before doing TTS, media staging, or draft creation.
        """
        if self.intelligent_mode_authorized():
            return None
        missing = []
        voice_spec = os.environ.get("JY_OPERATOR_VOICE", "").strip() or self.m.get("voice")
        if voice_spec is None or (isinstance(voice_spec, str) and not voice_spec.strip()):
            missing.append("voice")
        if not self.m.get("bgm"):
            missing.append("bgm")
        confirmed = self.m.get("audio_selection_confirmed") is True
        if not confirmed and not missing:
            missing = ["voice", "bgm"]
        if confirmed and not missing:
            return None
        full = str(self.m.get("full_script", "") or self.m.get("script", ""))
        text = full + " " + " ".join(c.text for c in clips)
        voices = recommend_voices(text, topk=4)
        voice_auditions = []
        try:
            audition_dir = self.report_dir / "audio_selection" / "voice_auditions"
            voice_auditions = voice_tts.generate_audition(
                script=text, out_dir=audition_dir, topn=min(4, len(voices)))
            voice_auditions = [a for a in voice_auditions
                               if a.get("preview_file") or a.get("file")]
            for idx, audition in enumerate(voice_auditions, 1):
                audition.setdefault("preview_file", audition.get("file", ""))
                audition["order"] = idx
        except Exception as exc:  #试听失败不应绕过确认门禁
            self._warn("voice_audition_unavailable", str(exc))
        # Normal-mode confirmation must be actionable: only expose tracks with
        # a direct preview/download URL. Unplayable library entries are hidden.
        bgms = [b for b in bgm_selector.recommend_bgm(text, topn=10)
                if b.get("url")][:5]
        # Prepare only the small candidate set, concurrently. This keeps the
        # selection step responsive while avoiding a full-library download.
        bgm_auditions = []
        bgm_candidate_count = len(bgms)
        if bgms:
            try:
                audition_dir = self.report_dir / "audio_selection" / "bgm_auditions"
                prepared = bgm_selector.prepare_preview_tracks(
                    bgms, audition_dir,
                    ffmpeg=str(self.ffmpeg) if self.ffmpeg else "ffmpeg",
                    seconds=8.0, workers=min(4, len(bgms)))
                # Only return files that can be played immediately. A URL by
                # itself is not enough: download/ffmpeg failures must stop the
                # selection gate instead of exposing an unplayable candidate.
                bgm_auditions = [a for a in prepared
                                 if a.get("preview_ready") and a.get("preview_file")]
                bgms = bgm_auditions
                for idx, audition in enumerate(bgm_auditions, 1):
                    audition["order"] = idx
            except Exception as exc:  # 试听失败不应绕过确认门禁
                self._warn("bgm_audition_unavailable", str(exc))
                bgms = []
        if "bgm" in missing and not bgm_auditions:
            self._event("bgm_preview_unavailable", candidate_count=bgm_candidate_count)
            return {
                "status": "BGM_PREVIEW_UNAVAILABLE",
                "message": "候选 BGM 无法生成可播放试听文件，未继续生成草稿。",
                "task_log_hint": "请检查 BGM 直链/网络，或在 manifest 中指定可访问的本地 bgm.path。",
            }
        self._event("audio_selection_required", missing=missing,
                    confirmation_required=not confirmed,
                    voice_candidates=voices, bgm_candidates=bgms)
        reply_parts = []
        if "voice" in missing:
            reply_parts.append("音色 <序号>")
        if "bgm" in missing:
            reply_parts.append("BGM <序号>")
        return {
            "status": "AUDIO_SELECTION_REQUIRED",
            "missing": missing,
            "voice_candidates": voices,
            "voice_auditions": voice_auditions,
            "bgm_candidates": bgms,
            "bgm_preview_available": bool(bgms),
            "bgm_auditions": bgm_auditions,
            "reply_format": "，".join(reply_parts),
        }

    # ---------- 补2：文案衔接校验 ----------
    def script_review(self, clips: list[Clip]) -> dict:
        full = str(self.m.get("full_script", ""))
        # 多镜摊平后同一句文本会重复出现在 N 个子段上，逐子段收集等于把这句话
        # 数 N 遍 —— 文案衔接校验会看到「同一句出现多次」的假问题。这里按原段落取。
        texts = [c.text for c in self._parent_clips(clips)
                 if c.audio_mode in ("tts", "native", "file")]
        review = script_polish.review_voiceover_script(full, texts)
        if not review["ok"]:
            for iss in review["issues"]:
                self._warn("script_" + iss.get("type", "issue"), iss["message"],
                           suggestion=iss.get("suggestion"))
            if self.strict_script:
                raise RuntimeError("口播文案校验未通过（strict）：" +
                                   json.dumps(review["issues"], ensure_ascii=False))
        return review

    def visual_evidence_gate(self, clips: list[Clip]) -> Optional[dict]:
        """Require a traceable source-frame match for every spoken product claim."""
        raw = getattr(self, "_raw_segments", [])
        if not raw:
            # Keep the runtime Clip metadata when callers invoke the gate
            # directly (and when a sequence-mode adapter has already built
            # enriched clips).  Dropping these fields would turn a valid
            # storyboard into a false missing-evidence blocker.
            raw = [{
                "video": c.video,
                    "text": c.text,
                "claim_text": c.claim_text,
                "visual_requirements": c.visual_requirements,
                "evidence_tags": c.evidence_tags,
                "visual_tags": c.visual_tags,
                "evidence_intervals": c.evidence_intervals,
                "rationale": c.rationale,
                "action_complete": c.action_complete,
                "head_waste": c.head_waste,
                "tail_waste": c.tail_waste,
            } for c in clips]
        else:
            # ``_autofill_script_voiceover`` may populate clip text after the
            # original raw manifest was captured.  Overlay runtime values so
            # a full-script task cannot bypass the evidence gate with blanks.
            merged: list[dict] = []
            for index, clip in enumerate(self._parent_clips(clips)):
                item = dict(raw[index]) if index < len(raw) and isinstance(raw[index], dict) else {}
                item.setdefault("video", clip.video)
                if not str(item.get("text", item.get("caption", "")) or "").strip():
                    item["text"] = clip.text
                if not str(item.get("claim_text", "") or "").strip() and clip.claim_text:
                    item["claim_text"] = clip.claim_text
                if item.get("action_complete") is None and clip.action_complete is not None:
                    item["action_complete"] = clip.action_complete
                if not item.get("evidence_intervals") and clip.evidence_intervals:
                    item["evidence_intervals"] = clip.evidence_intervals
                merged.append(item)
            raw = merged
        require_explicit = bool(self.m.get("visual_evidence_required", True))
        report = semantic_gate.evaluate_segments(raw, require_explicit=require_explicit)
        # Persist both passing and blocked reports.  A passing task must leave
        # an auditable claim-to-shot record for NAS/Feishu review; blocked tasks
        # keep the same artifact so operators can fix the exact pending items.
        source_items = []
        for item in raw:
            item = dict(item)
            if item.get("video"):
                item["video"] = str(self.resolve(str(item["video"])))
            source_items.append(item)
        source = visual_match.validate_source_intervals(
            source_items, probe=lambda path: self.probe(path), report_dir=self.report_dir,
            ffmpeg=str(self.ffmpeg) if self.ffmpeg else None,
            require_intervals=require_explicit)
        self._visual_source_records = source.get("records", [])
        report = visual_match.write_report(
            self.report_dir / "visual_evidence_report.json",
            semantic=report,
            source=source,
            final={"ok": None, "issues": [], "coverage": [],
                   "message": "等待稳定切片和配音定长后的最终窗口复核"})
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.report_dir / "visual_evidence_report.json"
        report["report_path"] = str(report_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._visual_evidence_report = report
        self._event("visual_evidence_checked", ok=report["ok"],
                    issue_count=report["issue_count"], coverage=report["coverage"],
                    report=str(report_path))
        if report.get("semantic", {}).get("ok") and report.get("source", {}).get("ok"):
            return None
        issues = list(report["pending_items"])
        self.pending.extend(issues)
        if self.delivery_mode == "preview":
            # preview（2026-09-18 用户定案）：证据缺口不阻断出稿 —— 逐条汇总成
            # 未认证理由进 `preview_uncertified_reasons`（report/coverage 如实上报，
            # 草稿画面保持干净，缺口只靠审计可见）。formal 仍走下方硬阻断。
            known = set(self.preview_uncertified_reasons)
            for item in issues:
                video = str(item.get("video") or "").replace("\\", "/").rsplit("/", 1)[-1]
                text = str(item.get("text") or item.get("claim_text") or "").strip()[:36]
                miss = "；".join(str(m) for m in (item.get("missing") or [])) \
                    or str(item.get("reason") or item.get("type") or "缺口")
                line = f"VISUAL_EVIDENCE@{video}: 剧本「{text}」缺画面证据[{miss}]"
                if line not in known:
                    self.preview_uncertified_reasons.append(line)
                    known.add(line)
            self._event("visual_evidence_preview_tolerated",
                        delivery_mode=self.delivery_mode, issue_count=len(issues),
                        message="preview：视觉证据缺口已计入未认证理由，继续出稿。")
            return None
        return {
            "status": "VISUAL_EVIDENCE_REQUIRED",
            "message": "口播卖点缺少对应画面证据或镜头动作未完成，已阻止写入草稿。",
            "pending_items": issues,
            "coverage": report["coverage"],
            "report": str(self.report_dir / "visual_evidence_report.json"),
        }

    def final_visual_evidence_gate(self, clips: list[Clip]) -> Optional[dict]:
        """Recheck source evidence after stability and audio-driven trimming."""
        report = getattr(self, "_visual_evidence_report", {}) or {}
        semantic = report.get("semantic", {})
        source = report.get("source", {})
        final_items = [{
            "video": c.video,
            "source_start": c.source_start,
            "duration": c.duration,
            "temporary_shot_id": c.temporary_shot_id,
            "visual_missing": bool(c.visual_missing),
            "operator_note": c.visual_missing_note,
        } for c in clips]
        final = visual_match.validate_final_windows(
            final_items, source.get("records", getattr(self, "_visual_source_records", [])))
        report = visual_match.write_report(
            self.report_dir / "visual_evidence_report.json",
            semantic=semantic, source=source, final=final)
        report["report_path"] = str(self.report_dir / "visual_evidence_report.json")
        (self.report_dir / "visual_evidence_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._visual_evidence_report = report
        self._event("visual_evidence_final_checked", ok=report["ok"],
                    issue_count=report["issue_count"], final_coverage=final.get("coverage", []),
                    report=str(self.report_dir / "visual_evidence_report.json"))
        if report["ok"]:
            return None
        issues = list(final.get("issues", []))
        self.pending.extend(issues)
        if self.delivery_mode == "preview":
            # preview（2026-09-18 用户定案）：稳定切片未全覆盖证据同样放行出稿，
            # 逐条记入未认证理由，不阻断。formal 仍走下方硬阻断。
            known = set(self.preview_uncertified_reasons)
            for idx, issue in enumerate(issues):
                video = str(issue.get("video") or "").replace("\\", "/").rsplit("/", 1)[-1]
                reason = str(issue.get("reason") or issue.get("type")
                             or f"该段最终切片未覆盖卖点证据 (index {issue.get('index', idx)})")
                line = f"FINAL_VISUAL_EVIDENCE@{video}: {reason[:90]}"
                if line not in known:
                    self.preview_uncertified_reasons.append(line)
                    known.add(line)
            self._event("visual_evidence_final_preview_tolerated",
                        delivery_mode=self.delivery_mode, issue_count=len(issues),
                        message="preview：最终证据缺口已计入未认证理由，继续出稿。")
            return None
        return {
            "status": "VISUAL_EVIDENCE_REQUIRED",
            "message": "最终稳定切片未覆盖全部口播卖点证据，已阻止写入草稿。",
            "pending_items": issues,
            "report": str(self.report_dir / "visual_evidence_report.json"),
        }

    # ---------- 需求2：音色选择 ----------
    DEFAULT_VOICE_KEY = "male_energetic"   # sami=zh_male_huoli，与剪映/底层 TTS 默认一致

    def decide_voice(self):
        """音色决策。

        - 不指定(None)/"default"：直接用剪映默认音色，不做额外推荐与挑选；
        - {"auto": true}：按剧情自动推荐首选+候选（可二次选择）；
        - 其它（key / sami id / 性别年龄条件）：按指定解析。
        """
        # The Windows poller exports the original Feishu label in
        # JY_OPERATOR_VOICE.  Treat it as the source of truth even if an LLM
        # manifest already contains a guessed fallback speaker id.
        v = os.environ.get("JY_OPERATOR_VOICE", "").strip() or self.m.get("voice")
        intelligent = self.intelligent_mode_authorized()
        if intelligent and v is None:
            full = self.m.get("full_script", "") + " " + " ".join(c.text for c in self._clips)
            rec = recommend_voices(full, topk=4)
            if rec:
                self._event("voice_selected", mode="intelligent", selected=rec[0], candidates=rec)
                return rec[0]["key"], rec
        if v is None or (isinstance(v, str) and v.strip().lower() in ("default", "默认")):
            return self.DEFAULT_VOICE_KEY, None
        if isinstance(v, dict) and v.get("auto"):
            full = self.m.get("full_script", "") + " " + " ".join(c.text for c in self._clips)
            rec = recommend_voices(full, style=str(v.get("style", "")),
                                   persona=str(v.get("persona", "")), topk=4)
            return rec[0]["key"], rec
        # Resolve explicit operator labels before any upload/TTS work.  This
        # prevents a fuzzy fallback from silently selecting another JianYing
        # voice and makes the selected speaker id traceable in the report.
        return resolve_voice(v), None

    # ---------- 需求8：稳定切片 ----------
    def stabilize(self, clips: list[Clip]):
        if not self.m.get("stabilize", True):
            return
        def analyze(c: Clip) -> tuple[Clip, object]:
            c.video_duration = self._probe_material(c)
            if c.visual_missing:
                return c, None
            want = c.duration if c.duration > 0 else 2.0
            # native 真人段入点由现场原声台词精确锚定，稳定性让位于音画对齐，不自动漂移
            span = 0.12 if c.audio_mode == "native" else 0.8
            w = stability.find_stable_window(
                c.video, want, requested_start=c.source_start,
                video_duration=c.video_duration, search_span=span,
                ffmpeg=str(self.ffmpeg) if self.ffmpeg else None,
                head_trim=0.0 if c.audio_mode == "native" else float(
                    self.m.get("head_trim_s", 0.3)),
                tail_trim=0.0 if c.audio_mode == "native" else float(
                    self.m.get("tail_trim_s", 0.2)))
            return c, w

        with ThreadPoolExecutor(max_workers=self.profile.analysis_workers) as ex:
            analyzed = list(ex.map(analyze, clips))
        for c, w in analyzed:
            if c.visual_missing:
                c.stable_start = 0.0
                c.stability = {
                    "status": "VISUAL_MISSING",
                    "reason": c.visual_missing_note or "没有可审计画面",
                }
                self._event("visual_missing_stability_skipped",
                            video=Path(c.video).name,
                            note=c.visual_missing_note)
                continue
            original_start = c.source_start
            # 稳定入点只做**提示**，不覆写 `source_start`（定案第 3 条：统一口径）。
            # 覆写会让引擎侧的可用素材区间与规划层选镜时用的区间不一致，
            # 于是「规划说有解、交付说无解」或者反过来静默换解。
            # 落点由 `align_audio_video` 的求解器在同一个可行集里挑。
            c.stable_start = w.source_start
            trim_head = 0.0 if c.audio_mode == "native" else float(self.m.get("head_trim_s", 0.3))
            trim_tail = 0.0 if c.audio_mode == "native" else float(self.m.get("tail_trim_s", 0.2))
            self._event("shot_window_cleaned", video=Path(c.video).name,
                        requested_start=original_start, stable_start=w.source_start,
                        duration=w.duration, head_trim_s=trim_head, tail_trim_s=trim_tail,
                        head_waste=c.head_waste, tail_waste=c.tail_waste,
                        reason=w.reason)
            if c.head_waste > 0.0 or c.tail_waste > 0.0:
                self._warn("shot_window_cleaned", f"{Path(c.video).name}: 已应用首尾废料保护区",
                           head_waste=c.head_waste, tail_waste=c.tail_waste)
            c.stability = w.to_dict()
            if "微晃" in w.reason:
                self._warn("shaky_shot", f"{Path(c.video).name}: {w.reason}")
        self._event("stability_completed", clips=len(clips), workers=self.profile.analysis_workers)

    def score_storyboard(self, clips: list[Clip]) -> None:
        """Score storyboard understanding without another model call.

        Low confidence is handled conservatively: clips carrying narration are
        never shortened automatically; silent clips may be capped to a short
        window so an uncertain shot cannot dominate the edit.
        """
        scores = []
        previous = None
        # 打分与「静音段封顶」都只针对**原段落**：多镜摊平后每个子段的 `duration`
        # 是求解器分配解的一部分，逐子段重打一遍分（同一句文本被算 N 次）只是脏数据，
        # 而一旦对某个 mute 子段执行 `silent_duration_cap`，等于事后改掉分配解 ——
        # 段长与旁白片长当场对不上。多镜段一律不封顶。
        for index, clip in enumerate(self._parent_clips(clips)):
            duration = clip.duration or min(max(clip.video_duration, 0.0), 2.0)
            score = script_polish.score_storyboard_shot(
                clip.text, duration_s=duration, video_name=Path(clip.video).name,
                previous=previous)
            score["index"] = index
            score["video"] = Path(clip.video).name
            if score["score"] < 0.5:
                if (clip.audio_mode == "mute" and not clip.text
                        and not getattr(clip, "multi_shot", False)):
                    old_duration = clip.duration
                    clip.duration = min(old_duration or 2.0, 1.5)
                    score["adjustment_applied"] = "silent_duration_cap"
                    score["duration_before"] = old_duration or 2.0
                    score["duration_after"] = clip.duration
                else:
                    score["adjustment_applied"] = "warning_only_audio_preserved"
                self._warn("storyboard_low_confidence",
                           f"{Path(clip.video).name}: 分镜理解置信度较低",
                           score=score)
            else:
                score["adjustment_applied"] = "none"
            self._event("storyboard_scored", **score)
            scores.append(score)
            previous = score.get("fields") or {}
        self._storyboard_scores = scores

    # ---------- 需求5+2：TTS 并发 ----------
    def synthesize(self, clips: list[Clip], global_voice):
        groups: dict[str, dict] = {}
        seen_parents: set[int] = set()
        # A voice supplied by the Feishu task is an operator decision.  Keep
        # it locked for every narrated segment so an LLM-produced per-shot
        # ``voice`` field cannot make the formal draft differ from audition.
        operator_voice = os.environ.get("JY_OPERATOR_VOICE", "").strip() or self.m.get("voice")
        # 多镜摊平后同一句旁白挂在 N 个子段上。这里按**父段落**建组、按父段落合成
        # 一次，再把结果回写给它的每个子段 —— 逐子段各跑一次虽然会命中 TTS 缓存
        # 不重复花钱，但会把一条配音数成 N 条（报告与日志都会脏），而且一旦缓存
        # 策略变化就会真的重复计费。旁白**只有一份**这件事必须在建组时就体现。
        for n, c in enumerate(clips):
            if c.audio_mode != "tts" or not c.text:
                continue
            pid = int(getattr(c, "parent_index", -1) or -1)
            if pid >= 0:
                if pid in seen_parents:
                    continue  # 同一句旁白已经建过一次组
                seen_parents.add(pid)
            key = f"k{n}"
            voice = global_voice if operator_voice else (c.voice if c.voice is not None else global_voice)
            group_key = json.dumps(voice, ensure_ascii=False, sort_keys=True, default=str)
            groups.setdefault(group_key, {"voice": voice, "items": [], "clips": {}})
            groups[group_key]["items"].append({"key": key, "text": c.text})
            targets = [c] if pid < 0 else [
                other for other in clips
                if int(getattr(other, "parent_index", -1) or -1) == pid
                and other.audio_mode == "tts" and other.text]
            groups[group_key]["clips"][key] = targets
        if not groups:
            return None
        tts_dir = self.report_dir / "tts"
        presets = []
        for group in groups.values():
            preset, paths = voice_tts.generate_batch(
                group["items"], tts_dir, voice_spec=group["voice"],
                concurrency=min(int(self.m.get("tts_concurrency", self.profile.tts_concurrency)),
                               self.profile.tts_concurrency))
            presets.append(preset)
            for key, c in group["clips"].items():
                # 同一条旁白回写给**它的每个子段**：多镜段的对齐、切片、字幕都
                # 按子段走，见不到 `tts_path` 就会被当成无声段。
                for target in (c if isinstance(c, list) else [c]):
                    target.tts_path = paths[key]
        self._event("tts_completed", groups=len(groups), clips=sum(len(g["items"]) for g in groups.values()))
        return presets[0] if presets else None

    def _evidence_span(self, clip: Clip) -> Optional[tuple[float, float]]:
        """证据区间并集跨度；没有可用区间时返回 None。"""
        spans: list[tuple[float, float]] = []
        for interval in clip.evidence_intervals or []:
            if not isinstance(interval, dict):
                continue
            start = max(0.0, float(interval.get("start", 0.0) or 0.0))
            end = float(interval.get("end", 0.0) or 0.0)
            if end > start:
                spans.append((start, end))
        if not spans:
            return None
        return min(s for s, _ in spans), max(e for _, e in spans)

    def _window_covering_evidence(self, clip: Clip, target: float,
                                  head_trim: float, tail_trim: float) -> float:
        """【已废弃，无调用者】v1.3.24 的「挪入点罩住证据」启发式。

        v1.3.24（用户定案）：证据区间是「已审核帧邻域」。切片长度由配音决定、不能动，
        能动的只有入点——把入点挪到让证据区间居中的位置，并在素材可用范围内夹紧。
        越界（播到未经分析的画面）由 final_visual_evidence_gate 单独校验。

        v1.3.27 起由 ``timing_contract.solve_window`` 接管：那边把「罩住证据」
        和其他约束（源边界、前后转场把手、速度范围）放进**同一个可行集**一起解，
        而不是先摆窗口再事后校验。这里保留定义只为留痕，**不要在新代码里调用**；
        真正在用的是 ``_evidence_span`` + ``_timing_request``。
        """
        span = self._evidence_span(clip)
        if span is None:
            return clip.source_start
        span_start, span_end = span
        earliest = head_trim
        latest_end = clip.video_duration - tail_trim
        if latest_end - earliest < target:          # 素材太短，无从挪动
            return clip.source_start
        if span_end - span_start >= target:         # 区间比切片还长：至少罩住区间开头
            start = span_start
        else:
            start = span_start - (target - (span_end - span_start)) / 2.0
        return max(earliest, min(start, latest_end - target))

    # ---------- 需求6+补3：音画严格对齐，以声音定长，多余画面舍弃 ----------
    def _timing_request(self, clip: Clip, sound_us: int) -> timing_contract.WindowRequest:
        """把段落 + 真实音频时长组装成一次窗口求解请求（v1.3.27 统一口径）。

        这里和 ``shot_analyzer.candidate_window`` 用**同一个** ``solve_window``：
        规划阶段算的是「这个镜头能不能装下这句话」，引擎这边算的是「用真正确认过的
        音频时长再解一次」。两边口径一致，才不会出现规划说行、交付时炸掉的情况。

        输入一律整数微秒（定案第 3 条），边界取整到帧：
        ``head_guard`` / ``tail_guard`` 是**保护**（素材头尾废料，不能进切片），
        ``transition_*_handle`` 是**转场把手**（转场要吃掉的那几帧），两者分开扣。
        """
        frm = int(self.m.get("fps", 0) or 0) or int(timing_contract.DEFAULT_FPS)
        head_guard = timing_contract.us_of(self.m.get("head_trim_s"), 300_000)
        tail_guard = timing_contract.us_of(self.m.get("tail_trim_s"), 200_000)
        tail_pad = timing_contract.us_of(self.m.get("tail_pad_s"), 120_000)
        span = self._evidence_span(clip)
        speed_min, speed_max = timing_contract.speed_range_for_role(
            self._clip_role(clip),
            time_sensitive=bool(getattr(clip, "time_sensitive", False)))
        # 可用源区间 = **被分析过的场景窗口**，不是整条素材。规划阶段抽帧、AI 打
        # 标签都只覆盖这一段；窗口越过 scene_source_end 会播到没分析过的画面，
        # 会被 final_visual_evidence_gate 拦下。段落没写 source_end 时才退回整条素材。
        source_in = float(clip.source_start)
        scene_end = float(clip.source_end or 0.0)
        source_out = scene_end if scene_end > source_in else float(clip.video_duration)
        # `stabilize` 选的稳定入点只作**提示**（定案第 3 条）：它进 `preferred_start_us`，
        # 求解器在可行集里找不到更好落点时优先落这里；找不到也行，绝不用它当边界。
        # 证据区间在场时求解器以证据覆盖优先，忽略此提示（见 `solve_window`）。
        stable = getattr(clip, "stable_start", None)
        preferred_us = None
        if stable is not None:
            candidate = int(round(float(stable) * US))
            if source_in * US <= candidate <= source_out * US:
                preferred_us = candidate
        return timing_contract.WindowRequest(
            source_in_us=int(round(source_in * US)),
            source_out_us=int(round(source_out * US)),
            audio_duration_us=int(sound_us),
            tail_pad_us=tail_pad, head_guard_us=head_guard, tail_guard_us=tail_guard,
            transition_in_handle_us=int(getattr(clip, "transition_in_handle_us", 0) or 0),
            transition_out_handle_us=int(getattr(clip, "transition_out_handle_us", 0) or 0),
            evidence_start_us=None if span is None else int(round(span[0] * US)),
            evidence_end_us=None if span is None else int(round(span[1] * US)),
            speed_min=speed_min, speed_max=speed_max,
            preferred_start_us=preferred_us,
            video_duration_us=int(round(float(clip.video_duration) * US)), fps=frm)

    def _clip_role(self, clip: Clip) -> str:
        """段落声明的视觉角色（决定变速范围；定案第 6 条）。

        角色在规划阶段（``shot_analyzer``）就随段落写下来了，引擎直接读。
        读到空值时**不猜** —— 返回空串，``speed_range_for_role`` 对未识别角色
        回落到 ``DEFAULT_SPEED_RANGE``（锁 1.0），宁可保守也不越权变速。
        """
        if str(clip.visual_role or "").strip():
            return str(clip.visual_role).strip()
        raw = self._raw_segment_for(clip)
        return str(raw.get("visual_role") or raw.get("role") or "").strip()

    def _raw_segment_for(self, clip: Clip) -> dict:
        for item in (getattr(self, "_raw_segments", None) or []):
            if str(item.get("temporary_shot_id") or "") and \
                    str(item.get("temporary_shot_id")) == str(clip.temporary_shot_id or ""):
                return item
        return {}

    # ---------- 多镜摊平的共享记账（v1.3.27 B 步） ----------
    def _audio_slice_us(self, clip: Clip) -> int:
        """本剪辑取旁白的哪**一片**：相对整句起点的偏移（µs）。

        多镜段摊平后，**同一句旁白**被 N 个子剪辑共用：每个子剪辑盖住输出时间线
        的一段，也就对应配音里的一段。`write_draft` 据此给 `AudioSegment` 写
        `source_timerange(start=偏移, duration=本段输出时长)` —— 偏移写错，第二段
        就会把整句配音从头再播一遍，音画互相打架。

        口径（与求解器的解**逐字对齐**，不另立规则）：

        · 偏移 = **本段 `start_us` − 父段落旁白铺在输出时间线上的起点**
          （`audio_clip_start_us`）。后者由 ``_mark_audio_origin`` 在
          ``align_audio_video`` 里按父段落**首次出现**建立，一次定死，
          兄弟段共用同一个基准。
        · **不用「前面各兄弟段长累加」**：那个和在正确解上与这里的差恒等，但它把
          记账绑死在游标推进方式上 —— 多累加/少累加一段就静默错位，而且不报错。
          这里只做一次减法：基准是一个点，偏移是相对于那个点的位移，与中间经过
          几段无关。
        · 单镜段落/未记账的段落返回 0，等价旧行为（整句从头播）。

        片长**不在这里**：`add_audio_safe` 只按「本段输出时长」取，所以
        「偏移 + 本段输出时长 ≤ 配音时长」这条得有人卡 —— 由 ``_align_span_clip``
        显式挡在写入之前，不指望调用方自觉。
        """
        # v1.3.27.2：**单镜段必须返回 0**，不能拿 `start_us` 当偏移。`multi_shot`
        # 为 False 时 `audio_clip_start_us` 恒为 0（展开时第 542 行写死），直接相减
        # 会把**时间线游标**当配音里的偏移写进 `source_timerange` —— 第 N 段
        # 旁白就从配音的第 N 秒中间开始播，前面的句子被无声吃掉；一旦游标超过
        # 该句配音长度还（如本例 7,468,375 > 1,899,000）直接在写入层炸
        # （video/audio_segment 的 `截取的素材时间范围...超出了素材时长`）。
        # 只有多镜子段（`multi_shot=True`，`_mark_audio_origin` 记过基准）才做减法。
        if not getattr(clip, "multi_shot", False):
            return 0
        return max(0, int(clip.start_us) - int(
            getattr(clip, "audio_clip_start_us", 0) or 0))

    def _tail_pad_us(self) -> int:
        """尾留白（输出时间线尾部留给转场/呼吸的那一小截），与 `_timing_request` 同源。"""
        return timing_contract.us_of(self.m.get("tail_pad_s"),
                                     timing_contract.DEFAULT_TAIL_PAD_US)

    def _mark_audio_origin(self, clip: Clip, cursor: int) -> int:
        """记下「本段旁白在输出时间线上从哪一刻开始」，并返回该起点（µs）。

        这是 ``_audio_slice_us`` 的基准。口径只有一条：**一段旁白铺在它自己那段
        画面的起点上**。所以

        · 单镜段：起点就是这个段自己的 ``start_us``，偏移恒为 0（旧行为）；
        · 多镜子段：起点是**父段落首子段的起点**，兄弟段共用同一基准，于是各自的
          偏移恰好等于「前面各兄弟段的段长之和」——但那不是本函数算的，是从同一个
          基准相减得来的（见 ``_audio_slice_us`` 的口径说明）。

        基准靠**按顺序首次遇到**建立（父段落的首个子段在列表里就在兄弟之前），
        不依赖任何跨段累加，也不要求求解器回传额外字段。返回值让调用方在
        ``clip.start_us`` 被赋值**之前**就能算出偏移，不必依赖「新建 Clip 时
        start_us 默认为 0」这种隐式前提。
        """
        pid = int(getattr(clip, "parent_index", -1) or -1)
        starts = getattr(self, "_span_audio_starts", None)
        if starts is None:
            starts = self._span_audio_starts = {}
        origin = int(starts[pid]) if pid >= 0 and pid in starts else int(cursor)
        clip.audio_clip_start_us = origin
        if pid >= 0:
            starts[pid] = origin
        return origin

    def _align_span_clip(self, clip: Clip, cursor: int, whole_audio_us: int = 0) -> bool:
        """多镜子段的对齐：**段长锚在音频上**，只校验、不重解（定案第 6 条）。

        为什么这里不调 `solve_window`：规划层已经把同一份配音按段解过一次了
        （`shot_analyzer` → `simulate_span`，逐段 `solve_window`），摊平出来的段长
        逐字来自那份分配。引擎再拿**整句**时长解一遍，等于把「按段分配」换成
        「每段都塞整句」——第二段起必然无解；而这里**没有加宽任何约束**
        （定案第 8 条：1.3.27 不放宽阈值），那是真的无解，不是保守。

        对齐要守住的不变量只有两条，且都是**显式检查**（定案第 8 条：不用
        Python assert 当防线）：

        · **段长必须正好等于本段该盖住的那一片旁白**（末段再加规划层留给整句的
          尾留白）。段长长过这一片 → 旁白早播完了画面还在走，空转；短过这一片 →
          下一段上来时旁白已经被切掉一截，句子缺字。`slice_audio_us` 由
          ``cursor − 父段落旁白起点`` 现算，不读任何缓存字段 —— 缓存字段在
          ``clip.start_us`` 赋值前是旧值。
        · 段长跑出素材可用范围 → 写入时 `VideoSegment` 会自己 `raise`，那时代码
          已经写到一半，不如在这里如实报结构化失败。

        ``whole_audio_us`` 是**这句旁白的完整时长**（ffprobe 真值），只在末段用来
        做一次收口：末段要盖到整句的末尾，所以「本段起点 + 段长」必须落在
        「父段落旁白起点 + 整句时长」上（误差 ≤1 帧）。这条是 v1.3.27 定案第 8 条
        要求的显式不变量（原来靠 assert），也是**唯一的全局收口**：逐段判据都只看
        自己那一片，加起来差多少没人管，正是这句话兜住。

        容差取 1 帧（``timing_contract.frame_us``），与 ``check_invariants`` 同一
        口径 —— 整数微秒栅格 + 剪映 round(source/speed) 回程误差都在 1 帧以内，
        放宽反而会把「少了一段画面」这种真错位放过去。
        """
        index = self._span_index_in(clip, self._clips or [])
        total = max(1, int(getattr(clip, "span_total", 1) or 1))
        is_last = index >= total
        pad_us = self._tail_pad_us() if is_last else 0
        # 本段该取配音的哪一片：`cursor` 是本段的起点，减掉父段落旁白的起点（由
        # `_mark_audio_origin` 建立并返回）即得。**必须在这里算**（`clip.start_us`
        # 还没赋值，用 `_audio_slice_us` 会退化成恒 0），并与 `write_draft` 里那次
        # 计算同源 —— 那边是 `start_us − audio_clip_start_us`，同一个基准、同一个减式。
        audio_origin_us = self._mark_audio_origin(clip, cursor)
        slice_audio_us = max(0, int(cursor) - int(audio_origin_us))
        solved = clip.solved_window if isinstance(clip.solved_window, dict) else {}
        source_end = solved.get("source_end")
        material_us = (to_us(float(source_end)) - to_us(clip.source_start)
                       if source_end is not None
                       else to_us(clip.duration * float(clip.video_speed or 1.0)))
        video_us = to_us(clip.video_duration)
        if to_us(clip.source_start) + material_us > video_us + 1000:
            self._timing_issue(clip, timing_contract.REASON_SOURCE_TOO_SHORT,
                               sound_us=to_us(clip.duration),
                               avail_us=max(0, video_us - to_us(clip.source_start)),
                               detail={"scope": "span_part", "span_index": index,
                                       "material_us": material_us,
                                       "source_start_us": to_us(clip.source_start),
                                       "video_duration_us": video_us})
            return False
        # 本段该盖住的那一片旁白：末段是「整句剩下的全部」（含规划层留给整句的
        # 尾留白），非末段就是等长的一片。所以溢出判据也得按同一口径给 pad ——
        # 拿「片长 + 尾留白」去卡末段，而末端本来就没有下一段要接。
        # ``slice_audio_us`` 是本段的**句内偏移**（首段 = 0），不是可用旁白长度：
        # 可用上限是「整句时长 − 偏移 + 本段垫的尾留白」。原先把偏移当成可用长度，
        # 每段首段（offset=0）恒被判溢出，多镜 span 第一次走到对齐就全灭，
        # ``clip.start_us`` 停在 0、游标不推进，write_draft 按 start=0 叠写直接
        # SegmentOverlap 炸掉整条 build（recvvfODIPTTBq build#2 现场）。
        avail_us = max(0, int(whole_audio_us) - slice_audio_us) + pad_us
        if to_us(clip.duration) > avail_us + 1000:
            self._timing_issue(
                clip, "SPAN_SLICE_OVERFLOW", sound_us=to_us(clip.duration),
                avail_us=avail_us,
                detail={"scope": "span_part", "span_index": index,
                        "slice_us": slice_audio_us, "tail_pad_us": pad_us,
                        "span_index_is_last": is_last})
            return False
        # 全局收口：末段必须正好落在「父段落旁白起点 + 整句时长 + 尾留白」上。
        # 逐段判据各自只保证「我没多盖、没少盖自己那一片」，谁都不管**加起来**差
        # 多少 —— 少了半个尾留白、多了几帧，逐段全绿但整句已经错位。这条是整个
        # 多镜装配唯一的全局不变量，也是原来那个 assert 的替代（定案第 8 条）。
        # 检查放在**写 start_us 之前**：不通过就整段原样退回，不留半截赋值。
        if is_last and whole_audio_us > 0:
            expected_end_us = int(audio_origin_us) + int(whole_audio_us) + pad_us
            tol_us = timing_contract.frame_us(self._fps())
            actual_end_us = int(cursor) + to_us(clip.duration)
            if abs(actual_end_us - expected_end_us) > tol_us:
                self._timing_issue(
                    clip, "SPAN_TIMELINE_MISMATCH",
                    sound_us=int(whole_audio_us),
                    avail_us=expected_end_us - int(audio_origin_us),
                    detail={"scope": "span_last_part", "span_index": index,
                            "audio_origin_us": int(audio_origin_us),
                            "whole_audio_us": int(whole_audio_us),
                            "tail_pad_us": pad_us,
                            "expected_end_us": expected_end_us,
                            "actual_end_us": actual_end_us,
                            "delta_us": actual_end_us - expected_end_us,
                            "tolerance_us": tol_us})
                return False
        clip.start_us = cursor
        # 不变量校验按「段长 = 本段音频 + 该段该带的尾留白」比对（`_invariant_items`
        # 的 `tail_pad_us`），所以 `runtime_audio_us` 只能是**本段音频**：末段扣掉
        # 尾留白、非末段原样。这里若填段长本身，每个多镜 item 都会报
        # `INV_DURATION_MISMATCH`，差值恰好就是一个尾留白 —— 假阳性，而且会
        # 挡住所有多镜草稿。
        clip.runtime_audio_us = max(0, int(clip.duration * US) - pad_us)
        self._event("timing_span_aligned", video=Path(clip.video).name,
                    span_index=index, start_us=clip.start_us,
                    segment_us=to_us(clip.duration), audio_slice_us=slice_audio_us,
                    runtime_audio_us=clip.runtime_audio_us,
                    audio_clip_start_us=clip.audio_clip_start_us,
                    speed=clip.video_speed)
        return True

    def align_audio_video(self, clips: list[Clip]):
        """以**真实音频时长**求解每段的可行窗口，引擎只执行解、只校验解。

        v1.3.27 结构修复（用户 2026-09-16 定案第 3~7 条）把这里从「先切再对、
        对不上就截断」改成了「先解再切、解不出就如实报错」：

        · 删掉了 v1.3.24 的 2 秒占位短路（``target > stable_window_duration + 0.05``
          整段 resize 分支）—— 那是在「还不知道真实音频有多长」的年代用占位探针
          估的窗口，现在音频时长在选镜前就是真值，占位窗口没有存在意义；
        · 原来「装不下就把 duration 截到 avail」的静默截断（定案第 7 条明确要删）
          换成结构化失败：解不出可行窗口就记 ``TIMING_INFEASIBLE``，
          由 ``validate_audio_video_sync`` 返回 ``TIMING_INFEASIBLE`` 让编排层重规划。
          画面短了**不能靠截音频或拉长画面蒙混**：截了音画不同步，拉了会播到
          没分析过的画面。
        """
        cursor = 0
        for c in clips:
            if not c.video_duration:
                c.video_duration = self._probe_material(c)
            sound_dur = 0.0
            if c.audio_mode == "tts" and c.tts_path:
                sound_dur = self.probe(c.tts_path)["duration"]
            elif c.audio_mode in ("file",) and c.audio:
                sound_dur = self.probe(c.audio)["duration"]
            elif c.audio_mode == "native":
                sound_dur = c.duration  # 现场原声窗口由分析阶段给定
            if sound_dur > 0 and c.audio_mode in ("tts", "file"):
                # 真实音频时长真值（ffprobe）。写入前的不变量校验必须拿它做
                # 「段长 = 音频 + 尾留白」的基准 —— 拿 c.duration 自己比自己
                # 恒成立，等于没校验。
                c.runtime_audio_us = int(round(sound_dur * US))
            if sound_dur > 0:
                if c.audio_mode == "native":
                    # 原声段的长度由**素材上的那段声音**决定，不是 TTS，
                    # 因此不参与窗口求解：只要保证切片放得下就行。
                    boundary = c.video_duration
                    avail = boundary - c.source_start
                    if avail + 1e-3 < sound_dur:
                        self._timing_issue(c, timing_contract.REASON_SOURCE_TOO_SHORT,
                                           sound_us=int(round(sound_dur * US)),
                                           avail_us=int(round(avail * US)))
                        if self.delivery_mode == "preview":
                            # 预览：素材装不下也把画面摆出来（按素材真实可用长度），
                            # 缺口由 `preview_uncertified_reasons` 如实上报，绝不
                            # 把它说成解出来了。正式交付走不到这一支。
                            c.duration = float(max(avail, 0.0))
                    else:
                        c.duration = float(sound_dur)
                else:
                    # 多镜摊平的两个改口径（定案第 6 条）：
                    #   ① **段长锚在音频上**，不再重复求解。规划层已经解过同一份
                    #      配音（`shot_analyzer` 里就是 `solve_span`/`solve_window`
                    #      的解），摊平的段长逐字来自那份分配。引擎若在这里拿整句
                    #      时长重解一遍，就会把「按段分配」变成「每段都塞整句」——
                    #      第二段起直接无解；而这里又**没有加宽任何约束**（定案第 8
                    #      条：1.3.27 不放宽阈值），是真的无解。
                    #   ② **入口守卫**：段长若大于它自己那一片的长度，旁白铺到
                    #      后面几秒时画面已经切走了，这种错位必须在写入前拦下。
                    if bool(getattr(c, "multi_shot", False)):
                        if not self._align_span_clip(
                                c, cursor, int(round(sound_dur * US))):
                            if self.delivery_mode == "preview":
                                # 预览兜底（定案第 9 条）：多镜子段真无解（素材剩余
                                # 不够盖它那一片）时，**不能**把它留在 start_us=0 ——
                                # write_draft 照原时长叠写会 SegmentOverlap 炸掉
                                # 整条 build。按素材真实余量摆占位、游标照常推进，
                                # 保证后续段不错位；缺口记入 preview_uncertified_reasons，
                                # 交付画面干净（标注已停写）。
                                avail_s = float(max(
                                    c.video_duration - float(c.source_start), 0.0))
                                c.duration = float(
                                    max(min(avail_s, c.duration or 1.0), 0.0))
                                if c.duration <= 0.0:
                                    c.duration = 0.2  # 0 长段会再炸，退个最短占位
                                c.start_us = cursor
                                cursor += to_us(c.duration)
                                self.preview_uncertified_reasons.append(
                                    f"多镜子段无解:{Path(c.video).name} "
                                    f"按素材余量出占位 {c.duration:.2f}s")
                                self._event(
                                    "span_preview_placeholder",
                                    video=Path(c.video).name,
                                    start_us=c.start_us,
                                    duration_us=to_us(c.duration),
                                    reason="span_align_failed")
                            # 该段已记 sync_issues，编排层会拿到 TIMING_INFEASIBLE
                            # 去重规划（formal）。游标在 formal 下**必须原样不动**：
                            # 这里若照旧推进，后面每一段都会跟着错位并各自报一条假
                            # 故障，真原因被埋掉。
                            continue
                        cursor = c.start_us + to_us(c.duration)
                        continue
                    req = self._timing_request(c, int(round(sound_dur * US)))
                    solution = timing_contract.solve_window(req)
                    if not solution.ok:
                        self._timing_issue(c, solution.reason, sound_us=req.audio_duration_us,
                                           detail=solution.detail)
                        if self.delivery_mode == "preview":
                            # 预览占位（定案第 9 条）：求解无解时按**素材真实可用
                            # 长度**摆一段出来，让运营能打开草稿看整体结构；同时把
                            # 这段标成 UNCERTIFIED。`c.solved_window` **刻意不写** ——
                            # 写进去就等于伪造一个「解」，写入前的不变量校验会拿它
                            # 当基准自证通过。宁可不变量校验多报一条，也不要假的解。
                            c.duration = float(max(
                                min(req.usable_us, req.timeline_us) / US, 0.0))
                    else:
                        # 求解器的原解**原样留档**：写入前的不变量校验要拿它做基准
                        # （`_invariant_items`），而不是拿吸附后的浮点值反推 ——
                        # 反推等于自证，发现不了求解器本身给错解的情况。
                        c.solved_window = solution.to_dict()
                        c.source_start = solution.source_start_us / US
                        # 吸附到 1 帧的整数微秒档位（让剪映的 round(素材/速度)
                        # 回程误差 ≤1µs），并夹紧在该画面角色的允许区间内 ——
                        # 否则栅格落点会差出 1e-5 量级，写入前的不变量校验会误报。
                        c.video_speed = frame_speed(
                            solution.speed, req.speed_min, req.speed_max,
                            fps=req.fps)
                        c.duration = solution.segment_us / US
                        c.start_us = cursor
                        cursor += int(solution.segment_us)
                        self._event(
                            "timing_window_solved", video=Path(c.video).name,
                            audio_duration_us=req.audio_duration_us,
                            source_start_us=solution.source_start_us,
                            source_end_us=solution.source_end_us,
                            speed=c.video_speed, segment_us=solution.segment_us,
                            handle_room_us=solution.handle_room_us)
                        continue
            elif c.audio_mode == "mute":
                boundary = c.video_duration - max(0.0, float(self.m.get("tail_trim_s", 0.2) or 0.0))
                c.duration = min(c.duration or 2.0, max(boundary - c.source_start, 0.1))
            else:
                # Silent clips still need a non-zero timeline window. Without
                # this, allow_silent manifests leave every clip at start=0 and
                # the draft writer raises SegmentOverlap on the second video.
                boundary = c.video_duration - max(0.0, float(self.m.get("tail_trim_s", 0.2) or 0.0))
                avail = max(boundary - c.source_start, 0.0)
                c.duration = c.duration or avail
            c.start_us = cursor
            cursor += to_us(c.duration)
        self.total_us = cursor

    def _timing_issue(self, clip: Clip, reason: str, *, sound_us: int,
                      avail_us: int = 0, detail: dict | None = None) -> None:
        """记录一次「窗口无解」。**不截断、不降级**，交编排层重规划。"""
        issue = {
            "video": Path(clip.video).name,
            "temporary_shot_id": clip.temporary_shot_id,
            "reason": reason,
            "audio_duration_us": int(sound_us),
            "available_us": int(avail_us),
            "detail": detail or {},
            "message": "该段配音时长在素材允许的窗口内无解（含保护区、转场把手、"
                       "变速范围与证据覆盖约束）；需重规划换镜/拼多镜/报缺口，"
                       "不得截断音频或拉长画面",
        }
        self.sync_issues.append(issue)
        self._event("timing_infeasible", **issue)

    def _uncertified_coverage(self) -> Optional[dict]:
        """本批闸门的认证状态；已认证或根本没跑到选镜时返回 None。

        数据来源是 manifest 里的 ``claim_gate_coverage``（由
        ``shot_analyzer.match_manifest`` 写进 ``output_manifest``），引擎只**读**
        不判 —— 认不认证由选镜层决定，引擎不给自己发合格证。

        判据同时看 ``certification`` 与 ``uncertified_reasons``：
        · ``certification`` 缺失（老 manifest / 非临时选镜路径）→ 未认证，
          理由记为 ``COVERAGE_MISSING``，因为「没记录」和「认证通过」必须分开；
        · 只要 ``certification`` 不是 ``CERTIFIED`` 或还有未认证理由，就带标注。
        """
        coverage = self.m.get("claim_gate_coverage")
        if not isinstance(coverage, dict) or not coverage:
            return None
        cert = str(coverage.get("certification") or "").strip().upper()
        reasons = [str(r) for r in (coverage.get("uncertified_reasons") or []) if str(r)]
        if cert == "CERTIFIED" and not reasons:
            return None
        return coverage

    def _coverage_report(self) -> Optional[dict]:
        """给运营看的认证状态：闸门口径 + 本次交付模式 + 预览放宽了什么。

        与 `_uncertified_coverage()` 分开是有意的：
        · `_uncertified_coverage()` 决定**要不要打画面标注**，只认「闸门未认证」；
        · 这里是**上报口径**，还要带上 `delivery_mode` 与预览逐条理由 ——
          预览稿即便闸门认证通过，也**不是**正式交付，必须在结果里说得清楚。
        两边都不改判据，只是用途不同。
        """
        coverage = self._uncertified_coverage()
        reasons = list(coverage.get("uncertified_reasons") or []) if coverage else []
        if self.delivery_mode == "preview":
            reasons = list(reasons) + list(self.preview_uncertified_reasons)
        if not coverage and not self.preview_uncertified_reasons:
            if self.delivery_mode == "formal":
                return None
            coverage = {}
        report = dict(coverage or {})
        report["delivery_mode"] = self.delivery_mode
        report["formal_delivery_allowed"] = (
            self.delivery_mode == "formal" and not self.preview_uncertified_reasons)
        if reasons:
            report["uncertified_reasons"] = reasons
            report["certification"] = str(report.get("certification") or "UNCERTIFIED")
        return report

    def _fps(self) -> float:
        """草稿帧率（不变量校验的容差基准）。取不到就用 30，与 create_draft 一致。"""
        try:
            value = float(self.m.get("fps", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        return value if value > 0 else float(timing_contract.DEFAULT_FPS)

    def _invariant_items(self, clips: list[Clip], built: list[dict]) -> list[dict]:
        """把「段落解 + 草稿回读结果」摊平成不变量校验条目（定案第 3、8 条）。

        每个字段都必须来自**真实来源**，不许就地重算：
        · ``source_start_us``/``segment_us`` 来自求解器写回段落的解；
        · ``audio_duration_us`` 来自最终音频 ffprobe；
        · ``actual_video_us`` 来自 pyJianYingDraft 回读 —— 这是最终渲染会用的
          那个数，也是唯一能发现「库自己按 round(source/speed) 回算」的入口。
        """
        by_index: dict[int, dict] = {}
        for row in built or []:
            if isinstance(row, dict) and "index" in row:
                by_index[int(row["index"])] = row
        items: list[dict] = []
        for index, c in enumerate(clips):
            span = self._evidence_span(c)
            row = by_index.get(index, {})
            speed_min, speed_max = timing_contract.speed_range_for_role(
                self._clip_role(c), time_sensitive=bool(c.time_sensitive))
            speed = float(c.video_speed or 1.0)
            # 源出点优先取**求解器给的那个数**（`solved_window`），拿不到才按
            # 「入点 + 输出时长 × 速度」回算。回算本身没错，但它是用吸附后的速度
            # 反推的，一旦吸附把速度挪出原解，回算值就会跟着偏 —— 校验不该建立在
            # 被校验对象自己的推导上。
            solved = c.solved_window if isinstance(c.solved_window, dict) else {}
            source_end_us = solved.get("source_end")
            source_end_us = (to_us(float(source_end_us)) if source_end_us is not None
                             else to_us(c.source_start + c.duration * speed))
            # 尾留白是**整句**时间线尾部的呼吸：多镜摊平后它只属于最后一个子段，
            # 前面的子段一毫秒都不带。这里按段给，阈值一个字没动（定案第 8 条）。
            if getattr(c, "multi_shot", False):
                span_total = max(1, int(getattr(c, "span_total", 1) or 1))
                tail_pad_us = (self._tail_pad_us()
                               if self._span_index_in(c, clips) >= span_total else 0)
            else:
                tail_pad_us = self._tail_pad_us()
            items.append({
                "index": index,
                "video": Path(c.video).name,
                "audio_mode": c.audio_mode,
                "source_start_us": to_us(c.source_start),
                "source_end_us": source_end_us,
                "segment_us": to_us(c.duration),
                "audio_duration_us": (int(c.runtime_audio_us) or to_us(c.duration)),
                "tail_pad_us": tail_pad_us,
                "video_duration_us": to_us(c.video_duration),
                # 1.3.27.1：素材总长（写入层同一把尺）。引擎的 video_duration 现在
                # 就是统一探针（pymediainfo 视频轨优先）返回的值，再单列一道硬边界。
                "material_duration_us": to_us(c.video_duration),
                "video_speed": speed,
                "speed_range": [speed_min, speed_max],
                "evidence_start_us": None if span is None else to_us(span[0]),
                "evidence_end_us": None if span is None else to_us(span[1]),
                "actual_video_us": (int(row["duration_us"]) if row.get("duration_us")
                                    else None),
            })
        return items

    # 「时间轴自相矛盾」的不变量码子集。**必须**用 timing_contract 的常量按值
    # 精确匹配 —— 1.3.27.1 之前这里拿 "INV_" 前缀去 startswith，而码值根本没有
    # 这个前缀，导致预览从不阻断窗口越界：20260917 的炸写正是穿透了这个缺口，
    # 直达写入层才爆的 ValueError。
    _CONTRADICTORY_CODES = (
        timing_contract.INV_WINDOW_OUT_OF_BOUNDS,
        timing_contract.INV_APERTURE_MISMATCH,
        timing_contract.INV_SPEED_OUT_OF_RANGE,
    )

    def _invariant_gate(self, clips: list[Clip], *, stage: str,
                        built: Optional[list[dict]] = None) -> Optional[dict]:
        """``check_invariants`` 的真实执行位（1.3.27.1 用户修正第 5 条）。

        共三个执行位：① 计划解算完成后（``run`` 内，``stage="plan_complete"``）；
        ② ``write_draft`` 真正写入片段前（``stage="pre_write"``）；③ 草稿写入
        内存、save 之前（``stage="post_write"``，带 ``built`` 回读）。
        任一位置失败：输出明确错误码/素材名/句子 index/具体数值，**停止生成该
        草稿** —— 链路里不存在 clamp/min 绕过。

        预览语义（定案第 9 条）不变：占位段天然带 ``DURATION_MISMATCH``（它
        没有「解」），预览如实记账不阻断；但「自相矛盾」类（窗口越界/切片与解
        不一致/变速越界）连预览稿都阻断 —— 预览放宽的是「没解出来」，不是
        「解错了还照发」。
        """
        violations = timing_contract.check_invariants(
            self._invariant_items(clips, built or []), fps=self._fps())
        if not violations:
            return None
        contradictory = [v for v in violations
                         if str(v.get("code", "")) in self._CONTRADICTORY_CODES]
        if self.delivery_mode == "preview" and not contradictory:
            known = set(self.preview_uncertified_reasons)
            self.preview_uncertified_reasons.extend(
                m for m in (f"PREVIEW_{v.get('code')}@{v.get('index')}"
                            for v in violations) if m not in known)
            self._event("preview_invariants_tolerated", stage=stage,
                        violation_count=len(violations),
                        codes=sorted({str(v.get("code")) for v in violations}),
                        message="预览稿存在未解出的占位段，已如实记账不阻断。")
            return None
        if contradictory:
            if self.delivery_mode == "preview":
                return {
                    "status": "PREVIEW_BLOCKED_BY_INVARIANTS",
                    "draft_path": None,
                    "delivery_mode": "preview", "stage": stage,
                    "violations": contradictory,
                    "reason": "时间轴自相矛盾（窗口越界/窗口倒挂/切片与解不一致/"
                              "变速越界），本次未生成草稿。预览放宽的是「未解出的"
                              "段落先出占位图」，不是「解错了也照发」。",
                    "formal_delivery_allowed": False,
                }
            return {
                "status": "TIMING_INVARIANT_VIOLATION",
                "draft_path": None, "stage": stage,
                "violations": violations,
                "reason": f"时序不变量校验未通过（{stage}），已阻止生成草稿，"
                          "请重规划换镜或拼多。",
            }
        return {
            "status": "TIMING_INVARIANT_VIOLATION",
            "draft_path": None, "stage": stage, "violations": violations,
            "reason": f"写入前时序不变量校验未通过（{stage}），已阻止生成草稿，"
                      "请重规划换镜或拼多镜。",
        }

    def validate_audio_video_sync(self, clips: list[Clip]) -> Optional[dict]:
        if self.sync_issues:
            # 只要有一条是窗口无解，就按结构化的 TIMING_INFEASIBLE 报出去，
            # 让编排层去重规划；其余历史性 sync 问题仍按 AUDIO_VIDEO_MISMATCH 报。
            # `SPAN_`（多镜装配的逐段/全局不变量）与 `SOURCE_`/`EVIDENCE_`/`SPEED_`
            # 同属结构性无解：这些都不是「音频比画面长一点」的旧式错位，而是求解
            # 或分配本身不成立，截断/拉长都救不了，只能重规划。
            infeasible = [i for i in self.sync_issues if i.get("detail") is not None
                          or str(i.get("reason", "")).startswith(
                              ("SOURCE_", "EVIDENCE_", "SPEED_", "SPAN_", "NO_"))]
            if infeasible:
                if self.delivery_mode == "preview":
                    return self._preview_tolerated("TIMING_INFEASIBLE", self.sync_issues,
                                                   "存在无解段落，预览稿按素材可用长度出占位画面，仅供参考。")
                return {"status": "TIMING_INFEASIBLE", "issues": self.sync_issues,
                        "message": "存在无解段落：配音时长在素材允许窗口内装不下，"
                                   "已阻止写入草稿，交编排层重规划。"}
            if self.delivery_mode == "preview":
                return self._preview_tolerated("AUDIO_VIDEO_MISMATCH", self.sync_issues,
                                               "存在音画时长错位，预览稿仍出图，仅供参考。")
            return {"status": "AUDIO_VIDEO_MISMATCH", "issues": self.sync_issues,
                    "message": "音频时长超过可用画面，已阻止写入草稿。"}
        total = self.total_us / US
        if total < 0:
            return {"status": "AUDIO_VIDEO_MISMATCH", "issues": [{"reason": "负时间轴长度"}]}
        return None

    def _preview_tolerated(self, status: str, issues: list, message: str) -> None:
        """预览模式：把本该阻断的时序问题**记为未认证理由**，不阻断出图。

        这是「预览可以为未解决的片段生成标记为 UNCERTIFIED 的占位符，正式交付
        不得静默通过」（定案第 9 条）里的预览半边。**它不改变任何交付语义** ——
        草稿照出、但结果里带 `coverage.certification != CERTIFIED` 与逐条理由，
        并且这条草稿**绝不能被当成正式交付**（`delivery_mode` 已写进 SUCCESS）。

        2026-09-18 用户定案后，视觉证据类缺口也走同样的「记未认证、不阻断」
        路径（见 `visual_evidence_gate` / `final_visual_evidence_gate`）。
        素材结构性缺口（素材真的一条候选都没有）仍照旧阻断。
        """
        self.preview_uncertified_reasons.append(
            f"PREVIEW_{status}: {len(issues)} 段时序问题未解决")
        self._event("preview_tolerated", status=status, issue_count=len(issues),
                    delivery_mode=self.delivery_mode, message=message)
        return None

    # ---------- 需求8：转场建议 ----------
    def _parent_clip_of(self, clip: Clip):
        """本段所属的**父段落首段**剪辑（单镜段就是它自己）。

        `_clips` 是 `_expand_spans` 的产物，同一父段落的子段在表里连续出现且
        首段在前，所以取第一个 `parent_index` 相同的即可。取不到（表还没建好）
        时退回自己 —— 单镜语义，不制造新行为。
        """
        pid = int(getattr(clip, "parent_index", -1) or -1)
        if pid < 0:
            return clip
        for other in (getattr(self, "_clips", None) or []):
            if int(getattr(other, "parent_index", -1) or -1) == pid:
                return other
        return clip

    def _same_span(self, prev: Clip, cur: Clip) -> bool:
        """两段是否同属一条**多镜**原段落（旁白只有一份、兄弟段之间无接缝）。"""
        if not getattr(cur, "multi_shot", False) and not getattr(prev, "multi_shot", False):
            return False
        a = int(getattr(prev, "parent_index", -2) or -2)
        b = int(getattr(cur, "parent_index", -2) or -2)
        return a >= 0 and a == b

    def _span_index_in(self, clip: Clip, clips: list[Clip]) -> int:
        """本段在它所属原段落里是第几段（布局 1 起；单镜恒为 1）。"""
        pid = int(getattr(clip, "parent_index", -1) or -1)
        if pid < 0:
            return 1
        position = 0
        for other in clips:
            if int(getattr(other, "parent_index", -1) or -1) == pid:
                position += 1
                if other is clip:
                    return position
        return int(getattr(clip, "span_index", 0) or 0) + 1

    def _clip_speed(self, clip: Clip) -> float:
        return float(getattr(clip, "video_speed", 1.0) or 1.0)

    def _transition_for(self, prev: Clip, cur: Clip, *, cut_inside_span: bool = False):
        """给一处相邻段算转场建议 + 把手（定案第 5 条）。

        ``cut_inside_span`` 为真时，这是**多镜段落内部的兄弟缝**：旁白是**一句**
        连续的话，往中间塞转场等于让这句话卡一下；而且这里的「两段相邻」本来
        就是求解器按同一份配音时长拆出来的，不是剪辑意义上的两个场景。所以
        兄弟缝一律**不加转场、不留把手**。

        把手口径（`prev.transition_out_handle_us` / `cur.transition_in_handle_us`）
        只在**同一条素材**上非零：那时转场是把同一段画面的两个切片接起来，
        接缝两侧的帧不能被切片用掉。跨素材转场是在两段之间重叠出来的，不占素材。
        """
        prev_speed = self._clip_speed(prev)
        cur_speed = self._clip_speed(cur)
        # 源侧真实出点：切片消耗的素材长度 = 输出时长 × 速度
        prev_end = prev.source_start + prev.duration * prev_speed
        peak = (cur.stability or {}).get("peak_motion")
        if cut_inside_span:
            # 兄弟缝：一句话被拆成几段画面，中间**不得**有转场，也就**不得**有把手。
            # 求解器是拿 `handles=(0,0)` 解的这几段（规划层按 `solve_span` 逐段解），
            # 这里若留着上一轮/上一段写下的陈旧把手，等于事后偷偷把素材可用区间
            # 又缩回去 —— 求解器批准的解会变成不变量校验的违规，或者更糟：写入时
            # 才发现出点越界。所以显式清零，而不是靠「没走到赋值分支」。
            prev.transition_out_handle_us = 0
            cur.transition_in_handle_us = 0
            cur.transition = None
            return None
        t = stability.recommend_transition(prev.video, cur.video,
                                           prev_source_end=prev_end,
                                           cur_source_start=cur.source_start,
                                           cur_peak_motion=peak)
        cur.transition = t
        if not t:
            return None
        same_source = (Path(prev.video).resolve() == Path(cur.video).resolve())
        half_us = int(round(to_us(t.get("duration", 0.0)) * 0.5))
        if same_source and half_us > 0:
            prev.transition_out_handle_us = half_us
            cur.transition_in_handle_us = half_us
        else:
            cur.transition_in_handle_us = 0
        if cur_speed != prev_speed:
            self._event("transition_speed_note", index=0,
                        prev_speed=prev_speed, cur_speed=cur_speed)
        return t

    def plan_transitions(self, clips: list[Clip]):
        """转场建议 + **把手回写**（定案第 5 条）。

        转场分两类，把手口径完全不同，必须分开算：

        1. **跨场景转场**（相邻镜头是同一条素材）：前后两段本来就是同一段画面，
           转场只是把两个切片接在一起。此时把手由「接缝两侧各留几帧」决定 ——
           也就是转场时长本身。
        2. **同场景内的补拍缝**（相邻镜头是同一条素材内的**不连续**两段，
           或不同素材）：转场吃掉的是**两段之间的时间**，不占素材，把手为 0。

        v1.3.24~26 把这两类混在一起：`prev_end = prev.source_start + prev.duration`
        在变速后就不等于真实的源出点了（真实出点还要乘 speed），于是「上一段
        尾巴」和「下一段开头」的判断全偏，转场把手从没被求解器扣过 —— 定案第 4 条
        要求它进约束，所以这里回写。

        v1.3.27 起多是第三类：**多镜段落内部的兄弟缝**（见 ``_transition_for``），
        一律不加转场、不留把手。
        """
        if not self.m.get("auto_transition", True):
            return
        for i in range(1, len(clips)):
            prev, cur = clips[i - 1], clips[i]
            self._transition_for(prev, cur, cut_inside_span=self._same_span(prev, cur))

    # ---------- 需求3：BGM ----------
    def prepare_bgm(self) -> Optional[str]:
        spec = self.m.get("bgm")
        if not spec and self.intelligent_mode_authorized():
            spec = {"auto": True, "script": str(self.m.get("full_script", ""))}
            self._event("bgm_auto_enabled", mode="intelligent")
        if not spec:
            return None
        bgm_dir = self.report_dir / "bgm"
        total = self.total_us / US
        src: Optional[Path] = None
        if isinstance(spec, dict) and spec.get("path"):
            src = self.resolve(spec["path"])
        elif isinstance(spec, dict) and (spec.get("music_id") or spec.get("mood") or
                                         spec.get("auto", True) or self.m.get("full_script")):
            # 默认基于脚本/剧情对曲库相关性排序；显式 mood/music_id 优先
            cands = bgm_selector.select_bgm(
                spec.get("mood", ""), music_id=spec.get("music_id", ""),
                topk=5, min_duration=total,
                script=spec.get("script", "") or str(self.m.get("full_script", "")))
            if not cands:
                self._warn("bgm_not_found", f"未找到匹配BGM: {spec}")
                return None
            chosen = cands[0]
            self._bgm_choice = chosen
            if chosen.get("url"):
                src = bgm_selector.download_track(chosen, bgm_dir)
            if src is None:
                self._warn("bgm_no_url", f"《{chosen['title']}》无直链，请在剪映同步音乐后指定 path")
                return None
        if src is None or not Path(src).exists():
            return None
        target_lufs = float(spec.get("target_lufs", -28.0)) if isinstance(spec, dict) else -28.0
        out = bgm_dir / "bgm_final.m4a"
        bgm_selector.prepare_bgm_track(
            src, out, total, ffmpeg=str(self.ffmpeg) if self.ffmpeg else "ffmpeg",
            target_lufs=target_lufs,
            fade_in=float(spec.get("fade_in", 0.4)) if isinstance(spec, dict) else 0.4,
            fade_out=float(spec.get("fade_out", 0.2)) if isinstance(spec, dict) else 0.2)
        return str(out)

    # ---------- 素材 stage（需求5：并行复制） ----------
    def _normalize_narrations(self, clips: list[Clip]) -> dict[str, str]:
        """把所有 TTS 旁白归一到统一响度，返回 {原始路径: 归一后路径}。

        逐条 TTS 的固有电平差是「声音忽大忽小」的根因（R3 实测跨度 15.9 LU），
        与具体某条视频无关，故按用户定案做成**通用规则**。只处理 `audio_mode
        == "tts"`：`file` 模式是操作方明确指定的配音，属于操作决策，不擅自改
        动它的电平（仍会记账，便于发现它是否也需要归一）。
        """
        spec = self.m.get("narration_loudness")
        spec = spec if isinstance(spec, dict) else {}
        if spec.get("enabled") is False:
            self._event("narration_loudness_disabled")
            return {}
        target = float(spec.get("target_lufs", audio_loudness.TARGET_LUFS))
        gain_db = float(spec.get("gain_db", 0.0))
        ffmpeg = str(self.ffmpeg) if self.ffmpeg else "ffmpeg"
        ffprobe = str(self.ffprobe) if self.ffprobe else "ffprobe"
        out_dir = self.report_dir / "loudness"

        wanted: dict[str, str] = {}
        for c in clips:
            if c.audio_mode != "tts" or not c.tts_path:
                continue
            src = Path(c.tts_path)
            key = str(src)
            if key in wanted:
                continue
            try:
                st = src.stat()
                identity = f"{src}|{st.st_size}|{st.st_mtime_ns}"
            except OSError:
                identity = key
            digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
            wanted[key] = str(out_dir / f"{src.stem}_{digest}.m4a")

        file_mode = [str(c.audio) for c in clips
                     if c.audio_mode == "file" and c.audio]
        if file_mode:
            self._event("narration_loudness_file_mode_untouched",
                        count=len(file_mode),
                        message="file 模式配音由操作方指定，未做响度归一。")
        if not wanted:
            return {}

        def _one(item: tuple[str, str]) -> tuple[str, str, dict]:
            src_key, dst = item
            detail = audio_loudness.normalize_narration(
                Path(src_key), Path(dst), ffmpeg=ffmpeg, ffprobe=ffprobe,
                target_lufs=target + gain_db)
            return src_key, dst, detail

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=self.profile.staging_workers) as ex:
            results = list(ex.map(_one, wanted.items()))
        overrides = {src: dst for src, dst, _ in results}
        details = [d for _, _, d in results]
        done = [d for d in details if d.get("out_lufs") is not None]
        self._event(
            "narration_loudness_normalized", count=len(details),
            normalized=len(done),
            skipped=[d.get("skipped") for d in details if d.get("skipped")],
            target_lufs=target + gain_db, gain_db=gain_db,
            in_spread_lu=(round(max(d["in_lufs"] for d in details) -
                                min(d["in_lufs"] for d in details), 2)
                          if all("in_lufs" in d for d in details) else None),
            out_spread_lu=(round(max(d["out_lufs"] for d in done) -
                                 min(d["out_lufs"] for d in done), 2) if done else None),
            elapsed_s=round(time.perf_counter() - started, 3),
            details=details)
        return overrides

    def _stage(self, source: Path, media_dir: Path) -> str:
        media_dir.mkdir(parents=True, exist_ok=True)
        source = Path(source).resolve()
        dst = media_dir / source.name
        # Avoid cross-directory same-name collisions while keeping repeat runs stable.
        try:
            st = source.stat()
            source_identity = f"{source}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            source_identity = str(source)
        source_hash = hashlib.sha256(source_identity.encode()).hexdigest()
        marker = dst.with_name(dst.name + ".source.sha256")
        if dst.exists() and marker.exists():
            try:
                if marker.read_text(encoding="utf-8") == source_hash:
                    return str(dst)
            except OSError:
                pass
            dst = media_dir / f"{source.stem}_{source_hash[:8]}{source.suffix}"
            marker = dst.with_name(dst.name + ".source.sha256")
        # A same-sized source may still have changed; marker mismatch forces refresh.
        if not dst.exists() or not marker.exists() or marker.read_text(encoding="utf-8") != source_hash:
            shutil.copy2(source, dst)
        try:
            marker.write_text(source_hash, encoding="utf-8")
        except OSError:
            pass
        return str(dst)

    def _upload_videos_and_check_capacity(self, clips: list[Clip]) -> Optional[dict]:
        """Copy the actual video inputs first, then apply the host limit.

        The upload session lives under the task report directory, so an
        over-limit submission never creates a JianYing draft and never starts
        TTS.  Only unique video sources are counted.
        """
        upload_dir = self.report_dir / "uploads" / "videos"
        upload_dir.mkdir(parents=True, exist_ok=True)
        unique = list(dict.fromkeys(str(Path(c.video).resolve()) for c in clips))
        copied: dict[str, str] = {}
        started = time.perf_counter()
        for source in unique:
            copied[source] = self._stage(Path(source), upload_dir)
        self._uploaded_videos = copied
        detail = capacity.summarize_sources([Path(p) for p in copied.values()])
        detail["upload_elapsed_s"] = round(time.perf_counter() - started, 3)
        detail["upload_dir"] = str(upload_dir)
        if not self.capacity_guard:
            self._event("capacity_admission_bypassed", reason="calibration_only", sources=detail)
            return None
        blocker, _ = capacity.admission_result(
            [Path(p) for p in copied.values()], storage_path=self._drafts_root)
        self._event("capacity_admission", **detail, blocked=bool(blocker), check_stage="after_upload")
        if blocker:
            limit = blocker.get("safe_source_limit")
            if limit:
                blocker["message"] = f"上传数量单次只支持最多传 {limit} 条，请重新调整后再试～"
            blocker["uploaded_dir"] = str(upload_dir)
            return blocker
        return None

    # ---------- 需求6/7：构建并安全写入草稿 ----------
    def write_draft(self, clips: list[Clip], bgm_path: Optional[str]) -> dict:
        import sys
        sys.path.insert(0, str(self.editor_root / "scripts"))
        from jy_wrapper import JyProject, draft  # type: ignore

        drafts_root = find_drafts_root(self.editor_root)
        name = str(self.m.get("draft_name") or "豆包剪映编排草稿")
        verdict = draft_safety.pre_write_check(
            name, Path(drafts_root), self.editor_root,
            force=bool(self.m.get("force_overwrite")))
        if not verdict.ok:
            return {"status": verdict.status if verdict.status != "BLOCKED" else "BLOCKED",
                    "reason": verdict.message}

        w, h = (1080, 1920) if str(self.m.get("aspect_ratio", "9:16")) == "9:16" else (1920, 1080)
        # ``find_drafts_root`` may resolve JianYing's Windows custom library
        # from the client registry.  Pass it through explicitly: the wrapper's
        # legacy default resolver otherwise falls back to LOCALAPPDATA.
        project = JyProject(name, width=w, height=h, drafts_root=str(drafts_root), overwrite=True)
        media_dir = Path(project.draft_dir) / "media"

        # 旁白响度归一（通用规则）：先产出归一成品，再让它替身进 stage 与写入。
        # 必须**先于** sources 收集 —— 否则 stage 的还是原始 TTS，草稿里写进去的
        # 也就还是忽大忽小的那批。
        loudness_overrides = self._normalize_narrations(clips)

        sources = []
        for c in clips:
            sources.append(Path(getattr(self, "_uploaded_videos", {}).get(
                str(Path(c.video).resolve()), c.video)))
            if c.tts_path:
                sources.append(Path(loudness_overrides.get(
                    str(Path(c.tts_path)), c.tts_path)))
            if c.audio_mode == "file" and c.audio:
                sources.append(Path(c.audio))
        if bgm_path:
            sources.append(Path(bgm_path))
        unique_sources = list(dict.fromkeys(str(s) for s in sources))
        pressure = self._background_pressure("before_staging")
        if pressure:
            return pressure
        with ThreadPoolExecutor(max_workers=self.profile.staging_workers) as ex:
            staged = dict(zip(unique_sources,
                              ex.map(lambda s: self._stage(Path(s), media_dir), unique_sources)))
        pressure = self._background_pressure("after_staging")
        if pressure:
            return pressure

        built = []
        prev_seg = None
        # 闸门未认证时整批打标注（定案第 10 条）。读出一次，循环内不再重算 ——
        # 认证状态是**整批**属性，逐段变化会让标注看起来像「某几段有问题」。
        # 预览稿里若有未解出的占位段，同样必须看得见：那种稿子打开就像成品，
        # 不打标注迟早被当成交付稿发出去。
        uncertified = self._uncertified_coverage()
        if uncertified is None and self.preview_uncertified_reasons:
            uncertified = {"certification": "UNCERTIFIED",
                           "coverage_status": "PREVIEW_UNRESOLVED",
                           "uncertified_reasons": list(self.preview_uncertified_reasons)}
        if uncertified:
            self._event("uncertified_mark_applied",
                        certification=uncertified.get("certification"),
                        coverage_status=uncertified.get("coverage_status"),
                        uncertified_reasons=uncertified.get("uncertified_reasons"),
                        track=UNCERTIFIED_MARK_TRACK,
                        message="本批闸门未认证，已在成片上打常驻标注。")
        # 1.3.27.1 执行位②：真正写入每个片段**前**再跑一次不变量校验 —— 第一个
        # 字节落进草稿之前，窗口越界/解不一致必须在计划层被拦下，而不是等
        # 写入层零容差 raise（20260917 炸写正是走到那一步才爆的）。
        pre_write_gate = self._invariant_gate(clips, stage="pre_write")
        if pre_write_gate:
            return pre_write_gate
        # 通用亮度规则的取值：默认 BRIGHTNESS_KEYFRAME_VALUE，可被任务清单覆盖，
        # 显式 0 表示「本任务不动亮度」。夹到 API 声明的 -1.0~1.0 区间内 ——
        # 越界值会被剪映静默忽略，等于规则悄悄失效。
        brightness_spec = self.m.get("brightness")
        if isinstance(brightness_spec, dict):
            brightness_value = float(brightness_spec.get(
                "value", BRIGHTNESS_KEYFRAME_VALUE))
        elif brightness_spec is None:
            brightness_value = BRIGHTNESS_KEYFRAME_VALUE
        else:
            brightness_value = float(brightness_spec)
        brightness_value = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, brightness_value))
        if brightness_value:
            self._event("brightness_rule_applied", value=brightness_value,
                        target="Video_BRoll",
                        message="通用亮度规则：视频段挂恒定亮度关键帧（端点保持，不过爆）。")
        for c in clips:
            uploaded_video = str(Path(getattr(self, "_uploaded_videos", {}).get(
                str(Path(c.video).resolve()), c.video)))
            # 变速解由规划阶段的窗口求解给出（``align_audio_video`` 执行），这里
            # 只把它**如实写进草稿**。v1.3.24~26 这里不带 speed，于是「解出的
            # 变速」永远停留在内存里 —— 素材短了照样按原速切，剪映再用
            # round(source/speed) 回算出另一个时长，音画对不上还没人知道。
            vid = project.add_media_safe(
                staged[uploaded_video], start_time=c.start_us,
                duration=to_us(c.duration), track_name="Video_BRoll",
                source_start=to_us(c.source_start),
                speed=(float(c.video_speed) if c.video_speed else None))
            if vid is None:
                raise RuntimeError(f"视频添加失败: {c.video}")
            # pyJianYingDraft may expose a shorter playable duration than ffprobe
            # (MOV edit lists/codec timestamps). Use the actual inserted segment
            # as the hard bound for attached audio and captions.
            actual_video_us = int(getattr(getattr(vid, "target_timerange", None),
                                          "duration", to_us(c.duration)))
            # 需求6：按 audio_mode 决定视频原声，绝不一刀切静音覆盖现场口播
            vid.volume = 1.0 if c.audio_mode == "native" else 0.0
            # 通用亮度规则：每段挂一个恒定亮度关键帧。用 `add_keyframe` 而不是
            # 滤镜，因为它在 `VisualSegment` 上会**自动**置
            # `enable_color_correct_adjust=True`（见 vendor segment.py），正是
            # 剪映认这条调色的开关；同时着色器端点保持，结构上不会过爆。
            if brightness_value:
                vid.add_keyframe(draft.KeyframeProperty.brightness, 0,
                                 brightness_value)
            # 需求8：跨场景叠化（加在前一段上）
            if c.transition and prev_seg is not None:
                project.add_transition_simple(
                    c.transition["type"], video_segment=prev_seg,
                    duration=to_us(c.transition["duration"]))
            narration = c.tts_path if c.audio_mode == "tts" else (
                c.audio if c.audio_mode == "file" else None)
            if narration:
                # 用户定案第 7 条：**删除 min(...) 静默截断**。旁白该多长就多长，
                # 画面短了是上游求解该发现的事（解不出会在 align_audio_video 就
                # 报 TIMING_INFEASIBLE，根本走不到这里）。这里若还悄悄截短，
                # 音画必然不同步，却因为「没报错」而无人察觉。
                #
                # 多镜段（定案第 6 条）：同一句配音被 N 个子段共用，各段取**自己
                # 那一片** —— `source_start` 就是该片在整句配音里的偏移。逐段各加
                # 一条 `AudioSegment`，相邻两段的「片偏移」恰好接上（偏移 = 本段
                # 起点 − 父段起点），所以渲染出来仍是一句连续的话，而旁白轨的
                # 段边界与画面轨**逐段对齐**。单镜段偏移恒为 0，与 v1.3.26 完全一致。
                # 归一后的成品替身进轨（`_audio_slice_us` 的偏移口径不变：归一
                # 已把时长钉成与源等长，切片刻度与求解时用的一致）。
                a = project.add_audio_safe(
                    staged[loudness_overrides.get(str(Path(narration)), str(Path(narration)))],
                    start_time=c.start_us,
                    duration=to_us(c.duration),
                    source_start=self._audio_slice_us(c),
                    track_name="Narration")
                if a is None:
                    raise RuntimeError(f"旁白添加失败: {narration}")
            # 字幕：多镜的子段**照常各出一条字幕**（各自的时间区间、各自的时长），
            # 这是摊平带来的必然结果 —— 中间若切成新的镜头，字幕条跟着走。同一句
            # 文本铺在 N 段上，视觉上就是一句话横跨 N 个镜头。判据仍与原版一致
            # （有文本才有字幕），只是现在按摊平后的段逐条走。
            if c.text:
                project.add_text_simple(
                    c.text, start_time=c.start_us, duration=to_us(c.duration),
                    track_name="Subtitles")
            # 画面标注（【素材待补】/【本批未认证】）已按用户 2026-09-17 定案
            # **停止写入草稿**：交付画面必须干净。降级段与未认证状态继续在
            # `built`/报告/事件里如实上报（`c.degraded`、`_uncertified_coverage`、
            # `uncertified_mark_*` 事件），审计可见性不受影响 —— 只是不再上画面。
            if c.degraded:
                self._event("degraded_segment_reported", start_us=c.start_us,
                            duration_us=to_us(c.duration), text=c.text[:24])
            built.append({"index": len(built), "start_us": c.start_us,
                          "duration_us": actual_video_us,
                          "source_start": c.source_start, "audio_mode": c.audio_mode,
                          "text": c.text, "transition": c.transition,
                          "stability": c.stability,
                          "visual_missing": bool(c.visual_missing),
                          "operator_note": c.visual_missing_note})
            prev_seg = vid

        # ── 写入后不变量自检（定案第 7、8 条）────────────────────────────
        #
        # 防线放在**草稿已经写进内存、尚未 save()** 的位置：这里能拿到
        # pyJianYingDraft 回读出来的真实段长（``actual_video_us``），也就是最终
        # 渲染会用的那个数。任一不变量不通过就**不写草稿**、返回结构化错误，
        # 由编排层重规划 —— 绝不「先写下去再说」。
        violations = timing_contract.check_invariants(
            self._invariant_items(clips, built), fps=self._fps())
        if violations and self.delivery_mode == "preview":
            # 预览（定案第 9 条）：「可以为未解决的片段生成标记为 UNCERTIFIED 的
            # 占位符」。占位段的段长本来就**不**等于「音频 + 尾留白」（它没有解），
            # 所以不变量在这里必然报 `INV_DURATION_MISMATCH` —— 这是预期内的，
            # 不是矛盾。因此 preview 不阻断，而是把逐条 violations 原样收进
            # `preview_uncertified_reasons`，让草稿带着「哪些段没解出来」出图。
            #
            # 但要与**真正的**矛盾区分开：`INV_WINDOW_OUT_OF_BOUNDS` /
            # `INV_APERTURE_MISMATCH` / `INV_SPEED_OUT_OF_RANGE` 说明写进草稿的
            # 时间轴本身自相矛盾（不是「没解」，是「解错了」），这种连预览稿都会
            # 打开成错的时间轴，一律继续阻断 —— 预览放宽的是「承认没解出来」，
            # 不是「承认解错了还照发」。
            contradictory = [v for v in violations
                             if str(v.get("code", "")) in self._CONTRADICTORY_CODES]
            if contradictory:
                return {
                    "status": "PREVIEW_BLOCKED_BY_INVARIANTS",
                    "draft_path": None,
                    "delivery_mode": "preview", "stage": "post_write",
                    "violations": contradictory,
                    "reason": "预览稿的时间轴自相矛盾（窗口越界/切片与解不一致/变速越界），"
                              "本次未生成草稿。预览放宽的是「未解出的段落先出占位图」，"
                              "不是「解错了也照发」。",
                    "formal_delivery_allowed": False,
                }
            known = set(self.preview_uncertified_reasons)
            self.preview_uncertified_reasons.extend(
                m for m in (f"PREVIEW_{v.get('code')}@{v.get('index')}"
                            for v in violations) if m not in known)
            self._event("preview_invariants_tolerated",
                        delivery_mode="preview", violation_count=len(violations),
                        codes=sorted({str(v.get("code")) for v in violations}),
                        message="预览稿存在未解出的占位段，已如实记账不阻断。")
        elif violations:
            return {
                "status": "TIMING_INVARIANT_VIOLATION",
                "draft_path": str(project.draft_dir),
                "violations": violations,
                "reason": "写入前时序不变量校验未通过（段长/窗口/证据覆盖/变速），"
                          "已阻止保存草稿，请重规划换镜或拼多镜。",
            }

        if bgm_path:
            # Bound BGM by the actual video track, not only probed source time.
            # Some MOV files contain edit lists that JianYing trims on import.
            actual_video_end_us = project.get_track_duration("Video_BRoll")
            b = project.add_audio_safe(staged[bgm_path], start_time=0,
                                       duration=min(self.total_us, actual_video_end_us), track_name="BGM")
            if b is None:
                raise RuntimeError("BGM 添加失败")

        project.save()
        draft_dir = Path(project.draft_dir)
        expected_root = Path(drafts_root).resolve()
        try:
            actual_root = draft_dir.resolve().parent
        except OSError:
            actual_root = draft_dir.parent
        if actual_root != expected_root:
            return {"status": "DRAFT_ROOT_MISMATCH", "draft_path": str(draft_dir),
                    "expected_drafts_root": str(expected_root), "actual_drafts_root": str(actual_root),
                    "reason": "底层写入目录与剪映当前草稿库不一致，已停止上报成功。"}
        if not draft_dir.is_dir():
            return {"status": "DRAFT_NOT_CREATED", "draft_path": str(draft_dir),
                    "reason": "保存后未在剪映当前草稿库发现新草稿目录。"}
        # 需求7：写后独立校验，不通过不上报成功
        #
        # 计数口径必须与**草稿里实际写了几条**一致（`post_write_validate` 是拿这些
        # 数去逐轨比对的，报错了就会 `DRAFT_INVALID`）。三类轨道的写入循环各自不同：
        #   · 视频轨：按 `clips` 逐段加 → 摊平后就是 `len(clips)`，多镜段各占一段；
        #   · 旁白轨：多镜的同一句配音虽然加了 N 条 `AudioSegment`（每段取一片），
        #     但**计数按 clips 走**才与写入一致 —— 每条 clip 只要挂了 tts/audio
        #     就加一条，没有去重；
        #   · 字幕轨：同样按 `clips` 逐条，同句多镜会出 N 条。
        # 所以三类都保持 `clips` 口径。曾经想把旁白/字幕改成「父段落去重」，
        # 那会与写入循环（按 clips）对不上，反而制造 DRAFT_INVALID 假阳性。
        narration_expected = sum(
            1 for c in clips
            if c.tts_path or (c.audio_mode == "file" and c.audio)
        )
        subtitle_expected = sum(1 for c in clips if c.text.strip())
        expected_tracks = {
            "video_segments": len(clips),
            "narration_segments": narration_expected,
            "subtitle_segments": subtitle_expected,
            "bgm_required": bool(bgm_path),
            "bgm_segments": 1,
            "subtitle_style_required": subtitle_expected > 0,
            # 与 core/text_ops.py 的 DEFAULT_SUBTITLE_* 必须逐字一致：写后校验拿这
            # 组数逐段比对 styles[0].size/bold 与 clip.transform，任一处对不上就
            # DRAFT_INVALID。旧值 (8.0 / True / -1000.0) 会让字幕落到画布外，
            # 成片里看不见 —— 见 text_ops 的常量注释。
            "subtitle_font_size": 8.0,
            "subtitle_bold": True,
            "subtitle_transform_x": 0.0,
            "subtitle_transform_y": -0.8,
            "subtitle_strip_punctuation": True,
        }
        issues = draft_safety.post_write_validate(
            draft_dir, expected_tracks=expected_tracks)
        if issues:
            return {"status": "DRAFT_INVALID", "draft_path": str(draft_dir), "issues": issues}
        draft_safety.sync_root_meta(Path(drafts_root), name, duration_us=self.total_us)
        actual_total_us = project.get_track_duration("Video_BRoll") or self.total_us
        intelligent = self.intelligent_mode_authorized()
        voice_rec = getattr(self, "_voice_rec", None)
        selected_voice = getattr(self, "_global_voice", self.DEFAULT_VOICE_KEY)
        try:
            selected_preset = resolve_voice(selected_voice)
            selected_data = selected_preset.to_dict()
            voice_used = {
                "input": os.environ.get("JY_OPERATOR_VOICE", "").strip() or self.m.get("voice"),
                "normalized_name": selected_data.get("label"),
                "speaker_id": selected_data.get("sami"),
                "display_name": selected_data.get("label"),
                "key": selected_data.get("key"),
            }
            if any(c.tts_path for c in clips):
                voice_used["backend"] = "sami"
        except (TypeError, ValueError):
            voice_used = {"input": os.environ.get("JY_OPERATOR_VOICE", "").strip() or self.m.get("voice"), "speaker_id": str(selected_voice)}
        operator_voice_input = os.environ.get("JY_OPERATOR_VOICE", "").strip() or self.m.get("voice")
        if voice_rec:
            voice_selection = {
                "mode": "intelligent_recommended" if intelligent else "recommended",
                "selected": voice_rec[0] if isinstance(voice_rec, list) else voice_rec,
                "alternatives": voice_rec[1:] if isinstance(voice_rec, list) else [],
            }
        elif operator_voice_input is not None and str(operator_voice_input).strip():
            voice_selection = {
                "mode": "explicit",
                "selected": voice_used,
                "alternatives": [],
                "hint": "音色来自运营填写值，并已按官方 tts_speakers.csv 解析；试听与正式 TTS 共用该 speaker_id。",
            }
        else:
            voice_selection = {
                "mode": "default",
                "selected": voice_used,
                "alternatives": [],
                "hint": "普通模式未指定 voice，使用默认音色；如需试听/更换，请先运行 recommend-voice 或 audition，再在 manifest 填 voice。",
            }
        bgm_choice = getattr(self, "_bgm_choice", None)
        audio_guidance = {
            "voice": voice_selection,
            "bgm": (bgm_choice if bgm_choice else {
                "mode": "none",
                "selected": None,
                "hint": "普通模式未指定 bgm，因此未添加背景音乐；可在 manifest 中指定 bgm.path、bgm.music_id 或 {auto:true}。"
            }),
        }
        return {"status": "SUCCESS", "draft_name": name, "draft_path": str(draft_dir),
                "drafts_root": str(expected_root),
                "drafts_root_source": self.env.get("drafts_root_source") if self.env else None,
                "total_s": round(actual_total_us / US, 3), "segments": built,
                # 交付模式必须出现在成功结果里（定案第 9 条）：预览稿能出图，
                # 但下游（回写飞书、分发）要能一眼看出「这份不是正式交付」。
                # 靠结果字段而不是靠文件名猜 —— 文件名是给人看的，会被改。
                "delivery_mode": self.delivery_mode,
                "execution_profile": self.profile.name,
                "bgm": Path(bgm_path).name if bgm_path else None,
                "warnings": self.warnings, "pending": self.pending,
                "voice_recommendation": getattr(self, "_voice_rec", None),
                "voice_used": voice_used,
                "bgm_choice": bgm_choice,
                "audio_selection": audio_guidance,
                "visual_evidence": getattr(self, "_visual_evidence_report", None),
                # 闸门认证状态透到运营侧（定案第 10 条）。键名用 ``coverage`` ——
                # `cli._compact_build_result` 的非 SUCCESS 白名单已含该键，改别的
                # 名字会被白名单挡在外面，运营反而看不到。
                "coverage": self._coverage_report(),
                "draft_open_guidance": [
                    f"在剪映中打开草稿列表，找到“{name}”。",
                    "双击打开；若提示格式转换，确认转换即可。",
                    "若列表未刷新，先退出并重启剪映后再查看。",
                ],
                "storyboard_scores": getattr(self, "_storyboard_scores", [])}

    # ---------- 主流水线 ----------
    def run(self) -> dict:
        self._event("profile_selected", profile=self.profile.__dict__, priority_applied=self.priority_applied)
        if self.m.get("trigger_phrase"):
            self._event("intelligent_mode_triggered",
                        trigger_phrase=self.m.get("trigger_phrase"), mode=self.m.get("mode"))
        env_err = self.self_check()
        if env_err:
            return env_err
        mode_err = self.mode_confirmation_gate()
        if mode_err:
            return mode_err
        self._drafts_root = find_drafts_root(self.editor_root)
        self._clips = clips = self.build_clips()
        length_gate = self._script_segment_length_gate(clips)
        if length_gate:
            return length_gate
        for c in clips:
            if c.visual_missing:
                note = c.visual_missing_note or "缺少可审计画面"
                marker = f"VISUAL_MISSING@{Path(c.video).name}: {note}"
                if marker not in self.preview_uncertified_reasons:
                    self.preview_uncertified_reasons.append(marker)
                self._event("visual_missing_reported", note=note,
                            text=c.text[:48])
        # 交付模式归一化后写回 manifest 一次（定案第 9 条），让引擎内部到处
        # 读到的口径一致。注意**选镜层读不到这一句** —— `shot_analyzer` 在
        # 引擎构造之前就跑完了，它读的是输入 manifest；所以 CLI 必须在自己
        # 那侧就把 `delivery_mode` 放进 manifest（见 `cli.cmd_build`），
        # 这里只做归一化，不是唯一入口。
        self.m["delivery_mode"] = self.delivery_mode
        if not clips:
            return {"status": "ERROR", "error": "没有可用分镜（检查 videos/segments）"}

        gate = self.voiceover_gate(clips)          # 需求4
        if gate:
            return gate
        selection_gate = self.audio_selection_gate(clips)
        if selection_gate:
            return selection_gate

        # Validate explicit voice labels before copying source media or
        # synthesizing TTS.  Unknown/ambiguous JianYing names return a
        # candidate list and never fall back to a different speaker.
        try:
            self._resolved_voice, self._resolved_voice_recommendation = self.decide_voice()
        except ValueError as exc:
            detail = getattr(exc, "voice_detail", None)
            detail = detail if isinstance(detail, dict) else {}
            return {
                "status": "VOICE_SELECTION_REQUIRED",
                "message": str(exc),
                "voice_input": detail.get("voice_input", os.environ.get("JY_OPERATOR_VOICE", "").strip() or str(self.m.get("voice", ""))),
                "voice_candidates": detail.get("candidates", []),
            }

        visual_gate = self.visual_evidence_gate(clips)
        if visual_gate:
            return visual_gate
        duplicate_gate = self._visual_duplicate_gate(clips)
        if duplicate_gate:
            return duplicate_gate

        # Audio choices are a user decision gate. Do it before video staging
        # and capacity admission so an unconfirmed normal-mode task does not
        # copy or analyze the whole source batch first.
        blocker = self._upload_videos_and_check_capacity(clips)
        if blocker:
            return blocker

        pressure = self._background_pressure("before_processing")
        if pressure:
            return pressure
        self._timed("script_review", self.script_review, clips)  # 补2
        global_voice = self._resolved_voice
        rec = self._resolved_voice_recommendation
        self._global_voice = global_voice
        self._voice_rec = rec
        self._timed("stability", self.stabilize, clips)                      # 需求8
        self._timed("storyboard_scoring", self.score_storyboard, clips)
        pressure = self._background_pressure("after_analysis")
        if pressure:
            return pressure
        self._timed("tts", self.synthesize, clips, global_voice)       # 需求2/5
        pressure = self._background_pressure("after_tts")
        if pressure:
            return pressure
        self._timed("audio_video_alignment", self.align_audio_video, clips)              # 需求6/补3
        sync_err = self.validate_audio_video_sync(clips)
        if sync_err:
            return sync_err
        # 1.3.27.1 执行位①：计划解算完成后、写草稿前，跑一次不变量校验。
        plan_gate = self._invariant_gate(clips, stage="plan_complete")
        if plan_gate:
            return plan_gate
        final_visual_gate = self.final_visual_evidence_gate(clips)
        if final_visual_gate:
            return final_visual_gate
        self._timed("transition_planning", self.plan_transitions, clips)               # 需求8
        bgm_path = self._timed("bgm", self.prepare_bgm)              # 需求3
        if not bgm_path:
            return {
                "status": "BGM_REQUIRED",
                "message": "已确认的背景音乐无法下载或处理，未写入草稿。",
                "task_log_hint": "请检查 BGM 直链/本地路径后重新运行。",
            }
        return self._timed("draft_write", self.write_draft, clips, bgm_path)   # 需求7
