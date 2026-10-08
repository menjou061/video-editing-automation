"""Deterministic storyboard evidence and caption helpers.

The model (or operator) supplies tags discovered from the source frames.  This
module deliberately does not pretend to understand pixels: it verifies that a
spoken product claim has an explicit, traceable visual evidence tag before the
draft writer is allowed to continue.  That keeps a missing shot visible as a
pending item instead of silently producing an unrelated B-roll cut.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


# Canonical claims used by the current household-paper workflows.  Synonyms
# make the gate tolerant of normal wording while keeping the output stable.
CLAIM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "提数": ("提数", "数字", "数值", "计数", "测量", "数出来", "统计"),
    "柔软": ("柔软", "柔韧", "软", "亲肤", "柔软亲肤", "面层", "白色面层", "压纹"),
    "湿水不破": ("湿水不破", "遇水不破", "湿水", "不易破", "不容易破", "不破"),
    "吸水": ("吸水", "吸水性", "吸液", "吸得快"),
    "厚实": ("厚实", "加厚", "厚度", "厚"),
    "大尺寸": ("大尺寸", "大号", "加大", "尺寸大", "够大"),
    "干净": ("干净", "清洁", "去污", "擦得干净", "不留残渣"),
    "不掉屑": ("不掉屑", "少掉屑", "无尘屑", "不易掉屑"),
    "耐用": ("耐用", "用得久", "耐撕", "结实"),
}

# 关联证据（2026-09-16 用户定案）：有些卖点不要求拍到该动作本身。
# 材质/面料特写、拉伸、厚度展示这类「用料与做工」画面，本身就是对该产品质量与结实度的
# 可视说明——例如展示了纸张纹理与韧性，就足以说明「湿水不破」，不必真出现湿水镜头。
#
# 因此为每个卖点登记一批「可替代的关联画面标签」。约束：
#   1) 只登记**材质/工艺/结构**类证据，不登记无关场景（避免把任何画面都算成证据）；
#   2) 关联放行必须在报告里留痕（evidence_basis 记 direct / associative+命中标签），
#      人工复核能一眼看出这一句是靠什么过的，不允许静默通过；
#   3) 画面被明确否定（「未展示湿水」等）时仍然阻断，关联证据不能翻案。
ASSOCIATIVE_EVIDENCE: dict[str, tuple[str, ...]] = {
    # 「湿水不破」：材质/工艺类画面即可说明用料结实，不必真出现湿水镜头
    "湿水不破": ("材质特写", "材质展示", "纸张特写", "纸面特写", "纹理", "压花纹理", "纤维",
                 "拉伸", "拉扯", "韧性", "结实", "厚实", "多层", "叠层",
                 "浸湿", "过水", "湿水"),
    # 「不掉屑」：用户定案——必须拍到实际擦拭/揉搓动作，材质特写不放行
    "不掉屑": ("擦拭", "擦试", "揉搓", "干擦", "抹拭"),
    "厚实": ("材质特写", "材质展示", "纸张特写", "多层", "叠层", "厚度", "手感", "按压", "捏"),
    "耐用": ("材质特写", "材质展示", "纸张特写", "拉伸", "拉扯", "韧性", "结实",
             "反复使用", "多次使用"),
    # 「柔软」：用户定案——纸张特写 / 压花纹理 / 手指按压即算
    "柔软": ("纸张特写", "纸面特写", "材质特写", "材质展示", "压花纹理", "纹理",
             "手指按压", "按压", "手感", "触摸", "揉", "褶皱", "垂坠", "贴合"),
    "吸水": ("材质特写", "材质展示", "纸张特写", "浸湿", "过水", "湿水", "倒水", "擦拭"),
    # 「大尺寸」：用户定案——单张纸完整展示本身就是在描述纸巾尺寸
    "大尺寸": ("单张展示", "单张纸", "纸张特写", "整张展开", "展开", "平铺", "手持展示", "对比"),
    "干净": ("擦拭", "擦试", "清洁", "对比"),
    "提数": ("数量展示", "堆叠", "整箱", "计数", "陈列"),
}

_FIELD_NAMES = (
    "evidence_tags", "visual_tags", "frame_tags", "scene_tags", "asset_tags",
    "visual_description", "visual_evidence", "action", "subject", "shot_type",
)
# Evidence tags are the claim vocabulary; at least one separate audit field
# must describe the observed frame/asset so a manifest cannot pass by merely
# copying the requirement into evidence_tags.
_AUDIT_FIELDS = (
    "visual_tags", "frame_tags", "scene_tags", "asset_tags",
    "visual_description", "visual_evidence", "action", "subject",
    "shot_type", "rationale", "description",
)
_EXPLICIT_REQUIREMENT_FIELDS = ("visual_requirements", "required_visuals")
_PUNCT_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f]")
_NEGATION_TAIL_RE = re.compile(
    r"(?:未展示|未拍到|未体现|没有展示|无法看到|不足以证明|不能证明|不支持|没有|没|未|无|无法|不能|不是|并非)$"
)


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，、;；|\n\r]+", value) if part.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        out: list[str] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return [str(value).strip()] if str(value).strip() else []


def normalize_tags(value: Any) -> list[str]:
    """Normalize tag fields while retaining human-readable Chinese labels."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in _flatten(value):
        tag = re.sub(r"\s+", "", raw).strip()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def inferred_claim_tags(text: str) -> list[str]:
    """Extract claims that need a visible demonstration from spoken text."""
    raw = re.sub(r"\s+", "", str(text or ""))
    found: list[str] = []
    for canonical, aliases in CLAIM_SYNONYMS.items():
        if any(alias in raw for alias in aliases):
            found.append(canonical)
    return found


def _matches(required: str, evidence: str) -> bool:
    req = re.sub(r"\s+", "", str(required or "")).lower()
    if not req:
        return True
    aliases = CLAIM_SYNONYMS.get(str(required), (str(required),))
    for alias in aliases:
        token = re.sub(r"\s+", "", alias).lower()
        if not token:
            continue
        cursor = 0
        while True:
            found = evidence.find(token, cursor)
            if found < 0:
                break
            prefix = evidence[max(0, found - 12):found]
            if not _NEGATION_TAIL_RE.search(prefix):
                return True
            cursor = found + len(token)
    return False


def shot_evidence_text(shot: dict[str, Any]) -> str:
    """Collapse a shot's auditable fields into one searchable evidence string.

    Shared by the evidence gate and the shot matcher so both judge relevance
    from exactly the same observation text.
    """
    values: list[str] = []
    for field in _FIELD_NAMES:
        values.extend(_flatten(shot.get(field)))
    values.extend(_flatten(shot.get("rationale")))
    values.extend(_flatten(shot.get("description")))
    return re.sub(r"\s+", "", " ".join(values)).lower()


def claim_supported(required: str, evidence: str) -> bool:
    """True when ``evidence`` supports ``required`` directly or by association."""
    return _matches(required, evidence) or bool(_associative_hit(required, evidence))


def _associative_hit(required: str, evidence: str) -> str:
    """Return the associated scene tag that stands in for ``required``, or "".

    Used only after direct matching fails; the caller records the hit so the
    report shows a claim passed via association rather than real footage.
    """
    for token in ASSOCIATIVE_EVIDENCE.get(str(required), ()):
        t = re.sub(r"\s+", "", token).lower()
        if t and t in evidence:
            return token
    return ""


def _is_negated(required: str, audit_text: str) -> bool:
    """Return true when an audit description explicitly denies the claim."""
    aliases = CLAIM_SYNONYMS.get(str(required), (str(required),))
    for alias in aliases:
        token = re.sub(r"\s+", "", alias).lower()
        cursor = 0
        while token:
            found = audit_text.find(token, cursor)
            if found < 0:
                break
            prefix = audit_text[max(0, found - 12):found]
            if _NEGATION_TAIL_RE.search(prefix):
                return True
            cursor = found + len(token)
    return False


def evaluate_segments(segments: list[dict[str, Any]], *, require_explicit: bool = False,
                      head_waste_threshold: float = 0.8,
                      tail_waste_threshold: float = 0.8) -> dict[str, Any]:
    """Return evidence coverage and pending blockers for storyboard segments.

    ``visual_requirements`` is required when ``require_explicit`` is true.
    Even without it, known product claims are inferred from the narration and
    still require matching evidence.  Evidence is read only from fields that
    describe the analyzed source frames; requirements themselves are never
    counted as evidence.
    """
    issues: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for index, item in enumerate(segments or [], 1):
        if not isinstance(item, dict):
            issues.append({"index": index, "type": "invalid_segment",
                           "message": "分镜条目不是对象"})
            continue
        text = str(item.get("text", item.get("caption", "")) or "")
        claim_text = str(item.get("claim_text", "") or "").strip()
        if item.get("visual_missing"):
            # 预览缺画面是显式的编排结果，不是把黑色视频冒充证据。
            # 旁白/字幕仍然进入时间线；证据缺口由 operator_note 与报告承载，
            # 不在这里重复生成一组“缺标签”错误。
            coverage.append({
                "index": index,
                "video": "",
                "text": text,
                "claim_text": claim_text,
                "required": [],
                "matched": [],
                "missing": ["visual_missing"],
                "negated": [],
                "evidence_basis": {},
                "match_tier": "visual_missing",
                "degraded_claims": [],
                "evidence": [],
                "audit_evidence": [],
                "action_complete": False,
                "status": "visual_missing",
                "operator_note": item.get("operator_note", ""),
                "reason": item.get("visual_missing_reason", ""),
            })
            continue
        explicit = []
        for field in _EXPLICIT_REQUIREMENT_FIELDS:
            explicit.extend(normalize_tags(item.get(field)))
        explicit_evidence = normalize_tags(item.get("evidence_tags"))
        required: list[str] = []
        for tag in explicit + inferred_claim_tags(text) + inferred_claim_tags(claim_text):
            if tag not in required:
                required.append(tag)

        evidence_values: list[str] = []
        for field in _FIELD_NAMES:
            evidence_values.extend(_flatten(item.get(field)))
        evidence_values.extend(_flatten(item.get("rationale")))
        evidence_values.extend(_flatten(item.get("description")))
        audit_values: list[str] = []
        for field in _AUDIT_FIELDS:
            audit_values.extend(_flatten(item.get(field)))
        evidence = shot_evidence_text(item)
        audit_text = re.sub(r"\s+", "", " ".join(audit_values)).lower()
        negated = [tag for tag in required if _is_negated(tag, audit_text)]
        matched = [tag for tag in required if tag not in negated and _matches(tag, evidence)]
        # 直接证据不足时，允许用「用料与做工」类关联画面补证（见 ASSOCIATIVE_EVIDENCE）。
        # 被画面明确否定的卖点不参与关联补证，且每一次关联放行都记进 evidence_basis。
        evidence_basis: dict[str, dict[str, Any]] = {
            tag: {"via": "direct"} for tag in matched}
        for tag in required:
            if tag in matched or tag in negated:
                continue
            hit = _associative_hit(tag, evidence)
            if hit:
                matched.append(tag)
                evidence_basis[tag] = {"via": "associative", "matched_tag": hit}
        missing = [tag for tag in required if tag not in matched]
        # 关联放行属于**降级档**：先精准匹配（真拍到该场景），找不到才退到
        # 产品展示类画面的模糊匹配。降级必须显式带出来，供人工复核。
        degraded_claims = [tag for tag in matched
                           if evidence_basis.get(tag, {}).get("via") == "associative"]
        match_tier = "fuzzy" if degraded_claims else ("exact" if matched else "none")

        if require_explicit and text and not claim_text:
            issues.append({"index": index, "type": "claim_text_missing",
                           "video": str(item.get("video", "")),
                           "message": "有口播的分镜缺少 claim_text，无法审计该镜头实际讲述的卖点",
                           "suggestion": "先逐句拆出该镜头的实际口播卖点，再建立对应证据区间"})
        if require_explicit and text and not (explicit or explicit_evidence):
            issues.append({"index": index, "type": "missing_visual_requirements",
                           "video": str(item.get("video", "")),
                           "message": "有口播的分镜缺少 visual_requirements/evidence_tags",
                           "suggestion": "先从素材抽帧标注 visual_requirements 与 visual_tags，再生成"})
        if require_explicit and text and not audit_values:
            issues.append({"index": index, "type": "visual_audit_missing",
                           "video": str(item.get("video", "")),
                           "message": "缺少可审计的抽帧/画面描述证据",
                           "suggestion": "补充 visual_tags/frame_tags/visual_description/rationale 等素材观察记录"})
        if missing:
            issues.append({"index": index, "type": "visual_evidence_missing",
                           "video": str(item.get("video", "")),
                           "required": required, "matched": matched, "missing": missing,
                           "message": "口播卖点没有对应的画面证据",
                           "suggestion": "补充能展示该卖点的镜头，或修改口播后重新确认"})
        if negated:
            issues.append({"index": index, "type": "visual_evidence_negated",
                           "video": str(item.get("video", "")), "claims": negated,
                           "message": "画面观察明确否定或未展示该卖点，不能用标签放行",
                           "suggestion": "更换能展示卖点的素材，或删除该卖点后重新确认"})

        action_complete = item.get("action_complete")
        action_status = str(item.get("action_status", "")).strip().lower()
        if (text and require_explicit and action_complete is not True) or action_complete is False or action_status in {"incomplete", "unfinished", "未完成"}:
            issues.append({"index": index, "type": "action_incomplete",
                           "video": str(item.get("video", "")),
                           "message": "镜头动作未完成，不能直接进入成片",
                           "suggestion": "扩大 source_start/duration 或换用完整动作镜头"})

        def number(name: str) -> float:
            try:
                return max(0.0, float(item.get(name, 0.0) or 0.0))
            except (TypeError, ValueError):
                return 0.0

        head_waste = number("head_waste")
        tail_waste = number("tail_waste")
        segment_blocked = bool(missing) or (
            text and require_explicit and (
                not claim_text or not (explicit or explicit_evidence) or not audit_values or action_complete is not True
            )
        )
        if head_waste > head_waste_threshold or tail_waste > tail_waste_threshold:
            segment_blocked = True
            issues.append({"index": index, "type": "excessive_waste",
                           "video": str(item.get("video", "")),
                           "head_waste": head_waste, "tail_waste": tail_waste,
                           "message": "镜头首尾废料过多，需重新选择窗口",
                           "suggestion": "启用镜头窗口清洗并保留完整动作"})

        coverage.append({"index": index, "video": str(item.get("video", "")),
                         "text": text, "claim_text": claim_text,
                         "required": required, "matched": matched,
                         "missing": missing, "negated": negated,
                         "evidence_basis": evidence_basis,
                         "match_tier": match_tier, "degraded_claims": degraded_claims,
                         "evidence": normalize_tags(evidence_values),
                         "audit_evidence": normalize_tags(audit_values),
                         "action_complete": action_complete is True and action_status not in {"incomplete", "unfinished", "未完成"},
                         "status": "pending" if segment_blocked else "ok"})

    return {"ok": not issues, "issue_count": len(issues), "issues": issues,
            "pending_items": issues, "coverage": coverage,
            "policy": "visual evidence is a hard gate for product claims"}


def strip_subtitle_punctuation(text: str) -> str:
    """Remove Chinese/English punctuation from displayed subtitle text."""
    value = str(text or "")
    out: list[str] = []
    for char in value:
        if _PUNCT_RE.match(char):
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        out.append(char)
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()
