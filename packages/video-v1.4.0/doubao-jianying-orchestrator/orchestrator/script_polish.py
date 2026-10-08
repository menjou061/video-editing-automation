"""口播文案衔接校验（文案确认门禁使用，零依赖规则检查）。

解决“AI 剧情是剧情、分镜成片脚本是分镜”的割裂：在生成草稿前的文案确认阶段，
检查分镜口播之间是否衔接自然、整稿是否有漏句/顺序错乱/重复/指代断裂，并给出
可执行的优化建议。真正的改写由大模型完成，本模块只做确定性问题定位。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional


_SENT_END = "。！？!?；;…\n"
_PRONOUN_START = ("它", "他", "她", "这", "那", "其", "该", "此")
_TRANSITION_WORDS = ("然后", "接着", "而且", "并且", "同时", "所以", "因此", "但是",
                     "不过", "于是", "另外", "还有", "再", "更", "也", "就", "才", "又")
_VOICEOVER_LABEL = re.compile(r"^\s*(?:口播|旁白|配音(?:文案)?|台词|文案)\s*[：:]\s*(.+?)\s*$", re.I)
_PRODUCTION_CUES = (
    "镜头", "画面", "景别", "机位", "运镜", "转场", "字幕", "时间码", "时长", "特写",
    "中景", "远景", "近景", "全景", "空镜", "b-roll", "shot", "场景", "动作", "出镜",
)


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    return [p.strip() for p in parts if p.strip()]


def check_cohesion(segment_texts: list[str]) -> list[dict]:
    """检查相邻分镜口播的衔接问题。返回 [{index,type,message,suggestion}]。"""
    issues: list[dict] = []
    texts = [t.strip() for t in segment_texts]
    for i, text in enumerate(texts):
        if not text:
            issues.append({"index": i, "type": "empty_voiceover",
                           "message": "该分镜没有口播文案",
                           "suggestion": "补一句口播，或明确该段为纯字幕/纯BGM镜头"})
            continue
        # 首句以代词开头且无前文，容易突兀
        if i == 0 and text.startswith(_PRONOUN_START):
            issues.append({"index": i, "type": "pronoun_cold_start",
                           "message": f"开场以指代词“{text[0]}”开头，观众无前文参照",
                           "suggestion": "开场先点明主体，再用代词承接"})
        if i == 0:
            continue
        prev = texts[i - 1]
        if not prev:
            continue
        # 相邻高度重复
        sim = _similar(prev, text)
        if sim > 0.72:
            issues.append({"index": i, "type": "repetition", "similarity": round(sim, 2),
                           "message": "与上一句口播高度重复",
                           "suggestion": "合并重复信息或改为递进/补充表述"})
            continue
        # 上句明显没说完（无句读、无连接词），下句又换新话题 → 衔接生硬
        prev_unfinished = prev[-1] not in _SENT_END
        starts_pronoun = text.startswith(_PRONOUN_START)
        has_transition = text.startswith(_TRANSITION_WORDS)
        if prev_unfinished and not starts_pronoun and not has_transition:
            issues.append({"index": i, "type": "abrupt_subject_change",
                           "message": "上一句未收束，本句又无连接词直接切换，衔接生硬",
                           "suggestion": "补上句读，或用“然后/而且/所以/再”等连接词承接"})
    return issues


def check_script_coverage(full_script: str, segment_texts: list[str]) -> list[dict]:
    """检查整稿句子是否都被分镜口播覆盖、顺序是否一致（防止漏句/错位）。"""
    issues: list[dict] = []
    if not full_script.strip():
        return issues
    script_sents = _split_sentences(re.sub(r"\s+", "", full_script))
    seg_joined = re.sub(r"\s+", "", "".join(segment_texts))
    cursor = 0
    for idx, sent in enumerate(script_sents):
        # 整句被拼接稿包含即视为覆盖；否则尝试最长前 8 字片段定位
        key = sent if len(sent) <= 24 else sent[:8]
        pos = seg_joined.find(key, cursor)
        if pos == -1:
            # 容错：按 6 字滑窗找部分重合
            head = sent[:6]
            if head and head in seg_joined:
                cursor = seg_joined.find(head, cursor)
                continue
            issues.append({"script_sentence_index": idx, "type": "missing_in_storyboard",
                           "sentence": sent[:40],
                           "message": "整稿中的这句没有出现在任何分镜口播里",
                           "suggestion": "把它补进对应分镜，或从整稿删除以保持一致"})
        else:
            if pos < cursor - 1:
                issues.append({"script_sentence_index": idx, "type": "order_mismatch",
                               "sentence": sent[:40],
                               "message": "分镜口播顺序与整稿不一致",
                               "suggestion": "按整稿叙事顺序调整分镜口播顺序"})
            cursor = pos
    return issues


def review_voiceover_script(full_script: str, segment_texts: list[str]) -> dict:
    """文案确认门禁的汇总入口：给出问题清单与是否可继续。"""
    cohesion = check_cohesion(segment_texts)
    coverage = check_script_coverage(full_script, segment_texts) if full_script else []
    problems = cohesion + coverage
    return {
        "ok": not problems,
        "issue_count": len(problems),
        "issues": problems,
        "advice": ("文案衔接/覆盖存在问题，请先按 suggestion 优化口播并与用户确认后再生成草稿"
                   if problems else "口播衔接与整稿覆盖校验通过"),
    }


# —— 口播确认单：默认采用用户输入脚本；要求“重新生成”时由大模型基于画面重写后再走本校验 ——

def split_to_sentences(text: str) -> list[str]:
    """把整稿按中文句读切成句子（去空白），用于整稿↔分镜对齐。"""
    return _split_sentences(str(text))


def assess_voiceover_eligibility(script: str) -> dict:
    """判断输入脚本能否原样作为口播，而不把制作说明误送入 TTS。

    这是保守的确定性分类：明确标注的口播/旁白行优先；没有制作指令特征的
    连续自然语言可直接使用；镜头表、时间码和画面说明为主的文本不自动朗读。
    结果仅用于自动填充空分镜，显式 ``text``/``caption`` 永远优先。
    """
    raw = str(script or "").strip()
    if not raw:
        return {"status": "empty", "usable": False, "text": "", "reason": "未提供整稿"}

    labelled: list[str] = []
    other_lines: list[str] = []
    for line in (x.strip(" -\t") for x in raw.splitlines()):
        if not line:
            continue
        match = _VOICEOVER_LABEL.match(line)
        if match:
            labelled.append(match.group(1).strip())
        else:
            other_lines.append(line)
    if labelled:
        text = "".join(labelled)
        return {
            "status": "labelled_voiceover", "usable": bool(text), "text": text,
            "reason": "提取到明确标注的口播/旁白/配音文案", "labelled_lines": len(labelled),
        }

    normalized = re.sub(r"\s+", "", raw)
    lower = normalized.lower()
    cue_count = sum(lower.count(cue) for cue in _PRODUCTION_CUES)
    has_timecode = bool(re.search(r"(?:^|\D)\d{1,2}:\d{2}(?:\D|$)|\b\d+(?:\.\d+)?\s*(?:秒|s)\b", lower))
    list_like = sum(1 for line in other_lines if re.match(r"^(?:\d+[.、)、]|[-*])", line))
    if cue_count >= 2 or has_timecode or (cue_count and list_like >= 2):
        return {
            "status": "production_script", "usable": False, "text": "",
            "reason": "检测到镜头/画面/时间等制作指令，不能直接作为口播", "production_cues": cue_count,
        }
    if len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", normalized)) < 6:
        return {"status": "too_short", "usable": False, "text": "", "reason": "脚本文字过短，无法可靠判断为口播"}
    return {
        "status": "direct_voiceover", "usable": True, "text": raw,
        "reason": "连续自然语言，未检测到制作指令，可直接作为口播",
    }


def distribute_voiceover(script: str, slots: int) -> list[str]:
    """按叙事顺序把可直接口播的整稿均匀分给可配音分镜。"""
    sentences = split_to_sentences(script)
    if slots <= 0 or not sentences:
        return []
    groups = ["" for _ in range(min(slots, len(sentences)))]
    for index, sentence in enumerate(sentences):
        target = min(index * len(groups) // len(sentences), len(groups) - 1)
        groups[target] += sentence
    return groups


def estimate_voice_duration_s(text: str, cps: float = 4.6) -> float:
    """按有效字数估算中文口播时长（秒）。带货口播约 4.2–5 字/秒，默认 4.6。"""
    if not text:
        return 0.0
    visible = re.sub(r"[\s，。！？；：、,.!?;:…~～\-—（）()「」“”\"']", "", str(text))
    n = len(visible)
    punct = len(re.findall(r"[，。！？；、,.!?;:…]", str(text)))
    return round(n / cps + 0.18 * punct + 0.12, 2)  # 句读停顿 + 尾留白


def build_confirmation(full_script: str, segment_texts: list[str],
                       avail_per_segment: list[float] | None = None,
                       cps: float = 4.6) -> dict:
    """生成“口播确认单”：逐句字数/预估时长/是否塞得进画面，以及整体结论。

    - 默认来源 = 用户输入脚本，引擎不擅自改写；
    - avail_per_segment：各分镜画面可用秒数（可选），用于预判声画是否等长；
    - 用户要求重新生成口播时，由大模型重写后再次调用本函数复核。
    """
    review = review_voiceover_script(full_script, segment_texts)
    rows = []
    total_est = 0.0
    for i, t in enumerate(segment_texts):
        est = estimate_voice_duration_s(t, cps)
        total_est += est
        avail = None
        fit = "unknown"
        if avail_per_segment and i < len(avail_per_segment):
            avail = round(float(avail_per_segment[i]), 2)
            if avail <= 0:
                fit = "unknown"
            elif est > avail + 0.35:
                fit = "too_long"          # 口播长于画面，会挂 pending/需精简或换镜头
            else:
                fit = "ok"
        rows.append({"index": i, "text": t, "chars": len(re.sub(r"\s", "", t)),
                     "est_voice_s": est, "avail_video_s": avail, "fit": fit})
    return {
        "source": "input_script",   # input_script=默认采用输入口播(aily 生口播字段/用户脚本)；model_rewritten=用户主动要求重写
        "sentence_count": len(segment_texts),
        "total_estimated_s": round(total_est, 2),
        "per_sentence": rows,
        "review": review,
        "policy": [
            "默认：口播来自输入（aily 内置“生口播”字段/用户脚本），引擎直接采用，只做去空白与按句对齐，不重写、不主动追问是否重生成；",
            "可选：仅当用户明确要求“重新生成/换个说法/重写口播”时，才由模型依据各分镜画面、产品主题、目标时长与带货风格重写；",
            "重写后必须满足：每句与对应画面一致、整稿无漏句、相邻衔接自然，再回到本确认单复核；完全无口播输入时才触发 NEED_VOICEOVER 引导。",
        ],
    }


_SHOT_SUBJECTS = ("人", "人物", "产品", "商品", "浴巾", "场景", "手", "脸", "宠物")
_SHOT_ACTIONS = ("展示", "拿", "放", "打开", "关闭", "擦", "铺", "折叠", "走", "坐", "说", "使用")
_SHOT_TYPES = ("特写", "近景", "中景", "远景", "全景", "俯拍", "侧拍", "推近", "拉远")


def score_storyboard_shot(text: str, *, duration_s: float = 0.0,
                          video_name: str = "", previous: dict | None = None) -> dict:
    """Explainable, low-cost shot understanding score.

    This intentionally starts from a neutral score so ordinary operator
    footage is not incorrectly marked as bad.  It only lowers confidence for
    concrete missing-context signals and never requires another model call.
    """
    raw = str(text or "").strip()
    haystack = f"{raw} {video_name}".lower()
    fields = {
        "subject": next((x for x in _SHOT_SUBJECTS if x in haystack), None),
        "action": next((x for x in _SHOT_ACTIONS if x in haystack), None),
        "shot_type": next((x for x in _SHOT_TYPES if x in haystack), None),
    }
    score = 0.78
    reasons: list[str] = []
    if fields["subject"]:
        score += 0.08
    else:
        reasons.append("未识别明确主体")
        score -= 0.08
    if fields["action"]:
        score += 0.06
    if fields["shot_type"]:
        score += 0.04
    if not raw:
        reasons.append("无口播或字幕上下文")
        score -= 0.05
    if duration_s and duration_s < 0.8:
        reasons.append("镜头时长过短")
        score -= 0.18
    if previous and fields["subject"] and fields["subject"] == previous.get("subject"):
        reasons.append("与上一镜头主体重复")
        score -= 0.04
    score = round(max(0.0, min(1.0, score)), 3)
    return {"score": score, "confidence": "high" if score >= 0.7 else ("medium" if score >= 0.5 else "low"),
            "fields": fields, "reasons": reasons,
            "adjustment": "保留素材，仅优化时长/转场/口播匹配" if score < 0.7 else "none"}
