"""Classify Feishu production feedback and provide auditable rule text.

This module is intentionally deterministic. It never decides that a task is
successful; it only labels the feedback scope and the reusable rule candidate.
"""
from __future__ import annotations

import re
from typing import Any

_CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("audio_visual_match", ("音画", "口播", "脚本", "画面", "卖点", "匹配", "提数", "柔软", "湿水", "吸水", "干爽", "侧漏")),
    ("shot_quality", ("粗剪", "废料", "动作", "镜头", "裁切", "掐头", "去尾", "重复分镜", "重复镜头")),
    ("subtitle_style", ("字幕", "字号", "字体", "加粗", "标点", "x轴", "y轴", "位置")),
    ("voice_mapping", ("配音", "音色", "speaker", "真人播客女", "音频名称")),
    ("duplicate_shots", ("重复分镜", "重复镜头", "同一镜头", "画面重复")),
)
_GENERIC_HINTS = (
    "通用", "统一", "全局", "默认", "以后", "后续", "每条", "每个", "所有", "都要",
    "整体", "普遍", "规则", "规范", "长期", "同类", "一律", "一直", "多条",
)
_SPECIFIC_HINTS = (
    r"第\s*[0-9一二三四五六七八九十]+\s*(个|条|段|镜头)",
    r"镜头\s*[0-9]+", r"片段\s*[0-9]+", r"本条", r"这条", r"当前视频", r"这个视频",
    r"某一镜", r"某个镜头",
)

_RULE_TEXT = {
    "audio_visual_match": "每句口播卖点必须绑定可审计画面证据；稳定裁切后仍须覆盖证据区间，否则阻断生成。",
    "shot_quality": "粗剪必须避开首尾废料并保留完整动作；动作未完成或废料超阈值时阻断。",
    "subtitle_style": "字幕默认字号 8、非粗体、去除中英文标点、位置 X=0/Y=-0.8（半个画布高），并执行写后结构校验。",
    "voice_mapping": "配音按剪映官方显示名精确映射 speaker_id；无匹配或多匹配时阻断，不自动猜测或回退。",
    "duplicate_shots": "同一临时镜头只允许使用一次，同源视频默认最多使用两次；达到上限时阻断，不用重复画面凑数。",
    "generic_feedback": "将经验证的通用运营反馈写入规则真源，并按版本号发布，禁止无版本静默修改。",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def classify_feedback(text: str) -> dict[str, Any]:
    original = _clean(text)
    categories: list[str] = []
    for category, terms in _CATEGORY_PATTERNS:
        if any(term.lower() in original.lower() for term in terms):
            categories.append(category)
    # duplicate_shots is a specialization of shot quality; preserve both tags
    # but make the more specific rule the primary candidate.
    if "duplicate_shots" in categories:
        primary = "duplicate_shots"
    elif categories:
        primary = categories[0]
    else:
        primary = "generic_feedback"
    explicit_generic = any(hint in original for hint in _GENERIC_HINTS)
    specific = any(re.search(pattern, original, re.IGNORECASE) for pattern in _SPECIFIC_HINTS)
    scope = "generic" if (explicit_generic or (bool(categories) and not specific)) else "task_specific"
    reason = "explicit_generic_phrase" if explicit_generic else ("category_without_shot_scope" if scope == "generic" else "shot_or_task_specific")
    return {
        "scope": scope,
        "category": primary,
        "categories": categories,
        "rule_key": f"feedback.{primary}",
        "rule_text": _RULE_TEXT.get(primary, _RULE_TEXT["generic_feedback"]),
        "reason": reason,
        "original_text": original,
    }


def rule_text(category: str) -> str:
    return _RULE_TEXT.get(category, _RULE_TEXT["generic_feedback"])
