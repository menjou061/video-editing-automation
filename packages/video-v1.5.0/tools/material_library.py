"""Ingest existing visual analysis once; synchronize the human-review ledger.

No provider is invoked here. Obtain a fresh shot_candidates.json through the
existing analyze-shots command only for new/invalidated materials. Publishing
requires explicit --apply; pull-review is a read-only remote operation.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

if __package__ in {None, ''}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import material_index as mi
from tools import production_eval as pe


def cli(command: str, base: str, table: str, *args: str) -> dict:
    result = subprocess.run([os.environ.get('LARK_CLI_PATH', 'lark-cli'), 'base', command,
                             '--base-token', base, '--table-id', table, '--as', 'user', *args],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('FEISHU_RESPONSE_NOT_JSON:' + command) from exc
    if result.returncode or payload.get('ok') is not True:
        raise RuntimeError('FEISHU_OPERATION_FAILED:' + command)
    return payload['data']


def remote_row(base: str, table: str, material_id: str) -> tuple[str | None, dict]:
    result = cli('+record-list', base, table, '--filter-json',
                 json.dumps({'logic': 'and', 'conditions': [['素材ID', '==', material_id]]}),
                 '--limit', '2', '--format', 'json')
    if result.get('has_more') or len(result.get('record_id_list', [])) > 1:
        raise ValueError('DUPLICATE_REMOTE_MATERIAL_ID')
    ids = result.get('record_id_list', [])
    if not ids:
        return None, {}
    return ids[0], dict(zip(result['fields'], result['data'][0]))


def ingest(analysis: dict, product_map: dict, *, policy: Path, tagger_version: str) -> dict:
    rows = []
    for shot in analysis.get('shots', []):
        source = Path(shot['video'])
        product = product_map.get(str(source), {})
        proof_paths = [Path(p) for p in shot.get('frame_paths', [])]
        facts = {'tags': shot.get('visual_tags', []), 'shot': shot,
                 'supported_claims': shot.get('supported_claims', []),
                 'evidence_grade': shot.get('evidence_grade', '无法证明'),
                 'product_identity_confirmed': product.get('human_confirmed_sku_binding') is True,
                 'doubt': shot.get('doubt', '中'), 'doubt_reason': shot.get('doubt_reason', '')}
        rows.append(mi.make_segment(source, float(shot['source_start']), float(shot['source_end']),
            category=product.get('category', ''), product=product.get('product', ''),
            sku=product.get('sku', ''), facts=facts, policy_hash=pe.file_hash(policy),
            tagger_version=tagger_version, evidence_paths=proof_paths))
    if not rows:
        raise ValueError('NO_VISUAL_SEGMENTS')
    batch_id = pe.digest({'analysis': analysis, 'policy': pe.file_hash(policy), 'tagger': tagger_version})
    for row in rows:
        row['batch_id'] = batch_id
    ids = [row['material_id'] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('DUPLICATE_VISUAL_SEGMENT')
    return {'schema_version': 1, 'index_version': mi.INDEX_VERSION,
            'tagger_version': tagger_version, 'materials': rows,
            'required_sample_ids': mi.sample_ids(rows)}


def publish(index: dict, base: str, table: str) -> list[dict]:
    fields = cli('+field-list', base, table).get('fields', [])
    names = {row['name'] for row in fields}
    receipts = []
    for row in index['materials']:
        cells = mi.feishu_cells(row)
        if not set(cells) <= names:
            raise ValueError('MATERIAL_LEDGER_SCHEMA_DRIFT')
        record_id, previous = remote_row(base, table, row['material_id'])
        immutable_fields = set(cells) - set(mi.MANUAL_FIELDS) - {'素材归档路径'}
        same = bool(record_id) and all(previous.get(field) == cells[field]
                                       for field in immutable_fields)
        if record_id and same:
            for field in mi.MANUAL_FIELDS:
                cells.pop(field)
        args = ['--json', json.dumps(cells, ensure_ascii=False)]
        if record_id:
            args += ['--record-id', record_id]
        # A timeout/unknown delivery is never blindly retried. Query the
        # business key on a later explicit invocation to discover its outcome.
        result = cli('+record-upsert', base, table, *args)
        actual_id, actual = remote_row(base, table, row['material_id'])
        if not actual_id or any(actual.get(k) != v for k, v in cells.items()):
            raise ValueError('MATERIAL_WRITE_READBACK_MISMATCH')
        receipts.append({'material_id': row['material_id'], 'record_id': actual_id,
                         'fields_sha256': pe.digest(cells), 'readback_verified': True})
    return receipts


def pull(index: dict, base: str, table: str, reviewer: str) -> dict:
    updated = dict(index)
    updated['materials'] = []
    for row in index['materials']:
        record_id, cells = remote_row(base, table, row['material_id'])
        expected = mi.feishu_cells(row)
        immutable_fields = set(expected) - set(mi.MANUAL_FIELDS) - {'素材归档路径'}
        if (record_id and all(cells.get(field) == expected[field]
                              for field in immutable_fields)):
            row = mi.apply_review(row, cells, reviewer)
        else:
            row = dict(row, doubt=row.get('suggested_doubt', '中'),
                       doubt_reason=row.get('suggested_doubt_reason', ''),
                       review_status=('需复核' if row.get('suggested_doubt', '中') in {'中', '高'}
                                      else '未抽检'),
                       use_advice='暂缓使用')
            row.pop('reviewed_by', None)
            row.pop('reviewed_facts_sha256', None)
        updated['materials'].append(row)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['ingest', 'publish', 'pull-review', 'archive'])
    parser.add_argument('--index', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--analysis', type=Path)
    parser.add_argument('--product-map', type=Path)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--tagger-version')
    parser.add_argument('--base-token', default=os.environ.get('LARK_MATERIAL_BASE_TOKEN'))
    parser.add_argument('--table-id', default=os.environ.get('LARK_MATERIAL_TABLE_ID'))
    parser.add_argument('--reviewer')
    parser.add_argument('--archive-root', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.command != 'ingest' and not args.index:
        parser.error('this command requires --index')
    if args.command == 'ingest' and not args.output:
        parser.error('ingest requires a new --output path')
    if (args.command in {'pull-review', 'archive'} or args.command == 'publish' and args.apply) and not args.output:
        parser.error('a new --output path is required; existing index snapshots are immutable')
    if args.command == 'ingest':
        if not all((args.analysis, args.product_map, args.policy, args.tagger_version)):
            parser.error('ingest requires analysis, product-map, policy and tagger-version')
        result = ingest(pe.read(args.analysis), pe.read(args.product_map), policy=args.policy,
                        tagger_version=args.tagger_version)
    else:
        index = pe.read(args.index)
        if args.command == 'archive':
            if not args.apply or not args.archive_root:
                parser.error('archive requires --apply and --archive-root')
            result = dict(index, materials=[mi.archive(row, args.archive_root) for row in index['materials']])
        else:
            if not args.base_token or not args.table_id:
                parser.error('the exact material Base and table are required')
            if args.command == 'publish':
                if not args.apply:
                    print(json.dumps({'dry_run': True, 'fields': [mi.feishu_cells(r) for r in index['materials']]}, ensure_ascii=False))
                    return 0
                result = {'receipts': publish(index, args.base_token, args.table_id)}
            else:
                if not args.reviewer:
                    parser.error('pull-review requires an accountable --reviewer')
                result = pull(index, args.base_token, args.table_id, args.reviewer)
    output = args.output or args.index
    pe.write_once(output, result)
    print(json.dumps({'output': str(output), 'materials': len(result.get('materials', []))}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
