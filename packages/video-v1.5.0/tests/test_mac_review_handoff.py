import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import eval_stage, mac_review_handoff, production_eval as pe  # noqa: E402


class MacReviewHandoffTests(unittest.TestCase):
    def test_stage_editable_copy_without_authorizing_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task_dir = root / "work" / "rec-a"
            task_dir.mkdir(parents=True)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            task = {"record_id": "rec-a", "task_id": "task-a", "script": "文案",
                    "category": "纸品", "sku": "SKU-A"}
            (task_dir / "task.json").write_text(json.dumps(task, ensure_ascii=False))
            runtime = {"tool_version": "1.5.0", "visual_policy_version": "1.4.1",
                       "package_sha256": "a" * 64, "visual_policy_sha256": "b" * 64}
            gold = pe.read(ROOT / "gold-reference.json")
            frozen = pe.freeze(task, "rec-a-run", [source], runtime, gold)
            pe.write_once(task_dir / "eval" / "rec-a-run" / "contract.json", frozen)
            pe.write_once(task_dir / "eval" / "active_contract.json", frozen)
            drafts = root / "drafts"
            draft = drafts / "draft-a"
            (draft / "media").mkdir(parents=True)
            (draft / "media" / "clip.mp4").write_bytes(b"clip")
            (draft / "draft_info.json").write_text(json.dumps({"id": "draft-id", "name": "draft-a",
                "platform": {"os": "windows"}, "materials": {"videos": [{"name": "clip.mp4", "path": "C:\\old\\clip.mp4"}]}}))
            (draft / "draft_meta_info.json").write_text("{}")
            qc_path = task_dir / "qc.json"
            qc_path.write_text(json.dumps({"status": "PASS_WITH_DEGRADED"}))
            (task_dir / "done.json").write_text(json.dumps({"status": "PREVIEW_READY", "drafts": ["draft-a"],
                "qc_reports": [{"draft": "draft-a", "status": "PASS_WITH_DEGRADED", "report": str(qc_path)}]}))
            eval_stage.prepare(task_dir, drafts)
            stage_root = root / "review"
            stage_root.mkdir()
            receipt = mac_review_handoff.stage(task_dir, drafts, stage_root, "/Volumes/Review")
            self.assertEqual(receipt["status"], "AWAITING_MAC_REVIEW")
            self.assertFalse(receipt["final_delivery_authorized"])
            self.assertEqual(receipt["draft_ids"], ["draft-id"])
            self.assertEqual(json.loads((task_dir / "done.json").read_text())["distribution_status"],
                             "AWAITING_MAC_REVIEW")
            staged = stage_root / "rec-a-run" / "draft-a" / "draft_info.json"
            staged_info = json.loads(staged.read_text())
            self.assertEqual(staged_info["materials"]["videos"][0]["path"],
                             "/Volumes/Review/rec-a-run/draft-a/media/clip.mp4")
            original_info = json.loads((draft / "draft_info.json").read_text())
            self.assertEqual(original_info["platform"]["os"], "windows")
            self.assertEqual(mac_review_handoff.stage(task_dir, drafts, stage_root, "/Volumes/Review"), receipt)
            (task_dir / "eval" / "rec-a-run" / "mac-review-handoff.json").unlink()
            (stage_root / "rec-a-run" / "mac-review-handoff.json").unlink()
            self.assertEqual(mac_review_handoff.stage(task_dir, drafts, stage_root, "/Volumes/Review"), receipt)
            review_dir = stage_root / "rec-a-run"
            evidence_file = review_dir / "evidence" / "mac-proof.png"
            evidence_file.write_bytes(b"review screenshot")
            mac_input = next(review_dir.glob("mac-review-*.input.json"))
            mac_payload = json.loads(mac_input.read_text())
            mac_payload.update(reviewed_by="tester", approved=True,
                               editable_project_opened=True,
                               reports=[{"path": "/Volumes/Review/rec-a-run/evidence/mac-proof.png",
                                         "sha256": pe.file_hash(evidence_file)}])
            mac_input.write_text(json.dumps(mac_payload))
            gold_input = next(review_dir.glob("gold-quality-review-*.input.json"))
            gold_payload = json.loads(gold_input.read_text())
            gold_payload["reviewed_by"] = "tester"
            gold_input.write_text(json.dumps(gold_payload))
            collected = eval_stage.collect(task_dir, drafts, review_stage_root=stage_root,
                                           mac_root="/Volumes/Review")
            self.assertEqual(collected["mac_status"], "PASS")
            self.assertEqual(collected["gold_status"], "UNCERTIFIED")
            staged.write_text("{}")
            with self.assertRaisesRegex(ValueError, "REVIEW_HANDOFF_DRIFT"):
                mac_review_handoff.stage(task_dir, drafts, stage_root, "/Volumes/Review")


if __name__ == "__main__":
    unittest.main()
