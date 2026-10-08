"""TTS 批量并发生成：按“文本+音色”哈希增量缓存，避免重复网络合成。"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .platform_env import find_ffmpeg, find_jianying_editor_root
from .timing_contract import AUDIO_PIPELINE_VERSION
from .voice_catalog import VoicePreset, resolve_voice


def _quiet_async_run(coro):
    """运行异步合成，并把底层 TTS 打印的 trace 日志导到 stderr，保证 stdout 只出 JSON。"""
    with contextlib.redirect_stdout(sys.stderr):
        return asyncio.run(coro)


def _import_tts():
    root = find_jianying_editor_root()
    scripts = str(root / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import universal_tts  # type: ignore
    return universal_tts


def _cache_name(text: str, speaker: str, *, provider: str = "sami",
                pipeline: str = AUDIO_PIPELINE_VERSION) -> str:
    """音频缓存键：**只含实际影响音频字节的参数**（用户定案第 2 条）。

    键 = `md5(speaker | provider | pipeline | text)`。

    刻意不包含的东西：
    - `video_speed` —— 变速只作用于**画面**素材，音频字节一个采样都不变，
      把它放进键只会让同一段配音被重复合成、白花钱；
    - `pitch` / `speed` —— 当前 TTS 后端（SAMI）根本不接受这两个参数
      （`universal_tts.generate_voice` 签名里没有它们），加进键是无效字段。

    包含 `pipeline`（`AUDIO_PIPELINE_VERSION`）是因为后处理链一变（采样率、
    容器、重采样、响度处理），同样的文本+音色产出的字节就不同了，必须各归各的缓存。
    """
    h = hashlib.md5(f"{speaker}|{provider}|{pipeline}|{text}".encode("utf-8")).hexdigest()[:12]
    return f"tts_{h}.ogg"


def _audio_cache_is_usable(path: Path) -> bool:
    """Size alone is not enough: reject truncated/corrupt cached OGG files."""
    try:
        if not path.is_file() or path.stat().st_size <= 256:
            return False
        ffprobe = find_ffmpeg()[1]
        if not ffprobe:
            return True
        run = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        if run.returncode != 0:
            return False
        duration = float((json.loads(run.stdout or "{}").get("format") or {}).get("duration") or 0.0)
        return duration > 0.0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def _split_tts_text(text: str, max_chars: int = 48) -> list[str]:
    """Split a long line at natural punctuation without changing its text."""
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[，。！？、；：,.!?;:])", text):
        if not sentence:
            continue
        while len(sentence) > max_chars:
            cut = max_chars
            # Keep punctuation with the preceding chunk when possible.
            for pos in range(max_chars, max(1, max_chars // 2), -1):
                if sentence[pos - 1] in "，。！？、；：,.!?;:":
                    cut = pos
                    break
            pieces.append(sentence[:cut])
            sentence = sentence[cut:]
        if sentence:
            pieces.append(sentence)
    return pieces


async def _recover_with_chunked_tts(utts, text: str, target: Path,
                                    speaker: str, retries: int) -> str | None:
    """Recover a corrupt long SAMI response while keeping the locked speaker."""
    chunks = _split_tts_text(text)
    if len(chunks) <= 1:
        return None
    ffmpeg = find_ffmpeg()[0]
    if not ffmpeg:
        return None
    part_dir = target.parent / f".{target.stem}.parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for index, chunk in enumerate(chunks, 1):
            part = part_dir / f"part_{index:03d}.ogg"
            part.unlink(missing_ok=True)
            path = await utts.generate_voice(
                chunk, str(part), speaker, backend="sami",
                allow_fallback=False, sami_retries=max(1, min(retries, 2)))
            actual = Path(path) if path else part
            if not path or not _audio_cache_is_usable(actual):
                raise RuntimeError(f"分段 {index}/{len(chunks)} 返回不可解码音频")
            parts.append(actual)

        labels = "".join(f"[{i}:a]" for i in range(len(parts)))
        filter_graph = f"{labels}concat=n={len(parts)}:v=0:a=1[a]"
        cmd = [str(ffmpeg), "-y"]
        for part in parts:
            cmd.extend(["-i", str(part)])
        cmd.extend([
            "-filter_complex", filter_graph, "-map", "[a]",
            "-c:a", "opus", "-strict", "-2", "-b:a", "64k", str(target),
        ])
        run = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
        if run.returncode != 0 or not _audio_cache_is_usable(target):
            detail = (run.stderr or run.stdout or "")[-800:]
            raise RuntimeError(f"分段音频合并失败 rc={run.returncode}: {detail}")
        Path(str(target) + ".backend").write_text("sami\n", encoding="utf-8")
        return str(target)
    except Exception:
        # Keep the per-part artifacts for postmortem; they are outside the
        # deterministic cache key and will be reused only after validation.
        return None


async def _synthesize_one(utts, text: str, target: Path, speaker: str,
                          sem: asyncio.Semaphore, retries: int = 2) -> str:
    async with sem:
        # Only reuse a cache carrying an explicit SAMI marker.  Old .ogg.mp3
        # files may have been produced by Edge fallback and have no trustworthy
        # speaker provenance, so they are deliberately ignored.
        marker = Path(str(target) + ".backend")
        if (_audio_cache_is_usable(target) and marker.exists()
                and marker.read_text(encoding="utf-8", errors="ignore").strip() == "sami"):
            return str(target)
        if target.exists() and marker.exists():
            # Preserve the bad generated artifact for diagnosis, but make the
            # deterministic cache key available for a clean regeneration.
            invalid = target.with_name(
                f"{target.name}.invalid.{int(time.time() * 1000)}")
            try:
                target.replace(invalid)
                marker.unlink(missing_ok=True)
            except OSError:
                pass
        last_err = None
        for attempt in range(retries):
            try:
                # Formal builds use the exact operator-selected JianYing
                # speaker.  A provider fallback (for example Edge-TTS) would
                # make the audible voice differ from the selected label, so
                # fail closed and let the task be retried with a valid SAMI
                # backend instead.
                path = await utts.generate_voice(
                    text, str(target), speaker, backend="sami",
                    allow_fallback=False, sami_retries=retries)
                if path and _audio_cache_is_usable(Path(path)):
                    Path(str(path) + ".backend").write_text("sami\n", encoding="utf-8")
                    return path
                last_err = RuntimeError(
                    "SAMI 返回文件但 ffprobe 无法解码（可能是长文本响应损坏）")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            await asyncio.sleep(0.3 * (attempt + 1))
        recovered = await _recover_with_chunked_tts(utts, text, target, speaker, retries)
        if recovered:
            return recovered
        raise RuntimeError(f"TTS 生成失败[{speaker}] {text[:20]}…: {last_err}")


async def _gather(items: list[dict], out_dir: Path, preset: VoicePreset,
                  concurrency: int) -> dict:
    utts = _import_tts()
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    tasks = []
    for it in items:
        # 音频缓存键只看「文本+音色+后端+后处理版本」。item 上的 video_speed /
        # source_start 只影响**画面**，绝不能进键（用户定案第 2 条）。
        target = out_dir / _cache_name(it["text"], preset.sami)
        tasks.append(_synthesize_one(utts, it["text"], target, preset.sami, sem))
    paths = await asyncio.gather(*tasks)
    return {it["key"]: p for it, p in zip(items, paths)}


def _validate_items(items: list[dict]) -> None:
    """TTS 之前先挡住坏输入（B 步多镜会带 source_start 进来）。

    发电机开动之前把参数筛一遍：负的 source_start 说明规划层算错了窗口，
    这时候去调网络合成只会浪费额度，还让错误以「音频已生成但切片非法」的形式
    渗到下游。直接抛，交编排层重规划。
    """
    for it in items:
        if not it.get("key"):
            raise ValueError("TTS item 缺少 key")
        if not str(it.get("text") or "").strip():
            raise ValueError(f"TTS item[{it.get('key')}] 文本为空")
        for field, unit in (("source_start", 1.0), ("source_start_us", 1.0)):
            if field in it and it[field] is not None and float(it[field]) < 0:
                raise ValueError(
                    f"TTS item[{it.get('key')}] 的 {field} 为负"
                    f"（{it[field]}），窗口求解结果非法，交编排层重规划")


def generate_batch(items: list[dict], out_dir: Path, voice_spec=None, *,
                   concurrency: int = 4) -> tuple[VoicePreset, dict]:
    """并发生成一批口播。

    items: [{"key": "s3", "text": "..."}]；同文本+同音色直接命中缓存。
    返回 (所用音色 preset, {key: 音频路径})。
    """
    _validate_items(items)
    preset = resolve_voice(voice_spec)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # 调用方已在事件循环中时，返回协程由其 await（少见）
        return preset, _gather(items, out_dir, preset, concurrency)
    paths = _quiet_async_run(_gather(items, out_dir, preset, concurrency))
    return preset, paths


async def _audition_one(utts, text: str, preset: VoicePreset, out_dir: Path,
                        ffmpeg: str, sem: asyncio.Semaphore,
                        reuse: bool = True) -> dict:
    """单个音色：SAMI 合成 ogg 后转 mp3，便于任意播放器试听。

    文件名只与音色绑定（固定样词下跨次复用、不重复合成）。
    """
    import subprocess
    mp3 = out_dir / f"{preset.label}_{preset.gender}.mp3"
    marker = Path(str(mp3) + ".backend")
    if (reuse and mp3.exists() and mp3.stat().st_size > 1024 and marker.exists()
            and marker.read_text(encoding="utf-8", errors="ignore").strip() == "sami"):
        return {"key": preset.key, "label": preset.label, "ok": True,
                "file": str(mp3), "backend": "cache_sami",
                "gender": preset.gender, "age": getattr(preset, "age", ""),
                "timbre": getattr(preset, "timbre", "")}
    async with sem:
        ogg = out_dir / f"{preset.key}.ogg"
        path, backend = await utts.generate_voice_with_meta(
            text, str(ogg), preset.sami, backend="sami",
            allow_fallback=False, sami_retries=1)
        if not path:
            return {"key": preset.key, "label": preset.label, "ok": False}
        subprocess.run([ffmpeg, "-y", "-i", str(path), "-codec:a", "libmp3lame",
                        "-q:a", "4", str(mp3)], capture_output=True)
        if mp3.exists():
            marker.write_text("sami\n", encoding="utf-8")
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        return {"key": preset.key, "label": preset.label, "ok": mp3.exists(),
                "file": str(mp3) if mp3.exists() else "", "backend": backend,
                "gender": preset.gender, "age": getattr(preset, "age", ""),
                "timbre": getattr(preset, "timbre", "")}


def generate_audition(script: str | None = None, out_dir: Path | str = ".voice_audition",
                      *, topn: int = 5, gender: str | None = None,
                      keys: list[str] | None = None, all_voices: bool = False,
                      concurrency: int = 4) -> list[dict]:
    """按脚本/剧情动态推荐并生成试听：默认固定样词 + 相关性 Top5（上限10）。

    - keys：手动指定音色；all_voices=True：全库（一般不用）；
    - 否则对全量库按剧情相关性排序，多取 3 个用于失效补位，最终返回前 topn 个可播放样本。
    """
    from .platform_env import find_ffmpeg
    from .voice_catalog import AUDITION_TEXT, recommend_voices, resolve_voice
    ffmpeg = find_ffmpeg()[0]
    ffmpeg = str(ffmpeg) if ffmpeg else "ffmpeg"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    utts = _import_tts()

    topn = max(1, min(int(topn), 10))
    if keys:
        presets = [resolve_voice(k) for k in keys]
    elif all_voices:
        from . import voice_library as vl
        presets = [resolve_voice(v.speaker_id) for v in vl.load_library()]
    else:
        # 多取若干作为失效补位候选
        recs = recommend_voices(script or "", topk=min(topn + 3, 10),
                                persona=gender or "")
        presets = [resolve_voice(r["sami"]) for r in recs]

    sem = asyncio.Semaphore(concurrency)

    async def _synth(group: list):
        return await asyncio.gather(*[
            _audition_one(utts, AUDITION_TEXT, p, out_dir, ffmpeg, sem, True)
            for p in group])

    # 两阶段：先合成前 topn；有失效才动用补位候选，避免多生成、零残留
    first = presets[:topn]
    results = [r for r in _quiet_async_run(_synth(first)) if r.get("ok")]
    ci = topn
    while len(results) < topn and ci < len(presets):
        nxt = presets[ci:ci + 1]
        results += [r for r in _quiet_async_run(_synth(nxt)) if r.get("ok")]
        ci += 1

    ok_list = results[:topn]
    for i, r in enumerate(ok_list, 1):
        r["order"] = i
    return ok_list


def VOICE_MATRIX():
    from .voice_catalog import VOICE_PRESETS
    return VOICE_PRESETS
