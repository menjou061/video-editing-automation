# -*- coding: utf-8 -*-
"""参考片校准规则的最小回归集。

这些用例只验证匹配门禁的治理边界，不生成草稿，也不启动远程视觉模型：
相关产品特写可以降级承接功能卖点，但不能伪装成 direct evidence。
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import demo_actions  # noqa: E402
from orchestrator import vision_analyzer  # noqa: E402


def _claim(claim_id: str):
    return demo_actions.CLAIMS_BY_ID[claim_id]


class RelatedProductCloseupTests(unittest.TestCase):
    def test_intent_is_classified_before_role_tiebreak(self):
        intents = demo_actions.infer_intents("整整六大卷，送定制支架，数量很划算")

        self.assertEqual(intents[0], "cta")
        self.assertIn("quantity", intents)
        self.assertEqual(
            demo_actions.intent_role_score({"role": "context"}, intents), 6,
        )

    def test_related_closeup_is_degraded_not_direct(self):
        claim = _claim("C-SOFT")
        shot = {
            "role": "product_display",
            "has_subject": True,
            "description": "纸张面层特写，能看到柔软的压花纹理",
            "visual_tags": ["纸张特写", "面层", "纹理"],
            "evidence_tags": ["面层柔软"],
        }

        self.assertFalse(demo_actions.judge_claim(shot, claim)[0])
        self.assertEqual(
            demo_actions.fallback_match(shot, claim),
            (True, "PRODUCT_SELLING_POINT_CLOSEUP_DEGRADED"),
        )

    def test_feature_specific_closeups_cover_dry_and_absorption(self):
        cases = (
            ("C-DRY-SURFACE", "纸面特写，展示表层干爽结构"),
            ("C-ABSORB-SPEED", "纸张材质特写，展示吸水和瞬吸结构"),
        )
        for claim_id, description in cases:
            with self.subTest(claim_id=claim_id):
                claim = _claim(claim_id)
                shot = {"role": "product_display", "has_subject": True,
                        "description": description, "visual_tags": ["产品特写"]}
                self.assertTrue(demo_actions.fallback_match(shot, claim)[0])

    def test_result_closeup_can_degrade_without_passing_action_gate(self):
        cases = (
            (
                "C-DRY-SURFACE",
                "吸收完成后的巾体表面呈白净干爽状态",
                "凝胶完全吸收，表面干爽白净",
            ),
            (
                "C-ABSORB-SPEED",
                "巾体表面液面缩小并逐渐变白",
                "蓝色凝胶逐渐被吸收，液面缩小",
            ),
        )
        for claim_id, description, result in cases:
            with self.subTest(claim_id=claim_id):
                claim = _claim(claim_id)
                shot = {
                    "role": "direct_evidence", "has_subject": True,
                    "evidence_strength": "A", "action": "吸收",
                    "result": result, "description": description,
                    "evidence_tags": [result],
                }
                self.assertFalse(demo_actions.judge_claim(shot, claim)[0])
                self.assertEqual(
                    demo_actions.fallback_match(shot, claim),
                    (True, "PRODUCT_RESULT_CLOSEUP_DEGRADED"),
                )

    def test_generic_direct_evidence_closeup_is_not_result_fallback(self):
        claim = _claim("C-DRY-SURFACE")
        shot = {
            "role": "direct_evidence", "has_subject": True,
            "evidence_strength": "A", "action": "展示",
            "result": "白色巾体平铺展示", "description": "巾体产品特写",
            "evidence_tags": ["巾体完整入镜"],
        }
        self.assertFalse(demo_actions.fallback_match(shot, claim)[0])

    def test_capacity_action_can_degrade_without_claim_proof(self):
        claim = _claim("C-ABSORB-CAPACITY")
        shot = {
            "role": "direct_evidence", "has_subject": True,
            "action": "倾倒", "result": "蓝色液体在巾体表面形成液面",
            "description": "手将液体倒在巾体上，观察承接过程",
            "visual_description": "手将液体倒在巾体上，观察承接过程",
            "evidence_tags": ["倒液", "液面"],
        }
        self.assertFalse(demo_actions.judge_claim(shot, claim)[0])
        self.assertEqual(
            demo_actions.fallback_match(shot, claim),
            (True, "PRODUCT_ACTION_DEGRADED"),
        )

    def test_low_confidence_cannot_pass_as_direct_evidence(self):
        claim = _claim("C-ABSORB-SPEED")
        shot = {
            "role": "direct_evidence", "has_subject": True,
            "evidence_strength": "A", "analysis_confidence": 0.25,
            "action": "倾倒", "object": "液体+巾体",
            "result": "液体被吸入", "description": "倒水后液体被吸入巾体",
        }

        self.assertEqual(
            demo_actions.judge_claim(shot, claim),
            (False, "EVIDENCE_CONFIDENCE_LOW"),
        )

    def test_generic_package_is_not_related_closeup(self):
        claim = _claim("C-SOFT")
        shot = {
            "role": "product_display",
            "has_subject": True,
            "description": "产品包装正面展示，镜头停留在外包装",
            "visual_tags": ["包装", "正面展示"],
            "evidence_tags": ["完整展示包装"],
        }

        self.assertEqual(demo_actions.fallback_match(shot, claim)[0], False)
        self.assertFalse(demo_actions.is_related_product_closeup(shot, claim))

    def test_related_closeup_prevents_false_material_gap(self):
        claim = _claim("C-SOFT")
        shot = {
            "role": "product_display",
            "has_subject": True,
            "description": "产品面层近景，展示纸张纹理和亲肤触感",
            "visual_tags": ["材质特写", "纸面", "纹理"],
        }

        self.assertEqual(demo_actions.unmet_claims([shot], [claim]), [])

    def test_unrelated_package_still_reports_material_gap(self):
        claim = _claim("C-SOFT")
        shot = {
            "role": "product_display",
            "has_subject": True,
            "description": "多包产品整齐陈列，展示包装数量",
            "visual_tags": ["多包", "包装", "陈列"],
        }

        gaps = demo_actions.unmet_claims([shot], [claim])
        self.assertEqual([item.claim_id for item in gaps], ["C-SOFT"])

    def test_empty_and_metaphor_never_become_closeup_fallback(self):
        claim = _claim("C-SOFT")
        for role, has_subject in (("product_display", False), ("visual_metaphor", True)):
            shot = {
                "role": role,
                "has_subject": has_subject,
                "description": "柔软材质特写，展示纸张纹理",
                "visual_tags": ["材质特写", "纹理"],
            }
            self.assertFalse(demo_actions.fallback_match(shot, claim)[0])

    def test_visual_understanding_cache_skips_second_model_call(self):
        with tempfile.TemporaryDirectory(prefix="visual_cache_") as raw_root:
            root = Path(raw_root)
            source = root / "paper.mp4"
            source.write_bytes(b"stable-source")
            report_dir = root / "task" / "report"
            manifest = {"product_id": "paper-demo"}
            shot = {
                "shot_id": "S1", "video": str(source), "source_start": 0.0,
                "source_end": 2.0, "frame_exists": True,
                "status": "pending_analysis", "description": "",
            }
            key = vision_analyzer._vision_cache_key(manifest, shot, "flash")
            cache_path = vision_analyzer._vision_cache_path(report_dir)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({key: {
                "status": "ready_for_matching", "action_complete": True,
                "description": "缓存的纸面特写", "visual_description": "缓存的纸面特写",
                "visual_tags": ["纸面"], "evidence_tags": ["材质特写"],
                "analysis_confidence": 0.92,
            }}, ensure_ascii=False), encoding="utf-8")

            with mock.patch.object(vision_analyzer, "_run_vision_with_retry",
                                  side_effect=AssertionError("cache miss unexpectedly")):
                result = vision_analyzer.enrich_with_vision(
                    manifest, {"shots": [shot]}, report_dir, profile="flash")

            self.assertEqual(result["vision_analysis"]["cache_hits"], 1)
            self.assertEqual(result["vision_analysis"]["cache_misses"], 0)
            self.assertEqual(result["shots"][0]["description"], "缓存的纸面特写")


if __name__ == "__main__":
    unittest.main()
