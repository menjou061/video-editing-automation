"""TTS 音色目录与选择：性别 × 年龄 × 风格矩阵，支持按剧情自动推荐与二次选择。

- sami：剪映内部高质量音色 id（优先）。
- edge：edge-tts 兜底音色（sami 不可用时离线/公网兜底）。
- 选择入口 resolve_voice：接受 preset 名 / 直接 speaker id / {gender,age,timbre} 条件。
- 推荐入口 recommend_voices：按脚本文案与剧情风格给出首选 + 候选（供二次选择）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, replace
from typing import Optional


@dataclass(frozen=True)
class VoicePreset:
    key: str
    label: str
    sami: str
    edge: str
    gender: str        # male / female / child
    age: str           # child / young / mature / middle / senior
    timbre: str        # 风格标签
    tags: tuple[str, ...]
    desc: str
    # 运营在「配音名称」里会把语速直接写在音色名后面（「猴哥1.6倍速」）。倍速不
    # 参与音色匹配，但必须解析出来随音色一起往下传，否则要么解析失败要么被静默
    # 丢掉。1.0 = 原速。
    speed: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


class VoiceResolutionError(ValueError):
    """Raised when an operator label cannot be mapped to one official voice."""

    def __init__(self, message: str, *, voice_input: str = "", candidates: Optional[list[dict]] = None):
        super().__init__(message)
        self.voice_detail = {
            "voice_input": voice_input,
            "candidates": list(candidates or []),
        }


# 精选可用音色矩阵（sami id 均来自剪映内置音色库；edge 为标准中文神经音色）
VOICE_PRESETS: list[VoicePreset] = [
    # —— 男声 ——
    VoicePreset("male_energetic", "阳光活力男声", "zh_male_huoli", "zh-CN-YunxiNeural",
                "male", "young", "活力带货", ("带货", "种草", "年轻", "阳光", "促销"), "默认带货男声，明快有感染力"),
    VoicePreset("male_friendly", "亲和暖男声", "zh_male_xionger_stream_gpu", "zh-CN-YunxiNeural",
                "male", "young", "亲和", ("亲和", "日常", "vlog"), "亲和松弛，适合生活/vlog"),
    VoicePreset("male_youth", "清透少年音", "zh_male_shaonianzixin_moon_bigtts", "zh-CN-YunzeNeural",
                "male", "young", "少年清透", ("少年", "青春", "清爽"), "清透少年感"),
    VoicePreset("male_warm", "磁性情感男声", "ICL_zh_male_qinggandiantai", "zh-CN-YunyangNeural",
                "male", "mature", "温柔磁性", ("情感", "电台", "治愈", "磁性", "温柔"), "低沉磁性，情感/治愈叙事"),
    VoicePreset("male_pro", "沉稳解说男声", "zh_male_commentate_emo_neutral", "zh-CN-YunyangNeural",
                "male", "middle", "专业解说", ("科普", "解说", "知识", "纪录片", "专业", "企业"), "沉稳专业，科普/企业解说"),
    VoicePreset("male_senior", "老年男声", "zh_male_iclvop_xiaolinlaotou", "zh-CN-YunfengNeural",
                "male", "senior", "苍老", ("老年", "爷爷", "长辈"), "老年男性音色"),
    VoicePreset("boy", "正太童声", "zh_male_zhengtaikp", "zh-CN-YunxiaNeural",
                "child", "child", "男童", ("正太", "男孩", "儿童"), "小男孩童声"),
    # —— 女声 ——
    VoicePreset("female_sweet", "甜美年轻女声", "zh_female_xiaopengyou", "zh-CN-XiaoxiaoNeural",
                "female", "young", "甜美", ("甜美", "年轻", "通用"), "甜美自然，通用女声"),
    VoicePreset("female_lively", "活泼带货女声", "ICL_zh_female_huoponvhai", "zh-CN-XiaoyiNeural",
                "female", "young", "活力带货", ("活泼", "带货", "种草", "元气"), "活泼元气，强带货节奏"),
    VoicePreset("female_soft", "轻柔乖巧女声", "zh_female_guaiqiaogirl", "zh-CN-XiaohanNeural",
                "female", "young", "轻柔", ("轻柔", "乖巧", "安静"), "轻柔乖巧，舒缓口播"),
    VoicePreset("female_gentle", "温柔知性女声", "ICL_zh_female_szrxinxin_jianying", "zh-CN-XiaoyiNeural",
                "female", "mature", "温柔知性", ("知性", "温柔", "母婴", "质感"), "温柔知性，母婴/质感种草"),
    VoicePreset("female_inspire", "大气励志女声", "zh_female_inspirational", "zh-CN-XiaoqiuNeural",
                "female", "mature", "大气", ("大气", "励志", "品牌", "宣传"), "大气有力，品牌/宣传片"),
    VoicePreset("female_pro", "专业科普女声", "zh_female_iclvop_dzyboyin", "zh-CN-XiaoqiuNeural",
                "female", "middle", "专业", ("科普", "专业", "知识", "讲解", "新闻"), "专业可信，知识讲解/新闻播报"),
    VoicePreset("female_mom", "暖心妈妈音", "zh_female_iclvop_gzyxqmama", "zh-CN-XiaoruiNeural",
                "female", "middle", "妈妈", ("妈妈", "长辈", "家庭", "母婴"), "暖心成熟妈妈声"),
    VoicePreset("girl", "女童声", "zh_female_iclvop_jiangsiqitongsheng", "zh-CN-YunxiaNeural",
                "child", "child", "女童", ("女童", "女孩", "儿童", "童声"), "小女孩童声"),
]

_BY_KEY = {p.key: p for p in VOICE_PRESETS}
_BY_SAMI = {p.sami: p for p in VOICE_PRESETS}

# Stable aliases are limited to labels that identify one official CSV entry.
# Generic or UI marketing labels stay ambiguous and must be selected by a user.
VOICE_ALIASES = {
    "甜美女声": "ICL_zh_female_basidigua2",
    "端庄女声": "ICL_zh_female_jilupianxq2",
    "温婉女声": "zh_female_iclvop_gzyxqmama",
    "乖巧女孩": "zh_female_iclvop_jiangsiqitongsheng",
    "活泼女孩": "ICL_zh_female_huoponvhai",
    # 「熊二」是剪映「变声」面板里同一个音色的名字，文本朗读面板显示名为「憨熊」。
    # 运营与历史记录会用「熊二」，指向的确实是同一个 sami id（已试听确认）。
    "熊二": "zh_male_xionger_stream_gpu",
    # The 1.4.1 package used this exact speaker id for the operator label.
    "猴哥": "zh_male_sunwukong_clone2",
}

# 剧情关键词 → 偏好音色 key（命中越多越优先）
_SCENE_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("宝宝", "婴儿", "母婴", "奶粉", "纸尿裤", "幼儿"), ("female_gentle", "female_mom", "female_sweet")),
    (("儿童", "小孩", "小朋友", "童", "动画"), ("girl", "boy", "female_sweet")),
    (("科普", "知识", "原理", "纪录片", "教学", "课堂", "为什么"), ("male_pro", "female_pro", "male_warm")),
    (("情感", "治愈", "故事", "心情", "深夜", "温暖"), ("male_warm", "female_soft", "female_gentle")),
    (("企业", "品牌", "宣传", "形象", "发布会"), ("female_inspire", "male_pro")),
    (("老年", "爷爷", "奶奶", "长辈", "爸妈"), ("male_senior", "female_mom")),
    (("带货", "种草", "下单", "闭眼冲", "买", "优惠", "爆款", "促销", "推荐"),
     ("male_energetic", "female_lively")),
    (("vlog", "日常", "分享", "生活"), ("male_friendly", "female_sweet")),
]

_GENDER_ALIAS = {"男": "male", "男性": "male", "男生": "male", "male": "male",
                 "女": "female", "女性": "female", "女生": "female", "female": "female",
                 "童": "child", "儿童": "child", "孩子": "child", "child": "child"}
_AGE_ALIAS = {"儿童": "child", "孩子": "child", "少年": "young", "年轻": "young", "青年": "young",
              "成熟": "mature", "中年": "middle", "老年": "senior", "长辈": "senior"}
_VALID_AGES = {"child", "young", "mature", "middle", "senior"}


def _norm_age(value: str) -> Optional[str]:
    """年龄归一化：兼容标准值(young/middle…)与中文(年轻/中年…)。"""
    s = str(value).strip()
    if s in _VALID_AGES:
        return s
    return _AGE_ALIAS.get(s)


def list_voices() -> list[dict]:
    """返回全部可选音色（供用户二次选择）。"""
    return [p.to_dict() for p in VOICE_PRESETS]


# 试听默认样词（固定，不每次让用户给；男女通用、能体现带货语感）
AUDITION_TEXT = "这款我用了大半个月，成分温和不刺激，家里人都能用，真心推荐，喜欢就别错过。"

# edge-tts 兜底音色按性别映射（全库音色 sami 不可用时的公网兜底）
_EDGE_BY_GENDER = {"male": "zh-CN-YunxiNeural", "female": "zh-CN-XiaoxiaoNeural",
                   "child": "zh-CN-YunxiaNeural", "unknown": "zh-CN-XiaoxiaoNeural"}
_LIB_PRESET_CACHE: dict[str, VoicePreset] = {}


def _preset_from_library(lib_voice, matched_name: str = "") -> VoicePreset:
    """把全库音色（LibraryVoice）动态包装成统一 VoicePreset。

    ``matched_name`` 是运营实际填的那个词。它和库里主名不一致时（例如填「憨熊」
    而该 id 的主名是推荐用名）拿它当展示名，这样生产日志和阻断提示里出现的是
    运营认得出来的名字，而不是另一个别名。
    """
    sid = lib_voice.speaker_id
    shown = (matched_name or lib_voice.name or sid).strip()
    key = (sid, shown)
    if key in _LIB_PRESET_CACHE:
        return _LIB_PRESET_CACHE[key]
    gender = lib_voice.gender if lib_voice.gender != "unknown" else "unknown"
    p = VoicePreset(sid, shown, sid, _EDGE_BY_GENDER.get(gender, "zh-CN-XiaoxiaoNeural"),
                    gender, "child" if gender == "child" else "unknown",
                    shown, (shown,), "剪映全库音色")
    _LIB_PRESET_CACHE[key] = p
    return p


def recommend_voices(script_text: str = "", *, style: str = "", persona: str = "",
                     topk: int = 5) -> list[dict]:
    """按脚本/AI剧情对【剪映全量音色库】动态相关性排序，返回 Top N（默认5，上限10）。

    脚本不同、推荐结果不同；首个为首选，其余为候选，供试听后二次选择。
    """
    from . import voice_library as vl
    text = f"{script_text} {style} {persona}".strip()
    gender = _GENDER_ALIAS.get(str(persona).strip()) or _GENDER_ALIAS.get(str(style).strip())
    recs = vl.recommend(text, topn=topk, maxn=10, gender=gender)
    out = []
    for r in recs:
        p = resolve_voice(r["speaker_id"])
        d = p.to_dict()
        d["score"] = r.get("score")
        d["matched_intents"] = r.get("matched_intents")
        out.append(d)
    return out


# 运营把语速写在音色名末尾：「猴哥1.6倍速」「真人播客女1.2倍速」。
_SPEED_RE = re.compile(r"^\s*(?P<name>.*?)[\s·]*?(?P<speed>\d+(?:\.\d+)?)\s*倍速\s*$")


def _split_speed(spec: str) -> tuple[str, Optional[float]]:
    """拆出末尾倍速：「猴哥1.6倍速」-> ("猴哥", 1.6)；没有倍速时原样返回。"""
    raw = str(spec or "").strip()
    m = _SPEED_RE.match(raw)
    if not m:
        return raw, None
    name = m.group("name").strip()
    try:
        speed = float(m.group("speed"))
    except ValueError:
        return raw, None
    # 离谱的数值当没写，别把一个打错的速度当真
    if not name or not 0.1 <= speed <= 10:
        return raw, None
    return name, speed


def resolve_voice(spec=None) -> VoicePreset:
    """把用户/脚本的声音描述解析为具体 preset。

    支持：
    - None：默认阳光活力男声；
    - str：preset key / sami speaker id / 中文标签（男/女/老年…），末尾可带倍速
      （「猴哥1.6倍速」），倍速解析进 preset.speed；
    - dict：{voice|speaker|key|gender|age|timbre}。
    """
    if spec is None:
        return _BY_KEY["male_energetic"]
    if isinstance(spec, VoicePreset):
        return spec

    if isinstance(spec, str):
        bare, speed = _split_speed(spec)
        if speed is not None:
            # 音色名部分照常走完整解析链，倍速只是附加属性
            return replace(resolve_voice(bare), speed=speed)
        s = spec.strip()
        if not s:
            raise VoiceResolutionError("音色名称为空；请填写剪映官方显示名或 speaker_id",
                                       voice_input=s)
        if s in _BY_KEY:
            return _BY_KEY[s]
        if s in _BY_SAMI:
            return _BY_SAMI[s]
        # 剪映全量音色库：按 speaker_id 或中文名定位并动态包装
        from . import voice_library as vl
        # Official display-name equality is authoritative.  Check it before
        # curated aliases so an exact catalog row can never be shadowed by a
        # heuristic/legacy alias with the same text.
        exact = vl.find_exact_name(s)
        if len(exact) == 1:
            return _preset_from_library(exact[0], matched_name=s)
        if len(exact) > 1:
            candidates = [v.to_dict() for v in exact]
            raise VoiceResolutionError(
                f"剪映显示名“{s}”对应多个 speaker_id；请从候选中选择 speaker_id",
                voice_input=s, candidates=candidates)
        # 大小写/空格/字母顺序差异（运营手输常见），只做确定性归并
        near = vl.find_normalized_name(s)
        if len(near) == 1:
            return _preset_from_library(near[0], matched_name=s)
        if len(near) > 1:
            raise VoiceResolutionError(
                f"剪映音色“{s}”忽略大小写与空格后对应多个 speaker_id；"
                "请从候选中选择 speaker_id",
                voice_input=s, candidates=[v.to_dict() for v in near])
        alias_sid = VOICE_ALIASES.get(s)
        if alias_sid:
            alias_voice = vl.find_by_id_or_name(alias_sid)
            if alias_voice is not None:
                return _preset_from_library(alias_voice)
        libv = vl.find_by_id_or_name(s)
        if libv is not None:
            return _preset_from_library(libv)
        if s in _GENDER_ALIAS:
            candidates = vl.find_candidates(s)
            raise VoiceResolutionError(
                f"音色“{s}”不是官方显示名；请填写剪映显示名或 speaker_id",
                voice_input=s, candidates=candidates)
        candidates = vl.find_candidates(s)
        raise VoiceResolutionError(
            f"音色“{s}”无法唯一映射到剪映音色；请从候选中选择 speaker_id",
            voice_input=s, candidates=candidates)

    if isinstance(spec, dict):
        if spec.get("speaker"):
            speaker = str(spec["speaker"]).strip()
            if speaker in _BY_SAMI:
                return _BY_SAMI[speaker]
            from . import voice_library as vl
            exact = vl.find_exact_name(speaker)
            if len(exact) == 1:
                return _preset_from_library(exact[0])
            if len(exact) > 1:
                raise VoiceResolutionError(
                    f"剪映显示名“{speaker}”对应多个 speaker_id；请从候选中选择 speaker_id",
                    voice_input=speaker, candidates=[v.to_dict() for v in exact])
            libv = vl.find_by_id_or_name(speaker)
            if libv is not None:
                return _preset_from_library(libv)
            candidates = vl.find_candidates(speaker)
            raise VoiceResolutionError(
                f"音色“{speaker}”无法唯一映射到剪映音色；请从候选中选择 speaker_id",
                voice_input=speaker, candidates=candidates)
        if spec.get("key") in _BY_KEY:
            return _BY_KEY[spec["key"]]
        if spec.get("voice") in _BY_KEY:
            return _BY_KEY[spec["voice"]]
        if spec.get("voice"):
            return resolve_voice(str(spec["voice"]))
        gender = _GENDER_ALIAS.get(str(spec.get("gender", "")).strip())
        age = _norm_age(spec.get("age", ""))
        timbre = str(spec.get("timbre", "")).strip()
        pool = VOICE_PRESETS
        if gender:
            pool = [p for p in pool if p.gender == gender] or pool
        if age:
            pool = [p for p in pool if p.age == age] or pool
        if timbre:
            hit = [p for p in pool if timbre in p.timbre or timbre in p.tags or timbre in p.label]
            if hit:
                return hit[0]
        return pool[0]

    raise ValueError(f"无法解析的音色描述: {spec!r}")


# ── 唯一音色决议（v1.3.27，用户 2026-09-16 定案第 1 条）────────────────────
#
# 为什么要「唯一」：v1.3.26 及以前，音色在**三个地方**各被决定了一次 ——
# `cli` 触发词注入时一次、`engine.decide_voice` 一次、`engine.synthesize` 再一次，
# 且 `decide_voice` 在智能模式下会自动推荐首选。后果是「同一支视频里，选镜阶段看的
# 音色」和「真正烧音频的音色」可能不是同一个，而合成是**花钱**的：改了音色，
# 之前合成的缓存全部作废，重合成一遍。
#
# 定案要求：manifest 已有 voice → 直接用；为空 → 只调用**一次**现有推荐器并
# 把结果**回写锁定**到 manifest；引擎后续**不得再次选音色**，只能读锁。

# 回写锁定用的 manifest 键（不覆盖运营原话，单独记锁定结果）
VOICE_LOCK_KEY = "voice"
VOICE_LOCKED_FROM_KEY = "voice_locked_from"
VOICE_LOCKED_KEY = "voice_locked"


def resolve_voice_once(spec=None, *, script: str = "",
                       manifest: Optional[dict] = None,
                       persist: bool = True) -> VoicePreset:
    """全流程唯一的音色决议入口。

    决议顺序（每一步都只有一个来源，不存在第二次挑选）：

    1. ``spec`` 显式给出（运营在飞书「配音名称」里填的，或 CLI 传的）→ 按
       ``resolve_voice`` 严格解析。解析失败直接抛 ``VoiceResolutionError``，
       **不静默换成别的音色** —— 换音色等于让运营听到的人声不是他选的那个。
    2. ``spec`` 为空 → 用 ``script`` 调一次 ``recommend_voices`` 取首选，
       并把结果回写 ``manifest["voice"]`` 锁定。
    3. 两者都为空且推荐失败 → 回落到剪映默认音色（``male_energetic``），
       同样回写锁定，保证下游任何时候读 manifest 都能拿到确定的音色。

    ``persist=True`` 时把锁定结果写进 ``manifest``：
    - ``voice``：锁定的音色描述（字符串或 dict），供引擎只读；
    - ``voice_locked_from``：``"operator"`` / ``"recommended"`` / ``"default"``；
    - ``voice_locked``：``True``。

    ``manifest`` 已有的 ``voice`` **优先**，不会被推荐器覆盖 —— 这是定案第 1 条
    头一句。带 ``spec`` 调用时同样不覆盖 manifest 里已有的运营原话。
    """
    m = manifest if isinstance(manifest, dict) else None
    explicit = spec
    if explicit is None and m is not None:
        explicit = m.get(VOICE_LOCK_KEY)

    source = "operator"
    preset: Optional[VoicePreset] = None

    if explicit is not None:
        if isinstance(explicit, str) and explicit.strip().lower() in ("default", "默认"):
            preset, source = resolve_voice(None), "default"
        else:
            preset = resolve_voice(explicit)          # 可能抛 VoiceResolutionError

    if preset is None:
        text = str(script or "").strip()
        if text:
            rec = recommend_voices(text, topk=1)
            if rec:
                preset = resolve_voice(rec[0]["key"] if rec[0].get("key")
                                       else rec[0]["speaker_id"])
                source = "recommended"
        if preset is None:
            preset, source = resolve_voice(None), "default"

    if persist and m is not None:
        # 只在「本来就是空的」时写入 voice；推荐/默认结果必须落进 manifest，
        # 后续引擎只读它，不再自己挑。
        if m.get(VOICE_LOCK_KEY) in (None, ""):
            if source == "operator" and isinstance(explicit, str):
                m[VOICE_LOCK_KEY] = explicit        # 保留运营原话（含倍速写法）
            else:
                m[VOICE_LOCK_KEY] = _as_spec(preset)
        m[VOICE_LOCKED_FROM_KEY] = source
        m[VOICE_LOCKED_KEY] = True

    return preset


def _as_spec(preset: VoicePreset) -> str:
    """音色锁定回写用的描述串：带倍速时保留倍速（「猴哥1.6倍速」）。"""
    if preset.speed and abs(float(preset.speed) - 1.0) > 1e-9:
        return f"{preset.key}{preset.speed:g}倍速"
    return preset.key
