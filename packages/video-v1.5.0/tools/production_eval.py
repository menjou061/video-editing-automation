"""Task-level video Eval adapter. No Gold slots are modified by production runs.

Local paths are private execution data, not source-registry entries. Receipt
verification always reads the evidence; URI-looking strings are not evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

DIMENSIONS = (
    'visual_match_and_product_identity', 'claim_evidence_coverage',
    'shot_structure_and_rhythm', 'subtitle_layout_and_readability',
    'audio_timing_and_loudness', 'draft_editability_and_delivery',
)
CRITERIA = ('INPUT-IDENTITY', 'RUNTIME-IDENTITY', 'DRAFT-PACKAGE',
            'VISUAL-QC', 'GOLD-QUALITY', 'MAC-REVIEW')
STATUSES = {'PASS', 'FAIL', 'UNCERTIFIED'}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError('NOT_A_REGULAR_EVIDENCE_FILE')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('JSON_OBJECT_REQUIRED')
    return value


def write_once(path: Path, value: dict) -> None:
    """Immutable receipts: an identical retry is a no-op, drift is rejected."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    except FileExistsError:
        if read(path) != value:
            raise ValueError('IMMUTABLE_RECEIPT_CHANGED')


def output_identity(paths: list[Path]) -> dict:
    rows = []
    for index, path in enumerate(paths):
        if path.is_symlink():
            raise ValueError('SYMLINK_OUTPUT_REJECTED')
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        for child in files:
            if child.is_symlink():
                raise ValueError('SYMLINK_OUTPUT_REJECTED')
            if child.is_file():
                relative = child.relative_to(path).as_posix() if path.is_dir() else path.name
                rows.append({'output_index': index, 'path': relative, 'sha256': file_hash(child)})
    if not rows:
        raise ValueError('OUTPUT_MISSING')
    return {'files': rows, 'sha256': digest(rows)}


def runtime_identity(root: Path) -> dict:
    manifest = read(root / 'release-manifest.json')
    rows = manifest.get('files', [])
    if not rows or len(rows) != manifest.get('file_count'):
        raise ValueError('RUNTIME_MANIFEST_INCOMPLETE')
    if not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('source_sha256', ''))):
        raise ValueError('RUNTIME_SOURCE_HASH_MISSING')
    version_path = root / 'VERSION'
    if not version_path.is_file():
        raise ValueError('RUNTIME_VERSION_FILE_MISSING')
    if version_path.read_text(encoding='utf-8-sig').strip() != manifest.get('version'):
        raise ValueError('RUNTIME_VERSION_METADATA_DRIFT')
    names = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            raise ValueError('RUNTIME_MANIFEST_ROW_INVALID')
        rel = Path(row['path'])
        if (rel.is_absolute() or '..' in rel.parts or '\\' in row['path']
                or row['path'] in names
                or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', '')))):
            raise ValueError('RUNTIME_MANIFEST_PATH_INVALID')
        names.add(row['path'])
        if file_hash(root / rel) != row['sha256']:
            raise ValueError('RUNTIME_HASH_DRIFT:' + row['path'])
    if ('VERSION' not in names
            or 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json' not in names):
        raise ValueError('RUNTIME_MANIFEST_REQUIRED_FILE_MISSING')
    policy = root / 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json'
    policy_payload = read(policy)
    if policy_payload.get('video_tool_version') != manifest.get('version'):
        raise ValueError('RUNTIME_POLICY_TOOL_VERSION_DRIFT')
    if policy_payload.get('visual_policy_version') != manifest.get('visual_policy_version'):
        raise ValueError('RUNTIME_POLICY_VERSION_METADATA_DRIFT')
    return {'tool_version': manifest['version'], 'package_sha256': digest(rows),
            'release_source_sha256': manifest.get('source_sha256'),
            'visual_policy_version': policy_payload['visual_policy_version'],
            'visual_policy_sha256': file_hash(policy), 'eval_pack_version': '1.3',
            'eval_schema_version': 1}


def validate_gold_reference(gold: dict) -> None:
    """Validate the immutable 11-sample identity before freezing a task."""
    slots = gold.get('sample_slots')
    sources = gold.get('sources')
    dimensions = gold.get('comparison_dimensions')
    if (not isinstance(slots, list) or len(slots) != 11
            or len(set(slots)) != 11
            or not isinstance(sources, list) or len(sources) != 11
            or not isinstance(dimensions, list) or tuple(dimensions) != DIMENSIONS):
        raise ValueError('GOLD_REFERENCE_IDENTITY_MISSING')
    if {row.get('video_slot') for row in sources if isinstance(row, dict)} != set(slots):
        raise ValueError('GOLD_REFERENCE_SOURCE_SLOTS_INVALID')
    for row in sources:
        if not isinstance(row, dict):
            raise ValueError('GOLD_REFERENCE_SOURCE_INVALID')
        source_ref = str(row.get('source_ref') or '')
        filename = str(row.get('source_filename') or '')
        if (not source_ref.startswith('external://video-montage/')
                or not filename or source_ref.rsplit('/', 1)[-1] != filename):
            raise ValueError('GOLD_REFERENCE_SOURCE_REF_INVALID')
        if not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_sha256', ''))):
            raise ValueError('GOLD_REFERENCE_SOURCE_HASH_INVALID')
    identity = {key: gold[key] for key in ('sample_slots', 'sources', 'comparison_dimensions')}
    if not re.fullmatch(r'[0-9a-f]{64}', str(gold.get('sha256', ''))):
        raise ValueError('GOLD_REFERENCE_HASH_MISSING')
    if digest(identity) != gold['sha256']:
        raise ValueError('GOLD_REFERENCE_HASH_MISMATCH')


def output_draft_ids(outputs: list[Path]) -> list[str]:
    values = []
    for output in outputs:
        info = read(output / 'draft_info.json')
        draft_id = str(info.get('id') or '').strip()
        if not draft_id:
            raise ValueError('DRAFT_ID_MISSING')
        values.append(draft_id)
    return values


def freeze(task: dict, run_id: str, inputs: list[Path], runtime: dict,
           gold: dict, mode: str = 'observe') -> dict:
    if mode not in {'observe', 'enforce'}:
        raise ValueError('EVAL_MODE_INVALID')
    task_id = str(task.get('task_id') or task.get('record_id') or '').strip()
    if not task_id or not run_id or not task.get('script') or not inputs:
        raise ValueError('TASK_INPUT_INCOMPLETE')
    for field in ('package_sha256', 'visual_policy_sha256'):
        if not re.fullmatch(r'[0-9a-f]{64}', str(runtime.get(field, ''))):
            raise ValueError('RUNTIME_IDENTITY_INCOMPLETE')
    if not runtime.get('tool_version') or not runtime.get('visual_policy_version'):
        raise ValueError('RUNTIME_VERSION_MISSING')
    validate_gold_reference(gold)
    identity = [{'path': str(p.absolute()), 'sha256': file_hash(p)} for p in inputs]
    contract = {'schema_version': 1, 'task_id': task_id, 'run_id': run_id,
                'mode': mode, 'task_sha256': digest(task), 'inputs': identity,
                'runtime': runtime, 'gold_reference': gold,
                'category': task.get('category', ''), 'sku': task.get('sku', ''),
                'eval_contract': {
                    'schema_version': 1, 'case_id': 'production:' + task_id,
                    'objective': 'Produce editable video meeting the frozen task and Gold quality rubric.',
                    'input_contract': ['task identity', 'media SHA-256', 'runtime and policy identity'],
                    'expected_route': 'diaodu -> video-tool -> video Eval -> rdm',
                    'expected_artifacts': ['draft', 'QC', 'quality review', 'Mac review', 'Eval receipt'],
                    'acceptance_criteria': [{'id': name, 'description': name,
                        'observable_evidence': 'Hash-bound current-run evidence for ' + name,
                        'unacceptable_substitutes': ['filename match', 'status-only approval']}
                        for name in CRITERIA],
                    'evidence_requirements': ['run_id', 'contract_hash', 'file SHA-256', 'output SHA-256'],
                    'human_gates': ['Gold quality review', 'Mac editable draft acceptance'],
                    'failure_taxonomy': ['wrong_sku', 'evidence_missing', 'version_drift', 'timeout'],
                    'replay_method': 'Rehash frozen inputs, output and evidence; evaluate the same criteria.'}}
    contract = json.loads(json.dumps(contract, ensure_ascii=False, allow_nan=False))
    contract['contract_hash'] = 'sha256:' + digest(contract)
    return contract


def verify_contract(contract: dict) -> None:
    value = dict(contract)
    supplied = value.pop('contract_hash', None)
    if supplied != 'sha256:' + digest(value):
        raise ValueError('CONTRACT_HASH_MISMATCH')
    ids = [row['id'] for row in contract['eval_contract']['acceptance_criteria']]
    if ids != list(CRITERIA):
        raise ValueError('ACCEPTANCE_ID_DRIFT')


def evidence(path: Path, contract: dict, output_sha256: str) -> dict:
    row = read(path)
    if not re.fullmatch(r'[0-9a-f]{64}', output_sha256):
        raise ValueError('OUTPUT_IDENTITY_MISSING')
    for key, value in {'run_id': contract['run_id'], 'task_id': contract['task_id'],
                       'contract_hash': contract['contract_hash'],
                       'output_sha256': output_sha256}.items():
        if row.get(key) != value:
            raise ValueError('EVIDENCE_BINDING_MISMATCH:' + key)
    reports = row.get('reports', [])
    if not isinstance(reports, list):
        raise ValueError('EVIDENCE_REPORTS_INVALID')
    for proof in reports:
        if (not isinstance(proof, dict) or not isinstance(proof.get('path'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', str(proof.get('sha256', '')))):
            raise ValueError('EVIDENCE_REPORT_INVALID')
        if file_hash(Path(proof['path'])) != proof['sha256']:
            raise ValueError('TRANSITIVE_EVIDENCE_HASH_MISMATCH')
    return row


def evaluate(contract: dict, task: dict, runtime: dict, outputs: list[Path],
             evidence_paths: dict[str, Path]) -> dict:
    verify_contract(contract)
    reasons = []
    try:
        output = output_identity(outputs)
    except (OSError, ValueError):
        output = {'files': [], 'sha256': ''}
    input_ok = bool(contract['inputs']) and digest(task) == contract['task_sha256']
    try:
        input_ok = input_ok and all(file_hash(Path(row['path'])) == row['sha256']
                                   for row in contract['inputs'])
    except (OSError, ValueError):
        input_ok = False
    statuses = {'INPUT-IDENTITY': 'PASS' if input_ok else 'UNCERTIFIED',
                'RUNTIME-IDENTITY': 'PASS' if runtime == contract['runtime'] else 'UNCERTIFIED'}
    proofs = {name: [] for name in CRITERIA}
    if input_ok:
        proofs['INPUT-IDENTITY'] = list(contract['inputs'])
    if statuses['RUNTIME-IDENTITY'] == 'PASS':
        proofs['RUNTIME-IDENTITY'] = [runtime]
    verified = {}
    for name in CRITERIA[2:]:
        path = evidence_paths.get(name)
        try:
            if path is None or not output['sha256']:
                raise ValueError('EVIDENCE_MISSING')
            row = evidence(path, contract, output['sha256'])
            verdict = row.get('status', 'UNCERTIFIED')
            if verdict not in STATUSES:
                verdict = 'UNCERTIFIED'
            if name == 'VISUAL-QC' and row.get('qc_status') not in {'PASS', 'PASS_WITH_DEGRADED'}:
                verdict = 'FAIL' if row.get('qc_status') == 'FAIL' else 'UNCERTIFIED'
            if verdict != 'FAIL' and name == 'DRAFT-PACKAGE' and not row.get('files_verified'):
                verdict = 'UNCERTIFIED'
            if verdict != 'FAIL' and name == 'MAC-REVIEW' and not (
                    row.get('reviewed_by') and row.get('approved') is True
                    and row.get('review_surface') == 'mac_jianying'
                    and row.get('editable_project_opened') is True
                    and row.get('draft_ids') == output_draft_ids(outputs)
                    and row.get('reports')):
                verdict = 'UNCERTIFIED'
            if verdict != 'FAIL' and name == 'GOLD-QUALITY' and not (
                    row.get('reviewed_by') and row.get('approved') is True):
                verdict = 'UNCERTIFIED'
            if name == 'GOLD-QUALITY':
                dims = row.get('dimensions', {})
                verdicts = [v.get('status') if isinstance(v, dict) else None for v in dims.values()]
                reference_sources = row.get('reference_sources', [])
                expected_sources = contract['gold_reference'].get('sources', [])
                source_map = {str(source.get('video_slot') or ''): source
                              for source in reference_sources if isinstance(source, dict)}
                if (row.get('gold_sha256') != contract['gold_reference']['sha256']
                        or row.get('sample_slots') != contract['gold_reference']['sample_slots']
                        or set(dims) != set(DIMENSIONS)
                        or set(source_map) != set(contract['gold_reference']['sample_slots'])):
                    verdict = 'UNCERTIFIED'
                elif 'FAIL' in verdicts:
                    verdict = 'FAIL'
                elif any(value != 'PASS' for value in verdicts):
                    verdict = 'UNCERTIFIED'
                else:
                    expected_by_slot = {source['video_slot']: source for source in expected_sources}
                    for slot, source in source_map.items():
                        expected = expected_by_slot[slot]
                        if (source.get('source_ref') != expected.get('source_ref')
                                or source.get('source_filename') != expected.get('source_filename')
                                or source.get('source_sha256') != expected.get('source_sha256')
                                or source.get('reviewed') is not True
                                or not source.get('evidence')):
                            verdict = 'UNCERTIFIED'
                        for ref in source.get('evidence', []):
                            if file_hash(Path(ref['path'])) != ref['sha256']:
                                raise ValueError('GOLD_REFERENCE_EVIDENCE_HASH_MISMATCH')
                    for dimension in dims.values():
                        refs = dimension.get('evidence', [])
                        if not refs or not dimension.get('notes'):
                            verdict = 'UNCERTIFIED'
                        for ref in refs:
                            if file_hash(Path(ref['path'])) != ref['sha256']:
                                raise ValueError('QUALITY_EVIDENCE_HASH_MISMATCH')
            statuses[name] = verdict
            proofs[name] = [{'path': str(path.absolute()), 'sha256': file_hash(path)}]
            verified[name] = row
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            statuses[name] = 'UNCERTIFIED'
            reasons.append(name + ':' + str(exc))
    status = ('FAIL' if 'FAIL' in statuses.values() else
              'PASS' if all(v == 'PASS' for v in statuses.values()) else 'UNCERTIFIED')
    qc = verified.get('VISUAL-QC', {}).get('qc_status')
    accepted = (statuses['DRAFT-PACKAGE'] == statuses['MAC-REVIEW'] == 'PASS'
                and statuses['INPUT-IDENTITY'] == statuses['RUNTIME-IDENTITY'] == 'PASS')
    acceptance = ('REJECTED' if status == 'FAIL' else
                  'ACCEPTED_DEGRADED' if accepted and qc == 'PASS_WITH_DEGRADED' else
                  'ACCEPTED' if status == 'PASS' else 'UNVERIFIED')
    result = {'schema_version': 1, 'task_id': contract['task_id'], 'run_id': contract['run_id'],
              'contract_hash': contract['contract_hash'], 'runtime': runtime,
              'output_identity': output, 'status': status, 'mode': contract['mode'],
              'finished_output_acceptance': acceptance, 'gold_eligibility': False,
              'criteria': [{'id': name, 'status': statuses[name], 'evidence': proofs[name]} for name in CRITERIA],
              'blocking_reasons': reasons}
    result['receipt_sha256'] = digest(result)
    return result


def verify_receipt(receipt: dict, contract: dict, task: dict,
                   runtime: dict, outputs: list[Path]) -> bool:
    value = dict(receipt)
    supplied = value.pop('receipt_sha256', None)
    if supplied != digest(value):
        return False
    paths = {row['id']: Path(row['evidence'][0]['path']) for row in receipt.get('criteria', [])
             if row.get('id') in CRITERIA[2:] and row.get('evidence')}
    try:
        return evaluate(contract, task, runtime, outputs, paths) == receipt
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def rollout_ready(observations: list[dict]) -> bool:
    """Call only with reverified receipts; historical QC is never an observation."""
    qualified = [row for row in observations if row.get('receipt_verified') is True
                 and row.get('real_task') is True and row.get('human_confirmed') is True
                 and row.get('mac_review_verified') is True and row.get('table_readback_verified') is True
                 and row.get('status') == 'PASS' and row.get('sku')
                 and row.get('runtime')]
    return (len({row['task_id'] for row in qualified}) >= 2
            and len({row['sku'] for row in qualified}) >= 2
            and len({digest(row['runtime']) for row in qualified}) == 1
            and any(row.get('material_cache_reused') is True for row in qualified))


def verify_observations(path: Path, runtime: dict) -> dict:
    """Derive eligibility from current files, never from operator summary flags."""
    qualified, blocked = [], []
    for entry in read(path).get('observations', []):
        try:
            contract, task, receipt = (read(Path(entry[key])) for key in ('contract', 'task', 'receipt'))
            outputs = [Path(p) for p in entry['outputs']]
            if (contract['mode'] != 'observe' or receipt['status'] != 'PASS'
                    or not verify_receipt(receipt, contract, task, runtime, outputs)):
                raise ValueError('OBSERVATION_RECEIPT_NOT_VERIFIED')
            output_hash = receipt['output_identity']['sha256']
            confirmation = evidence(Path(entry['human_confirmation']), contract, output_hash)
            table = evidence(Path(entry['table_readback']), contract, output_hash)
            if not (confirmation.get('reviewed_by') and confirmation.get('approved') is True
                    and confirmation.get('real_task') is True and table.get('status') == 'PASS'
                    and table.get('record_id') == task.get('record_id')
                    and table.get('source') == 'lark-cli-readback' and table.get('accepted_fields')):
                raise ValueError('REAL_TASK_CONFIRMATION_OR_TABLE_READBACK_MISSING')
            cache_reused = False
            if entry.get('material_cache_receipt'):
                cache = evidence(Path(entry['material_cache_receipt']), contract, output_hash)
                cache_reused = cache.get('cache_reused') is True and bool(cache.get('material_index_sha256'))
            qualified.append({'task_id': contract['task_id'], 'sku': contract['sku'],
                'runtime': runtime, 'receipt_verified': True, 'real_task': True,
                'human_confirmed': True, 'mac_review_verified': True, 'table_readback_verified': True,
                'status': 'PASS', 'material_cache_reused': cache_reused})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blocked.append(str(exc))
    return {'ready': rollout_ready(qualified), 'qualified': qualified, 'blocking_reasons': blocked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['evaluate', 'verify'])
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, action='append', required=True)
    parser.add_argument('--evidence-map', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    contract, task, runtime = read(args.contract), read(args.task), runtime_identity(args.runtime_root)
    if args.command == 'verify':
        return 0 if verify_receipt(read(args.receipt), contract, task, runtime, args.output) else 2
    paths = {k: Path(v) for k, v in read(args.evidence_map).items()}
    result = evaluate(contract, task, runtime, args.output, paths)
    write_once(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' or contract['mode'] == 'observe' else 2


if __name__ == '__main__':
    raise SystemExit(main())
