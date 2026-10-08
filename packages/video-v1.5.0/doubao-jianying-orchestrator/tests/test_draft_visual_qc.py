"""Post-build QC gates with isolated files and simulated vision responses."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import draft_visual_qc as qc, vision_analyzer  # noqa: E402


class DraftVisualQCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.draft = self.root / "draft"
        self.report = self.root / "report"
        self.draft.mkdir()
        self.report.mkdir()
        self.output = self.report / "draft_visual_qc.json"
        videos, segments = [], []
        for index in (1, 2):
            media = self.draft / f"source-{index}.mp4"
            media.write_bytes(b"isolated fixture; extraction is mocked")
            videos.append({"id": str(index), "path": str(media)})
            segments.append({"material_id": str(index),
                             "source_timerange": {"start": 1_000_000, "duration": 2_000_000},
                             "target_timerange": {"start": (index - 1) * 2_000_000,
                                                   "duration": 2_000_000}})
        self.info = {"materials": {"videos": videos},
                     "tracks": [{"name": "video_broll", "segments": segments}]}
        self.claims = [{"claim_text": f"claim {index}", "temporary_shot_id": str(index)}
                       for index in (1, 2)]
        self._save()

    def tearDown(self):
        self.temp.cleanup()

    def _save(self):
        (self.draft / "draft_info.json").write_text(json.dumps(self.info), encoding="utf-8-sig")
        (self.report / "shot_match_report.json").write_text(
            json.dumps({"segments": self.claims}), encoding="utf-8-sig")

    @staticmethod
    def _response(level="DIRECT", overall="PASS"):
        return {"segments": [{"index": index, "match_level": level,
                               "visual_ok": level in {"DIRECT", "ACCEPTABLE_DEGRADED"}}
                              for index in (1, 2)], "overall": overall}

    def _run(self, response=None, *, vision_ok=True, ffmpeg="fixture-ffmpeg", sheet_ok=True):
        def extract(_ffmpeg, _source, _seconds, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fresh fixture frame")
            return True, ""

        def sheet(_items, output, **_kwargs):
            output.write_bytes(b"fixture contact sheet")
            return sheet_ok

        raw = json.dumps(self._response() if response is None else response)
        with mock.patch.object(sys, "argv", [str(qc.__file__), "--draft", str(self.draft),
                                              "--report-dir", str(self.report), "--output", str(self.output)]), \
                mock.patch.object(qc, "_ffmpeg", return_value=ffmpeg), \
                mock.patch.object(qc, "_extract", side_effect=extract), \
                mock.patch.object(vision_analyzer, "_write_sheet", side_effect=sheet), \
                mock.patch.object(vision_analyzer, "_run_vision_with_retry",
                                  return_value=(vision_ok, raw)) as vision, \
                redirect_stdout(StringIO()):
            code = qc.main()
        return code, json.loads(self.output.read_text(encoding="utf-8")), vision

    def test_complete_direct_review_allows_ship_and_reads_bom_json(self):
        code, report, vision = self._run()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["release_eligibility"], "SHIP_ALLOWED")
        self.assertEqual(len(report["model_review"]), 2)
        self.assertEqual(vision.call_args.kwargs["max_retries"], 0)

    def test_degraded_review_is_preview_only(self):
        code, report, _ = self._run(self._response("ACCEPTABLE_DEGRADED", "PASS_WITH_DEGRADED"))
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "PASS_WITH_DEGRADED")
        self.assertEqual(report["release_eligibility"], "PREVIEW_ONLY")

    def test_qc_uses_worker_vision_profile_default_and_override(self):
        for configured, expected in (("", "volc"), ("renderer-profile", "renderer-profile")):
            with self.subTest(profile=configured), \
                    mock.patch.dict(qc.os.environ, {"JY_VISION_PROFILE": configured}):
                _, _, vision = self._run()
            self.assertEqual(vision.call_args.kwargs["profile"], expected)

    def test_upstream_cta_preview_cannot_be_promoted_by_direct_model_review(self):
        self.claims[1]["cta_product_display_fallback"] = True
        self._save()
        _, report, _ = self._run()
        self.assertEqual(report["status"], "PASS_WITH_DEGRADED")
        self.assertEqual(report["release_eligibility"], "PREVIEW_ONLY")

    def test_explicit_model_fail_blocks_even_with_direct_rows(self):
        code, report, _ = self._run(self._response(overall="FAIL"))
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["release_eligibility"], "BLOCKED")

    def test_explicit_model_uncertified_cannot_be_promoted_by_direct_rows(self):
        code, report, _ = self._run(self._response(overall="UNCERTIFIED"))
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "UNCERTIFIED")

    def test_legacy_boolean_array_still_checks_each_segment(self):
        code, report, _ = self._run([{"index": 1, "visual_ok": True},
                                     {"index": 2, "visual_ok": True}])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "PASS")

    def test_intentional_gap_preserves_original_claim_indices_and_preview_only(self):
        self.info["materials"]["videos"].append({
            "id": "gap", "path": self.info["materials"]["videos"][0]["path"],
            "material_name": "visual_missing_placeholder"})
        self.info["tracks"][0]["segments"].insert(1, {
            "material_id": "gap", "source_timerange": {"start": 0, "duration": 2_000_000},
            "target_timerange": {"start": 2_000_000, "duration": 2_000_000}})
        self.info["tracks"][0]["segments"][2]["target_timerange"]["start"] = 4_000_000
        self.claims.insert(1, {"claim_text": "intentional gap", "visual_missing": True})
        self._save()
        code, report, _ = self._run()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "PASS_WITH_GAPS")
        self.assertEqual(report["release_eligibility"], "PREVIEW_ONLY")
        self.assertEqual([row["index"] for row in report["model_review"]], [1, 3])
        batch = json.loads((self.report / "draft_visual_qc_batch_01.json").read_text())
        self.assertEqual(batch["claim_indices"], [1, 3])
        self.assertEqual(batch["frame_labels"], ["S01 mid", "S01 end", "S02 mid", "S02 end"])

    def test_unknown_grade_with_true_boolean_is_uncertified(self):
        response = self._response()
        response["segments"][0]["match_level"] = "UNRECOGNIZED"
        _, report, _ = self._run(response)
        self.assertEqual(report["status"], "UNCERTIFIED")

    def test_conflicting_grade_and_boolean_are_uncertified(self):
        response = self._response()
        response["segments"][0]["visual_ok"] = False
        _, report, _ = self._run(response)
        self.assertEqual(report["status"], "UNCERTIFIED")

    def test_missing_segment_judgment_stays_uncertified(self):
        response = self._response()
        response["segments"].pop()
        code, report, _ = self._run(response)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "UNCERTIFIED")

    def test_unavailable_vision_stays_uncertified(self):
        code, report, _ = self._run(vision_ok=False)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "UNCERTIFIED")

    def test_missing_ffmpeg_never_calls_vision_or_passes(self):
        code, report, vision = self._run(ffmpeg=None)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "UNCERTIFIED")
        self.assertIn("FFMPEG_MISSING", report["issues"])
        vision.assert_not_called()

    def test_failed_contact_sheet_never_passes_or_calls_vision(self):
        code, report, vision = self._run(sheet_ok=False)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "UNCERTIFIED")
        self.assertIn("CONTACT_SHEET_FAILED", report["issues"])
        vision.assert_not_called()

    def test_structure_mismatch_is_a_hard_failure(self):
        self.info["tracks"][0]["segments"].pop()
        self._save()
        code, report, _ = self._run()
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "FAIL")

    def test_repeated_consistent_json_is_accepted_and_conflict_is_rejected(self):
        rows = self._response()["segments"]
        parsed = qc._parse_model(json.dumps(rows) + json.dumps({"segments": rows, "overall": "PASS"}))
        self.assertEqual(len(parsed["segments"]), 2)
        changed = [dict(row) for row in rows]
        changed[0]["match_level"] = "MISMATCH"
        with self.assertRaises(ValueError):
            qc._parse_model(json.dumps(rows) + json.dumps(changed))

    def test_cli_writes_bounded_report_for_missing_draft(self):
        result = subprocess.run([sys.executable, qc.__file__, "--draft", str(self.root / "missing"),
                                 "--report-dir", str(self.report), "--output", str(self.output)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "UNCERTIFIED")
        self.assertTrue(any(issue.startswith("POST_DRAFT_QC_EXCEPTION:") for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
