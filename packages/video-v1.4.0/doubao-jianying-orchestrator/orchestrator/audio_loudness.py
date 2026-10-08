"""旁白响度归一（通用剪辑处理规则，2026-09-17 用户定案）。

## 要解决的问题

TTS 逐条合成，各条自带不同电平。R3 实测 11 条旁白的 Integrated 响度从
-37.12 到 -21.22 LUFS —— **跨度 15.9 LU**。铺到同一条 Narration 轨上，成片
听起来就是「忽大忽小」。这不是某条视频的特例，是逐条 TTS 的固有属性，所以
按用户定案做成**通用规则**，对每条旁白一律执行。

## 为什么不用 loudnorm

两遍 `loudnorm`（带 measured_* 的线性模式）在真实数据上**不成立**：真峰值已经
到 -2.13 dBTP 的那几条，按 -16 LUFS 反推需要的增益会顶破 -1.5 dBTP 上限，
ffmpeg 于是静默回退到动态模式，输出仍散在 -21.90 ~ -16.02（5.88 LU）。实测见下。

改用「**纯增益 + 限幅**」：先按 Integrated 差值定增益，再用 `alimiter` 把峰值
钉在 TP_LIMIT 上。实测 11 条：

    输入跨度  15.90 LU  (-37.12 .. -21.22)
    输出跨度   1.88 LU  (-17.88 .. -16.00)
    时长漂移  -6.5 ms 恒定（AAC 帧对齐，与素材长短无关）

残余 1.88 LU 来自限幅器对最响几条的削峰，属可接受范围（远低于人耳能察觉的
「忽大忽小」阈值，且比原始 15.9 LU 小一个数量级）。

## 为什么时长必须钉死

-6.5 ms 是 AAC 编码器按 1024 采样帧对齐的结果，**恒定且不随素材长短累积**
（0.762s 与 3.641s 的素材漂移量相同）。虽然极小，但 `media_ops.add_audio_safe`
会按 `phys_duration - src_start` 裁切，末段因此可能短 6.5ms。所以这里用
`apad` + `atrim=duration=D` 把输出**强制**成与源完全等长（D 由 ffprobe 量得，
与 `engine.probe` 同口径），从根上消除这层不确定性。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

# 旁白目标响度：短视频人声常用 -16 LUFS（YouTube/抖音投放口径的常见取值）。
TARGET_LUFS = -16.0
# 真峰值上限：-1.5 dBTP 给编码器留出 inter-sample 余量，避免导出后削顶。
TP_LIMIT_DB = -1.5
# 增益上限：防止把近乎静音的废条放大成底噪（-37 LUFS 那条需要 +21dB）。
MAX_GAIN_DB = 24.0
# 输入低于此值视为废条，不参与归一（放大只会得到噪声）。
MIN_INPUT_LUFS = -50.0


def _loudnorm_summary(ffmpeg: str, src: Path) -> Optional[dict]:
    """跑一遍 loudnorm 的测量模式，取回 JSON 汇总（不改动文件）。"""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(src),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def measure_integrated_lufs(ffmpeg: str, src: Path) -> Optional[float]:
    summary = _loudnorm_summary(ffmpeg, src)
    if not summary:
        return None
    try:
        return float(summary["input_i"])
    except (KeyError, TypeError, ValueError):
        return None


def _probe_duration(ffprobe: str, src: Path) -> Optional[float]:
    """与 `engine.probe` 同口径的时长（ffprobe format=duration，单位秒）。"""
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True)
    try:
        value = float((proc.stdout or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def normalize_narration(src: Path, dst: Path, *, ffmpeg: str = "ffmpeg",
                        ffprobe: str = "ffprobe",
                        target_lufs: float = TARGET_LUFS,
                        tp_limit_db: float = TP_LIMIT_DB,
                        max_gain_db: float = MAX_GAIN_DB) -> dict:
    """把一条旁白归一到 `target_lufs`，峰值钉在 `tp_limit_db`。

    返回测量明细（供报告与复算）。``dst`` 已存在且大小非零时直接复用 —— 与
    `engine._stage` 的幂等思路一致，重跑同一任务不重复编码。
    """
    src = Path(src)
    dst = Path(dst)
    detail: dict = {"src": str(src), "dst": str(dst),
                    "target_lufs": target_lufs, "tp_limit_db": tp_limit_db}
    if dst.exists() and dst.stat().st_size > 0:
        detail["reused"] = True
        detail["out_lufs"] = measure_integrated_lufs(ffmpeg, dst)
        return detail

    summary = _loudnorm_summary(ffmpeg, src)
    if not summary:
        detail["skipped"] = "measure_failed"
        return detail
    try:
        in_lufs = float(summary["input_i"])
        in_tp = float(summary["input_tp"])
    except (KeyError, TypeError, ValueError):
        detail["skipped"] = "measure_unparsable"
        return detail
    detail.update(in_lufs=in_lufs, in_tp=in_tp)
    if in_lufs <= MIN_INPUT_LUFS:
        detail["skipped"] = "below_floor"
        return detail

    gain_db = min(target_lufs - in_lufs, max_gain_db)
    limit = 10 ** (tp_limit_db / 20.0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 限幅器 level=disabled：只削峰，不再叠加自动增益，否则响度会被二次改动。
    af = f"volume={gain_db:.3f}dB,alimiter=limit={limit:.6f}:level=disabled"
    # 时长钉死：apad 补到源长再 atrim 切齐，抵消 AAC 帧对齐造成的 -6.5ms。
    src_duration = _probe_duration(ffprobe, src)
    if src_duration:
        af += f",apad=whole_dur={src_duration:.6f},atrim=duration={src_duration:.6f}"
        detail["pinned_duration_s"] = round(src_duration, 6)
        # 接缝微淡变（用户 2026-09-17 反馈：分镜衔接处旁白不连贯）。逐句 TTS 是
        # 硬切的：起音辅音/尾音截断在镜头切换点上听着像「跳了一下」。20/30ms 的
        # 微淡变在人耳的响度感知之下，但足以消除硬切爆点 —— 这是广播级的标准做法。
        FADE_IN_S, FADE_OUT_S = 0.020, 0.030
        out_start = max(0.0, src_duration - FADE_OUT_S)
        af += f",afade=t=in:st=0:d={FADE_IN_S},afade=t=out:st={out_start:.6f}:d={FADE_OUT_S}"
        detail["fade_in_s"] = FADE_IN_S
        detail["fade_out_s"] = FADE_OUT_S
    proc = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
         str(dst)], capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        detail["skipped"] = "encode_failed"
        detail["stderr"] = (proc.stderr or "")[-400:]
        return detail

    detail["gain_db"] = round(gain_db, 3)
    detail["out_lufs"] = measure_integrated_lufs(ffmpeg, dst)
    if src_duration:
        out_duration = _probe_duration(ffprobe, dst)
        if out_duration:
            detail["out_duration_s"] = round(out_duration, 6)
            detail["duration_delta_ms"] = round((out_duration - src_duration) * 1000, 2)
    return detail
