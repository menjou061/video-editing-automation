import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import eval_bootstrap  # noqa: E402


class EvalBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        (self.media / "clip.mp4").write_bytes(b"visible source")
        self.task_path = self.root / "task.json"
        self.mapping_path = self.root / "identity.json"
        self.task = {
            "record_id": "rec-test", "task_id": "task-test", "script": "真实文案",
            "product": "棉柔巾", "material_dir": r"\\nas\素材\棉柔巾",
            "material_dir_z": str(self.media), "category": "纸品", "sku": "SKU-A",
        }
        self.mapping = {"records": {"rec-test": {
            "product": "棉柔巾", "material_dir": self.task["material_dir"],
            "category": "纸品", "sku": "SKU-A", "human_confirmed": True,
        }}}
        self._write()

    def _write(self):
        self.task_path.write_text(json.dumps(self.task, ensure_ascii=False), encoding="utf-8")
        self.mapping_path.write_text(json.dumps(self.mapping, ensure_ascii=False), encoding="utf-8")

    def test_exact_identity_required(self):
        row = eval_bootstrap.resolve_identity(self.task, self.mapping)
        self.assertEqual(row["sku"], "SKU-A")
        self.mapping["records"]["rec-test"]["sku"] = "待确认"
        with self.assertRaisesRegex(ValueError, "PRODUCT_IDENTITY_NOT_CONFIRMED"):
            eval_bootstrap.resolve_identity(self.task, self.mapping)
        self.mapping["records"]["rec-test"]["sku"] = "SKU-B"
        self.task["material_dir"] = r"\\nas\素材\其他"
        with self.assertRaisesRegex(ValueError, "PRODUCT_OR_SOURCE_BINDING_DRIFT"):
            eval_bootstrap.resolve_identity(self.task, self.mapping)

    def test_confirmed_directory_binding_can_be_reused(self):
        binding = self.mapping["records"].pop("rec-test")
        self.mapping["sources"] = {self.task["material_dir"]: binding}
        self.assertEqual(eval_bootstrap.resolve_identity(self.task, self.mapping)["sku"], "SKU-A")
        second = dict(self.task, record_id="rec-other", task_id="task-other")
        self.assertEqual(eval_bootstrap.resolve_identity(second, self.mapping)["sku"], "SKU-A")
        second["product"] = "别的产品"
        with self.assertRaisesRegex(ValueError, "PRODUCT_OR_SOURCE_BINDING_DRIFT"):
            eval_bootstrap.resolve_identity(second, self.mapping)

    def test_freeze_is_before_generation_and_reuses_only_identical_inputs(self):
        runtime = {"tool_version": "1.5.0", "visual_policy_version": "1.4.1",
                   "package_sha256": "a" * 64, "visual_policy_sha256": "b" * 64,
                   "eval_pack_version": "1.3", "eval_schema_version": 1}
        with mock.patch.object(eval_bootstrap.pe, "runtime_identity", return_value=runtime):
            first = eval_bootstrap.prepare(self.task_path, ROOT, self.mapping_path)
            self.assertEqual(first["status"], "FROZEN")
            frozen = json.loads((self.root / "eval" / "active_contract.json").read_text())
            self.assertEqual(frozen["sku"], "SKU-A")
            again = eval_bootstrap.prepare(self.task_path, ROOT, self.mapping_path)
            self.assertEqual(again["status"], "FROZEN_REUSED")
            (self.media / "clip.mp4").write_bytes(b"changed source")
            with self.assertRaisesRegex(ValueError, "ACTIVE_EVAL_INPUT_DRIFT"):
                eval_bootstrap.prepare(self.task_path, ROOT, self.mapping_path)

    def test_enforce_requires_real_observations(self):
        with mock.patch.object(eval_bootstrap.pe, "runtime_identity", return_value={}):
            with self.assertRaisesRegex(ValueError, "EVAL_ROLLOUT_REQUIRES_TWO_VERIFIED_REAL_TASKS"):
                eval_bootstrap.prepare(self.task_path, ROOT, self.mapping_path, mode="enforce")


if __name__ == "__main__":
    unittest.main()
