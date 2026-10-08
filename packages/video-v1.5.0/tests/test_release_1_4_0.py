import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout
from io import StringIO


ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "doubao-jianying-orchestrator"
sys.path.insert(0, str(ORCH))
sys.path.insert(0, str(ROOT))

from orchestrator import __version__  # noqa: E402
from tools import closed_loop  # noqa: E402


class Release140ContractTests(unittest.TestCase):
    def test_published_entrypoint_exists(self):
        package = json.loads((ROOT / "SKILL_PACKAGE.json").read_text(encoding="utf-8"))
        entrypoint = ROOT / package["entrypoint"]
        self.assertTrue(entrypoint.is_file(), entrypoint)

    def test_published_entrypoint_help_returns_success(self):
        from orchestrator.cli import main

        with redirect_stdout(StringIO()):
            self.assertEqual(main(["--help"]), 0)

    def test_runtime_150_and_independent_policy_141(self):
        package = json.loads((ROOT / "SKILL_PACKAGE.json").read_text(encoding="utf-8"))
        policy = json.loads((ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json").read_text(encoding="utf-8"))

        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "1.5.0")
        self.assertEqual(package["version"], "v1.5.0")
        self.assertIn("version: 1.4.1", (ROOT / "RULES.md").read_text(encoding="utf-8"))
        self.assertEqual((ROOT / "jianying-editor" / "VERSION").read_text(encoding="utf-8").strip(), "1.5.0")
        self.assertEqual((ROOT / "doubao-jianying-orchestrator" / "VERSION").read_text(encoding="utf-8").strip(), "1.5.0")
        self.assertEqual(__version__, "1.5.0")
        self.assertEqual(policy["video_tool_version"], "1.5.0")
        self.assertEqual(policy["visual_policy_version"], "1.4.1")

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

    def test_standalone_runner_reports_failures(self):
        text = (ROOT / "run_task.template.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("failure-update", text)
        self.assertIn("FAILURE_NOTIFICATION", text)
        self.assertNotIn("飞书不回写", text)

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

    def test_failure_update_writes_local_receipt_without_token(self):
        with tempfile.TemporaryDirectory(prefix="video_failure_receipt_") as raw:
            root = Path(raw)
            task = root / "task.json"
            done = root / "done.json"
            task.write_text(json.dumps({"record_id": "rec-fail", "task_id": "task-fail", "issue": "视觉匹配失败"}), encoding="utf-8")
            done.write_text(json.dumps({"status": "ERROR", "failures": [{"phase": "visual", "message": "SHOT_MATCH_BLOCKED"}]}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                result = closed_loop.failure_update(task, done, ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "LARK_BASE_TOKEN_MISSING")
            receipt = json.loads((root / "failure_notification_receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["patch"]["任务状态"], ["生成失败"])
            self.assertIn("[自动化失败回执]", receipt["patch"]["问题反馈"])
            self.assertNotIn("草稿文件", receipt["patch"])

    def test_failure_update_accepts_power_shell_utf8_bom(self):
        with tempfile.TemporaryDirectory(prefix="video_failure_bom_") as raw:
            root = Path(raw)
            task = root / "task.json"
            done = root / "done.json"
            task.write_text(json.dumps({"record_id": "rec-bom", "task_id": "task-bom"}), encoding="utf-8-sig")
            done.write_text(json.dumps({"status": "ERROR", "error": "bom"}), encoding="utf-8-sig")
            with mock.patch.dict(os.environ, {}, clear=True):
                result = closed_loop.failure_update(task, done, ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json")
            self.assertEqual(result["record_id"], "rec-bom")
            self.assertEqual(result["task_id"], "task-bom")

    def test_failure_update_sends_status_and_feedback_only(self):
        with tempfile.TemporaryDirectory(prefix="video_failure_write_") as raw:
            root = Path(raw)
            task = root / "task.json"
            done = root / "done.json"
            task.write_text(json.dumps({"record_id": "rec-fail", "task_id": "task-fail"}), encoding="utf-8")
            done.write_text(json.dumps({"status": "PARTIAL", "error": "timeout"}), encoding="utf-8")
            fake = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
            with mock.patch.dict(os.environ, {"LARK_BASE_TOKEN": "token", "LARK_TABLE_ID": "tbl"}, clear=True), \
                 mock.patch.object(closed_loop.subprocess, "run", return_value=fake) as run:
                result = closed_loop.failure_update(task, done, ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json")
            self.assertTrue(result["ok"])
            sent = json.loads(run.call_args.args[0][-1])
            patch = sent["update_records"]["rec-fail"]
            self.assertEqual(patch["任务状态"], ["生成失败"])
            self.assertIn("问题反馈", patch)
            self.assertNotIn("草稿文件", patch)


if __name__ == "__main__":
    unittest.main()
