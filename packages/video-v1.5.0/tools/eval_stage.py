"""Prepare, collect, and finalize hash-bound human review without rebuilding drafts."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import production_eval as pe


def _context(task_dir: Path, draft_root: Path) -> tuple[dict, dict, dict, list[str], list[Path], dict, list[str]]:
    contract = pe.read(task_dir / 'eval' / 'active_contract.json')
    pe.verify_contract(contract)
    task = pe.read(task_dir / 'task.json')
    done = pe.read(task_dir / 'done.json')
    names = done.get('local_only_drafts') or done.get('drafts') or []
    if not names or any(not isinstance(name, str) or Path(name).name != name for name in names):
        raise ValueError('TASK_DRAFTS_MISSING_OR_INVALID')
    outputs = [draft_root / name for name in names]
    identity = pe.output_identity(outputs)
    if task.get('task_id') != contract.get('task_id'):
        raise ValueError('TASK_CONTRACT_IDENTITY_MISMATCH')
    ids = []
    for output in outputs:
        info = pe.read(output / 'draft_info.json')
        draft_id = str(info.get('id') or '').strip()
        if not draft_id:
            raise ValueError('DRAFT_ID_MISSING')
        ids.append(draft_id)
    return contract, task, done, names, outputs, identity, ids


def _binding(contract: dict, output_sha256: str) -> dict:
    return {'task_id': contract['task_id'], 'run_id': contract['run_id'],
            'contract_hash': contract['contract_hash'], 'output_sha256': output_sha256}


def _input_once(path: Path, value: dict) -> None:
    if path.exists():
        existing = pe.read(path)
        for key in ('task_id', 'run_id', 'contract_hash', 'output_sha256'):
            if existing.get(key) != value.get(key):
                raise ValueError('REVIEW_INPUT_CONTEXT_DRIFT:' + key)
        return
    pe.write_once(path, value)


def _write_pointer(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepare(task_dir: Path, draft_root: Path) -> dict:
    contract, task, done, names, outputs, identity, draft_ids = _context(task_dir, draft_root)
    binding = _binding(contract, identity['sha256'])
    eval_dir = task_dir / 'eval' / contract['run_id']
    eval_dir.mkdir(parents=True, exist_ok=True)
    gold_path = eval_dir / ('gold-quality-review-' + identity['sha256'] + '.input.json')
    mac_path = eval_dir / ('mac-review-' + identity['sha256'] + '.input.json')
    package_path = eval_dir / ('review-package-' + identity['sha256'] + '.json')
    gold = dict(binding, status='UNCERTIFIED', reviewed_by='', approved=False,
                gold_sha256=contract['gold_reference']['sha256'],
                sample_slots=contract['gold_reference']['sample_slots'],
                reference_sources=[dict(source, reviewed=False, evidence=[])
                                   for source in contract['gold_reference']['sources']],
                dimensions={name: {'status': 'UNCERTIFIED', 'notes': '', 'evidence': []}
                            for name in pe.DIMENSIONS})
    mac = dict(binding, status='UNCERTIFIED', reviewed_by='', approved=False,
               review_surface='mac_jianying', editable_project_opened=False,
               draft_ids=draft_ids, reports=[])
    package = dict(binding, draft_names=names, draft_ids=draft_ids,
                   output_files=identity['files'],
                   gold_sources=contract['gold_reference']['sources'],
                   gold_review_input=str(gold_path), mac_review_input=str(mac_path),
                   required_gold_dimensions=list(pe.DIMENSIONS),
                   instructions={
                       'gold': 'Compare the complete finished video with all 11 Gold references. Record PASS/FAIL/UNCERTIFIED for each dimension, concise notes, and hash-bound evidence files.',
                       'mac': 'Open every listed editable project in Jianying on Mac. Record reviewer, approval, and hash-bound screenshots or review evidence. Do not approve by filename alone.'})
    _input_once(gold_path, gold)
    _input_once(mac_path, mac)
    pe.write_once(package_path, package)
    return {'status': 'REVIEW_INPUTS_READY', 'package': str(package_path),
            'gold_review_input': str(gold_path), 'mac_review_input': str(mac_path),
            'output_sha256': identity['sha256'], 'draft_ids': draft_ids}


def _validate_refs(refs: object, *, required: bool, resolve_path=None) -> list[dict]:
    if not isinstance(refs, list) or (required and not refs):
        raise ValueError('REVIEW_EVIDENCE_REQUIRED')
    values = []
    for ref in refs:
        if (not isinstance(ref, dict) or not isinstance(ref.get('path'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha256', '')))):
            raise ValueError('REVIEW_EVIDENCE_HASH_INVALID')
        path = resolve_path(ref['path']) if resolve_path else Path(ref['path'])
        if pe.file_hash(path) != ref['sha256']:
            raise ValueError('REVIEW_EVIDENCE_HASH_INVALID')
        values.append({'path': str(path.absolute()), 'sha256': ref['sha256']})
    return values


def _review_stage_resolver(task_dir: Path, stage_root: Path, mac_root: str,
                           contract: dict, names: list[str], source_sha256: str):
    run_id = contract['run_id']
    handoff = pe.read(task_dir / 'eval' / run_id / 'mac-review-handoff.json')
    body = dict(handoff)
    supplied = body.pop('receipt_sha256', None)
    staged = [stage_root / run_id / name for name in names]
    if (supplied != pe.digest(body) or handoff.get('contract_hash') != contract['contract_hash']
            or handoff.get('source_output_sha256') != source_sha256
            or handoff.get('mac_drafts') != [mac_root.rstrip('/') + '/' + run_id + '/' + name for name in names]
            or handoff.get('staged_output_sha256') != pe.output_identity(staged)['sha256']):
        raise ValueError('REVIEW_STAGE_HANDOFF_DRIFT')
    prefix = mac_root.rstrip('/') + '/' + run_id + '/evidence/'
    evidence_root = (stage_root / run_id / 'evidence').resolve()

    def resolve(raw: str) -> Path:
        if not raw.startswith(prefix):
            raise ValueError('REVIEW_EVIDENCE_NOT_IN_STAGE')
        relative = raw[len(prefix):]
        if not relative or '\\' in relative or any(part in {'', '.', '..'} for part in relative.split('/')):
            raise ValueError('REVIEW_EVIDENCE_PATH_UNSAFE')
        path = (evidence_root / relative).resolve()
        if not path.is_relative_to(evidence_root):
            raise ValueError('REVIEW_EVIDENCE_PATH_UNSAFE')
        return path

    return resolve


def collect(task_dir: Path, draft_root: Path, gold_input: Path | None = None,
            mac_input: Path | None = None, *, review_stage_root: Path | None = None,
            mac_root: str | None = None) -> dict:
    contract, task, done, names, outputs, identity, draft_ids = _context(task_dir, draft_root)
    binding = _binding(contract, identity['sha256'])
    eval_dir = task_dir / 'eval' / contract['run_id']
    if bool(review_stage_root) != bool(mac_root):
        raise ValueError('REVIEW_STAGE_MAPPING_INCOMPLETE')
    resolve_path = (_review_stage_resolver(task_dir, review_stage_root, mac_root,
                                           contract, names, identity['sha256'])
                    if review_stage_root is not None and mac_root is not None else None)
    input_dir = review_stage_root / contract['run_id'] if review_stage_root is not None else eval_dir
    gold_input = gold_input or input_dir / ('gold-quality-review-' + identity['sha256'] + '.input.json')
    mac_input = mac_input or input_dir / ('mac-review-' + identity['sha256'] + '.input.json')
    if not gold_input.is_file() and list(eval_dir.glob('gold-quality-review-*.input.json')):
        raise ValueError('REVIEW_CONTEXT_OUTPUT_DRIFT')
    if not mac_input.is_file() and list(eval_dir.glob('mac-review-*.input.json')):
        raise ValueError('REVIEW_CONTEXT_OUTPUT_DRIFT')
    gold_raw, mac_raw = pe.read(gold_input), pe.read(mac_input)
    for row in (gold_raw, mac_raw):
        for key, value in binding.items():
            if row.get(key) != value:
                raise ValueError('REVIEW_CONTEXT_BINDING_MISMATCH:' + key)

    dimensions = gold_raw.get('dimensions')
    if not isinstance(dimensions, dict) or set(dimensions) != set(pe.DIMENSIONS):
        raise ValueError('GOLD_DIMENSION_SET_MISMATCH')
    normalized_dimensions = {}
    for name in pe.DIMENSIONS:
        row = dimensions[name]
        if not isinstance(row, dict) or row.get('status') not in pe.STATUSES:
            raise ValueError('GOLD_DIMENSION_STATUS_INVALID:' + name)
        notes = str(row.get('notes') or '').strip()
        refs = _validate_refs(row.get('evidence'), required=row.get('status') == 'PASS',
                              resolve_path=resolve_path)
        if row.get('status') == 'PASS' and not notes:
            raise ValueError('GOLD_DIMENSION_NOTES_REQUIRED:' + name)
        normalized_dimensions[name] = {'status': row['status'], 'notes': notes, 'evidence': refs}
    if gold_raw.get('gold_sha256') != contract['gold_reference']['sha256']:
        raise ValueError('GOLD_REVIEW_REFERENCE_MISMATCH')
    if gold_raw.get('sample_slots') != contract['gold_reference']['sample_slots']:
        raise ValueError('GOLD_REVIEW_SAMPLE_SLOTS_MISMATCH')
    expected_sources = contract['gold_reference']['sources']
    supplied_sources = gold_raw.get('reference_sources')
    if not isinstance(supplied_sources, list) or len(supplied_sources) != 11:
        raise ValueError('GOLD_REFERENCE_REVIEW_SET_MISMATCH')
    supplied_by_slot = {str(row.get('video_slot') or ''): row for row in supplied_sources
                        if isinstance(row, dict)}
    if set(supplied_by_slot) != set(contract['gold_reference']['sample_slots']):
        raise ValueError('GOLD_REFERENCE_REVIEW_SLOTS_MISMATCH')
    normalized_sources = []
    sources_complete = True
    for expected in expected_sources:
        row = supplied_by_slot[expected['video_slot']]
        if any(row.get(key) != expected.get(key)
               for key in ('source_ref', 'source_filename', 'source_sha256')):
            raise ValueError('GOLD_REFERENCE_REVIEW_IDENTITY_MISMATCH:' + expected['video_slot'])
        reviewed = row.get('reviewed') is True
        refs = _validate_refs(row.get('evidence'), required=reviewed,
                              resolve_path=resolve_path)
        sources_complete = sources_complete and reviewed and bool(refs)
        normalized_sources.append(dict(expected, reviewed=reviewed, evidence=refs))
    if not str(gold_raw.get('reviewed_by') or '').strip():
        raise ValueError('GOLD_REVIEWER_REQUIRED')
    dim_statuses = [row['status'] for row in normalized_dimensions.values()]
    gold_status = ('FAIL' if 'FAIL' in dim_statuses else
                   'PASS' if all(status == 'PASS' for status in dim_statuses)
                   and gold_raw.get('approved') is True and sources_complete else 'UNCERTIFIED')
    gold = dict(binding, status=gold_status,
                reviewed_by=str(gold_raw['reviewed_by']).strip(),
                approved=(gold_status == 'PASS'),
                gold_sha256=contract['gold_reference']['sha256'],
                sample_slots=contract['gold_reference']['sample_slots'],
                reference_sources=normalized_sources,
                dimensions=normalized_dimensions)

    if mac_raw.get('review_surface') != 'mac_jianying':
        raise ValueError('MAC_REVIEW_SURFACE_INVALID')
    if mac_raw.get('draft_ids') != draft_ids:
        raise ValueError('MAC_REVIEW_DRAFT_ID_MISMATCH')
    reviewer = str(mac_raw.get('reviewed_by') or '').strip()
    if not reviewer:
        raise ValueError('MAC_REVIEWER_REQUIRED')
    reports = _validate_refs(mac_raw.get('reports'), required=mac_raw.get('approved') is True,
                             resolve_path=resolve_path)
    mac_approved = mac_raw.get('approved') is True and mac_raw.get('editable_project_opened') is True and bool(reports)
    mac_status = ('PASS' if mac_approved else
                  'FAIL' if mac_raw.get('status') == 'FAIL' else 'UNCERTIFIED')
    mac = dict(binding, status=mac_status, reviewed_by=reviewer,
               approved=mac_approved, review_surface='mac_jianying',
               editable_project_opened=mac_raw.get('editable_project_opened') is True,
               draft_ids=draft_ids, reports=reports)
    gold_receipt = eval_dir / ('gold-quality-review-' + identity['sha256'] + '-' + pe.digest(gold)[:16] + '.json')
    mac_receipt = eval_dir / ('mac-review-' + identity['sha256'] + '-' + pe.digest(mac)[:16] + '.json')
    pe.write_once(gold_receipt, gold)
    pe.write_once(mac_receipt, mac)
    selection = dict(binding,
        gold_quality_review={'path': str(gold_receipt), 'sha256': pe.file_hash(gold_receipt)},
        mac_review={'path': str(mac_receipt), 'sha256': pe.file_hash(mac_receipt)})
    selection['selection_sha256'] = pe.digest(selection)
    _write_pointer(eval_dir / ('review-selection-' + identity['sha256'] + '.json'), selection)

    approval_path = task_dir / ('mac_review_approved-' + identity['sha256'] + '.json')
    if mac_approved:
        approval = {'schema_version': 1, 'record_id': task.get('record_id'),
                    'task_id': contract['task_id'], 'run_id': contract['run_id'],
                    'contract_hash': contract['contract_hash'],
                    'output_sha256': identity['sha256'], 'approved_drafts': names,
                    'draft_ids': draft_ids, 'approved': True,
                    'approval_source': 'hash_bound_eval_stage_mac_review'}
        pe.write_once(approval_path, approval)
    return {'status': 'REVIEWS_COLLECTED', 'gold_status': gold_status,
            'mac_status': mac_status, 'gold_receipt': str(gold_receipt),
            'mac_receipt': str(mac_receipt),
            'approval_file': str(approval_path) if mac_approved else None}


def finalize(task_dir: Path, draft_root: Path, *, deliver: bool) -> dict:
    contract, task, done, names, outputs, identity, draft_ids = _context(task_dir, draft_root)
    runtime_root = Path(os.environ.get('JY_EVAL_RUNTIME_ROOT', str(ROOT)))
    runtime = pe.runtime_identity(runtime_root)
    if contract['mode'] == 'enforce':
        observations = os.environ.get('JY_EVAL_OBSERVATIONS', '').strip()
        if not observations or not pe.verify_observations(Path(observations), runtime)['ready']:
            raise ValueError('EVAL_ROLLOUT_REQUIRES_TWO_VERIFIED_REAL_TASKS')
    sys.path.insert(0, str(ROOT))
    import batch_worker as worker
    worker.PIPE = task_dir.parent.parent
    worker.WORK = task_dir.parent
    worker.SKILL = worker.PIPE / 'doubao-jianying-orchestrator'
    worker.DRAFTS = draft_root
    worker.PIPE_ROOT = runtime_root
    worker.CLOSED_LOOP_TOOL = worker.PIPE / 'tools' / 'closed_loop.py'
    worker.CLOSED_LOOP_POLICY = worker.PIPE / 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json'
    os.environ['JY_EVAL_MODE'] = contract['mode']
    qc_reports = done.get('qc_reports') or []
    receipt = worker.evaluate_task(task_dir, task, names, qc_reports, contract)
    if not deliver:
        return {'status': 'EVAL_PREVIEW', 'eval': receipt,
                'delivery_started': False}
    result = worker.finalize_closed_loop(task_dir, task, names, qc_reports,
                                          done.get('distribution_receipts') or [])
    return {'status': result.get('status'), 'delivery_started': True, 'result': result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'collect', 'finalize'))
    parser.add_argument('--task-dir', required=True, type=Path)
    parser.add_argument('--draft-root', type=Path,
                        default=Path(os.environ.get('JY_DRAFT_ROOT', str(ROOT / 'drafts'))))
    parser.add_argument('--gold-input', type=Path)
    parser.add_argument('--mac-input', type=Path)
    parser.add_argument('--review-stage-root', type=Path)
    parser.add_argument('--mac-root')
    parser.add_argument('--deliver', action='store_true',
                        help='allow final NAS distribution and Feishu writeback after verified gates')
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare(args.task_dir, args.draft_root)
    elif args.command == 'collect':
        result = collect(args.task_dir, args.draft_root, args.gold_input, args.mac_input,
                         review_stage_root=args.review_stage_root, mac_root=args.mac_root)
    else:
        result = finalize(args.task_dir, args.draft_root, deliver=args.deliver)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('status') in {'REVIEW_INPUTS_READY', 'REVIEWS_COLLECTED', 'EVAL_PREVIEW', 'CLOSED_LOOP_COMPLETE'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
