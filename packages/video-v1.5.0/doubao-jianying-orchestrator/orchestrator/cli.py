"""命令行入口：环境自检 / 音色与 BGM 推荐 / 扫描 / 生成草稿。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import (
    bgm_selector,
    capacity,
    script_polish,
    semantic_gate,
    shot_analyzer,
    timing_contract,
    voice_catalog,
    voice_tts,
)
from .engine import OrchestrationEngine
from .platform_env import clear_environment_cache, environment_report
from .task_log import TaskAlreadyRunningError, TaskLogger


def _read(path: Path) -> dict:
    # Accept UTF-8 with or without BOM, as Windows PowerShell 5's
    # `Set-Content -Encoding UTF8` emits a BOM by default.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _emit(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def _compact_build_result(result: dict) -> dict:
    """Keep chat output minimal; full evidence remains in task logs."""
    status = result.get("status")
    if status == "AUDIO_SELECTION_REQUIRED":
        # The chat host renders preview_file entries as playable attachments.
        # Keep stdout deliberately minimal so no implementation details leak
        # into the operator's selection conversation.
        return {
            "voice_auditions": result.get("voice_auditions", []),
            "bgm_auditions": result.get("bgm_auditions", []),
            "reply_format": result.get("reply_format", "音色 <序号>，BGM <序号>"),
        }
    if status != "SUCCESS":
        keys = ("status", "terminal_state", "failure_class", "failure_code",
                "retryable", "safe_to_continue", "next_action", "run_id",
                "pipeline_version", "message", "problems", "reason", "error", "missing",
                "voice_input", "voice_candidates", "voice_auditions", "bgm_candidates", "bgm_auditions",
                "pending_items", "coverage", "report", "confirmation", "task_id", "task_log")
        return {k: result[k] for k in keys if k in result}
    draft_path = result.get("draft_path")
    artifacts = []
    if draft_path:
        p = Path(str(draft_path))
        if p.is_dir():
            for name in ("draft_info.json", "draft_meta_info.json", "draft_settings", "key_value.json"):
                if (p / name).is_file():
                    artifacts.append(name)
            media_count = sum(1 for child in (p / "media").iterdir() if child.is_file()) if (p / "media").is_dir() else 0
            if media_count:
                artifacts.append(f"media/ ({media_count} files)")
    # This exact surface is the operator-facing success contract. Durations,
    # chosen settings, warnings and all diagnostic structure remain in
    # result.json/events.jsonl and must not be rendered into the conversation.
    payload = {
        "草稿名称": result.get("draft_name", ""),
        "草稿路径": str(draft_path or ""),
        "草稿文件": artifacts,
        "生成日志": result.get("task_log", ""),
    }
    # 唯一允许进成功合约的例外：**这份草稿不是正式交付**（定案第 9 条）。
    # 预览稿和有未认证理由的稿子，打开来跟正式稿一模一样，运营无从分辨 ——
    # 只看草稿文件是看不出来的，所以必须写在结果里。形状与已有合约一致：
    # 正式且已认证时这几个键**完全不出现**，老运营侧读法不受影响。
    coverage = result.get("coverage")
    if isinstance(coverage, dict) and coverage:
        if result.get("delivery_mode") == "preview":
            payload["交付模式"] = "preview（预览稿，不得用于正式交付）"
        cert = str(coverage.get("certification") or "").strip().upper()
        reasons = coverage.get("uncertified_reasons")
        if (cert and cert != "CERTIFIED") or reasons:
            payload["认证状态"] = cert or "UNCERTIFIED"
        if reasons:
            payload["未认证原因"] = reasons
    return payload


def _compact_control_result(result: dict) -> dict:
    """Keep recovery/preflight stdout actionable; retain detail in receipts."""
    keys = (
        "status", "terminal_state", "failure_class", "failure_code", "retryable",
        "safe_to_continue", "next_action", "task_id", "run_id", "pipeline_version",
        "parent_run_id", "recovery", "recovery_stage", "message", "error", "missing",
        "analysis", "matched_manifest", "matched_manifest_sha256", "draft_path", "task_log",
    )
    return {key: result[key] for key in keys if key in result}


def cmd_env(_args) -> int:
    return _emit(environment_report())


def cmd_setup_ffmpeg(args) -> int:
    """公司禁装 ffmpeg 时，自动下载绿色静态构建到 bin/ 并复验（不安装、不写注册表）。"""
    from .ffmpeg_bootstrap import bootstrap_ffmpeg
    with contextlib.redirect_stdout(sys.stderr):
        result = bootstrap_ffmpeg(force=bool(getattr(args, "force", False)))
    # A successful bootstrap changes the paths discovered by the cached report.
    # Invalidate even after a failed attempt so a manually supplied binary is visible.
    clear_environment_cache()
    code = 0 if result.get("status") in ("READY", "ALREADY_READY") else 1
    return _emit(result, code)


def cmd_list_voices(_args) -> int:
    return _emit({"voices": voice_catalog.list_voices()})


def cmd_recommend_voice(args) -> int:
    rec = voice_catalog.recommend_voices(args.text or "", style=args.style or "",
                                         persona=args.persona or "", topk=args.topk)
    return _emit({"recommended": rec[0] if rec else None, "alternatives": rec,
                  "hint": "首选为自动匹配；如需更换，把 voice 设为候选 key 后重新生成"})


def cmd_audition(args) -> int:
    """按脚本/剧情动态推荐 Top N（默认5）并用固定样词生成试听，先听再选。"""
    keys = [k.strip() for k in args.keys.split(",") if k.strip()] if args.keys else None
    out_dir = Path(args.out_dir).expanduser().resolve()
    samples = voice_tts.generate_audition(
        script=args.script or "", out_dir=out_dir, topn=args.topn,
        gender=args.gender or None, keys=keys, all_voices=args.all)
    ok = all(s.get("ok") for s in samples)
    return _emit({"hint": "按 order 逐个播放试听，选定后把该 key(speaker id) 填到 manifest 的 voice；都不合适可加大 --topn(上限10)、加 --gender，或让助手从全库再找",
                  "samples": samples}, 0 if samples and ok else 1)


def cmd_review_script(args) -> int:
    """口播确认单：逐句字数/预估时长/能否塞进画面 + 衔接覆盖问题。"""
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    eng = OrchestrationEngine(manifest, input_path.parent, Path(args.report_dir).resolve())
    full = str(manifest.get("full_script", ""))
    segs = manifest.get("segments", [])
    with contextlib.redirect_stdout(sys.stderr):
        if segs:
            texts, avails = [], []
            for s in segs:
                texts.append(str(s.get("text", s.get("caption", ""))))
                avail = 0.0
                if s.get("video"):
                    try:
                        dur = eng.probe(eng.resolve(s["video"]))["duration"]
                        avail = max(dur - float(s.get("source_start", s.get("in", 0.0))), 0.0)
                    except Exception:
                        avail = 0.0
                avails.append(avail)
        else:
            texts = [str(c) for c in manifest.get("captions", [])]
            avails = None
    cps = float(manifest.get("speech_cps", 4.6))
    conf = script_polish.build_confirmation(full, texts, avails, cps=cps)
    visual_segments = segs if segs else [{"text": t} for t in texts]
    visual = semantic_gate.evaluate_segments(
        visual_segments,
        require_explicit=bool(manifest.get("visual_evidence_required", True)),
    )
    if segs:
        # review-script performs the same source-frame/evidence-interval gate
        # as build, before any media upload or TTS.  Final-window coverage is
        # added by build after stability and audio-driven trimming.
        try:
            eng._raw_segments = segs
            eng.visual_evidence_gate(eng.build_clips())
            visual = getattr(eng, "_visual_evidence_report", visual)
        except Exception as exc:  # review must remain machine-readable
            visual = {**visual,
                      "ok": False,
                      "issues": list(visual.get("issues", [])) + [{
                          "type": "visual_evidence_review_error",
                          "message": str(exc),
                      }]}
    conf["visual_evidence"] = visual
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "visual_evidence_report.json").write_text(
        json.dumps(visual, ensure_ascii=False, indent=2), encoding="utf-8")
    code = 0 if conf["review"]["ok"] and visual["ok"] and all(
        r["fit"] in ("ok", "unknown") for r in conf["per_sentence"]) else 1
    return _emit(conf, code)


def cmd_list_library(args) -> int:
    """浏览剪映全量音色库（默认164），支持性别/关键词筛选。"""
    from . import voice_library as vl
    voices = vl.load_library()
    gender = {"男": "male", "女": "female", "童": "child"}.get(args.gender or "", args.gender or None)
    rows = []
    for v in voices:
        if gender and v.gender != gender:
            continue
        if args.kw and args.kw not in v.name and args.kw not in v.speaker_id:
            continue
        rows.append(v.to_dict())
    return _emit({"total_library": len(voices), "shown": len(rows), "voices": rows})


def cmd_recommend_bgm(args) -> int:
    cands = bgm_selector.select_bgm(args.mood or "", music_id=args.id or "",
                                    topk=args.topk, min_duration=args.min_dur,
                                    script=args.script or "")
    moods = cands[0]["moods"] if cands else bgm_selector.detect_bgm_moods(args.script or "")
    return _emit({"detected_moods": moods, "candidates": cands,
                  "hint": "基于脚本/剧情对616首曲库相关性排序取Top；选定后把 bgm 设为 {\"music_id\": ...} 或 {\"path\": 本地文件}，无直链的需在剪映内同步后指定 path"})


def cmd_scan(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    eng = OrchestrationEngine(manifest, input_path.parent,
                              Path(args.report_dir).resolve())
    with contextlib.redirect_stdout(sys.stderr):  # 引擎/底层 print 导到 stderr，stdout 只留 JSON
        err = eng.self_check()
        if err:
            return _emit(err, 1)
        meta = {"videos": [], "audios": []}
        clips = eng.build_clips()
        seen_v, seen_a = set(), set()
        for clip in clips:
            if clip.video not in seen_v:
                seen_v.add(clip.video)
                meta["videos"].append(eng.probe(clip.video))
            if clip.audio and clip.audio not in seen_a:
                seen_a.add(clip.audio)
                meta["audios"].append(eng.probe(clip.audio))
    Path(args.report_dir).mkdir(parents=True, exist_ok=True)
    out = Path(args.report_dir) / "asset_metadata.json"
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return _emit({"status": "SCANNED", "metadata": str(out)})


def _run_shot_analysis(manifest: dict, input_path: Path, report_dir: Path) -> dict:
    """Create or consume task-local shot candidates; never writes a library."""
    report_dir.mkdir(parents=True, exist_ok=True)
    analysis_file = manifest.get("shot_analysis_file")
    if analysis_file:
        path = Path(str(analysis_file)).expanduser()
        path = (path if path.is_absolute() else input_path.parent / path).resolve()
        try:
            return _read(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "pending_items": [{"type": "SHOT_ANALYSIS_FILE_INVALID", "message": str(exc)}]}
    env = environment_report(refresh=True)
    analysis = shot_analyzer.analyze_manifest(
        manifest, input_path.parent, report_dir,
        ffmpeg=env.get("ffmpeg"), ffprobe=env.get("ffprobe"))
    # Automatic vision is the default for temporary task material.  The raw
    # extractor remains available through ``analyze-shots --no-vision`` for
    # diagnostics, but build must never ask the outer Codex turn to hand-fill
    # semantic labels.
    if not bool(manifest.get("vision_analysis", True)):
        return analysis
    from . import vision_analyzer
    return vision_analyzer.enrich_with_vision(manifest, analysis, report_dir,
                                              ffmpeg=env.get("ffmpeg"))


_PROBE_CACHE: dict = {}


def _audio_probe():
    """返回 ffprobe 时长探测函数；没有 ffprobe 时返回 None（调用方显式降级）。"""
    if "probe" in _PROBE_CACHE:
        return _PROBE_CACHE["probe"]
    from .platform_env import find_ffmpeg
    ffprobe = find_ffmpeg()[1]
    if not ffprobe:
        _PROBE_CACHE["probe"] = None
        return None

    def probe(path: str) -> dict:
        out = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "ffprobe 读取失败").strip()[:200])
        data = json.loads(out.stdout or "{}")
        return {"duration": float((data.get("format") or {}).get("duration") or 0.0)}

    _PROBE_CACHE["probe"] = probe
    return probe


def _lock_voice_once(manifest: dict) -> tuple[object | None, dict | None]:
    """全流程唯一的音色决议（用户 2026-09-16 定案第 1 条）。

    manifest 已有 ``voice`` → 直接解析使用；为空 → 只调**一次**现有推荐器并把
    结果回写锁定到 manifest。解析失败**不静默换音色**，返回
    ``VOICE_SELECTION_REQUIRED`` 候选清单交运营选择。
    """
    sentences = timing_contract.split_sentences(manifest)
    script = " ".join(timing_contract.sentence_text(s) for s in sentences).strip()
    try:
        preset = voice_catalog.resolve_voice_once(
            None, script=script, manifest=manifest, persist=True)
    except voice_catalog.VoiceResolutionError as exc:
        detail = getattr(exc, "voice_detail", None)
        detail = detail if isinstance(detail, dict) else {}
        return None, {
            "status": "VOICE_SELECTION_REQUIRED",
            "message": str(exc),
            "voice_input": detail.get("voice_input") or str(manifest.get("voice", "")),
            "voice_candidates": detail.get("candidates", []),
        }
    return preset, None


def _pre_synthesize_narration(manifest: dict, report_dir: Path, preset) -> dict:
    """选镜**之前**拿到逐句真实配音时长（定案第 3 条）。

    切句口径与规划层共用 `timing_contract.split_sentences`（否则真值表的键对不
    上段落）；合成结果落 `narration_timing.json`，引擎 `synthesize` 因为缓存名
    只含「文本+音色+后端+后处理版本」会命中同一批文件，**不额外花钱**。

    单句合成失败不阻断：该句显式降级为 ``timing_certainty="estimated"``，
    并随段落一路带进成片报告，绝不在正常路径上冒充真值。
    """
    sentences = timing_contract.split_sentences(manifest)
    rows: list[dict] = []
    tts_items: list[dict] = []
    for i, s in enumerate(sentences, 1):
        key = timing_contract.sentence_key(s, i)
        text = timing_contract.sentence_text(s)
        seg = s if isinstance(s, dict) else {}
        declared_audio = str(seg.get("audio") or "").strip()
        declared_us = int(seg.get("audio_duration_us") or 0)
        rows.append({
            "key": key, "text": text, "tts_path": declared_audio,
            "audio_duration_us": declared_us,
            "timing_certainty": str(seg.get("timing_certainty") or ""),
        })
        # 已带音频文件/已声明时长的段落不再合成：段内参数先于本次合成。
        if text and not declared_audio and declared_us <= 0:
            tts_items.append({"key": key, "text": text})

    paths: dict[str, str] = {}
    tts_error = ""
    if tts_items:
        try:
            _, paths = voice_tts.generate_batch(
                tts_items, report_dir / "tts", voice_spec=preset, concurrency=4)
        except Exception as exc:  # noqa: BLE001 —— 合成失败只降级，不能拦住选镜
            tts_error = f"{type(exc).__name__}: {exc}"
    for row in rows:
        resolved = paths.get(row["key"])
        if resolved:
            row["tts_path"] = resolved

    timing = timing_contract.narration_timing(rows, ffprobe=_audio_probe(), manifest=manifest)
    timing["tts_error"] = tts_error
    timing["tts_attempted"] = len(tts_items)
    timing["voice"] = str(manifest.get("voice", ""))
    (report_dir / "narration_timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")
    return timing


def _temporary_match(manifest: dict, input_path: Path, report_dir: Path,
                     narration_timing: dict | None = None) -> tuple[dict, dict | None]:
    analysis = _run_shot_analysis(manifest, input_path, report_dir)
    if not analysis.get("shots"):
        return manifest, {"status": "SHOT_MATCH_BLOCKED", "message": "当前商品没有可分析的素材镜头。",
                          "pending_items": analysis.get("pending_items", []),
                          "report": str(report_dir / "shot_candidates.json")}
    from . import vision_analyzer
    vision_meta = analysis.get("vision_analysis")
    vision_meta = vision_meta if isinstance(vision_meta, dict) else {}
    if not analysis.get("semantic_matches") and vision_meta.get("errors") == []:
        analysis = vision_analyzer.match_with_codex(manifest, analysis, report_dir)
    matched = shot_analyzer.match_manifest(
        manifest, analysis, topk=int(manifest.get("shot_topk", 3) or 3),
        auto_accept_top1=bool(manifest.get("auto_accept_top1", True)),
        narration_timing=narration_timing)
    (report_dir / "shot_match_report.json").write_text(
        json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
    # DEMO_ACTIONS V0.2 §7（用户定案）：素材缺口单独落盘，交付前必须可见 ——
    # 不允许只躺在 shot_match_report.json 里等人翻。
    if matched.get("material_gaps"):
        (report_dir / "material_gaps.json").write_text(
            json.dumps({
                "count": matched.get("material_gap_count", 0),
                "claims": matched.get("material_gap_claims", []),
                "gaps": matched["material_gaps"],
                "gate_coverage": matched.get("claim_gate_coverage", {}),
                "policy": matched.get("claim_gate_policy", {}),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not matched.get("ok"):
        return manifest, {"status": "SHOT_MATCH_BLOCKED",
                          "message": "当前商品素材已切成候选镜头，但逐句匹配仍缺少可审计的画面描述、卖点证据或动作完成确认。",
                          "pending_items": matched.get("pending_items", []),
                          "recommendations": matched.get("recommendations", []),
                          "analysis_report": str(report_dir / "shot_candidates.json"),
                          "match_report": str(report_dir / "shot_match_report.json"),
                          "analysis_prompt": str(report_dir / "shot_analysis_prompt.md")}
    return matched["manifest"], None


def cmd_build(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    trigger_phrase = getattr(args, "trigger_phrase", "")
    if trigger_phrase:
        manifest = dict(manifest)
        manifest["mode"] = "intelligent"
        manifest["trigger_phrase"] = trigger_phrase
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    # 交付模式（定案第 9 条）必须**在选镜之前**就写进 manifest：`_temporary_match`
    # 里的 `match_manifest` 会读它决定「窗口无解时是报缺口还是先出占位画面」。
    # 引擎构造之后才注入就晚了 —— 那时整句已经被判成 SHOT_TIMING_INFEASIBLE，
    # 草稿层根本没有机会出图。这里是唯一入口，引擎侧只做归一化与读取。
    mode = str(getattr(args, "delivery_mode", "") or "formal").strip().lower()
    manifest = dict(manifest)
    manifest["delivery_mode"] = "preview" if mode == "preview" else "formal"
    try:
        logger = TaskLogger(report_dir, manifest,
                            parent_run_id=str(manifest.get("parent_run_id") or "").strip() or None)
    except TaskAlreadyRunningError as exc:
        return _emit({
            "status": "TASK_ALREADY_RUNNING",
            "terminal_state": "BLOCKED",
            "failure_class": "environment",
            "failure_code": "TASK_ALREADY_RUNNING",
            "retryable": False,
            "safe_to_continue": False,
            "next_action": "manual_review",
            "message": str(exc),
        }, 1)
    shot_match_report = None

    def finalize(result: dict) -> dict:
        if shot_match_report and shot_match_report.is_file():
            result.setdefault("shot_match_report", str(shot_match_report))
        return logger.finalize(result)

    # Do the cheap, deterministic checks before TTS or vision work.  A missing
    # source or unusable runtime must stop this run and never become a generic
    # retryable model failure.
    missing_sources = [str(path) for path in _manifest_sources(manifest, input_path.parent)
                       if not path.is_file()]
    if missing_sources:
        result = finalize({
            "status": "ENV_ERROR",
            "failure_class": "environment",
            "message": "输入素材不存在，未开始视觉分析或写稿。",
            "missing": missing_sources,
        })
        return _emit(_compact_build_result(result), 1)
    env = environment_report(refresh=True)
    if not env.get("ready"):
        result = finalize({
            "status": "ENV_ERROR",
            "failure_class": "environment",
            "message": "运行环境预检未通过，未开始视觉分析或写稿。",
            "problems": [
                *(["缺少 jianying-editor wrapper"] if not env.get("wrapper_available") else []),
                *(["缺少 pyJianYingDraft vendor"] if not env.get("pyjianyingdraft_vendor") else []),
                *(["缺少 ffmpeg/ffprobe"] if not (env.get("ffmpeg") and env.get("ffprobe")) else []),
                *(["剪映草稿目录不可用"] if not env.get("drafts_root_exists") or env.get("drafts_root_access_issues") else []),
                *(["当前外部 Skill 包未通过校验"]
                  if not (env.get("active_package") or {}).get("active_external_package") else []),
                *list((env.get("python_deps") or {}).get("missing_required") or []),
            ],
            "environment": env,
        })
        return _emit(_compact_build_result(result), 1)

    # 音色先锁一次并回写 manifest：全流程只在这里决议音色，引擎后续只读锁。
    # 必须在 TTS 之前，否则「按哪个音色合成」和「按哪个音色选镜」可能不是同一个。
    locked_voice, voice_blocker = _lock_voice_once(manifest)
    if voice_blocker:
        voice_blocker["report_dir"] = str(report_dir)
        return _emit(_compact_build_result(finalize(voice_blocker)), 1)
    try:
        if manifest.get("temporary_material_analysis") or manifest.get("auto_match_shots"):
            # 选镜之前先跑一遍 TTS 拿逐句真实时长：规划层据此求解窗口，
            # 装不下的镜头在**规划阶段**就被判 SHOT_TIMING_INFEASIBLE，
            # 而不是拖到 write_draft 才 AUDIO_VIDEO_MISMATCH。
            narration_timing = _pre_synthesize_narration(manifest, report_dir, locked_voice)
            manifest, blocker = _temporary_match(manifest, input_path, report_dir,
                                                 narration_timing=narration_timing)
            shot_match_report = report_dir / "shot_match_report.json"
            if blocker:
                blocker["report_dir"] = str(report_dir)
                return _emit(_compact_build_result(finalize(blocker)), 1)
    except Exception as exc:  # noqa: BLE001
        logger.event("prebuild_exception", error_type=type(exc).__name__, error=str(exc))
        result = finalize({
            "status": "ERROR",
            "failure_class": "unknown",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return _emit(_compact_build_result(result), 1)

    eng = OrchestrationEngine(manifest, input_path.parent, report_dir,
                              strict_script=args.strict_script, logger=logger)
    try:
        # Engine and dependency chatter belongs to the task log, not the chat.
        # The only terminal payload is _compact_build_result below.
        runtime_log = logger.task_dir / "engine_output.log"
        with runtime_log.open("a", encoding="utf-8") as runtime_out, \
                contextlib.redirect_stdout(runtime_out), \
                contextlib.redirect_stderr(runtime_out):
            result = eng.run()
    except Exception as exc:  # noqa: BLE001
        logger.event("task_exception", error_type=type(exc).__name__, error=str(exc))
        result = {"status": "ERROR", "error": str(exc), "error_type": type(exc).__name__}
    result = finalize(result)
    code = 0 if result.get("status") == "SUCCESS" else 1
    return _emit(_compact_build_result(result), code)


def cmd_analyze_shots(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    report_dir = Path(args.report_dir).expanduser().resolve()
    env = environment_report()
    result = shot_analyzer.analyze_manifest(
        manifest, input_path.parent, report_dir,
        ffmpeg=env.get("ffmpeg"), ffprobe=env.get("ffprobe"),
        threshold=args.threshold, min_shot_s=args.min_shot_s)
    if not args.no_vision:
        from . import vision_analyzer
        result = vision_analyzer.enrich_with_vision(manifest, result, report_dir,
                                                    ffmpeg=env.get("ffmpeg"))
    result.update({"status": "SHOT_ANALYSIS_READY",
                   "candidate_file": str(report_dir / "shot_candidates.json"),
                   "prompt_file": str(report_dir / "shot_analysis_prompt.md")})
    return _emit(result, 0 if result.get("shots") else 1)


def cmd_match_shots(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    report_dir = Path(args.report_dir).expanduser().resolve()
    analysis_path = Path(args.analysis).expanduser().resolve()
    try:
        analysis = _read(analysis_path)
    except (OSError, ValueError) as exc:
        return _emit({"status": "SHOT_ANALYSIS_FILE_INVALID", "message": str(exc)}, 1)
    result = shot_analyzer.match_manifest(manifest, analysis, topk=args.topk,
                                          auto_accept_top1=not args.require_selection)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "shot_match_report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("ok"):
        matched_path = report_dir / "matched_manifest.json"
        matched_path.write_text(json.dumps(result["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
        result["matched_manifest"] = str(matched_path)
    result["status"] = "SHOT_MATCH_READY" if result.get("ok") else "SHOT_MATCH_BLOCKED"
    result["match_report"] = str(report_path)
    return _emit(result, 0 if result.get("ok") else 1)


def cmd_diagnose_timeline(args) -> int:
    from .diagnostics import diagnose_timeline
    result = diagnose_timeline(Path(args.draft), expect_external_package=bool(args.expect_external_package))
    if args.report_dir:
        out = Path(args.report_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        path = out / "timeline_diagnosis.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["report"] = str(path)
    return _emit(result, 0 if result.get("status") == "DIAGNOSIS_OK" else 1)


def cmd_recover(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        return _emit({"status": "RECOVERY_MANIFEST_REQUIRED", "message": "恢复必须提供完整 manifest 文件。"}, 1)
    manifest = _read(input_path)
    if not (manifest.get("segments") or manifest.get("videos") or
            manifest.get("material_sources")):
        return _emit({"status": "RECOVERY_MANIFEST_REQUIRED",
                      "message": "manifest 缺少 material_sources、segments 或 videos，无法安全恢复。"}, 1)
    stage = str(getattr(args, "stage", "draft_write") or "draft_write").strip().lower()
    parent_run_id = str(manifest.get("parent_run_id") or manifest.get("run_id") or
                        manifest.get("task_id") or "").strip() or None
    manifest = dict(manifest)
    manifest["parent_run_id"] = parent_run_id
    manifest["recovery_stage"] = stage
    suffix = time.strftime("%Y%m%d_%H%M%S")
    manifest["draft_name"] = f"{manifest.get('draft_name') or '豆包剪映编排草稿'}_recovery_{suffix}"
    manifest["force_overwrite"] = False
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        logger = TaskLogger(report_dir, manifest,
                            parent_run_id=str(manifest.get("parent_run_id") or "").strip() or None)
    except TaskAlreadyRunningError as exc:
        return _emit({
            "status": "TASK_ALREADY_RUNNING",
            "terminal_state": "BLOCKED",
            "failure_class": "environment",
            "failure_code": "TASK_ALREADY_RUNNING",
            "retryable": False,
            "safe_to_continue": False,
            "next_action": "manual_review",
            "message": str(exc),
        }, 1)

    def finalize(result: dict) -> dict:
        result["recovery"] = True
        result["recovery_stage"] = stage
        return logger.finalize(result)

    if stage == "preflight":
        missing = [str(path) for path in _manifest_sources(manifest, input_path.parent)
                   if not path.is_file()]
        env = environment_report(refresh=True)
        if missing or not env.get("ready"):
            result = finalize({
                "status": "ENV_ERROR",
                "failure_class": "environment",
                "message": "恢复前预检未通过，未进入生成阶段。",
                "missing": missing,
                "environment": env,
            })
            return _emit(_compact_control_result(result), 1)
        result = finalize({
            "status": "PREFLIGHT_READY",
            "message": "恢复前预检通过；尚未执行生成。",
            "environment": env,
        })
        return _emit(_compact_control_result(result), 0)

    if stage == "ui_acceptance":
        draft = str(getattr(args, "draft", "") or "").strip()
        if not draft:
            result = finalize({
                "status": "RECOVERY_DRAFT_REQUIRED",
                "failure_class": "ui",
                "message": "ui_acceptance 阶段必须提供 --draft，恢复不会重新做视觉分析或写稿。",
            })
            return _emit(_compact_control_result(result), 1)
        from .diagnostics import diagnose_timeline
        try:
            diagnosis = diagnose_timeline(Path(draft), expect_external_package=True)
        except Exception as exc:  # noqa: BLE001
            result = finalize({
                "status": "ERROR",
                "failure_class": "ui",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            return _emit(_compact_control_result(result), 1)
        diagnosis_status = str(diagnosis.get("status") or "DESKTOP_IMPORT_BLOCKED")
        diagnosis_ok = diagnosis_status == "DIAGNOSIS_OK"
        if diagnosis_ok:
            diagnosis.update({
                "status": "UI_ACCEPTANCE_PENDING",
                "message": "草稿诊断已完成；剪映桌面端可见性和完整播放仍需人工确认。",
            })
        else:
            # Preserve a failed diagnostic as a UI blocker.  It is not merely
            # an uncertified preview: the draft/report must stay at this stage
            # until the concrete import/package issue is fixed.
            diagnosis.update({
                "diagnosis_status": diagnosis_status,
                "status": (diagnosis_status if diagnosis_status in {
                    "DESKTOP_IMPORT_BLOCKED", "PACKAGE_NOT_ACTIVE"
                } else "DESKTOP_IMPORT_BLOCKED"),
                "failure_class": "ui",
                "message": "草稿诊断未通过，已停在桌面验收阶段；保留草稿和诊断报告。",
            })
        result = finalize(diagnosis)
        return _emit(_compact_control_result(result), 0 if diagnosis_ok else 1)

    if stage == "semantic_match":
        analysis = str(manifest.get("shot_analysis_file") or "").strip()
        analysis_path = Path(analysis).expanduser() if analysis else report_dir / "shot_candidates.json"
        if not analysis_path.is_absolute():
            analysis_path = input_path.parent / analysis_path
        if not analysis_path.is_file():
            result = finalize({
                "status": "RECOVERY_ANALYSIS_REQUIRED",
                "failure_class": "semantic",
                "message": "语义恢复要求已有 shot_candidates.json；未找到时不会重新扫描整批素材。",
                "analysis": str(analysis_path),
            })
            return _emit(_compact_control_result(result), 1)
        manifest["shot_analysis_file"] = str(analysis_path.resolve())
        try:
            matched_manifest, blocker = _temporary_match(manifest, input_path, report_dir)
        except Exception as exc:  # noqa: BLE001
            result = finalize({"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)})
            return _emit(_compact_control_result(result), 1)
        if blocker:
            blocker["report_dir"] = str(report_dir)
            result = finalize(blocker)
            return _emit(_compact_control_result(result), 1)
        matched_path = report_dir / "matched_manifest.json"
        matched_path.write_text(json.dumps(matched_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = finalize({
            "status": "SHOT_MATCH_READY",
            "message": "语义匹配已从已有分析恢复；下一步显式执行 draft_write。",
            "matched_manifest": str(matched_path),
        })
        return _emit(_compact_control_result(result), 0)

    if stage != "draft_write":
        result = finalize({"status": "RECOVERY_STAGE_INVALID", "message": f"不支持的恢复阶段：{stage}"})
        return _emit(_compact_control_result(result), 1)

    # draft_write 只消费已存在的匹配结果；没有缓存就明确阻断，避免恢复命令
    # 偷偷重新扫描视觉素材，造成“恢复”实际变成整稿重建。
    cached_path = report_dir / "matched_manifest.json"
    if not cached_path.is_file():
        result = finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED",
            "failure_class": "semantic",
            "message": "draft_write 只接受已有匹配 manifest，不会重新做视觉分析。",
            "matched_manifest": str(cached_path),
        })
        return _emit(_compact_control_result(result), 1)
    try:
        cached_result = _read(cached_path)
    except (OSError, ValueError, TypeError):
        cached_result = {}
    # A raw input manifest can also contain segments.  Require the nested
    # result shape emitted by semantic_match so draft_write cannot silently
    # fall back to the original, unverified segments after a corrupt cache.
    cached = cached_result.get("manifest") if isinstance(cached_result, dict) else None
    if (not isinstance(cached_result, dict) or cached_result.get("ok") is not True
            or not isinstance(cached, dict)
            or not (cached.get("segments") or cached.get("videos"))):
        result = finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED",
            "failure_class": "semantic",
            "message": "matched_manifest.json 缺少已匹配的 segments/videos，未进入写稿。",
            "matched_manifest": str(cached_path),
        })
        return _emit(_compact_control_result(result), 1)
    manifest.update(cached)
    matched_manifest_sha256 = hashlib.sha256(
        json.dumps(cached_result, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    manifest["temporary_material_analysis"] = False
    manifest["auto_match_shots"] = False
    eng = OrchestrationEngine(manifest, input_path.parent, report_dir, logger=logger)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = eng.run()
    except Exception as exc:  # noqa: BLE001
        result = {"status": "ERROR", "error": str(exc), "error_type": type(exc).__name__}
    result["matched_manifest_sha256"] = matched_manifest_sha256
    result = finalize(result)
    return _emit(_compact_control_result(result), 0 if result.get("status") == "SUCCESS" else 1)


def cmd_migrate_encrypted(args) -> int:
    """Hand encrypted drafts to JianYing's official copy/migration flow."""
    from .migration import migrate_encrypted
    result = migrate_encrypted(Path(args.source_draft), args.new_name,
                               wait_seconds=args.wait_seconds)
    return _emit(result, 0 if result.get("status") in {"MIGRATION_VERIFIED", "MIGRATION_NOT_REQUIRED"} else 1)


def _manifest_sources(manifest: dict, base_dir: Path) -> list[Path]:
    if manifest.get("material_sources"):
        values = manifest.get("material_sources", [])
    elif manifest.get("segments"):
        values = [item.get("video") for item in manifest.get("segments", [])
                  if isinstance(item, dict)]
    else:
        values = manifest.get("videos", [])
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        p = Path(str(value)).expanduser()
        p = (p if p.is_absolute() else base_dir / p).resolve()
        if str(p) not in seen:
            seen.add(str(p)); paths.append(p)
    return paths


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _task_stage_times(task_dir: Path) -> dict:
    events = task_dir / "events.jsonl"
    result: dict[str, float] = {}
    try:
        for line in events.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            event = str(row.get("event", ""))
            if event.endswith("_finish") and "elapsed_s" in row:
                result[event[:-7]] = row["elapsed_s"]
    except (OSError, ValueError, TypeError):
        pass
    return result


def _jianying_ui_probe() -> dict:
    """Read only the Windows process responsiveness flag; never drive the UI."""
    if not sys.platform.startswith("win"):
        return {"checked": False, "reason": "not_windows"}
    script = ("$p=Get-Process JianyingPro,CapCut,VideoFusion -ErrorAction SilentlyContinue;"
              "if($p){$p | Select-Object ProcessName,Id,Responding | ConvertTo-Json -Compress}else{'[]'}")
    try:
        run = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, timeout=10, encoding="utf-8", errors="replace")
        data = json.loads((run.stdout or "[]").strip() or "[]")
        rows = data if isinstance(data, list) else [data]
        return {"checked": bool(rows), "processes": rows,
                "all_responding": bool(rows) and all(r.get("Responding") is not False for r in rows)}
    except Exception as exc:  # diagnostics only
        return {"checked": False, "reason": type(exc).__name__}


def cmd_calibrate_capacity(args) -> int:
    """Run isolated incremental source-count calibration and persist the safe limit."""
    input_path = Path(args.input).expanduser().resolve()
    base_manifest = _read(input_path)
    originals = _manifest_sources(base_manifest, input_path.parent)
    missing = [str(p) for p in originals if not p.is_file()]
    if not originals or missing:
        return _emit({"status": "CALIBRATION_INPUT_INVALID", "message": "校准素材缺失，未开始测试。",
                      "missing": missing}, 1)
    test_root = Path(args.test_root).expanduser().resolve()
    report_root = Path(args.report_dir).expanduser().resolve()
    test_id = time.strftime("v131_%Y%m%d_%H%M%S")
    isolated = test_root / test_id
    assets = isolated / "assets"
    reports = report_root / "capacity_calibration" / test_id
    assets.mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=False)
    profile_path = Path(args.profile_path).expanduser().resolve() if args.profile_path else None
    baseline = []
    for path in originals:
        stat = path.stat()
        baseline.append({"path": str(path), "bytes": stat.st_size, "sha256": _sha256(path)})
    host_before = capacity.resource_snapshot(test_root)
    calibration_minimum = capacity.CRITICAL_MEMORY_LIMIT
    if host_before["memory_available_bytes"] and host_before["memory_available_bytes"] < calibration_minimum:
        output = {"status": "SYSTEM_RESOURCE_PRESSURE", "message": "可用内存不足 256 MiB，为避免影响剪映编辑，校准已暂停。",
                  "test_root": str(isolated), "baseline": baseline, "host_before": host_before}
        (reports / "calibration.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return _emit(output, 1)

    step, maximum = max(1, args.step), max(1, args.max_sources)
    trials, last_passing = [], 0
    for source_count in range(step, maximum + 1, step):
        trial_assets = assets / f"sources_{source_count:03d}"
        trial_assets.mkdir()
        copied: list[Path] = []
        copy_started = time.perf_counter()
        try:
            for idx in range(source_count):
                origin = originals[idx % len(originals)]
                target = trial_assets / f"{idx + 1:03d}_{origin.name}"
                shutil.copy2(origin, target)
                copied.append(target)
        except OSError as exc:
            trials.append({"source_count": source_count, "status": "COPY_FAILED", "error": str(exc),
                           "copy_elapsed_s": round(time.perf_counter() - copy_started, 3)})
            break
        manifest = {
            "draft_name": f"JYSkill_v131_capacity_{test_id}_{source_count:03d}",
            "segments": [{"video": str(path), "audio_mode": "mute", "duration": 0.0} for path in copied],
            "allow_silent": True,
            "stabilize": False,
            "auto_transition": False,
            "execution_profile": "background_friendly",
            "force_overwrite": False,
        }
        manifest_path = isolated / f"manifest_{source_count:03d}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        trial_report = reports / f"trial_{source_count:03d}"
        trial_report.mkdir()
        logger = TaskLogger(trial_report, manifest)
        started = time.perf_counter()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                engine = OrchestrationEngine(manifest, isolated, trial_report, logger=logger,
                                             capacity_guard=False)
                result = engine.run()
        except Exception as exc:  # noqa: BLE001
            result = {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)}
        result = logger.finalize(result)
        host_after = capacity.resource_snapshot(test_root)
        ui = _jianying_ui_probe()
        trial = {
            "source_count": source_count,
            "copy_elapsed_s": round(time.perf_counter() - copy_started, 3),
            "engine_elapsed_s": round(time.perf_counter() - started, 3),
            "status": result.get("status"), "result": result,
            "stage_elapsed_s": _task_stage_times(logger.task_dir),
            "host_after": host_after, "jianying_ui_probe": ui,
        }
        trials.append(trial)
        pressure = (host_after["memory_available_bytes"] and
                    host_after["memory_available_bytes"] < capacity.CRITICAL_MEMORY_LIMIT)
        ui_failed = ui.get("checked") and not ui.get("all_responding")
        if result.get("status") != "SUCCESS" or pressure or ui_failed:
            break
        last_passing = source_count

    profile = None
    if last_passing:
        # Use the actual drafts drive as fingerprint anchor, not the temporary
        # asset directory, so normal builds use the calibrated profile.
        storage = Path(environment_report().get("drafts_root") or test_root)
        profile = capacity.create_profile(last_passing_count=last_passing, storage_path=storage,
                                          samples=trials)
        saved = capacity.save_profile(profile, profile_path)
    else:
        saved = None
    output = {
        "status": "CAPACITY_CALIBRATED" if profile else "CAPACITY_CALIBRATION_FAILED",
        "test_root": str(isolated), "report_dir": str(reports), "baseline": baseline,
        "host_before": host_before, "trials": trials, "last_passing_count": last_passing,
        "safe_source_limit": profile.get("safe_source_limit") if profile else None,
        "profile_path": str(saved) if saved else None,
        "ui_interaction_note": "SSH 仅检查剪映进程 Responding 状态，未自动模拟 10 轮人工时间线交互。",
    }
    (reports / "calibration.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return _emit(output, 0 if profile else 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jianying-orchestrator",
                                     description="豆包剪映智能编排引擎（跨平台）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="环境自检").set_defaults(func=cmd_env)

    sf = sub.add_parser("setup-ffmpeg",
                        help="缺ffmpeg或被公司禁装时，自动下载绿色版到bin并复验（不安装）")
    sf.add_argument("--force", action="store_true", help="已存在也强制重新下载")
    sf.set_defaults(func=cmd_setup_ffmpeg)

    sub.add_parser("list-voices", help="列出可选 TTS 音色").set_defaults(func=cmd_list_voices)

    rv = sub.add_parser("recommend-voice", help="按剧情推荐音色（可二次选择）")
    rv.add_argument("--text", default="", help="脚本文案/剧情内容")
    rv.add_argument("--style", default="")
    rv.add_argument("--persona", default="")
    rv.add_argument("--topk", type=int, default=5)
    rv.set_defaults(func=cmd_recommend_voice)

    au = sub.add_parser("audition", help="按脚本推荐Top音色并用固定样词生成试听")
    au.add_argument("--script", default="", help="脚本/AI剧情文本，用于相关性推荐；空则按通用带货")
    au.add_argument("--topn", type=int, default=5, help="试听条数，默认5、上限10")
    au.add_argument("--gender", default="", help="可选：男/女/童")
    au.add_argument("--keys", default="", help="手动指定音色key/id，逗号分隔，优先于推荐")
    au.add_argument("--all", action="store_true", help="对全库生成（一般不用）")
    au.add_argument("--out-dir", default=".voice_audition")
    au.set_defaults(func=cmd_audition)

    ll = sub.add_parser("list-library", help="浏览剪映全量音色库(约164)，可筛选")
    ll.add_argument("--gender", default="", help="男/女/童")
    ll.add_argument("--kw", default="", help="名称或id关键词，如 解说/妈妈/广告")
    ll.set_defaults(func=cmd_list_library)

    rb = sub.add_parser("recommend-bgm", help="基于脚本/剧情从曲库排序推荐BGM Top5")
    rb.add_argument("--script", "--text", dest="script", default="", help="脚本文案/AI剧情，用于情绪识别与相关性排序")
    rb.add_argument("--mood", default="", help="显式指定情绪(轻快/舒缓/动感/可爱/大气/治愈/国风)，权重更高")
    rb.add_argument("--id", default="")
    rb.add_argument("--min-dur", type=float, default=0.0)
    rb.add_argument("--topk", type=int, default=5)
    rb.set_defaults(func=cmd_recommend_bgm)

    sc = sub.add_parser("scan", help="只扫描素材元数据")
    sc.add_argument("--input", required=True)
    sc.add_argument("--report-dir", default=".jy_run")
    sc.set_defaults(func=cmd_scan)

    sa = sub.add_parser("analyze-shots", help="只分析当前商品素材并生成临时镜头候选，不读取脚本文案")
    sa.add_argument("--input", required=True, help="含 material_sources/videos 的 manifest.json")
    sa.add_argument("--report-dir", default=".jy_run")
    sa.add_argument("--threshold", type=float, default=0.30, help="ffmpeg 场景切换阈值")
    sa.add_argument("--min-shot-s", type=float, default=0.45, help="最短候选镜头秒数")
    sa.add_argument("--no-vision", action="store_true",
                    help="仅生成原始抽帧清单，不调用自动视觉分析（仅诊断用）")
    sa.set_defaults(func=cmd_analyze_shots)

    ms = sub.add_parser("match-shots", help="按脚本逐句给临时镜头推荐 Top3 并自动采用 Top1")
    ms.add_argument("--input", required=True, help="含 full_script/segments 的 manifest.json")
    ms.add_argument("--analysis", required=True, help="analyze-shots 生成并经 AI 填写描述的 shot_candidates.json")
    ms.add_argument("--report-dir", default=".jy_run")
    ms.add_argument("--topk", type=int, default=3)
    ms.add_argument("--require-selection", action="store_true", help=argparse.SUPPRESS)
    ms.set_defaults(func=cmd_match_shots)

    rs = sub.add_parser("review-script", help="口播确认单：逐句字数/预估时长/衔接覆盖体检")
    rs.add_argument("--input", required=True, help="manifest.json（默认口播来自输入，不重写）")
    rs.add_argument("--report-dir", default=".jy_run")
    rs.set_defaults(func=cmd_review_script)

    bd = sub.add_parser("build", help="生成剪映草稿")
    bd.add_argument("--input", required=True, help="manifest.json")
    bd.add_argument("--report-dir", default=".jy_run")
    bd.add_argument("--strict-script", action="store_true", help="文案校验不通过则阻断")
    bd.add_argument("--delivery-mode", choices=("formal", "preview"), default="formal",
                    help="formal（默认）：时序无解即阻断，不得静默通过；"
                         "preview：未解出的段落按素材可用长度出占位画面并在成片上打"
                         "未认证标注，仅供预览，不得用于正式交付。")
    bd.add_argument("--trigger-phrase", default="", help=argparse.SUPPRESS)
    bd.set_defaults(func=cmd_build)

    dt = sub.add_parser("diagnose-timeline", help="只读诊断草稿导入/时间线状态")
    dt.add_argument("--draft", required=True, help="草稿目录")
    dt.add_argument("--report-dir", default=".jy_run")
    dt.add_argument("--expect-external-package", action="store_true",
                    help="核验当前命令是否来自标准豆包 Skills 交付包")
    dt.set_defaults(func=cmd_diagnose_timeline)

    rc = sub.add_parser("recover", help="从完整 manifest 创建不覆盖原稿的恢复草稿")
    rc.add_argument("--input", required=True, help="完整 manifest.json")
    rc.add_argument("--report-dir", default=".jy_run")
    rc.add_argument("--stage", choices=("preflight", "semantic_match", "draft_write", "ui_acceptance"),
                    default="draft_write", help="只恢复一个阶段；默认只消费已有匹配结果写稿")
    rc.add_argument("--draft", default="", help="ui_acceptance 阶段的草稿目录")
    rc.set_defaults(func=cmd_recover)

    me = sub.add_parser("migrate-encrypted", help="通过剪映官方副本流程迁移加密草稿（不解密、不覆盖）")
    me.add_argument("--source-draft", required=True, help="明确的源草稿目录，禁止猜测最近草稿")
    me.add_argument("--new-name", required=True, help="全新的目标草稿名")
    me.add_argument("--wait-seconds", type=int, default=0,
                    help="等待官方副本出现并验证，最多300秒；默认0表示只打开客户端并返回指引")
    me.set_defaults(func=cmd_migrate_encrypted)

    cc = sub.add_parser("calibrate-capacity", help="在隔离目录递增压测并保存本机素材容量上限")
    cc.add_argument("--input", required=True, help="含测试视频的 manifest.json")
    cc.add_argument("--test-root", required=True, help="隔离测试根目录，命令只会在其下创建新目录")
    cc.add_argument("--report-dir", default=".jy_run", help="独立校准报告目录")
    cc.add_argument("--step", type=int, default=13, help="每轮新增素材数，默认 13")
    cc.add_argument("--max-sources", type=int, default=65, help="最多测试的唯一素材数，默认 65")
    cc.add_argument("--profile-path", default="", help="可选的 host_capacity.json 保存位置")
    cc.set_defaults(func=cmd_calibrate_capacity)

    # 兼容旧用法：无子命令时按 build 处理，--analysis 暂接受并忽略（segments 直接内嵌）
    if argv is None:
        argv = sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        parser.print_help()
        return 0
    phrases = {"智能编排", "智能生成", "开启智能模式"}
    if argv and argv[0] in phrases:
        phrase = argv[0]
        if len(argv) == 1:
            return _emit({"status": "INPUT_REQUIRED", "mode": "intelligent",
                          "trigger_phrase": phrase,
                          "message": "请补充本次任务的素材路径和脚本文案后再试。"}, 1)
        argv = ["build", *argv[1:], "--trigger-phrase", phrase]
    if argv and not argv[0].startswith("-") and argv[0] in ("env", "setup-ffmpeg",
        "list-voices",
        "recommend-voice", "audition", "list-library", "recommend-bgm",
        "review-script", "scan", "analyze-shots", "match-shots", "build", "diagnose-timeline", "recover", "migrate-encrypted",
        "calibrate-capacity"):
        args = parser.parse_args(argv)
        return args.func(args)

    # 旧式：--input x [--scan-only]
    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("--input")
    legacy.add_argument("--report-dir", default=".jy_run")
    legacy.add_argument("--scan-only", action="store_true")
    legacy.add_argument("--analysis")
    known, _ = legacy.parse_known_args(argv)
    if known.input:
        if known.scan_only:
            return cmd_scan(argparse.Namespace(input=known.input,
                                               report_dir=known.report_dir))
        return cmd_build(argparse.Namespace(input=known.input,
                                            report_dir=known.report_dir,
                                            strict_script=False))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
