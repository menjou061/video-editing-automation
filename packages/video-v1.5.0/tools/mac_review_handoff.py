"""Stage a hash-bound editable draft copy for Mac review, without delivery writeback."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import production_eval as pe


def mark_review_ready(done_path: Path, receipt: dict) -> None:
    done = pe.read(done_path)
    done["distribution_status"] = "AWAITING_MAC_REVIEW"
    done["mac_review_handoff"] = receipt
    fd, temporary = tempfile.mkstemp(prefix=done_path.name + ".", dir=done_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(done, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, done_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_mac_paths(draft: Path, mac_parent: str) -> None:
    prefix = mac_parent.rstrip("/") + "/" + draft.name + "/media/"
    for filename in ("draft_info.json", "draft_content.json"):
        path = draft / filename
        if not path.is_file():
            continue
        payload = pe.read(path)
        for kind in ("videos", "audios", "images", "gifs"):
            for row in payload.get("materials", {}).get(kind, []):
                name = row.get("name") or row.get("material_name")
                if not name:
                    continue
                if (not isinstance(name, str) or name in {".", ".."} or "/" in name or "\\" in name
                        or not (draft / "media" / name).is_file()
                        or row.get("path") != prefix + name):
                    raise ValueError("REVIEW_MEDIA_PATH_NOT_PORTABLE")
    meta = pe.read(draft / "draft_meta_info.json")
    if meta.get("draft_fold_path") != mac_parent.rstrip("/") + "/" + draft.name:
        raise ValueError("REVIEW_DRAFT_META_PATH_DRIFT")


def stage(task_dir: Path, draft_root: Path, stage_root: Path, mac_root: str) -> dict:
    from importlib.util import module_from_spec, spec_from_file_location

    ship_path = Path(__file__).resolve().parents[1] / "doubao-jianying-orchestrator" / "orchestrator" / "ship_distribute.py"
    spec = spec_from_file_location("review_ship_distribute", ship_path)
    assert spec and spec.loader
    ship = module_from_spec(spec)
    spec.loader.exec_module(ship)

    contract = pe.read(task_dir / "eval" / "active_contract.json")
    pe.verify_contract(contract)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", contract["run_id"]):
        raise ValueError("REVIEW_RUN_ID_UNSAFE")
    task = pe.read(task_dir / "task.json")
    if pe.digest(task) != contract["task_sha256"]:
        raise ValueError("REVIEW_TASK_CONTRACT_DRIFT")
    done = pe.read(task_dir / "done.json")
    names = done.get("local_only_drafts") or done.get("drafts") or []
    if not names or any(not isinstance(name, str) or Path(name).name != name for name in names):
        raise ValueError("REVIEW_DRAFT_NAMES_MISSING")
    qc_rows = done.get("qc_reports") or []
    if len(qc_rows) != len(names) or {row.get("draft") for row in qc_rows} != set(names):
        raise ValueError("REVIEW_QC_REPORTS_MISSING")
    qc_evidence = []
    for row in qc_rows:
        path = Path(str(row.get("report") or ""))
        status = row.get("status")
        if status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"} or not path.is_file():
            raise ValueError("REVIEW_QC_NOT_READY")
        if pe.read(path).get("status") != status:
            raise ValueError("REVIEW_QC_REPORT_DRIFT")
        qc_evidence.append({"draft": row["draft"], "status": status,
                            "path": str(path), "sha256": pe.file_hash(path)})
    if not mac_root.startswith("/") or not stage_root.is_dir():
        raise ValueError("MAC_REVIEW_DESTINATION_MISSING")
    sources = [draft_root / name for name in names]
    source_identity = pe.output_identity(sources)
    draft_ids = pe.output_draft_ids(sources)
    run_dir = stage_root / contract["run_id"]
    run_dir.mkdir(exist_ok=True)
    (run_dir / "evidence").mkdir(exist_ok=True)
    receipt_path = task_dir / "eval" / contract["run_id"] / "mac-review-handoff.json"
    if receipt_path.exists():
        previous = pe.read(receipt_path)
        body = dict(previous)
        supplied = body.pop("receipt_sha256", None)
        if (supplied != pe.digest(body)
                or previous.get("contract_hash") != contract["contract_hash"]
                or previous.get("source_output_sha256") != source_identity["sha256"]
                or previous.get("draft_ids") != draft_ids
                or previous.get("qc_evidence") != qc_evidence
                or any(pe.file_hash(run_dir / ("qc-" + str(i + 1) + ".json")) != row["sha256"]
                       for i, row in enumerate(qc_evidence))
                or previous.get("mac_drafts") != [mac_root.rstrip("/") + "/" + contract["run_id"] + "/" + name for name in names]
                or pe.output_identity([run_dir / name for name in names])["sha256"] != previous.get("staged_output_sha256")):
            raise ValueError("REVIEW_HANDOFF_DRIFT")
        pe.write_once(run_dir / "mac-review-handoff.json", previous)
        mark_review_ready(task_dir / "done.json", previous)
        return previous
    staged = []
    for source in sources:
        target = run_dir / source.name
        temporary_parent = run_dir / (".staging-" + uuid.uuid4().hex)
        temporary_parent.mkdir()
        temporary = temporary_parent / source.name
        try:
            shutil.copytree(source, temporary, symlinks=False)
            prior = os.environ.get("JY_MAC_DRAFT_ROOT")
            os.environ["JY_MAC_DRAFT_ROOT"] = mac_root.rstrip("/") + "/" + contract["run_id"]
            try:
                ship.rewrite(str(temporary), "mac")
            finally:
                if prior is None:
                    os.environ.pop("JY_MAC_DRAFT_ROOT", None)
                else:
                    os.environ["JY_MAC_DRAFT_ROOT"] = prior
            verify_mac_paths(temporary, mac_root.rstrip("/") + "/" + contract["run_id"])
            if pe.output_draft_ids([temporary]) != [draft_ids[len(staged)]]:
                raise ValueError("REVIEW_DRAFT_ID_DRIFT")
            if target.exists():
                if pe.output_identity([target])["sha256"] != pe.output_identity([temporary])["sha256"]:
                    raise ValueError("REVIEW_STAGE_TARGET_DRIFT")
            else:
                os.replace(temporary, target)
            staged.append(target)
        finally:
            if temporary_parent.exists():
                shutil.rmtree(temporary_parent)
    staged_identity = pe.output_identity(staged)
    review_dir = task_dir / "eval" / contract["run_id"]
    packages = sorted(review_dir.glob("*.input.json")) + sorted(review_dir.glob("review-package-*.json"))
    if len(packages) < 3:
        raise ValueError("REVIEW_INPUT_PACKAGE_MISSING")
    for package in packages:
        pe.write_once(run_dir / package.name, pe.read(package))
    for i, row in enumerate(qc_evidence):
        target = run_dir / ("qc-" + str(i + 1) + ".json")
        if target.exists():
            if pe.file_hash(target) != row["sha256"]:
                raise ValueError("REVIEW_QC_STAGE_CONFLICT")
        else:
            shutil.copy2(row["path"], target)
        if pe.file_hash(target) != row["sha256"]:
            raise ValueError("REVIEW_QC_STAGE_HASH_MISMATCH")
    receipt = {
        "schema_version": 1, "status": "AWAITING_MAC_REVIEW",
        "task_id": contract["task_id"], "run_id": contract["run_id"],
        "contract_hash": contract["contract_hash"],
        "source_output_sha256": source_identity["sha256"],
        "staged_output_sha256": staged_identity["sha256"],
        "draft_ids": draft_ids,
        "qc_evidence": qc_evidence,
        "mac_qc_paths": [mac_root.rstrip("/") + "/" + contract["run_id"] + "/qc-" + str(i + 1) + ".json"
                         for i, _ in enumerate(qc_evidence)],
        "mac_drafts": [mac_root.rstrip("/") + "/" + contract["run_id"] + "/" + name for name in names],
        "stage_paths": [str(path) for path in staged],
        "final_delivery_authorized": False,
    }
    receipt["receipt_sha256"] = pe.digest(receipt)
    pe.write_once(receipt_path, receipt)
    pe.write_once(run_dir / "mac-review-handoff.json", receipt)
    mark_review_ready(task_dir / "done.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--draft-root", required=True, type=Path)
    parser.add_argument("--stage-root", required=True, type=Path)
    parser.add_argument("--mac-root", required=True)
    args = parser.parse_args()
    result = stage(args.task_dir, args.draft_root, args.stage_root, args.mac_root)
    print(json.dumps({"status": result["status"], "run_id": result["run_id"],
                      "receipt_sha256": result["receipt_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
