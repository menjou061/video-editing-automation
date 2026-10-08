import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "doubao-jianying-orchestrator"
sys.path.insert(0, str(ORCH))
sys.path.insert(0, str(ROOT))

from orchestrator import __version__  # noqa: E402
from tools import closed_loop  # noqa: E402


class Release140ContractTests(unittest.TestCase):
    def test_all_runtime_version_markers_are_1_4_0(self):
        package = json.loads((ROOT / "SKILL_PACKAGE.json").read_text(encoding="utf-8"))
        policy = json.loads((ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json").read_text(encoding="utf-8"))

        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "1.4.0")
        self.assertEqual(package["version"], "v1.4.0")
        self.assertIn("version: 1.4.0", (ROOT / "RULES.md").read_text(encoding="utf-8"))
        self.assertEqual((ROOT / "jianying-editor" / "VERSION").read_text(encoding="utf-8").strip(), "1.4.0")
        self.assertEqual((ROOT / "doubao-jianying-orchestrator" / "VERSION").read_text(encoding="utf-8").strip(), "1.4.0")
        self.assertEqual(__version__, "1.4.0")
        self.assertEqual(policy["video_tool_version"], "1.4.0")
        self.assertEqual(policy["visual_policy_version"], "1.4.0")

    def test_credentials_are_environment_bound(self):
        paths = [
            ROOT / "RULES.md",
            ROOT / "jy_poll.ps1",
            ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json",
            ROOT / "tools" / "closed_loop.py",
            ROOT / "run_task.template.ps1",
        ]
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths)
        self.assertNotRegex(text, r"--base-token\s+[A-Za-z0-9]{8,}")
        self.assertNotRegex(text, r"NAS_PASSWORD\s*=\s*['\"][^$'\"]+['\"]")
        self.assertIn("LARK_BASE_TOKEN", text)
        self.assertIn("NAS_PASSWORD", text)

    def test_subtitles_use_the_release_canvas_position(self):
        source = (ROOT / "jianying-editor/scripts/vendor/pyJianYingDraft/script_file.py").read_text(encoding="utf-8")
        self.assertIn("TextStyle(size=8, bold=True", source)
        self.assertIn("ClipSettings(transform_x=0.0, transform_y=-0.8)", source)
        self.assertNotIn("ClipSettings(transform_x=0.0, transform_y=-1000.0)", source)

    def test_table_update_blocks_without_token(self):
        with tempfile.TemporaryDirectory(prefix="video_release_140_") as raw:
            root = Path(raw)
            task = root / "task.json"
            done = root / "done.json"
            task.write_text(json.dumps({"record_id": "rec-test", "task_id": "task-test"}), encoding="utf-8")
            done.write_text(json.dumps({"status": "OK", "nas_parent_folder": "\\\\server\\draft"}), encoding="utf-8")
            with mock.patch.object(closed_loop, "audit", return_value={"eligible": True}), mock.patch.dict(os.environ, {}, clear=True):
                result = closed_loop.table_update(task, done, ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json")
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason"], "LARK_BASE_TOKEN_MISSING")


if __name__ == "__main__":
    unittest.main()
