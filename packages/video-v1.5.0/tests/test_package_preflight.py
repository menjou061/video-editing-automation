import tempfile
import json
import shutil
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_preflight import run_preflight


class PackagePreflightTests(unittest.TestCase):
    def test_complete_offline_source_can_pass_without_claiming_windows_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            qc = Path(directory) / "draft_visual_qc.py"
            qc.write_text("# external machine-supplied helper\n", encoding="utf-8")
            result = run_preflight(ROOT, require_windows=False, draft_qc_script=qc)
        self.assertTrue(result["ok"], result["blockers"])
        self.assertEqual(result["status"], "READY")

    def test_missing_qc_helper_is_a_blocker_not_a_pass(self):
        result = run_preflight(ROOT, require_windows=False,
                               draft_qc_script=ROOT / "missing-qc.py")
        self.assertFalse(result["ok"])
        self.assertTrue(any(item.startswith("DRAFT_VISUAL_QC_MISSING:")
                            for item in result["blockers"]))

    def test_runtime_preflight_requires_windows(self):
        result = run_preflight(ROOT, require_windows=True,
                               draft_qc_script=ROOT / "missing-qc.py")
        if __import__("os").name != "nt":
            self.assertIn("WINDOWS_RENDERER_REQUIRED", result["blockers"])

    def test_component_version_drift_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "package"
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
            (root / "jianying-editor" / "VERSION").write_text("1.4.1\n", encoding="utf-8")
            result = run_preflight(root, require_windows=False)
        self.assertIn("COMPONENT_VERSION_MISMATCH:jianying-editor/VERSION", result["blockers"])

    def test_invalid_metadata_shape_is_a_bounded_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL_PACKAGE.json").write_text(json.dumps([]), encoding="utf-8")
            (root / "VERSION").write_text("1.5.0\n", encoding="utf-8")
            result = run_preflight(root, require_windows=False)
        self.assertFalse(result["ok"])
        self.assertIn("PACKAGE_METADATA_UNREADABLE", result["blockers"])


if __name__ == "__main__":
    unittest.main()
