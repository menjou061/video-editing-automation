"""剪映草稿写入安全：防止“草稿无法打开”再次发生。

三条硬规则（对应事故复盘）：
1. 剪映运行中绝不触碰已存在草稿：覆盖同名草稿可能让“旧版→新版工程结构”迁移中断，
   也可能影响业务同学正在编辑的任务。无法确认时返回 BLOCKED，由用户自行回主页/退出。
2. 写入后立即用独立路径重新解析校验：素材引用齐全、时间轴无重叠、媒体文件存在，
   不通过即判定失败并保留现场，绝不上报成功。
3. 同步草稿列表索引 root_meta_info.json：登记新草稿、清除指向已删草稿的坏条目。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .platform_env import is_jianying_running


@dataclass
class PreWriteVerdict:
    ok: bool
    status: str          # OK / BLOCKED / RELEASED
    message: str
    jianying_running: bool


def drafts_root_access_issues(drafts_root: Path) -> list[str]:
    """Verify the selected library is usable before expensive task stages.

    This uses a short-lived probe in the library root, never in an existing
    draft.  ``os.access`` alone is unreliable on Windows network/sandboxed
    locations, while a create-and-remove probe gives the operator a useful
    failure before TTS, media staging, or draft creation begins.
    """
    root = Path(drafts_root)
    if not root.exists():
        return [f"剪映草稿目录不存在：{root}"]
    if not root.is_dir():
        return [f"剪映草稿路径不是目录：{root}"]
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".jy_skill_probe_",
                                         delete=True) as probe:
            probe.write(b"jianying-skill-access-check")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as exc:
        return [f"剪映草稿目录不可写：{root}（{exc}）"]
    return []


def inspect_draft_format(draft_dir: Path) -> dict:
    """Classify an existing JianYing draft without modifying it.

    ``draft_info.json`` is the legacy/plain-text format used by the bundled
    writer. Newer JianYing versions may migrate the project into
    ``Timelines/**/project.json`` or replace the root JSON with encrypted
    bytes. The latter cannot be safely generated or edited without JianYing's
    private key store, so callers must stop before constructing a project.
    """
    draft_dir = Path(draft_dir)
    if not draft_dir.exists():
        return {"kind": "missing", "path": str(draft_dir)}

    timeline_projects = list((draft_dir / "Timelines").glob("**/project.json")) \
        if (draft_dir / "Timelines").exists() else []
    info = draft_dir / "draft_info.json"
    content = draft_dir / "draft_content.json"
    candidates = [p for p in (info, content) if p.exists()]
    if timeline_projects:
        return {"kind": "migrated", "path": str(draft_dir),
                "timeline_projects": [str(p) for p in timeline_projects]}
    if not candidates:
        # A newly allocated/empty folder is safe for the writer to populate.
        return {"kind": "empty", "path": str(draft_dir)}

    for p in candidates:
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            raw = ""
        if raw.lstrip().startswith("{"):
            try:
                json.loads(raw)
                return {"kind": "plain", "path": str(draft_dir), "json": str(p)}
            except (TypeError, ValueError):
                continue

    # Non-JSON root files with no migrated timeline are treated as encrypted
    # or otherwise unreadable; do not overwrite them with a 5.9 draft.
    return {"kind": "encrypted", "path": str(draft_dir),
            "files": [str(p) for p in candidates]}


def pre_write_check(draft_name: str, drafts_root: Path, editor_root: Path, *,
                    force: bool = False, auto_release: bool = True) -> PreWriteVerdict:
    """写入前占用检查，不操作剪映 UI 或业务草稿。"""
    running = is_jianying_running()
    target = drafts_root / draft_name
    exists = target.exists()

    if exists:
        fmt = inspect_draft_format(target)
        if fmt.get("kind") == "encrypted":
            return PreWriteVerdict(
                False, "DRAFT_ENCRYPTED_UNSUPPORTED",
                "目标草稿采用剪映加密格式且缺少可迁移的 Timelines/project.json；"
                "无法安全构造或覆盖，请通过剪映官方迁移/导入路径创建副本后重试。",
                running,
            )

    if not running:
        return PreWriteVerdict(True, "OK", "剪映未运行，可安全写入", False)

    if not exists:
        # A unique draft name is isolated from the editor's existing work.
        return PreWriteVerdict(True, "OK", "新建独立草稿，不操作剪映当前任务", True)

    return PreWriteVerdict(
        False, "BLOCKED",
        "剪映正在运行且目标草稿已存在；为避免影响正在编辑的业务任务或损坏草稿，"
        "不会覆盖或操作该草稿。请改用新的 draft_name，或在剪映退出后重试。",
        True,
    )


def _collect_material_ids(draft: dict) -> set[str]:
    ids: set[str] = set()
    materials = draft.get("materials", {})
    for value in materials.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for k in ("id", "global_id"):
                        if item.get(k):
                            ids.add(item[k])
    return ids


def _text_from_material(material: dict) -> Optional[str]:
    """Return the text from JyProject's structured text material.

    JianYing stores subtitle text in a JSON ``content`` string. A legacy
    hand-written ``text`` field is deliberately not accepted: it can make a
    text track appear in JSON while rendering nothing in the editor.
    """
    content = material.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return None
    if not isinstance(content, dict):
        return None
    value = content.get("text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _subtitle_style_issues(material: dict, segment: dict, index: int,
                            expected_tracks: dict) -> list[str]:
    """Validate the concrete JianYing JSON emitted by TextSegment.

    Styling is stored inside the material's JSON ``content.styles`` while
    position is stored on each timeline segment's ``clip.transform`` object.
    Keep this check independent from the writer so a readable draft cannot be
    reported as compliant when a later refactor drops one of the defaults.
    """
    if not expected_tracks.get("subtitle_style_required"):
        return []
    issues: list[str] = []
    # 兜底值必须与 core/text_ops.py 的 DEFAULT_SUBTITLE_* 同口径：调用方漏传时
    # 若退回旧值 (-1000.0)，校验就会把「字幕被推出画布」判为合规。
    expected_size = float(expected_tracks.get("subtitle_font_size", 8.0))
    expected_bold = bool(expected_tracks.get("subtitle_bold", True))
    expected_x = float(expected_tracks.get("subtitle_transform_x", 0.0))
    expected_y = float(expected_tracks.get("subtitle_transform_y", -0.8))
    content = material.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            content = None
    styles = content.get("styles") if isinstance(content, dict) else None
    style = styles[0] if isinstance(styles, list) and styles and isinstance(styles[0], dict) else None
    if style is None:
        issues.append(f"字幕第 {index} 条缺少可检查的 styles")
    else:
        try:
            actual_size = float(style.get("size"))
        except (TypeError, ValueError):
            actual_size = None
        if actual_size is None or abs(actual_size - expected_size) > 1e-6:
            issues.append(f"字幕第 {index} 条字号不符：期望 {expected_size:g}，实际 {actual_size}")
        if bool(style.get("bold")) != expected_bold:
            issues.append(f"字幕第 {index} 条粗体状态不符：期望 {expected_bold}，实际 {bool(style.get('bold'))}")

    text = _text_from_material(material) or ""
    if any(unicodedata.category(char).startswith("P") for char in text):
        issues.append(f"字幕第 {index} 条仍含标点符号")

    clip = segment.get("clip") if isinstance(segment, dict) else None
    transform = clip.get("transform") if isinstance(clip, dict) else None
    if not isinstance(transform, dict):
        issues.append(f"字幕第 {index} 条缺少位置 transform")
    else:
        try:
            actual_x = float(transform.get("x"))
            actual_y = float(transform.get("y"))
        except (TypeError, ValueError):
            actual_x = actual_y = None
        if (actual_x is None or abs(actual_x - expected_x) > 1e-6 or
                actual_y is None or abs(actual_y - expected_y) > 1e-6):
            issues.append(f"字幕第 {index} 条位置不符：期望 ({expected_x:g},{expected_y:g})，实际 ({actual_x},{actual_y})")
    return issues


def _expected_track_issues(draft: dict, expected_tracks: dict) -> list[str]:
    """Validate the semantic tracks required by a generated manifest."""
    issues: list[str] = []
    tracks = draft.get("tracks", [])
    by_name = {str(t.get("name", "")): t for t in tracks if isinstance(t, dict)}

    def find(name: str, track_type: str, *, allow_type_fallback: bool = False) -> Optional[dict]:
        exact = by_name.get(name)
        if exact is not None:
            return exact
        if allow_type_fallback:
            return next((t for t in tracks if isinstance(t, dict) and
                         str(t.get("type", "")) == track_type), None)
        return None

    def require_count(label: str, name: str, track_type: str, expected: int,
                      *, required: bool = True, allow_type_fallback: bool = False) -> Optional[dict]:
        track = find(name, track_type, allow_type_fallback=allow_type_fallback)
        actual = len(track.get("segments", [])) if track else 0
        if required and actual != expected:
            issues.append(f"{label}轨道片段数不符：期望 {expected}，实际 {actual}")
        elif not required and actual:
            issues.append(f"{label}轨道不应存在，但发现 {actual} 个片段")
        return track

    require_count("视频", "Video_BRoll", "video",
                  int(expected_tracks.get("video_segments", 0)),
                  allow_type_fallback=True)
    require_count("旁白", "Narration", "audio",
                  int(expected_tracks.get("narration_segments", 0)),
                  required=int(expected_tracks.get("narration_segments", 0)) > 0)
    subtitle_track = require_count("字幕", "Subtitles", "text",
                                   int(expected_tracks.get("subtitle_segments", 0)),
                                   required=int(expected_tracks.get("subtitle_segments", 0)) > 0)
    bgm_required = bool(expected_tracks.get("bgm_required"))
    bgm_track = require_count("BGM", "BGM", "audio",
                              int(expected_tracks.get("bgm_segments", 1)),
                              required=bgm_required)

    if subtitle_track:
        materials = draft.get("materials", {}).get("texts", [])
        text_by_id = {str(m.get("id")): m for m in materials if isinstance(m, dict)}
        for idx, segment in enumerate(subtitle_track.get("segments", []), 1):
            material = text_by_id.get(str(segment.get("material_id")))
            if not material:
                continue  # missing reference is reported by the generic pass
            if material.get("type") not in {"text", "subtitle"}:
                issues.append(f"字幕第 {idx} 条素材类型不是标准 text/subtitle")
            if not _text_from_material(material):
                issues.append(f"字幕第 {idx} 条缺少可解析的 content.text")
            issues.extend(_subtitle_style_issues(material, segment, idx, expected_tracks))

    if bgm_required and bgm_track:
        audio_materials = draft.get("materials", {}).get("audios", [])
        audio_by_id = {str(m.get("id")): m for m in audio_materials if isinstance(m, dict)}
        for idx, segment in enumerate(bgm_track.get("segments", []), 1):
            material = audio_by_id.get(str(segment.get("material_id")))
            if not material or not material.get("path"):
                issues.append(f"BGM第 {idx} 条缺少可用音频素材路径")
    return issues


def post_write_validate(draft_dir: Path, *, expected_tracks: Optional[dict] = None) -> list[str]:
    """独立重新解析草稿，返回问题列表（空列表代表通过）。

    ``expected_tracks`` is optional for compatibility with read-only diagnosis
    callers. Build callers pass it to prevent a structurally readable draft
    from being reported successful when narration, subtitles, or confirmed BGM
    was silently omitted.
    """
    issues: list[str] = []
    info = draft_dir / "draft_info.json"
    if not info.exists():
        fmt = inspect_draft_format(draft_dir)
        if fmt.get("kind") == "migrated":
            return []
        if fmt.get("kind") == "encrypted":
            return [f"DRAFT_ENCRYPTED_UNSUPPORTED: 草稿为加密格式且无法解析: {draft_dir}"]
        return [f"缺少 draft_info.json: {info}"]
    raw = info.read_text(encoding="utf-8", errors="ignore")
    if not raw.strip():
        return [f"draft_info.json 为空（写入中断或被占用）: {info}"]
    if not raw.lstrip().startswith("{"):
        # 非明文 JSON：草稿已被剪映打开并迁移/加密（根文件变密文、工程转入
        # Timelines/project.json），这是“已成功打开过”的正常状态，不是损坏。
        tl = draft_dir / "Timelines"
        migrated = list(tl.glob("**/project.json")) if tl.exists() else []
        if migrated:
            return []
        return [f"DRAFT_ENCRYPTED_UNSUPPORTED: draft_info.json 既非明文也无迁移工程: {info}"]
    try:
        draft = json.loads(raw)
    except Exception as exc:
        return [f"draft_info.json 无法解析（可能写入中断/被占用）: {exc}"]

    if expected_tracks:
        issues.extend(_expected_track_issues(draft, expected_tracks))

    # The sidecar is part of the on-disk draft contract. A placeholder sidecar
    # can make the project appear in the library while JianYing refuses to
    # open it, so fail closed before reporting SUCCESS.
    meta_path = draft_dir / "draft_meta_info.json"
    if not meta_path.exists():
        issues.append(f"缺少 draft_meta_info.json: {meta_path}")
    else:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("draft_name") != draft.get("name"):
                issues.append("draft_meta_info.json 的 draft_name 与 draft_info.json 的 name 不一致")
            if meta.get("draft_id") != draft.get("id"):
                issues.append("draft_meta_info.json 的 draft_id 与 draft_info.json 的 id 不一致")
            if Path(str(meta.get("draft_fold_path", ""))).resolve() != draft_dir.resolve():
                issues.append("draft_meta_info.json 的 draft_fold_path 与实际草稿目录不一致")
        except (OSError, TypeError, ValueError) as exc:
            issues.append(f"draft_meta_info.json 无法解析: {exc}")

    material_ids = _collect_material_ids(draft)
    tracks = draft.get("tracks", [])
    for tr in tracks:
        prev_end = -1
        for seg in tr.get("segments", []):
            tr_type = tr.get("type")
            mid = seg.get("material_id")
            if mid and mid not in material_ids:
                issues.append(f"{tr.get('name')} 段 {seg.get('id','')[:8]} 引用缺失素材 {mid[:8]}")
            # 文本段的占位 extra_ref 是库固有结构，跳过；其余类型必须可追溯
            if tr_type != "text":
                for ref in seg.get("extra_material_refs", []):
                    if ref not in material_ids:
                        issues.append(f"{tr.get('name')} 段缺失附加素材 {ref[:8]}")
            trange = seg.get("target_timerange", {})
            try:
                start = int(trange["start"]); dur = int(trange["duration"])
            except (KeyError, TypeError, ValueError):
                issues.append(f"{tr.get('name')} 段时间范围非法"); continue
            if start < prev_end - 1:  # 同轨重叠（1us 容差）
                issues.append(f"{tr.get('name')} 轨道片段重叠 @{start}")
            prev_end = start + dur

    # 引用的媒体文件必须真实存在于草稿 media 目录
    media_dir = draft_dir / "media"
    for group in ("videos", "audios"):
        for mat in draft.get("materials", {}).get(group, []):
            p = mat.get("path") or ""
            if p and not Path(p).exists():
                issues.append(f"媒体文件缺失: {Path(p).name}")
    issues.extend(validate_audio_video_bounds(draft_dir))
    return issues


def validate_audio_video_bounds(draft_dir: Path) -> list[str]:
    """Ensure narration/BGM tracks cannot extend beyond the video timeline."""
    info = Path(draft_dir) / "draft_info.json"
    try:
        raw = info.read_text(encoding="utf-8", errors="ignore")
        if not raw.lstrip().startswith("{"):
            return []
        draft = json.loads(raw)
    except (OSError, ValueError):
        return []
    video_end = 0
    audio_ends: list[tuple[str, int]] = []
    for track in draft.get("tracks", []):
        name = str(track.get("name", ""))
        typ = str(track.get("type", ""))
        end = 0
        for seg in track.get("segments", []):
            tr = seg.get("target_timerange", {})
            try:
                end = max(end, int(tr.get("start", 0)) + int(tr.get("duration", 0)))
            except (TypeError, ValueError):
                continue
        if typ == "video" or name.lower().startswith("video"):
            video_end = max(video_end, end)
        elif typ == "audio" or name in {"Narration", "BGM"}:
            audio_ends.append((name or typ or "audio", end))
    if not video_end:
        return []
    return [f"{name} 音频结束于 {end}us，超过画面结束 {video_end}us"
            for name, end in audio_ends if end > video_end]


def sync_root_meta(drafts_root: Path, draft_name: str, *, duration_us: int = 0,
                   remove_missing: bool = False) -> None:
    """登记新草稿到列表索引，并清除指向已删除草稿的坏条目（加密/异常时安全跳过）。"""
    meta_path = drafts_root / "root_meta_info.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return  # 剪映可能加密存储，交给其自身扫描重建，不强行改
    store = meta.get("all_draft_store")
    if not isinstance(store, list):
        return

    if remove_missing:
        kept = []
        for e in store:
            fold = e.get("draft_fold_path", "")
            # 仅清理“登记了路径但目录已不存在”的坏条目
            if fold and not Path(fold).exists():
                continue
            kept.append(e)
        store[:] = kept

    draft_dir = drafts_root / draft_name
    store[:] = [e for e in store if e.get("draft_name") != draft_name]
    # 以任意现存本地条目为模板，保证字段齐全
    template = next((json.loads(json.dumps(e)) for e in store if e.get("draft_fold_path")), {})
    entry = template or {}
    cover = draft_dir / "draft_cover.jpg"
    now = int(time.time() * 1e6)
    entry.update({
        "draft_name": draft_name,
        "draft_fold_path": str(draft_dir),
        "draft_json_file": str(draft_dir / "draft_info.json"),
        "draft_root_path": str(drafts_root),
        "draft_cover": str(cover) if cover.exists() else "",
        "tm_duration": int(duration_us),
        "tm_draft_modified": now,
    })
    entry.setdefault("tm_draft_create", now)
    store.insert(0, entry)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
