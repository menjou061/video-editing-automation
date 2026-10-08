import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "doubao-jianying-orchestrator"
sys.path.insert(0, str(ORCH))

from orchestrator import demo_actions, shot_analyzer  # noqa: E402


class VisualGovernanceRulesTest(unittest.TestCase):
    def test_packaging_claim_requires_readable_pointing(self):
        pointed = {
            "role": "product_display",
            "has_subject": True,
            "description": "手指明确指向包装上的100%原生木浆标识",
            "visual_description": "手指指向包装文字",
            "readable_claims": ["100%原生木浆"],
            "pointing_to_text": True,
            "pointing_action": "手指指向文字",
        }
        self.assertTrue(
            demo_actions.packaging_claim_direct(pointed, "百分百原生木浆")["ok"]
        )

        ordinary = dict(pointed, pointing_to_text=False, pointing_action="拿起包装")
        self.assertFalse(
            demo_actions.packaging_claim_direct(ordinary, "百分百原生木浆")["ok"]
        )

    def test_packaging_claim_cannot_bypass_functional_claim(self):
        shot = {
            "role": "product_display",
            "has_subject": True,
            "description": "手指指向包装上的100%原生木浆",
            "readable_claims": ["100%原生木浆"],
            "pointing_to_text": True,
            "pointing_action": "指向文字",
        }
        result = demo_actions.packaging_claim_direct(shot, "百分百原生木浆而且柔软")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "FUNCTIONAL_CLAIM_REQUIRES_ACTION_RESULT")

    def test_cta_rejects_single_pack_and_accepts_visible_quantity(self):
        single = {
            "role": "product_display",
            "has_subject": True,
            "description": "手持一包产品正面展示",
            "visual_tags": ["单包", "包装特写"],
        }
        ok, reason = demo_actions.cta_quantity_eligible(single, "便宜又大碗值得囤")
        self.assertFalse(ok)
        self.assertEqual(reason, "SINGLE_PACK_OR_NO_VISIBLE_QUANTITY")

        multi = dict(single, description="三包产品成排展示", visual_tags=["三包", "成排"])
        ok, reason = demo_actions.cta_quantity_eligible(multi, "便宜又大碗值得囤")
        self.assertTrue(ok)
        self.assertEqual(reason, "VISIBLE_MULTI_PACK_OR_QUANTITY")

    def test_policy_records_24_as_cap_not_fixed_count(self):
        policy = json.loads(
            (ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json").read_text(
                encoding="utf-8"
            )
        )
        matching = policy["matching"]
        self.assertEqual(matching["max_final_materials"], 24)
        self.assertTrue(matching["max_is_not_fixed_count"])
        self.assertTrue(matching["degraded_never_steals_direct"])

    def test_variant_rotation_only_applies_within_near_tied_same_tier(self):
        rows = [
            {"shot_id": "a", "source_video": "a.mp4", "score": 100,
             "shot": {"setup_id": "one", "action_phase": "perform", "role": "direct_evidence"}},
            {"shot_id": "b", "source_video": "b.mp4", "score": 96,
             "shot": {"setup_id": "two", "action_phase": "result", "role": "direct_evidence"}},
            {"shot_id": "c", "source_video": "c.mp4", "score": 99,
             "shot": {"setup_id": "three", "action_phase": "result", "role": "product_display"}},
        ]

        def priority(row):
            same_evidence_tier = row["shot"]["role"] == "direct_evidence"
            return (1, int(same_evidence_tier), row["score"])

        variant = shot_analyzer._variant_tiebreak(
            rows, priority, variant_index=1, score_tolerance=8
        )
        self.assertEqual(variant["shot_id"], "b")

        distant = shot_analyzer._variant_tiebreak(
            rows[:2], priority, variant_index=1, score_tolerance=2
        )
        self.assertEqual(distant["shot_id"], "a")


if __name__ == "__main__":
    unittest.main()
