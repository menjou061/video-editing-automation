"""背景音乐选择与铺底制作。

- 读取 jianying-editor/data/cloud_music_library.csv（剪映云曲库，含风格分类与直链）。
- select_bgm：按情绪/风格关键词或 music_id / 本地路径选曲，返回候选供二次选择。
- prepare_bgm_track：用 ffmpeg 裁剪到成片时长、自动压到铺底响度、首尾淡变，
  产出可直接铺到 BGM 轨的成品（固化“比人声低约 12dB、不抢旁白”的经验）。
  响度口径为 EBU R128（LUFS），与 `audio_loudness` 的旁白归一同一把尺子。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import audio_loudness
from .platform_env import find_jianying_editor_root


# 情绪/风格关键词 → 曲库分类/标题匹配词
MOOD_ALIASES: dict[str, tuple[str, ...]] = {
    "轻快": ("轻快", "轻松", "欢快", "开心", "快乐", "活泼", "俏皮", "愉悦", "Ukulele", "Happy", "Upbeat"),
    "舒缓": ("舒缓", "轻柔", "安静", "宁静", "放松", "治愈", "温暖", "Relax", "Calm", "Lofi", "Soft"),
    "动感": ("动感", "酷炫", "节奏", "运动", "燃", "Dance", "House", "Funk", "Energetic"),
    "可爱": ("可爱", "萌", "童趣", "卡通", "Cute"),
    "大气": ("企业", "进取", "大气", "励志", "磅礴", "Corporate", "Motivational", "Epic"),
    "治愈": ("治愈", "亲情", "温暖", "温柔", "Acoustic", "Piano"),
    "国风": ("国风", "古风", "中国风", "传统"),
}

# 脚本/AI剧情关键词 → BGM 情绪标签（有序，命中越多越靠前）；与音色推荐同一思路
SCRIPT_BGM_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("带货", "种草", "下单", "买", "优惠", "促销", "爆款", "引流", "闭眼冲", "链接", "划算",
      "囤", "价", "叫卖", "直播", "奶被纸"), ("轻快", "动感")),
    (("宝宝", "婴儿", "母婴", "妈妈", "亲子", "温柔", "亲情", "家庭", "长辈"), ("治愈", "舒缓", "可爱")),
    (("科普", "知识", "原理", "讲解", "纪录片", "教学", "为什么", "成分", "技术"), ("舒缓", "轻快")),
    (("企业", "品牌", "宣传", "形象", "发布", "大气", "励志", "进取", "磅礴", "时代"), ("大气",)),
    (("情感", "深夜", "孤独", "故事", "心情", "伤感", "怀念", "遗憾"), ("舒缓", "治愈")),
    (("美食", "好吃", "烹饪", "餐厅", "探店"), ("轻快", "可爱")),
    (("运动", "健身", "燃", "跑", "训练", "酷炫", "街舞", "卡点"), ("动感",)),
    (("国风", "古风", "传统", "汉服", "非遗", "山水"), ("国风",)),
    (("可爱", "萌", "童趣", "卡通", "宠物", "小孩", "玩具"), ("可爱", "轻快")),
    (("旅行", "vlog", "Vlog", "出游", "风景", "夏日", "海边"), ("轻快", "舒缓")),
]

# 情绪 → 曲目标题里常见的英文风格词（371 首 categories=unknown 时靠标题匹配）
EN_MOOD_TERMS: dict[str, tuple[str, ...]] = {
    "轻快": ("ukulele", "happy", "upbeat", "funky", "funk", "cheerful", "bright",
             "sunny", "tropical", "whistle", "quirky", "playful", "pop", "groovy"),
    "舒缓": ("lofi", "lo-fi", "soft", "piano", "acoustic", "calm", "relax", "gentle",
             "warm", "ambient", "chill", "ballad", "slow"),
    "动感": ("dance", "house", "edm", "energetic", "beat", "groove", "electro",
             "driving", "rock", "hype", "workout"),
    "可爱": ("cute", "kids", "playful", "whimsical", "comic", "toy", "bouncy"),
    "大气": ("corporate", "motivational", "inspire", "inspirational", "epic",
             "cinematic", "grand", "uplifting", "powerful"),
    "治愈": ("acoustic", "piano", "warm", "tender", "emotional", "heartfelt", "soft"),
    "国风": ("chinese", "oriental", "guzheng", "erhu", "asian"),
}


def detect_bgm_moods(script: str) -> list[str]:
    """从脚本/剧情识别 BGM 情绪，按命中强度排序；无命中回退通用轻快。"""
    hits: dict[str, int] = {}
    for kws, moods in SCRIPT_BGM_RULES:
        n = sum(1 for k in kws if k in script)
        if n:
            for m in moods:
                hits[m] = hits.get(m, 0) + n
    ordered = [m for m, _ in sorted(hits.items(), key=lambda x: -x[1])]
    return ordered or ["轻快"]


def _mood_terms(mood: str) -> list[str]:
    """把用户输入（可含多个词/标点）拆词并扩展为曲库匹配词，去重保序。"""
    words = [w for w in re.split(r"[\s,，、/\\|]+", mood) if w] or [mood]
    terms: list[str] = []
    for w in words:
        if w not in terms:
            terms.append(w)
        for key, vals in MOOD_ALIASES.items():
            hit = (w == key or w in key or key in w or
                   any(w in v or v in w for v in vals if v.isascii() is False))
            if hit:
                for v in vals:
                    if v not in terms:
                        terms.append(v)
    return terms


@dataclass
class MusicTrack:
    music_id: str
    title: str
    duration_s: float
    categories: str
    url: str

    def to_dict(self) -> dict:
        return {"music_id": self.music_id, "title": self.title,
                "duration_s": self.duration_s, "categories": self.categories,
                "url": self.url}


def _music_csv_path() -> Optional[Path]:
    root = find_jianying_editor_root()
    p = root / "data" / "cloud_music_library.csv"
    return p if p.exists() else None


def load_library() -> list[MusicTrack]:
    """读取云曲库；找不到时返回空列表（上层可回退到本地文件）。"""
    path = _music_csv_path()
    if not path:
        return []
    tracks: list[MusicTrack] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#") or row[0] == "music_id":
                continue
            if len(row) < 4:
                continue
            try:
                dur = float(row[2]) if row[2] else 0.0
            except ValueError:
                dur = 0.0
            tracks.append(MusicTrack(
                music_id=row[0].strip(), title=row[1].strip(), duration_s=dur,
                categories=row[3].strip(), url=row[4].strip() if len(row) > 4 else "",
            ))
    return tracks


def _score_track(t: MusicTrack, moods: list[str], explicit: list[str],
                 min_duration: float) -> tuple[int, list[str]]:
    """对单首按情绪相关性打分，返回 (分数, 命中标签)。"""
    cat = t.categories.lower()
    title = t.title.lower()
    score = 0
    matched: list[str] = []
    for rank, mood in enumerate(moods):
        weight = max(4 - rank, 1)          # 越靠前的剧情情绪权重越高
        pool = set(w.lower() for w in MOOD_ALIASES.get(mood, (mood,)))
        pool.update(w.lower() for w in EN_MOOD_TERMS.get(mood, ()))
        pool.add(mood.lower())
        for w in pool:
            if not w:
                continue
            in_cat = w in cat
            in_title = w in title
            if in_cat:
                score += 3 * weight
                if w not in matched:
                    matched.append(w)
            elif in_title:
                score += 2 * weight
                if w not in matched:
                    matched.append(w)
    # 用户显式点名的情绪额外加权
    for w in explicit:
        wl = w.lower()
        if wl in cat:
            score += 4
        elif wl in title:
            score += 2
    # 可用性/适配度
    if t.url:
        score += 1                        # 有直链可直接下载
    if min_duration and t.duration_s >= min_duration:
        score += 1
    if 20.0 <= t.duration_s <= 360.0:
        score += 1                        # 短视频常用时长区间
    if t.duration_s and t.duration_s < 15.0:
        score -= 5                        # 十几秒的原声片段不适合铺底
    if "douyin_collect" in t.categories:
        score -= 3
    return score, matched[:6]


def recommend_bgm(script: str = "", *, mood: str = "", topn: int = 5,
                  min_duration: float = 0.0, prefer_url: bool = True) -> list[dict]:
    """基于脚本/AI剧情对全曲库(616)做情绪相关性排序，返回 Top N（默认5）。

    script：脚本文案/剧情，自动识别情绪；mood：用户显式指定的情绪（更高权重）。
    """
    lib = load_library()
    moods = detect_bgm_moods(script)
    if mood:
        explicit = _mood_terms(mood)
        # 显式情绪插到最前
        head = [w for w in re.split(r"[\s,，、/\\|]+", mood) if w]
        moods = head + [m for m in moods if m not in head]
    else:
        explicit = []

    scored = []
    for t in lib:
        if min_duration and t.duration_s and t.duration_s < min_duration:
            continue
        s, matched = _score_track(t, moods, explicit, min_duration)
        if s <= 0:
            continue
        d = t.to_dict()
        d["score"] = s
        d["matched"] = matched
        d["moods"] = moods
        d["has_url"] = bool(t.url)
        scored.append((s, t.duration_s, d))
    # 相关分降序；同分：有直链优先、时长合适优先
    scored.sort(key=lambda x: (-x[0], 0 if x[2]["has_url"] else 1, -x[1]))
    if prefer_url:
        with_url = [x for x in scored if x[2]["has_url"]]
        no_url = [x for x in scored if not x[2]["has_url"]]
        ordered = with_url + no_url
    else:
        ordered = scored
    return [d for _, _, d in ordered[:topn]]


def select_bgm(mood: str = "", *, music_id: str = "", topk: int = 5,
               min_duration: float = 0.0, script: str = "") -> list[dict]:
    """选曲入口（保持引擎兼容）：music_id 精确 > 剧情/情绪相关性排序 > 通用兜底。"""
    lib = load_library()
    if music_id:
        hit = [t for t in lib if t.music_id == music_id]
        return [t.to_dict() for t in hit[:topk]]
    rec = recommend_bgm(script, mood=mood, topn=topk, min_duration=min_duration)
    if rec:
        return rec
    # 无命中：给一批时长足够的通用曲目兜底
    cand = [t for t in lib if t.duration_s >= max(min_duration, 30.0)]
    return [t.to_dict() for t in cand[:topk]]


def _cloud_download(music_id: str) -> Optional[Path]:
    """用底层 CloudManager 按音乐 id 实时换链下载（csv 里的静态签名直链会过期导致 403）。"""
    try:
        import sys
        scripts = str(find_jianying_editor_root() / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from cloud_manager import CloudManager  # type: ignore
        p = CloudManager().download_asset(music_id)
        if p and Path(p).exists() and Path(p).stat().st_size > 1024:
            return Path(p)
    except Exception:
        return None
    return None


def download_track(track: dict, out_dir: Path, *, timeout: int = 40) -> Optional[Path]:
    """下载选中的曲目：优先按 id 实时换链（带全局缓存），失败再回退静态直链。"""
    music_id = track.get("music_id", "")
    if music_id:
        cached = _cloud_download(music_id)
        if cached:
            return cached
    out_dir.mkdir(parents=True, exist_ok=True)
    url = track.get("url") or ""
    if not url:
        return None
    ext = ".m4a"
    m = re.search(r"mime_type=audio_(\w+)", url)
    if m:
        ext = "." + ("mp3" if m.group(1) == "mp3" else "m4a")
    target = out_dir / f"bgm_{music_id or 'track'}{ext}"
    if target.exists() and target.stat().st_size > 1024:
        return target
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, target.open("wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception:
        return None
    return target if target.stat().st_size > 1024 else None


def prepare_preview_tracks(tracks: list[dict], out_dir: Path, *, ffmpeg: str = "ffmpeg",
                           seconds: float = 8.0, workers: int = 4) -> list[dict]:
    """并行缓存候选试听片段，避免用户逐首等待完整处理。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(track: dict) -> dict:
        result = dict(track)
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(track.get("music_id") or "track"))
        preview = out_dir / f"{safe_id}_{int(seconds)}s.mp3"
        if not preview.exists() or preview.stat().st_size <= 1024:
            src = download_track(track, out_dir / "sources")
            if src:
                cmd = [ffmpeg, "-y", "-i", str(src), "-t", str(seconds), "-vn",
                       "-acodec", "libmp3lame", "-b:a", "96k", str(preview)]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0 or not preview.exists() or preview.stat().st_size <= 1024:
                    return result
        result["preview_file"] = str(preview)
        result["preview_seconds"] = seconds
        result["preview_ready"] = True
        return result

    prepared = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tracks) or 1))) as pool:
        futures = [pool.submit(one, t) for t in tracks]
        for future in as_completed(futures):
            prepared.append(future.result())
    order = {str(t.get("music_id")): i for i, t in enumerate(tracks)}
    prepared.sort(key=lambda t: order.get(str(t.get("music_id")), 9999))
    return prepared


def _mean_volume(ffmpeg: str, src: Path, *, start: float = 0.0,
                 duration: float | None = None) -> Optional[float]:
    cmd = [ffmpeg]
    if duration:
        cmd += ["-ss", str(start), "-t", str(duration)]
    cmd += ["-i", str(src), "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", proc.stderr)
    return float(m.group(1)) if m else None


def _integrated_lufs(ffmpeg: str, src: Path, *, start: float = 0.0,
                     duration: float | None = None) -> Optional[float]:
    """所选窗口的 EBU R128 整体响度（LUFS）。

    `volumedetect` 的 mean_volume 是逐采样均方，与人耳感知的响度不是一回事；
    旁白侧已按 LUFS 归一（见 `audio_loudness`），BGM 必须同口径才能谈「比人声
    低多少 dB」，否则两边各说各话。R3 实测：源 mean_volume=-24.0dB 而
    Integrated=-21.75 LUFS，差 2.25 —— 按 mean 定目标必然偏响。
    """
    cmd = [ffmpeg]
    if duration:
        cmd += ["-ss", str(start), "-t", str(duration)]
    cmd += ["-i", str(src), "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        return None
    try:
        value = float(json.loads(m.group(0))["input_i"])
    except (KeyError, TypeError, ValueError):
        return None
    # loudnorm 对静音/极短素材报 -inf，视为不可用
    return value if value > -70 else None


def prepare_bgm_track(src: Path, out_path: Path, total_duration: float, *,
                      ffmpeg: str = "ffmpeg", target_lufs: float = -28.0,
                      fade_in: float = 0.4, fade_out: float = 0.2,
                      start: float = 0.0, tolerance_lu: float = 1.5) -> Path:
    """裁剪/增益/淡变，产出与成片等长的铺底成品，并闭环校正响度。

    target_lufs：铺底整体响度（EBU R128），默认 -28 LUFS。旁白统一归一到
    -16 LUFS（见 `audio_loudness`），这里低 12 LU —— 即模块文档所说的「比人声
    低约 12dB、不抢旁白」，只是换成了与旁白同口径的度量。

    为什么不用 mean_volume：R3 实测源 mean_volume=-24.0dB 而 Integrated=
    -21.75 LUFS，两者差 2.25dB。旁白按 LUFS 归一而 BGM 按 mean 定档，等于拿两把
    尺子量同一件事 —— 实测结果就是 BGM 做到 -21.7 LUFS，比 8 条旁白还响。

    先测「所选窗口」的真实响度再定增益，成品复测，偏差超阈值再做一次纯增益校正，
    避免按全曲均值估算导致的响度漂移。
    """
    if total_duration <= 0:
        raise ValueError("total_duration must be positive")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fade_out_start = max(0.0, total_duration - fade_out)
    limit = 10 ** (audio_loudness.TP_LIMIT_DB / 20.0)

    # 1) 测所选窗口真实响度（不含淡变），据此定初始增益
    window_lufs = _integrated_lufs(ffmpeg, src, start=start, duration=total_duration)
    if window_lufs is None:
        # 测不出就退回 mean_volume 估算，至少不把增益留成 1.0 静默放行
        window_mean = _mean_volume(ffmpeg, src, start=start, duration=total_duration)
        gain_db = (target_lufs - window_mean) if window_mean is not None else 0.0
    else:
        gain_db = target_lufs - window_lufs

    def _render(g_db: float, dst: Path) -> None:
        af = (f"atrim=start={start}:duration={total_duration},"
              f"volume={g_db:.3f}dB,"
              f"alimiter=limit={limit:.6f}:level=disabled,"
              f"afade=t=in:st=0:d={fade_in},"
              f"afade=t=out:st={fade_out_start:.3f}:d={fade_out},"
              f"apad=whole_dur={total_duration},atrim=duration={total_duration}")
        cmd = [ffmpeg, "-y", "-i", str(src), "-af", af,
               "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(dst)]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    _render(gain_db, out_path)
    # 2) 成品复测，偏差过大则二次纯增益校正
    final_lufs = _integrated_lufs(ffmpeg, out_path)
    if final_lufs is not None and abs(final_lufs - target_lufs) > tolerance_lu:
        tmp = out_path.with_name(out_path.stem + "_fix.m4a")
        subprocess.run([ffmpeg, "-y", "-i", str(out_path),
                        "-af", f"volume={target_lufs - final_lufs:.3f}dB",
                        "-c:a", "aac", "-b:a", "128k", str(tmp)],
                       check=True, capture_output=True, text=True)
        shutil.move(str(tmp), str(out_path))
    return out_path
