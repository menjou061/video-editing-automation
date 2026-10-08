import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch_worker  # noqa: E402


class BundledQCWorkerTests(unittest.TestCase):
    def test_worker_invokes_real_bundled_script_and_consumes_failure_report(self):
        skill = ROOT / "doubao-jianying-orchestrator"
        helper = skill / "orchestrator/draft_visual_qc.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "report"
            with mock.patch.object(batch_worker, "SKILL", skill), \
                    mock.patch.object(batch_worker, "POST_DRAFT_QC", helper), \
                    mock.patch.object(batch_worker, "PY", sys.executable):
                result = batch_worker.run_post_draft_qc(root / "missing-draft", report_dir)
            self.assertEqual(result["rc"], 2, result)
            self.assertEqual(result["status"], "UNCERTIFIED")
            payload = json.loads((report_dir / "draft_visual_qc.json").read_text())
            self.assertEqual(payload["release_eligibility"], "BLOCKED")
            self.assertTrue(payload["issues"][0].startswith("POST_DRAFT_QC_EXCEPTION:"))


if __name__ == "__main__":
    unittest.main()
