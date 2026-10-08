import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class QueueGuardrailTests(unittest.TestCase):
    def test_rework_queue_is_not_hidden_by_initial_queue(self):
        source = (ROOT / "jy_poll.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$worksets +=", source)
        self.assertIn("$worksets | Sort-Object", source)
        self.assertIn("$mode = $workset.mode", source)
        self.assertNotIn("if ($rids.Count -gt 0) { break }", source)

    def test_terminal_film_states_are_never_dispatched(self):
        source = (ROOT / "jy_poll.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$filmStatus -in @('验收通过','已二次修改','废弃')", source)
        self.assertIn("['\"fldyPLziiz\",\"!=\",\"废弃\"]".replace("'", ""), source)

    def test_rework_identity_and_dispatch_are_stable(self):
        poll = (ROOT / "jy_poll.ps1").read_text(encoding="utf-8-sig")
        runner = (ROOT / "run_task.template.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("SHA256]::Create()", poll)
        self.assertIn("SHA256]::Create()", runner)
        self.assertIn("$legacyIssueHash", poll)
        self.assertNotIn(".GetHashCode()", runner)
        self.assertIn("DISPATCH_CREATE_FAILED", poll)
        self.assertIn("DISPATCH_RUN_FAILED", poll)
        self.assertIn("product = $product", poll)
        self.assertIn("JY_PRODUCT_IDENTITY_FILE", poll)
        self.assertIn("category = [string]$identityRow.category", poll)
        self.assertIn("$evalTool = Join-Path $PIPE 'tools\\eval_bootstrap.py'", runner)
        self.assertLess(runner.index("EVAL_BOOTSTRAP "), runner.index("Start-Process -FilePath 'cmd.exe'"))
        self.assertIn("mac_review_handoff.py", runner)
        self.assertIn("AWAITING_MAC_REVIEW", runner)
        self.assertIn("PREVIEW_PENDING_TRANSFER", runner)
        self.assertIn("REVIEW_RETRY_READY", poll)
        self.assertLess(poll.index("REVIEW_RETRY_READY"), poll.index("# ---- 读飞书"))

    def test_scheduled_entrypoints_load_external_config_and_share_data_roots(self):
        poll = (ROOT / "jy_poll.ps1").read_text(encoding="utf-8-sig")
        runner = (ROOT / "run_task.template.ps1").read_text(encoding="utf-8-sig")
        worker = (ROOT / "batch_worker.py").read_text(encoding="utf-8")
        loader = (ROOT / "tools" / "load-runtime-config.ps1").read_text(encoding="utf-8")
        self.assertIn("load-runtime-config.ps1", poll)
        self.assertIn("load-runtime-config.ps1", runner)
        self.assertIn("JY_WORK_ROOT", poll)
        self.assertIn("JY_WORK_ROOT", runner)
        self.assertIn("JY_STATE_ROOT", runner)
        self.assertIn("JY_LOG_ROOT", poll)
        self.assertIn("PIPE = PIPE_ROOT", worker)
        self.assertIn('read_text(encoding="utf-8-sig")', worker)
        self.assertIn("ConvertTo-SecureString $cipher", loader)
        self.assertNotIn("$profile =", loader)

    def test_missing_material_is_deferred_without_a_generation_attempt(self):
        worker = (ROOT / "batch_worker.py").read_text(encoding="utf-8")
        self.assertIn('reason="material_dir_unavailable"', worker)
        self.assertIn('"deferred_reason": reason', worker)
        self.assertIn('if preflight.get("missing_material"):', worker)


if __name__ == "__main__":
    unittest.main()
