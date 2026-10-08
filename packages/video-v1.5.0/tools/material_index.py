"""One-time visual segment ledger and deterministic category/SKU selection.

Consumes existing visual-analysis facts, not narration-derived guesses. The
Feishu ledger is the human-review authority; this JSON cache is replaceable.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

from .production_eval import digest, file_hash

INDEX_VERSION = 'material-index-1.2'
MANUAL_FIELDS = ('存疑度', '存疑原因', '抽检状态', '使用建议')


def immutable_facts(row: dict) -> dict:
    fields = ('material_id', 'source_path', 'source_sha256', 'start', 'end',
              'category', 'product', 'sku', 'identity_confirmed', 'tags',
              'supported_claims', 'evidence_grade', 'suggested_doubt',
              'suggested_doubt_reason', 'index_version', 'visual_policy_sha256',
              'tagger_version', 'evidence', 'visual_facts')
    return {key: row.get(key) for key in fields}


def facts_valid(row: dict) -> bool:
    return row.get('facts_sha256') == digest(immutable_facts(row))


def make_segment(source: Path, start: float, end: float, *, category: str,
                 product: str, sku: str, facts: dict, policy_hash: str,
                 tagger_version: str, evidence_paths: list[Path]) -> dict:
    if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end <= start:
        raise ValueError('SEGMENT_TIME_INVALID')
    source_hash = file_hash(source)
    proofs = [{'path': str(p.absolute()), 'sha256': file_hash(p)} for p in evidence_paths]
    identity_confirmed = facts.get('product_identity_confirmed') is True and bool(category and sku)
    doubt = '高' if not identity_confirmed or not proofs else str(facts.get('doubt', '中'))
    if doubt not in {'无', '中', '高'}:
        raise ValueError('DOUBT_INVALID')
    tags = facts.get('tags', [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ValueError('VISUAL_TAGS_INVALID')
    row = {'material_id': digest({'source_sha256': source_hash, 'start': start, 'end': end}),
           'source_path': str(source.absolute()), 'source_sha256': source_hash,
           'start': start, 'end': end, 'category': category if identity_confirmed else '待确认',
           'product': product if identity_confirmed else '待确认',
           'sku': sku if identity_confirmed else '待确认',
           'identity_confirmed': identity_confirmed, 'tags': tags,
           'supported_claims': facts.get('supported_claims', []),
           'evidence_grade': facts.get('evidence_grade', '无法证明'),
           'suggested_doubt': doubt,
           'suggested_doubt_reason': str(facts.get('doubt_reason') or
               ('产品身份或视觉证据未确认' if doubt == '高' else '')),
           'doubt': doubt, 'doubt_reason': str(facts.get('doubt_reason') or
               ('产品身份或视觉证据未确认' if doubt == '高' else '')),
           'review_status': '需复核' if doubt in {'中', '高'} else '未抽检',
           'use_advice': '暂缓使用', 'index_version': INDEX_VERSION + ':' + digest({
               'policy': policy_hash, 'tagger': tagger_version})[:12],
           'visual_policy_sha256': policy_hash, 'tagger_version': tagger_version,
           'evidence': proofs, 'archive_path': ''}
    row['visual_facts'] = json.loads(json.dumps(facts.get('shot', {}), ensure_ascii=False))
    row['facts_sha256'] = digest(immutable_facts(row))
    return row


def review_identity(row: dict) -> str:
    """Human approval binds all immutable visual facts, not just the file."""
    return digest(immutable_facts(row))


def apply_review(row: dict, cells: dict, reviewer: str) -> dict:
    if not facts_valid(row):
        raise ValueError('MATERIAL_FACTS_HASH_MISMATCH')
    value = dict(row)
    if cells.get('存疑度') not in {'无', '中', '高'} or cells.get('抽检状态') not in {'未抽检', '已抽检', '需复核'}:
        raise ValueError('REVIEW_OPTIONS_INVALID')
    if cells.get('使用建议') not in {'直接使用', '降级使用', '暂缓使用'} or not reviewer:
        raise ValueError('REVIEWER_OR_USE_ADVICE_INVALID')
    value.update(doubt=cells['存疑度'], doubt_reason=str(cells.get('存疑原因') or ''),
                 review_status=cells['抽检状态'], use_advice=cells['使用建议'], reviewed_by=reviewer)
    value['reviewed_facts_sha256'] = review_identity(value)
    return value


def reusable(row: dict, policy_hash: str, tagger_version: str) -> bool:
    try:
        return (facts_valid(row)
                and row['source_sha256'] == file_hash(Path(row['source_path']))
                and row['visual_policy_sha256'] == policy_hash
                and row['tagger_version'] == tagger_version
                and all(file_hash(Path(p['path'])) == p['sha256'] for p in row['evidence'])
                and bool(row['evidence']))
    except (KeyError, OSError, ValueError):
        return False


def sample_ids(rows: list[dict]) -> list[str]:
    selected = {r['material_id'] for r in rows if r['doubt'] in {'中', '高'}}
    groups = {}
    for row in rows:
        if row['doubt'] == '无':
            groups.setdefault((row['category'], row['sku']), []).append(row)
    for group in groups.values():
        ordered = sorted(group, key=lambda r: r['material_id'])
        selected.update(r['material_id'] for r in ordered[:max(1, math.ceil(len(group) * .1))])
    return sorted(selected)


def select(rows: list[dict], *, category: str, sku: str, tags: list[str],
           policy_hash: str, tagger_version: str) -> list[dict]:
    if not category or not sku or category == '待确认' or sku == '待确认':
        raise ValueError('TASK_PRODUCT_IDENTITY_MISSING')
    chosen = []
    batches = {}
    for row in rows:
        if (row.get('category') == category and row.get('sku') == sku
                and row.get('doubt') == '无' and row.get('batch_id')):
            batches.setdefault(row['batch_id'], []).append(row)
    approved_batches = set()
    for batch_id, group in batches.items():
        selected_ids = set(sample_ids(group))
        samples = [r for r in group if r['material_id'] in selected_ids]
        if samples and all(r.get('review_status') == '已抽检'
                and r.get('use_advice') == '直接使用' and r.get('reviewed_by')
                and r.get('reviewed_facts_sha256') == review_identity(r)
                and reusable(r, policy_hash, tagger_version) for r in samples):
            approved_batches.add(batch_id)
    for row in rows:
        individual = (row.get('review_status') == '已抽检'
                      and row.get('use_advice') in {'直接使用', '降级使用'}
                      and facts_valid(row)
                      and row.get('reviewed_facts_sha256') == review_identity(row))
        sampled = (row.get('batch_id') in approved_batches and row.get('doubt') == '无'
                   and row.get('review_status') == '未抽检' and not row.get('reviewed_by')
                   and row.get('identity_confirmed') and facts_valid(row))
        if (row.get('category') != category or row.get('sku') != sku
                or not row.get('identity_confirmed') or row.get('doubt') == '高'
                or not (individual or sampled)
                or not reusable(row, policy_hash, tagger_version)):
            continue
        score = len(set(tags) & set(row.get('tags', [])))
        if not tags or score:
            chosen.append((score, row))
    return [row for score, row in sorted(chosen, key=lambda pair: (-pair[0], pair[1]['material_id']))]


def archive(row: dict, root: Path) -> dict:
    """Copy only. Never remove or move a material used by a running task."""
    for field in ('category', 'sku'):
        value = row.get(field, '')
        if not value or value == '待确认' or any(ch in value for ch in '/\\<>:"|?*') or value in {'.', '..'}:
            raise ValueError('ARCHIVE_CATEGORY_SKU_INVALID')
    source = Path(row['source_path'])
    if file_hash(source) != row['source_sha256']:
        raise ValueError('SOURCE_CHANGED')
    target = root / row['category'] / row['sku'] / (row['source_sha256'][:16] + source.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst)
    if file_hash(target) != row['source_sha256']:
        raise ValueError('ARCHIVE_HASH_MISMATCH')
    value = dict(row, archive_path=str(target.absolute()))
    # Archiving is not a new visual fact and preserves existing approvals.
    if row.get('reviewed_facts_sha256') == review_identity(row):
        value['reviewed_facts_sha256'] = review_identity(value)
    return value


def feishu_cells(row: dict) -> dict:
    return {'素材ID': row['material_id'], '素材品类': row['category'],
            '产品名称': row['product'], 'SKU': row['sku'], '视觉标签': ','.join(row['tags']),
            '分镜时间': f"{row['start']:.3f}-{row['end']:.3f}s",
            '可支持卖点': ','.join(row['supported_claims']), '证据等级': row['evidence_grade'],
            '素材归档路径': row.get('archive_path') or row['source_path'],
            '内容哈希': row['source_sha256'], '索引版本': row['index_version'],
            '存疑度': row['doubt'], '存疑原因': row['doubt_reason'],
            '抽检状态': row['review_status'], '使用建议': row['use_advice']}


def shot_analysis(rows: list[dict]) -> dict:
    """Use the existing semantic matcher and visual rule engine, unchanged."""
    return {'shots': [dict(row.get('visual_facts', {}), **{'shot_id': row['material_id'], 'video': row['source_path'],
                       'source_start': row['start'], 'source_end': row['end'],
                       'duration': row['end'] - row['start'], 'description': ','.join(row['tags']),
                       'visual_tags': row['tags'], 'supported_claims': row['supported_claims'],
                       'evidence_tags': row['tags'],
                       'material_index_id': row['material_id']}) for row in rows],
            'index_version': INDEX_VERSION, 'semantic_matches': [],
            'vision_analysis': {'errors': [], 'source': 'reviewed_material_index'}}
