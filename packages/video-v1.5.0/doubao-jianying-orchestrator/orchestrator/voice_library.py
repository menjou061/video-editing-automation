"""剪映全量音色库加载与“按脚本/剧情相关性”动态推荐。

候选池来自三处，按 speaker_id 合并（同一个 id 的多个名字进 aliases）：
- VALIDATED_META：15 个已实测验证音色，命名准确，推荐排序置顶；
- jianying_voice_catalog.csv：剪映官方文本朗读音色表（861 条界面显示名 ->
  sami speaker_id），运营在飞书「配音名称」里填的就是这张表上的名字，是解析
  运营填写值的权威来源；
- tts_speakers.csv：历史 name_hint，保留兼容。

- 依据音色中文名自动画像（性别、风格、是否方言/外语/动漫角色）；
- 根据脚本文本与 AI 剧情识别意图，对全库按相关性打分排序；
- 默认返回 Top5、最多 Top10，脚本不同结果不同；
- 普通普通话带货自动排除方言、外语、说唱、纯动漫角色、搞怪特效音。
"""
from __future__ import annotations

import csv
import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .platform_env import find_jianying_editor_root

# 已逐个真实合成验证可用的音色（sami id），同等相关度下优先、并作为可靠保底。
# 同时给出比官方 name_hint 更准确、可直接用于相关性匹配的中文名（官方名有“熊二/小孩”等歧义）。
VALIDATED_META: dict[str, tuple[str, str]] = {
    "zh_male_huoli": ("阳光活力男声", "male"),
    # Historical operator label retained from the 1.4.1 Windows package.
    # Explicit resolution is allowed; the role remains excluded from generic
    # recommendation unless the script specifically calls for a character.
    "zh_male_sunwukong_clone2": ("猴哥", "male"),
    "zh_male_xionger_stream_gpu": ("亲和暖男声", "male"),
    "zh_male_shaonianzixin_moon_bigtts": ("清透少年音", "male"),
    "ICL_zh_male_qinggandiantai": ("磁性情感男声", "male"),
    "zh_male_commentate_emo_neutral": ("沉稳解说男声", "male"),
    "zh_male_iclvop_xiaolinlaotou": ("老年男声", "male"),
    "zh_male_zhengtaikp": ("正太童声", "child"),
    "zh_female_xiaopengyou": ("甜美年轻女声", "female"),
    "ICL_zh_female_huoponvhai": ("活泼带货女声", "female"),
    "zh_female_guaiqiaogirl": ("轻柔乖巧女声", "female"),
    "ICL_zh_female_szrxinxin_jianying": ("温柔知性女声", "female"),
    "zh_female_inspirational": ("大气励志女声", "female"),
    "zh_female_iclvop_dzyboyin": ("专业科普女声", "female"),
    "zh_female_iclvop_gzyxqmama": ("暖心妈妈音", "female"),
    "zh_female_iclvop_jiangsiqitongsheng": ("女童声", "child"),
}
VALIDATED_SAMI = set(VALIDATED_META)

# —— 性别画像 ——
_FEMALE = ("女", "姐", "妹", "妈", "奶", "婆", "娘", "主妇", "靓女", "女王", "女主播",
           "幺妹", "紫薇", "黛玉", "girl", "Girl", "顾姐", "安琦拉")
_MALE = ("男", "哥", "爷", "叔", "爸", "大叔", "小伙", "掌柜", "男主", "佛祖", "皇上",
         "老者", "boy", "Male", "表哥", "柜哥")
_CHILD = ("娃", "童", "孩", "萌", "小可酱", "佩奇", "小新", "海绵", "波波", "弟弟", "妹妹")

# —— 默认排除项（普通普通话带货不适合）——
_DIALECT = ("东北", "广西", "台湾", "川", "粤语", "西安", "重庆", "河南", "天津", "幺妹")
_FOREIGN = ("English", "英语")
_ROLE = ("猴哥", "八戒", "紫薇", "皇上", "容嬷嬷", "如来", "观音", "太乙", "小新", "海绵",
         "佩奇", "周星星", "黛玉", "知青", "魔童", "侦探", "安琦拉", "云龙", "懒小羊",
         "派星星", "天线", "蜡笔", "小明", "八戒", "TV")
_NOVELTY = ("说唱", "感冒电音", "抽象", "恐怖", "咆哮", "做作", "夹子", "阴柔", "威猛",
            "搞怪", "苦命")

# —— 剧情意图 → 音色名称命中词（命中越多越相关）——
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "带货": ("广告", "促销", "叫卖", "电视广告", "步行街", "活泼", "元气", "阳光", "甜美",
             "亲切", "主播", "柜", "欢快", "靓女", "小姐姐", "活力"),
    "科普": ("科普", "知识讲解", "讲解", "纪录片", "解说", "专题片", "知性", "沉稳", "知识",
             "清亮", "播报"),
    "新闻专业": ("新闻", "主播", "宣讲", "企业", "宣传", "专题", "播音", "端庄", "正气",
                 "广告", "沉稳", "电台", "广播"),
    "情感治愈": ("情感", "深情", "温情", "温暖", "温柔", "鸡汤", "感性", "磁声", "低语",
                 "电台", "讲述", "诉说", "旁白", "馨馨", "温婉"),
    "年轻甜美": ("甜美", "甜心", "小姐姐", "少女", "俏皮", "盐系", "清爽", "温婉", "亲切",
                 "靓女", "乖巧", "妹妹"),
    "母婴亲子": ("萌娃", "少儿", "小孩", "小女孩", "故事", "妈妈", "主妇", "姐姐", "玲玲",
                 "温柔", "肥花花", "萌"),
    "大气励志": ("激昂", "壮阔", "正气", "宣传", "专题", "旁白", "威猛", "磅礴", "厚重",
                 "饱满", "厚实"),
    "悬疑叙事": ("悬疑", "电影", "深沉", "讲述", "低声", "沉稳", "旁白"),
}
# 脚本/剧情关键词 → 意图
SCENE_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("带货", "种草", "下单", "买", "优惠", "促销", "爆款", "闭眼冲", "推荐", "划算", "价",
      "宝贝", "链接", "囤", "奶被纸", "纸巾"), ("带货", "年轻甜美")),
    (("科普", "知识", "原理", "为什么", "讲解", "纪录片", "冷知识", "成分", "技术"),
     ("科普", "新闻专业")),
    (("新闻", "播报", "企业", "品牌", "宣传", "发布会", "形象", "宣讲", "正式"),
     ("新闻专业", "大气励志")),
    (("情感", "治愈", "故事", "心情", "深夜", "温暖", "温柔", "孤独", "陪伴", "电台"),
     ("情感治愈",)),
    (("婴儿", "母婴", "宝宝", "妈妈", "亲子", "孩子", "儿童", "家庭", "长辈", "老人"),
     ("母婴亲子", "情感治愈")),
    (("励志", "奋斗", "梦想", "燃", "磅礴", "大气", "祖国", "时代"), ("大气励志",)),
    (("悬疑", "推理", "电影", "剧情反转", "探秘", "惊险"), ("悬疑叙事",)),
    (("少女", "甜美", "年轻", "元气", "活泼", "俏皮", "可爱"), ("年轻甜美", "带货")),
)


@dataclass
class LibraryVoice:
    speaker_id: str
    name: str
    gender: str          # male/female/child/unknown
    tags: tuple[str, ...]
    validated: bool
    # 同一个 speaker_id 在剪映里可能同时挂着多个名字：官方界面显示名、历史
    # name_hint、以及本文件为推荐而写的准确命名。运营填的是官方显示名，所以
    # 解析必须认全部名字，只认 name 会把「憨熊」这类运营常用音色判成不存在。
    aliases: tuple[str, ...] = ()
    official: bool = False   # name 是否来自剪映官方音色表

    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for n in (self.name, *self.aliases) if n))

    def to_dict(self) -> dict:
        return {"speaker_id": self.speaker_id, "name": self.name,
                "gender": self.gender, "tags": list(self.tags),
                "validated": self.validated, "aliases": list(self.aliases),
                "official": self.official}


def _csv_path() -> Optional[Path]:
    p = _voice_data_root() / "tts_speakers.csv"
    return p if p.exists() else None


def _catalog_path() -> Optional[Path]:
    """剪映官方音色表（界面显示名 -> sami speaker_id）。"""
    p = _voice_data_root() / "jianying_voice_catalog.csv"
    return p if p.exists() else None


def _voice_data_root() -> Path:
    """Resolve machine-provided voice catalogs without bundling app data."""
    configured = os.environ.get("JY_VOICE_CATALOG_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return find_jianying_editor_root() / "data"


def _infer_gender(name: str, sid: str) -> str:
    blob = name + " " + sid
    if any(w in blob for w in _CHILD):
        # 童声优先（“萌娃/小孩/童”），但“少年/少女”归男女年轻
        if any(w in name for w in ("娃", "童", "孩", "萌", "佩奇", "小新", "波波")):
            return "child"
    if any(w in blob for w in _FEMALE):
        return "female"
    if any(w in blob for w in _MALE):
        return "male"
    if "female" in sid.lower():
        return "female"
    if "male" in sid.lower() and "female" not in sid.lower():
        return "male"
    return "unknown"


def _gender_from_tags(tags: tuple[str, ...]) -> Optional[str]:
    """官方音色表的标签比名字更可靠：很多显示名（曼波讲故事/懒洋洋）不含性别词。"""
    s = set(tags)
    if "儿童" in s or "童声" in s:
        return "child"
    if "女" in s or "女声" in s:
        return "female"
    if "男" in s or "男声" in s:
        return "male"
    return None


def _excluded(name: str, intents: set[str]) -> bool:
    """普通场景排除方言/外语/角色/搞怪；剧情明确需要时保留。"""
    if any(w in name for w in _FOREIGN):
        return True
    if any(w in name for w in _DIALECT):
        return True
    if any(w in name for w in _NOVELTY):
        return True
    if any(w in name for w in _ROLE):
        # 角色音仅在明确剧情/角色意图时保留
        return not ({"悬疑叙事"} & intents)
    return False


@functools.lru_cache(maxsize=1)
def load_library() -> list[LibraryVoice]:
    """读取并画像全量音色（进程内缓存）。

    三层按 speaker_id 合并，第一个见到的名字当主名（推荐展示用），后面见到的
    同名 id 只追加进 aliases。官方表排在历史 csv 之前，所以同一 id 的主名优先
    是剪映界面真实显示名。
    """
    voices: list[LibraryVoice] = []
    index: dict[str, LibraryVoice] = {}

    def add(sid, name, gender, validated, official, tags=()):
        sid = (sid or "").strip()
        name = (name or "").strip()
        if not sid:
            return
        cur = index.get(sid)
        if cur is None:
            if not name:
                name = sid
            voice = LibraryVoice(sid, name, gender or _infer_gender(name, sid),
                                 tags or (name,), validated, (), official)
            index[sid] = voice
            voices.append(voice)
            return
        # 同一个 id 又见到别的名字 -> 只加别名，不动既有画像
        if name and name not in cur.names():
            cur.aliases = (*cur.aliases, name)

    # 1) 已验证音色（命名准确，推荐排序置顶）
    for sid, (name, gender) in VALIDATED_META.items():
        add(sid, name, gender, True, False)
    # 2) 剪映官方音色表（权威界面显示名 -> sami id）
    path = _catalog_path()
    if path:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for row in csv.reader(f):
                if not row or row[0].startswith("#") or row[0] == "title":
                    continue
                title = row[0].strip()
                sid = row[1].strip() if len(row) > 1 else ""
                tags = tuple(t for t in (row[4].split("/") if len(row) > 4 else []) if t)
                add(sid, title, _gender_from_tags(tags), sid in VALIDATED_SAMI,
                    True, tags=tags or (title,))
    # 3) 历史 name_hint（保留兼容）
    path = _csv_path()
    if path:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for row in csv.reader(f):
                if not row or row[0].startswith("#") or row[0] == "speaker_id":
                    continue
                sid = row[0].strip()
                name = row[1].strip() if len(row) > 1 else sid
                add(sid, name, None, sid in VALIDATED_SAMI, False)
    return voices


def detect_intents(script: str) -> list[str]:
    """从脚本/剧情识别相关意图，按命中先后与次数排序。"""
    hits: dict[str, int] = {}
    for kws, intents in SCENE_RULES:
        n = sum(1 for k in kws if k in script)
        if n:
            for it in intents:
                hits[it] = hits.get(it, 0) + n
    ordered = [k for k, _ in sorted(hits.items(), key=lambda x: -x[1])]
    return ordered or ["带货", "年轻甜美"]  # 无命中按通用带货


def _score(v: LibraryVoice, intents: list[str], gender: Optional[str]) -> int:
    s = 0
    for rank, intent in enumerate(intents):
        weight = 4 - rank  # 越靠前的意图权重越高
        for kw in INTENT_KEYWORDS.get(intent, ()):
            if kw in v.name:
                s += weight
    if gender and v.gender == gender:
        s += 1
    if v.validated:
        s += 2  # 已实测可用，可靠保底
    return s


def recommend(script: str = "", *, topn: int = 5, maxn: int = 10,
              gender: Optional[str] = None,
              include_exotic: bool = False) -> list[dict]:
    """按脚本/剧情对全量音色做相关性排序，返回 Top N（默认5，硬上限10）。"""
    topn = max(1, min(int(topn), int(maxn)))
    intents = set(detect_intents(script))
    ordered_intents = detect_intents(script)
    gender = ({"男": "male", "女": "female", "童": "child"}.get(gender or "", gender))

    pool = load_library()
    scored: list[tuple[int, int, LibraryVoice]] = []
    for order, v in enumerate(pool):
        if not include_exotic and _excluded(v.name, intents):
            continue
        if gender and v.gender not in (gender, "unknown"):
            continue
        sc = _score(v, ordered_intents, gender)
        scored.append((sc, -order if v.validated else order, v))
    # 相关分降序；同分：已验证优先、其次保持官方热门顺序
    scored.sort(key=lambda x: (-x[0], 0 if x[2].validated else 1, x[1]))

    picked: list[dict] = []
    for sc, _, v in scored:
        d = v.to_dict()
        d["score"] = sc
        d["matched_intents"] = ordered_intents
        picked.append(d)
        if len(picked) >= topn:
            break
    return picked


def find_by_id_or_name(token: str) -> Optional[LibraryVoice]:
    """按 speaker_id 或任一显示名/别名在全库定位。"""
    token = token.strip()
    for v in load_library():
        if v.speaker_id == token or token in v.names():
            return v
    return None


def find_exact_name(token: str) -> list[LibraryVoice]:
    """Return every row whose display name or alias is exactly ``token``."""
    token = str(token or "").strip()
    if not token:
        return []
    return [v for v in load_library() if token in v.names()]


_NAME_NOISE = re.compile(r"[\s　·・\-_—–()（）\[\]【】]")


def _fold(name: str) -> str:
    """归一化：去掉空格与常见分隔符并折叠大小写。"""
    return _NAME_NOISE.sub("", str(name or "")).casefold()


def _split_key(folded: str) -> tuple[list[str], list[str]]:
    """把归一化后的名字拆成（拉丁字母数字, 其余）两个多重集。

    字母错位只可能发生在拉丁部分，中文必须逐字相同——否则「ASMR女声」会被
    「AMSR男声」误命中，「Vlog旁白」会被「元气volg」误命中。
    """
    latin = sorted(c for c in folded if c.isascii() and c.isalnum())
    other = sorted(c for c in folded if not (c.isascii() and c.isalnum()))
    return latin, other


def find_normalized_name(token: str) -> list[LibraryVoice]:
    """容错解析：忽略大小写/空格/连字符，再容忍拉丁字母错位。

    运营是手输的，实测有「元气volg / 元气Vlog」「AMSR男声 / ASMR男声」这两种
    确定性的书写差异。这里只补这两类——语义最近邻不在其中，那正是之前发错
    音色的原因。
    """
    token = str(token or "").strip()
    if not token:
        return []
    folded = _fold(token)
    if not folded:
        return []
    pool = load_library()
    hits = [v for v in pool if any(_fold(n) == folded for n in v.names())]
    if hits:
        return hits
    # 字母错位（AMSR <-> ASMR）：拉丁部分比多重集，中文部分必须完全一致，
    # 且拉丁部分至少 3 个字符，否则「TVB女声」这类短串会跟别的名字撞上。
    latin, other = _split_key(folded)
    if len(latin) < 3:
        return []
    out = []
    for v in pool:
        for n in v.names():
            if _split_key(_fold(n)) == (latin, other):
                out.append(v)
                break
    return out


def find_candidates(token: str, *, limit: int = 8) -> list[dict]:
    """Return deterministic alternatives for an unknown operator label.

    Exact official names are ranked first, followed by name-token matches and
    finally ordinary female/male candidates inferred from the request.  The
    result is for an explicit human choice; callers must not auto-select it.
    """
    query = str(token or "").strip().lower()
    pool = load_library()
    ranked: list[tuple[int, int, LibraryVoice]] = []
    for order, voice in enumerate(pool):
        name = voice.name.lower()
        score = 0
        if query and name == query:
            score += 100
        if query and query in name:
            score += 50
        query_tokens = [x for x in re.split(r"\s+", query) if x]
        score += sum(5 for x in query_tokens if x in name)
        if "女" in query or "female" in query:
            score += 8 if voice.gender == "female" else 0
        if "男" in query or "male" in query:
            score += 8 if voice.gender == "male" else 0
        if score:
            ranked.append((score, order, voice))
    if not ranked:
        # No exact official row: show candidates for explicit human choice
        # rather than guessing an unrelated legacy mapping.
        target_gender = "female" if any(x in query for x in ("女", "female")) else None
        ranked = [(1 if v.validated else 0, i, v) for i, v in enumerate(pool)
                  if not target_gender or v.gender == target_gender]
    ranked.sort(key=lambda row: (-row[0], 0 if row[2].validated else 1, row[1]))
    return [v.to_dict() for _, _, v in ranked[:max(1, int(limit))]]
