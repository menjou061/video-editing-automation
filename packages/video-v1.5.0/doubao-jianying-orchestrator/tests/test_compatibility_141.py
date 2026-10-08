"""Regressions for evidence-backed v1.4.1 behavior retained in v1.5.0."""
from __future__ import annotations

import sys
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import shot_analyzer, timing_contract  # noqa: E402
from orchestrator import demo_actions, semantic_gate, voice_catalog  # noqa: E402
from orchestrator.engine import Clip, OrchestrationEngine  # noqa: E402


class ShortClipCompatibilityTests(unittest.TestCase):
    def _shot(self, start: float = 1.0, end: float = 2.0) -> dict:
        return {
            "status": "ready_for_matching",
            "source_start": 0.0,
            "source_end": 4.0,
            "material_duration_us": 4_000_000,
            "fps": 30,
            "frame_time": (start + end) / 2,
            "evidence_intervals": [{"start": start, "end": end}],
            "role": "direct_evidence",
        }

    def test_short_analyzed_clip_uses_one_frame_guards_when_evidence_is_safe(self):
        solution = shot_analyzer.candidate_window(
            self._shot(), audio_duration_us=3_700_000,
            manifest={"fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2,
                      "tail_pad_s": 0.12},
            time_sensitive=True,
        )
        self.assertTrue(solution.ok, solution.reason)
        self.assertEqual(solution.detail["guard_policy"], "short_clip_frame_guard")

    def test_short_clip_does_not_relax_when_evidence_touches_head_edge(self):
        solution = shot_analyzer.candidate_window(
            self._shot(start=0.01, end=0.4), audio_duration_us=3_700_000,
            manifest={"fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2,
                      "tail_pad_s": 0.12},
            time_sensitive=True,
        )
        self.assertFalse(solution.ok)
        self.assertEqual(solution.reason, timing_contract.REASON_EVIDENCE_OUT_OF_RANGE)

    def test_draft_writer_uses_measured_per_clip_head_and_tail_waste(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {"fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2}
        clip = Clip(
            video="sample.mp4", source_start=0.0, source_end=4.0,
            duration=3.7, video_duration=4.0, visual_role="direct_evidence",
            time_sensitive=True, head_waste=0.05, tail_waste=0.07,
            evidence_intervals=[{"start": 1.0, "end": 2.0}],
        )
        request = engine._timing_request(clip, 3_700_000)
        self.assertEqual(request.head_guard_us, 50_000)
        self.assertEqual(request.tail_guard_us, 70_000)

    def test_short_clip_guard_from_planner_reaches_writer_unchanged(self):
        shot = self._shot()
        manifest = {"fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2,
                    "tail_pad_s": 0.12}
        solution = shot_analyzer.candidate_window(
            shot, audio_duration_us=3_700_000, manifest=manifest, time_sensitive=True)
        self.assertTrue(solution.ok, solution.reason)
        head_s, tail_s = shot_analyzer.guard_seconds_for_solution(shot, manifest, solution)
        self.assertEqual(head_s, solution.detail["head_guard_us"] / timing_contract.US)
        self.assertEqual(tail_s, solution.detail["tail_guard_us"] / timing_contract.US)
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = manifest
        clip = Clip(video="sample.mp4", source_start=0.0, source_end=4.0,
                    duration=3.7, video_duration=4.0, visual_role="direct_evidence",
                    time_sensitive=True, head_waste=head_s, tail_waste=tail_s,
                    evidence_intervals=[{"start": 1.0, "end": 2.0}])
        writer_request = engine._timing_request(clip, 3_700_000)
        self.assertEqual(writer_request.head_guard_us, solution.detail["head_guard_us"])
        self.assertEqual(writer_request.tail_guard_us, solution.detail["tail_guard_us"])
        self.assertTrue(timing_contract.solve_window(writer_request).ok)

    def test_measured_waste_is_shared_by_single_span_and_writer(self):
        shot = {**self._shot(), "head_waste": 0.6, "tail_waste": 0.1}
        manifest = {"fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2}
        solution = shot_analyzer.candidate_window(
            shot, audio_duration_us=2_500_000, manifest=manifest, time_sensitive=True)
        self.assertTrue(solution.ok, solution.reason)
        span = shot_analyzer.span_segment_for(shot, manifest=manifest, time_sensitive=True)
        self.assertEqual(solution.detail["head_guard_us"], 600_000)
        self.assertEqual(solution.detail["tail_guard_us"], 100_000)
        self.assertEqual(span.head_guard_us, 600_000)
        self.assertEqual(span.tail_guard_us, 100_000)

    def test_short_retry_does_not_relax_measured_waste(self):
        shot = {**self._shot(), "head_waste": 0.6, "tail_waste": 0.2}
        solution = shot_analyzer.candidate_window(
            shot, audio_duration_us=3_700_000,
            manifest={"fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2},
            time_sensitive=True)
        self.assertFalse(solution.ok)

    def test_matching_output_survives_real_engine_clip_construction(self):
        shot = {**self._shot(), "shot_id": "display", "video": "sample.mp4",
                "duration": 4.0, "role": "product_display", "has_subject": True,
                "description": "产品包装完整展示", "visual_tags": ["product_display"],
                "evidence_tags": [], "evidence_strength": "A", "action_complete": True,
                "result": "包装完整", "setup_id": "s1", "action_phase": "static"}
        manifest = {"segments": [{"text": "产品包装完整", "audio_duration_us": 3_700_000}],
                    "fps": 30, "head_trim_s": 0.6, "tail_trim_s": 0.5}
        matched = shot_analyzer.match_manifest(manifest, {"shots": [shot], "semantic_matches": []})
        self.assertTrue(matched["ok"], matched["pending_items"])
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m, engine.pending = matched["manifest"], []
        engine.resolve = lambda value: Path(value)
        with mock.patch.object(engine, "_autofill_script_voiceover"):
            clip = engine.build_clips()[0]
        clip.video_duration = 4.0
        request = engine._timing_request(clip, 3_700_000)
        detail = matched["segments"][0]["window_solution"]["detail"]
        self.assertEqual(request.head_guard_us, detail["head_guard_us"])
        self.assertEqual(request.tail_guard_us, detail["tail_guard_us"])
        self.assertTrue(timing_contract.solve_window(request).ok)


class RulepackAndVoiceCompatibilityTests(unittest.TestCase):
    def _rulepack_file(self, directory: str) -> Path:
        source = Path(__file__).resolve().parents[2] / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        target = Path(directory) / "policy.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return target

    def test_loaded_policy_does_not_certify_metadata_without_claim_decisions(self):
        shot = {"role": "direct_evidence", "has_subject": True,
                "action": "按压", "setup_id": "s1", "action_phase": "static"}
        with tempfile.TemporaryDirectory() as directory:
            policy_path = self._rulepack_file(directory)
            with mock.patch.dict(os.environ, {"JY_CLOSED_LOOP_POLICY": str(policy_path)}):
                coverage = demo_actions.gate_coverage([shot])
        self.assertEqual(coverage["coverage_status"], "CLAIM_GATE_EVIDENCE_MISSING")
        self.assertEqual(coverage["certification"], "UNCERTIFIED")

    def test_loaded_policy_certifies_only_successful_gated_segments(self):
        shot = {"role": "direct_evidence", "has_subject": True,
                "action": "按压", "setup_id": "s1", "action_phase": "static"}
        segments = [{"claim_gate": {"status": "ok", "ok": True},
                     "required_claims": [{"claim_id": "C-SOFT"}]}]
        with tempfile.TemporaryDirectory() as directory:
            policy_path = self._rulepack_file(directory)
            with mock.patch.dict(os.environ, {"JY_CLOSED_LOOP_POLICY": str(policy_path)}):
                passing = demo_actions.gate_coverage([shot], segments)
                ungated = demo_actions.gate_coverage(
                    [shot], [{"claim_gate": {"status": "ungated", "ok": True}}])
        self.assertEqual(passing["certification"], "CERTIFIED")
        self.assertEqual(ungated["certification"], "UNCERTIFIED")

    def test_stale_policy_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = self._rulepack_file(directory)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["video_tool_version"] = "1.4.1"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.dict(os.environ, {"JY_CLOSED_LOOP_POLICY": str(policy_path)}):
                loaded, error = demo_actions._load_closed_loop_policy()
        self.assertIsNone(loaded)
        self.assertEqual(error, "RULEPACK_VERSION_MISMATCH")

    def test_historical_mihou_voice_and_speed_are_preserved_without_catalog_files(self):
        from orchestrator import voice_library
        voice_library.load_library.cache_clear()
        with mock.patch.object(voice_library, "_catalog_path", return_value=None), \
                mock.patch.object(voice_library, "_csv_path", return_value=None):
            voice = voice_catalog.resolve_voice("猴哥1.6倍速")
        self.assertEqual(voice.sami, "zh_male_sunwukong_clone2")
        self.assertEqual(voice.speed, 1.6)
        voice_library.load_library.cache_clear()

    def test_softness_aliases_remain_supported(self):
        self.assertTrue(semantic_gate._matches("柔软", "白色面层压纹特写"))


if __name__ == "__main__":
    unittest.main()
