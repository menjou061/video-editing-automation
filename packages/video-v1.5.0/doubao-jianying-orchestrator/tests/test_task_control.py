import json
import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator import cli, vision_analyzer
from orchestrator.task_log import (TaskAlreadyRunningError, TaskLogger,
                                   result_contract, runtime_version)  # noqa: E402
import batch_worker  # noqa: E402


class ResultContractTests(unittest.TestCase):
    def test_runtime_version_is_read_from_package(self):
        self.assertEqual(runtime_version(), "1.5.0")

    def test_logger_ignores_stale_caller_version(self):
        with tempfile.TemporaryDirectory() as raw:
            logger = TaskLogger(Path(raw), {}, version="v1.3.13")
            try:
                self.assertEqual(logger.version, "1.5.0")
            finally:
                logger.finalize({"status": "ERROR", "error": "test cleanup"})

    def test_transient_vision_failure_is_retryable_but_not_safe_to_continue(self):
        result = result_contract(
            {"status": "ERROR", "message": "VISION_CALL_FAILED: network timeout"},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["pipeline_version"], "1.5.0")
        self.assertEqual(result["failure_class"], "vision")
        self.assertEqual(result["failure_code"], "VISION_CALL_FAILED")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["safe_to_continue"])
        self.assertEqual(result["next_action"], "retry_stage")

    def test_semantic_block_is_not_retryable(self):
        result = result_contract(
            {"status": "SHOT_MATCH_BLOCKED", "message": "material gaps reported (2)",
             "pending_items": [{"type": "material_gap"}]},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["terminal_state"], "BLOCKED")
        self.assertEqual(result["failure_class"], "semantic")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["safe_to_continue"])
        self.assertEqual(result["next_action"], "manual_review")

    def test_environment_problems_remain_blocked(self):
        result = result_contract(
            {"status": "ENV_ERROR", "problems": ["ffmpeg missing"]},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["terminal_state"], "BLOCKED")
        self.assertEqual(result["failure_class"], "environment")

    def test_known_marker_overrides_generic_failure_class(self):
        result = result_contract(
            {"status": "ERROR", "failure_class": "unknown",
             "error": "VISION_CALL_FAILED: credentials rejected"},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["failure_class"], "vision")
        self.assertFalse(result["retryable"])

    def test_deferred_result_is_not_safe_to_continue(self):
        result = result_contract(
            {"status": "DEFERRED", "message": "operator skipped"},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["terminal_state"], "DEFERRED")
        self.assertEqual(result["next_action"], "skip_task")
        self.assertFalse(result["safe_to_continue"])

    def test_preview_success_is_uncertified(self):
        result = result_contract(
            {"status": "SUCCESS", "delivery_mode": "preview",
             "coverage": {"certification": "UNCERTIFIED"}},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["terminal_state"], "UNCERTIFIED")
        self.assertFalse(result["safe_to_continue"])

    def test_package_not_active_is_a_blocked_ui_result(self):
        result = result_contract(
            {"status": "PACKAGE_NOT_ACTIVE", "failure_class": "ui"},
            task_id="task", run_id="run", task_log="/tmp/task",
        )
        self.assertEqual(result["terminal_state"], "BLOCKED")
        self.assertEqual(result["failure_class"], "ui")
        self.assertEqual(result["failure_code"], "PACKAGE_NOT_ACTIVE")
        self.assertFalse(result["safe_to_continue"])

    def test_logger_persists_runtime_and_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            logger = TaskLogger(root, {"task_id": "source-task"})
            result = logger.finalize({"status": "SUCCESS"})
            self.assertEqual(result["run_id"], logger.run_id)
            self.assertEqual(result["pipeline_version"], "1.5.0")
            self.assertTrue(result["manifest_sha256"])
            self.assertEqual(result["runtime"]["skill_root"], str(
                Path(__file__).resolve().parents[1]))
            task = json.loads((logger.task_dir / "task.json").read_text(encoding="utf-8"))
            saved = json.loads((root / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(task["pipeline_version"], "1.5.0")
            self.assertIn("runtime", task)
            self.assertTrue(saved["safe_to_continue"])

    def test_same_report_directory_cannot_run_twice(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = TaskLogger(root, {"task_id": "source-task"})
            try:
                with self.assertRaises(TaskAlreadyRunningError):
                    TaskLogger(root, {"task_id": "source-task"})
            finally:
                first.finalize({"status": "ERROR", "error": "test cleanup"})

    def test_build_missing_input_stops_before_generation_with_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "manifest.json"
            report = root / "report"
            manifest.write_text(json.dumps({
                "task_id": "source-task",
                "segments": [{"video": "missing.mov", "duration": 1}],
            }), encoding="utf-8")
            code = cli.cmd_build(argparse.Namespace(
                input=str(manifest), report_dir=str(report),
                strict_script=False, delivery_mode="formal", trigger_phrase="",
            ))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "ENV_ERROR")
            self.assertEqual(saved["failure_class"], "environment")
            self.assertFalse(saved["safe_to_continue"])


class BatchContinuationTests(unittest.TestCase):
    def test_deterministic_block_is_not_retried_as_provider_failure(self):
        classified = batch_worker.classify_build_failure(
            "SHOT_MATCH_BLOCKED: material gaps reported")
        self.assertTrue(classified["deterministic_block"])
        self.assertIn("SHOT_MATCH_BLOCKED", classified["markers"])

    def test_invalid_argument_is_a_deterministic_environment_block(self):
        classified = batch_worker.classify_build_failure(
            "[Errno 22] Invalid argument")
        self.assertTrue(classified["deterministic_block"])

    def test_task_budget_is_blocked_and_never_retryable(self):
        contract = batch_worker._batch_result_contract(
            "ERROR", run_id="run", error="TASK_BUDGET_EXHAUSTED")
        self.assertEqual(contract["terminal_state"], "BLOCKED")
        self.assertEqual(contract["failure_class"], "environment")
        self.assertFalse(contract["retryable"])

    def test_vision_credentials_are_not_classified_as_transient(self):
        classified = batch_worker.classify_build_failure(
            "VISION_CALL_FAILED: credentials rejected")
        self.assertFalse(classified["transient"])
        self.assertTrue(classified["deterministic_block"])

    def test_vision_network_failure_is_transient(self):
        classified = batch_worker.classify_build_failure(
            "VISION_CALL_FAILED: network timeout while calling provider")
        self.assertTrue(classified["transient"])
        self.assertFalse(classified["deterministic_block"])

    def test_main_skip_current_marks_deferred_and_selects_only_next_task(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            queue = [
                {"record_id": "first", "title": "First"},
                {"record_id": "second", "title": "Second"},
            ]
            (root / "batch_queue.json").write_text(json.dumps(queue), encoding="utf-8")
            (root / "first").mkdir()
            (root / "first" / "done.json").write_text(json.dumps({
                "record_id": "first", "status": "ERROR", "run_id": "old-run",
                "terminal_state": "BLOCKED", "safe_to_continue": False,
            }), encoding="utf-8")
            selected = []
            with mock.patch.object(batch_worker, "WORK", root), \
                 mock.patch.object(batch_worker, "_queue_preflight", return_value={"ok": True, "issues": []}), \
                 mock.patch.object(batch_worker, "PROGRESS", root / "progress.jsonl"), \
                 mock.patch.object(batch_worker, "STATUS_SNAPSHOT", root / "snapshot.json"), \
                 mock.patch.object(batch_worker, "LOG", root / "worker.log"), \
                 mock.patch.object(batch_worker, "_run_task", side_effect=lambda task: selected.append(task["record_id"])), \
                 mock.patch.dict(batch_worker.os.environ, {
                     "JY_TASK_SKIP_CURRENT": "1", "JY_TASK_RECORD_ID": "",
                     "JY_TASK_FORCE_REBUILD": "",
                 }, clear=False):
                batch_worker.main()
            self.assertEqual(selected, ["second"])
            saved = json.loads((root / "first" / "done.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "DEFERRED")
            self.assertEqual(saved["parent_run_id"], "old-run")

    def test_failed_previous_task_requires_explicit_decision(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rid = "rec-test"
            (root / rid).mkdir()
            (root / rid / "done.json").write_text(json.dumps({
                "record_id": rid,
                "status": "ERROR",
                "terminal_state": "BLOCKED",
                "safe_to_continue": False,
                "failure_class": "semantic",
            }), encoding="utf-8")
            with mock.patch.object(batch_worker, "WORK", root), \
                 mock.patch.dict(batch_worker.os.environ, {}, clear=False):
                allowed, prior = batch_worker._continuation_allowed({"record_id": rid})
            self.assertFalse(allowed)
            self.assertEqual(prior["failure_class"], "semantic")

    def test_repeated_worker_invocations_keep_failed_task_held(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            queue = [
                {"record_id": "first", "title": "First"},
                {"record_id": "second", "title": "Second"},
            ]
            (root / "batch_queue.json").write_text(json.dumps(queue), encoding="utf-8")
            (root / "first").mkdir()
            failed = {
                "record_id": "first", "status": "ERROR", "run_id": "old-run",
                "terminal_state": "BLOCKED", "failure_class": "semantic",
                "safe_to_continue": False,
            }
            (root / "first" / "done.json").write_text(json.dumps(failed), encoding="utf-8")
            selected = []
            with mock.patch.object(batch_worker, "WORK", root), \
                 mock.patch.object(batch_worker, "PROGRESS", root / "progress.jsonl"), \
                 mock.patch.object(batch_worker, "STATUS_SNAPSHOT", root / "snapshot.json"), \
                 mock.patch.object(batch_worker, "LOG", root / "worker.log"), \
                 mock.patch.object(batch_worker, "_run_task",
                                   side_effect=lambda task: selected.append(task["record_id"])), \
                 mock.patch.dict(batch_worker.os.environ, {
                     "JY_TASK_SKIP_CURRENT": "", "JY_TASK_RECORD_ID": "",
                     "JY_TASK_FORCE_REBUILD": "",
                 }, clear=False):
                batch_worker.main()
                batch_worker.main()
            self.assertEqual(selected, [])
            saved = json.loads((root / "first" / "done.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["run_id"], "old-run")
            events = (root / "progress.jsonl").read_text(encoding="utf-8")
            self.assertEqual(events.count('"event": "queue_hold"'), 2)
            hold_rows = [json.loads(line) for line in events.splitlines()
                         if '"event": "queue_hold"' in line]
            self.assertTrue(all(row.get("failure_class") == "semantic" for row in hold_rows))

    def test_successful_previous_task_allows_next_step(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rid = "rec-test"
            (root / rid).mkdir()
            (root / rid / "done.json").write_text(json.dumps({
                "record_id": rid,
                "status": "OK",
                "terminal_state": "SUCCESS",
                "safe_to_continue": True,
                "finished_at": "2026-10-04 22:00:00",
            }), encoding="utf-8")
            with mock.patch.object(batch_worker, "WORK", root):
                allowed, _ = batch_worker._continuation_allowed({"record_id": rid})
            self.assertTrue(allowed)

    def test_success_without_finish_receipt_is_held(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rid = "rec-test"
            (root / rid).mkdir()
            (root / rid / "done.json").write_text(json.dumps({
                "record_id": rid,
                "status": "OK",
                "terminal_state": "SUCCESS",
                "safe_to_continue": True,
            }), encoding="utf-8")
            with mock.patch.object(batch_worker, "WORK", root):
                allowed, prior = batch_worker._continuation_allowed({"record_id": rid})
            self.assertFalse(allowed)
            self.assertEqual(prior["reason"], "TASK_FINISH_EVENT_MISSING")

    def test_explicit_skip_marks_task_deferred_and_links_parent_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rid = "rec-test"
            (root / rid).mkdir()
            prior = {"record_id": rid, "status": "ERROR", "run_id": "old-run",
                     "terminal_state": "BLOCKED", "safe_to_continue": False}
            with mock.patch.object(batch_worker, "WORK", root), \
                 mock.patch.object(batch_worker, "_pipeline_version", return_value="1.4.0"), \
                 mock.patch.object(batch_worker, "progress"):
                saved = batch_worker._mark_deferred({"record_id": rid}, prior)
            self.assertEqual(saved["status"], "DEFERRED")
            self.assertEqual(saved["terminal_state"], "DEFERRED")
            self.assertEqual(saved["parent_run_id"], "old-run")
            persisted = json.loads((root / rid / "done.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["next_action"], "skip_task")

    def test_batch_record_lock_blocks_concurrent_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "rec-test"
            task_dir.mkdir()
            lock = batch_worker._acquire_record_lock(task_dir, "run-1")
            try:
                with self.assertRaises(RuntimeError) as caught:
                    batch_worker._acquire_record_lock(task_dir, "run-2")
                self.assertIn("TASK_ALREADY_RUNNING", str(caught.exception))
            finally:
                batch_worker._release_record_lock(lock, "run-1")


class RecoveryStageTests(unittest.TestCase):
    def _manifest(self, root: Path, **extra) -> Path:
        payload = {"task_id": "recover-task", "segments": [{"video": "missing.mov", "duration": 1}]}
        payload.update(extra)
        path = root / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_preflight_stops_on_missing_source_without_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            with mock.patch.object(cli, "environment_report", return_value={"ready": True}):
                code = cli.cmd_recover(argparse.Namespace(
                    input=str(self._manifest(root)), report_dir=str(report),
                    stage="preflight", draft=""))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "ENV_ERROR")
            self.assertEqual(saved["recovery_stage"], "preflight")
            self.assertFalse((report / "matched_manifest.json").exists())

    def test_semantic_match_requires_existing_analysis(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            with mock.patch.object(cli, "environment_report", return_value={"ready": True}):
                code = cli.cmd_recover(argparse.Namespace(
                    input=str(self._manifest(root)), report_dir=str(report),
                    stage="semantic_match", draft=""))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "RECOVERY_ANALYSIS_REQUIRED")
            self.assertEqual(saved["failure_class"], "semantic")

    def test_draft_write_requires_cached_match(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            with mock.patch.object(cli, "environment_report", return_value={"ready": True}):
                code = cli.cmd_recover(argparse.Namespace(
                    input=str(self._manifest(root)), report_dir=str(report),
                    stage="draft_write", draft=""))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "RECOVERY_MATCHED_MANIFEST_REQUIRED")
            self.assertFalse(saved["safe_to_continue"])

    def test_draft_write_rejects_raw_manifest_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            report.mkdir()
            # A raw input manifest has segments too, but it is not the nested
            # successful result emitted by semantic_match.
            (report / "matched_manifest.json").write_text(json.dumps({
                "segments": [{"video": "already-listed.mov", "duration": 1}],
            }), encoding="utf-8")
            code = cli.cmd_recover(argparse.Namespace(
                input=str(self._manifest(root)), report_dir=str(report),
                stage="draft_write", draft=""))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "RECOVERY_MATCHED_MANIFEST_REQUIRED")
            self.assertEqual(saved["failure_class"], "semantic")

    def test_ui_acceptance_requires_explicit_draft(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            with mock.patch.object(cli, "environment_report", return_value={"ready": True}):
                code = cli.cmd_recover(argparse.Namespace(
                    input=str(self._manifest(root)), report_dir=str(report),
                    stage="ui_acceptance", draft=""))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "RECOVERY_DRAFT_REQUIRED")
            self.assertEqual(saved["failure_class"], "ui")

    def test_ui_acceptance_keeps_manual_gate_after_read_only_diagnosis(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            draft = root / "draft"
            draft.mkdir()
            with mock.patch("orchestrator.diagnostics.diagnose_timeline",
                            return_value={"status": "DIAGNOSIS_OK", "issues": []}):
                code = cli.cmd_recover(argparse.Namespace(
                    input=str(self._manifest(root)), report_dir=str(report),
                    stage="ui_acceptance", draft=str(draft)))
            self.assertEqual(code, 0)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "UI_ACCEPTANCE_PENDING")
            self.assertEqual(saved["terminal_state"], "UNCERTIFIED")
            self.assertFalse(saved["safe_to_continue"])

    def test_ui_acceptance_failure_remains_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "report"
            draft = root / "draft"
            draft.mkdir()
            with mock.patch("orchestrator.diagnostics.diagnose_timeline",
                            return_value={"status": "DESKTOP_IMPORT_BLOCKED",
                                          "issues": ["DRAFT_INFO_MISSING"]}):
                code = cli.cmd_recover(argparse.Namespace(
                    input=str(self._manifest(root)), report_dir=str(report),
                    stage="ui_acceptance", draft=str(draft)))
            self.assertEqual(code, 1)
            saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "DESKTOP_IMPORT_BLOCKED")
            self.assertEqual(saved["terminal_state"], "BLOCKED")
            self.assertEqual(saved["failure_class"], "ui")


class VisionRetryTests(unittest.TestCase):
    def test_transient_vision_call_has_only_one_bounded_retry(self):
        with mock.patch.object(vision_analyzer, "_run_vision",
                               side_effect=[(False, "502"), (False, "502"),
                                            (True, "should not be called")]) as call, \
                mock.patch.object(vision_analyzer.time, "sleep"):
            ok, text = vision_analyzer._run_vision_with_retry(
                Path("sheet.jpg"), "prompt", Path("response.txt"),
                profile="flash", timeout=1)
        self.assertFalse(ok)
        self.assertEqual(text, "502")
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
