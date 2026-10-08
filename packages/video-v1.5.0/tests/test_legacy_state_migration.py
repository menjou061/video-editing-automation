import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import migrate_legacy_state as migration  # noqa: E402


class LegacyStateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "old-renderer"
        (self.source / "state").mkdir(parents=True)
        (self.source / "work").mkdir()
        processed = {"r-error#rev4": {"status": "ERROR", "attempts": 3,
                                     "issue_hash": "old-dotnet-hash"}}
        (self.source / "state" / "processed.json").write_text(
            "\ufeff" + json.dumps(processed), encoding="utf-8")
        self._record("r-ok", {"status": "OK", "finished_at": "yesterday"},
                     {"task_id": "old-task"})
        self._record("r-error", {"status": "ERROR", "error": "render failed",
                                  "attempts": 3}, {"task_id": "old-error"})
        self._record("r-deferred", {"status": "DEFERRED",
                                     "deferred_reason": "material_dir_unavailable"},
                     {"task_id": "old-deferred"})
        active = self.source / "work" / "r-running"
        active.mkdir()
        (active / ".task_run.lock").write_text("pid=unknown", encoding="utf-8")
        (active / "task.json").write_text(json.dumps({"task_id": "active"}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _record(self, record_id, done, task):
        folder = self.source / "work" / record_id
        folder.mkdir()
        (folder / "done.json").write_text("\ufeff" + json.dumps(done), encoding="utf-8")
        (folder / "task.json").write_text(json.dumps(task), encoding="utf-8")

    def test_inspect_is_read_only_and_never_fabricates_v150_receipts(self):
        before = {p.relative_to(self.source): p.read_bytes()
                  for p in self.source.rglob("*") if p.is_file()}
        report = migration.inspect_source(self.source)
        after = {p.relative_to(self.source): p.read_bytes()
                 for p in self.source.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        rows = {row["record_id"]: row for row in report["records"]}
        self.assertEqual(rows["r-ok"]["decision"], "history_only_unverified")
        self.assertEqual(rows["r-error"]["decision"], "manual_retry_or_skip_required")
        self.assertEqual(rows["r-error"]["attempts"], 3)
        self.assertEqual(rows["r-deferred"]["decision"], "deferred_no_attempt")
        self.assertEqual(rows["r-running"]["decision"], "live_process_check_required")
        self.assertEqual(report["safe_to_continue"], False)
        self.assertNotIn("terminal_state", rows["r-ok"])
        self.assertNotIn("safe_to_continue", rows["r-ok"])

    def test_migration_archives_evidence_idempotently_without_touching_source(self):
        destination = self.root / "new-renderer"
        before = migration.inspect_source(self.source)["source_snapshot_sha256"]
        first = migration.migrate(self.source, destination)
        second = migration.migrate(self.source, destination)
        after = migration.inspect_source(self.source)["source_snapshot_sha256"]
        self.assertEqual(first["status"], "archived")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(before, after)
        archived = destination / migration.ARCHIVE_NAME / "source_snapshot" / "state" / "processed.json"
        self.assertTrue(archived.is_file())
        self.assertEqual(archived.read_bytes(), (self.source / "state" / "processed.json").read_bytes())

    def test_conflicting_destination_is_refused(self):
        destination = self.root / "new-renderer"
        migration.migrate(self.source, destination)
        report_path = destination / migration.ARCHIVE_NAME / "migration-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["source_snapshot_sha256"] = "different"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(FileExistsError):
            migration.migrate(self.source, destination)

    def test_empty_source_can_be_archived_and_repeated(self):
        source = self.root / "empty-renderer"
        source.mkdir()
        destination = self.root / "new-empty"
        first = migration.migrate(source, destination)
        second = migration.migrate(source, destination)
        self.assertEqual(first["status"], "archived")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["report"]["source_files"], [])

    def test_repeated_migration_rejects_corrupted_or_missing_archived_evidence(self):
        for change in ("corrupt", "remove"):
            with self.subTest(change=change):
                destination = self.root / ("new-" + change)
                migration.migrate(self.source, destination)
                path = destination / migration.ARCHIVE_NAME / "source_snapshot" / "state" / "processed.json"
                if change == "corrupt":
                    path.write_text("{}", encoding="utf-8")
                else:
                    path.unlink()
                with self.assertRaises(FileExistsError):
                    migration.migrate(self.source, destination)

    def test_global_scheduler_lock_is_archived_and_requires_live_check(self):
        lock = self.source / "state" / "RUNNING.lock"
        lock.write_text("r-error", encoding="utf-8")
        report = migration.inspect_source(self.source)
        self.assertIn("state/RUNNING.lock", report["active_or_unknown_locks"])
        self.assertTrue(all(row["decision"] == "live_process_check_required"
                            for row in report["records"]))

    def test_record_key_cannot_read_outside_source(self):
        (self.source / "state" / "processed.json").write_text(
            json.dumps({"../outside": {"status": "OK"}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            migration.inspect_source(self.source)

    def test_task_directory_symlink_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "task.json").write_text("{}", encoding="utf-8")
        try:
            (self.source / "work" / "linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit directory symlinks")
        with self.assertRaises(ValueError):
            migration.inspect_source(self.source)


if __name__ == "__main__":
    unittest.main()
