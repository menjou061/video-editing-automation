# -*- coding: utf-8 -*-
"""1.3.27 A 步回归：timing_contract 求解器 + 不变量防线 + 7A 认证字段。

跑法（在本 skill 根目录）：
    python3 -m unittest discover -s tests -v
    python3 tests/test_timing_contract.py

用例 ① 是 A 步的验收线（用户 2026-09-16 定案第 9 条）：
    2026-09-16 的 recvvfR4QKW8Ky 里，段 2「心相印也太会了…」选中了全库最短的
    波点挂抽_IMG_7950.MOV（3.945s），配音 4.16s，到 write_draft 阶段才炸
    AUDIO_VIDEO_MISMATCH，一个草稿都出不来。现在要求：**在规划阶段**就得到
    合法解，或者明确报 SOURCE_TOO_SHORT —— 不允许拖到最后才炸。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import demo_actions  # noqa: E402
from orchestrator import timing_contract as tc  # noqa: E402
from orchestrator import shot_analyzer  # noqa: E402

US = tc.US


def _req(**kwargs):
    base = dict(source_in_us=0, source_out_us=int(10 * US),
                audio_duration_us=int(4 * US))
    base.update(kwargs)
    return tc.WindowRequest(**base)


def _fake_pymediainfo(video_ms=None, have_video=True):
    """替身 pymediainfo 模块：MediaInfo.parse 返回按需构造的 tracks。"""
    import types

    class _MediaInfo:
        video_tracks = [] if not have_video else [
            types.SimpleNamespace(duration=video_ms)]

    module = types.ModuleType("pymediainfo")

    class MediaInfo:
        @staticmethod
        def parse(path, **kwargs):
            return _MediaInfo()

    module.MediaInfo = MediaInfo
    return module


class SolveWindowTests(unittest.TestCase):
    """① ② ④ ⑦：可行解 / 装不下 / 不许变速 / 不许用变速伪造证据。"""

    # ---- ① A 步验收线：IMG_7950 在规划阶段就有定论 ----
    def test_img7950_is_decided_at_planning_time(self):
        """3.945s 素材 + 4.16s 配音 + product_display。

        用户定案第 9 条：规划阶段**必须**给出「合法解」或「明确 SOURCE_TOO_SHORT」，
        不允许拖到 write_draft 才炸 AUDIO_VIDEO_MISMATCH；不要求强行保住 IMG_7950。

        实测口径：含 0.3/0.2 首尾保护区后可用 3.445s，product_display 最慢 0.85 倍
        仍需 3.638s —— 这个镜头**真的**装不下，所以正确答案就是 SOURCE_TOO_SHORT，
        由规划层换镜。旧实现在这里会返回「截断到 3.745s 可控」的假解，一路带到
        write_draft 才暴露。
        """
        req = tc.WindowRequest(
            source_in_us=0, source_out_us=3_945_000,
            audio_duration_us=4_160_000,
            **dict(zip(("speed_min", "speed_max"),
                       tc.speed_range_for_role("product_display"))),
        )
        sol = tc.solve_window(req)
        self.assertFalse(sol.ok, "按 product_display 的速度范围，此镜头确实装不下")
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)
        # 关键：失败是「结构化 + 有数」的，规划层据此换镜，而不是截断
        self.assertLess(sol.detail["usable_us"], sol.detail["required_us"])
        self.assertEqual(sol.segment_us, 0)          # 绝不给短一点的假解

    def test_img7950_old_formula_is_what_broke(self):
        """旧口径（usable − 0.2 常数）算出 3.745s「装得下」，正是它把矛盾拖到最后。"""
        old_avail = 3_945_000 - 200_000 - 0          # 旧实现：video_duration − tail_trim
        self.assertGreater(old_avail, 3_445_000)     # 旧口径比真实可用量还乐观
        self.assertLess(old_avail, 4_160_000)        # 真到对齐时又不够 → 整支炸掉

    def test_same_audio_finds_solution_on_longer_source(self):
        """同一句 4.16s 配音，换到够长的素材上，规划阶段就能拿到解（不被迫变速）。"""
        sol = tc.solve_window(tc.WindowRequest(
            source_in_us=0, source_out_us=8_000_000, audio_duration_us=4_160_000,
            **dict(zip(("speed_min", "speed_max"),
                       tc.speed_range_for_role("product_display"))),
        ))
        self.assertTrue(sol.ok)
        self.assertEqual(sol.speed, 1.0)
        self.assertEqual(sol.segment_us, 4_280_000)

    def test_img7950_solvable_when_wider_speed_allowed(self):
        """若该段画面角色允许放宽到 0.80 倍，同一镜头立刻有解（求解器不是死的）。"""
        sol = tc.solve_window(tc.WindowRequest(
            source_in_us=0, source_out_us=3_945_000, audio_duration_us=4_160_000,
            speed_min=0.80, speed_max=1.25,
        ))
        self.assertTrue(sol.ok)
        self.assertAlmostEqual(sol.speed, 0.8049, places=3)
        self.assertLessEqual(sol.source_end_us, 3_945_000 - 200_000)   # 不越尾保护区
        self.assertEqual(sol.segment_us, 4_280_000)

    def test_exact_fit_needs_no_speed_change(self):
        """装得下就不许动速度：|v−1| 最小者优先。"""
        sol = tc.solve_window(_req(source_out_us=int(10 * US),
                                   audio_duration_us=int(4 * US),
                                   speed_min=0.5, speed_max=2.0))
        self.assertTrue(sol.ok)
        self.assertEqual(sol.speed, 1.0)
        self.assertEqual(sol.material_us, sol.segment_us)

    # ---- ② 装不下 → 结构化失败，不是截断 ----
    def test_source_too_short_is_explicit(self):
        sol = tc.solve_window(_req(source_out_us=int(2 * US),
                                   audio_duration_us=4_160_000,
                                   speed_min=0.8, speed_max=1.25))
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)
        self.assertLess(sol.detail["usable_us"], sol.detail["required_us"])

    def test_short_source_never_returns_truncated_solution(self):
        """失败时不得返回「短一点的解」—— 那正是旧的静默截断行为。"""
        sol = tc.solve_window(_req(source_out_us=500_000, audio_duration_us=4_160_000))
        self.assertFalse(sol.ok)
        self.assertEqual(sol.segment_us, 0)
        self.assertEqual(sol.material_us, 0)

    def test_zero_audio_is_reported(self):
        sol = tc.solve_window(_req(audio_duration_us=0, tail_pad_us=0))
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_NO_AUDIO)

    # ---- ④ 速度锁死 + 素材偏短 → SOURCE_TOO_SHORT，不得偷偷变速 ----
    def test_locked_speed_does_not_silently_stretch(self):
        sol = tc.solve_window(_req(source_out_us=int(3 * US),
                                   audio_duration_us=int(4 * US),
                                   speed_min=1.0, speed_max=1.0))
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)
        self.assertEqual(sol.speed, 1.0)      # 失败解也必须如实报速度

    # ---- ⑦ direct_evidence 锁 1.0：不得用变速伪造证据 ----
    def test_direct_evidence_speed_is_locked(self):
        self.assertEqual(tc.speed_range_for_role("direct_evidence"), (1.0, 1.0))
        self.assertEqual(tc.speed_range_for_role("usage_demo", time_sensitive=True),
                         (1.0, 1.0))       # 时间相关证据一律锁 1.0
        self.assertEqual(tc.speed_range_for_role(""), (1.0, 1.0))   # 未知角色宁可不做

        lo, hi = tc.speed_range_for_role("direct_evidence")
        sol = tc.solve_window(tc.WindowRequest(
            source_in_us=0, source_out_us=3_945_000, audio_duration_us=4_160_000,
            speed_min=lo, speed_max=hi,
        ))
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)

    # ---- ⑤ 全 int µs，无浮点累积 ----
    def test_all_integer_microseconds(self):
        sol = tc.solve_window(_req(audio_duration_us=4_161_337, speed_min=0.9,
                                   speed_max=1.1))
        self.assertTrue(sol.ok)
        for value in (sol.source_start_us, sol.source_end_us, sol.segment_us,
                      sol.material_us, sol.handle_room_us):
            self.assertIsInstance(value, int)
        self.assertEqual(sol.segment_us, 4_161_337 + tc.DEFAULT_TAIL_PAD_US)

    def test_frame_snapped_boundary_stays_inside_material(self):
        """写入层吸附变速后，边界窗口不能多出几十微秒。

        现场复现：4.285s 素材、4.086667s 配音、0.12s 尾留白。原解刚好
        铺满可用素材，但 engine 的 frame_speed() 向上吸附后多取 58µs，
        最终在 add_media_safe() 才炸掉。规划层必须返回同一套吸附后的合法解。
        """
        req = tc.WindowRequest(
            source_in_us=0, source_out_us=4_285_000,
            audio_duration_us=4_086_667, tail_pad_us=120_000,
            head_guard_us=300_000, tail_guard_us=0,
            speed_min=0.85, speed_max=1.20,
        )
        sol = tc.solve_window(req)
        self.assertTrue(sol.ok)
        snapped = tc.frame_speed(sol.speed, req.speed_min, req.speed_max,
                                 fps=req.fps)
        usable = req.hi_bound_us - req.lo_bound_us
        material = int(round(snapped * sol.segment_us))
        self.assertLessEqual(material, usable)
        self.assertEqual(sol.source_end_us - sol.source_start_us, material)

    def test_frame_us_matches_draft_fps(self):
        self.assertEqual(tc.frame_us(30), 33_333)
        self.assertEqual(tc.frame_us(0), 33_333)      # 非法 fps 退回 30
        self.assertEqual(tc.frame_us("bogus"), 33_333)


class WindowConstraintTests(unittest.TestCase):
    """③：证据覆盖 / 保护区 / 转场把手 / tail_pad 归属。"""

    def test_evidence_out_of_range_is_reported(self):
        """③ 证据落在 head_guard 里 → EVIDENCE_OUT_OF_RANGE（不是硬夹紧）。"""
        req = _req(evidence_start_us=100_000, evidence_end_us=400_000,
                   head_guard_us=300_000, speed_min=0.8, speed_max=1.25)
        self.assertLess(req.evidence_start_us, req.lo_bound_us)
        sol = tc.solve_window(req)
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_EVIDENCE_OUT_OF_RANGE)

    def test_evidence_beyond_source_is_reported(self):
        req = _req(source_out_us=int(5 * US), evidence_start_us=int(5 * US) - 100_000,
                   evidence_end_us=int(5 * US) + 200_000)
        sol = tc.solve_window(req)
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_EVIDENCE_OUT_OF_RANGE)

    def test_solution_covers_evidence(self):
        ev_s, ev_e = int(2.0 * US), int(2.4 * US)
        sol = tc.solve_window(_req(evidence_start_us=ev_s, evidence_end_us=ev_e,
                                   speed_min=0.9, speed_max=1.1))
        self.assertTrue(sol.ok)
        self.assertLessEqual(sol.source_start_us, ev_s + tc.frame_us())
        self.assertGreaterEqual(sol.source_end_us, ev_e - tc.frame_us())

    def test_evidence_span_too_long(self):
        """证据跨度本身就超过最大可用切片 → EVIDENCE_SPAN_TOO_LONG。"""
        sol = tc.solve_window(_req(
            source_out_us=int(10 * US), audio_duration_us=int(2 * US),
            evidence_start_us=int(4 * US), evidence_end_us=int(9 * US),
            speed_min=1.0, speed_max=1.05,
        ))
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_EVIDENCE_SPAN_TOO_LONG)

    def test_evidence_conflict_when_in_range_but_unplaceable(self):
        sol = tc.solve_window(_req(
            source_out_us=int(4 * US), audio_duration_us=int(6 * US),
            evidence_start_us=int(2 * US), evidence_end_us=int(3 * US),
            speed_min=0.8, speed_max=1.25,
        ))
        self.assertFalse(sol.ok)
        self.assertIn(sol.reason, (tc.REASON_EVIDENCE_SPEED_CONFLICT,
                                  tc.REASON_SOURCE_TOO_SHORT))

    def test_tail_pad_lives_on_output_timeline_only(self):
        """修订 5：tail_pad 只加在输出侧，素材侧不再被多扣一次。"""
        req = _req(source_out_us=int(10 * US), audio_duration_us=int(4 * US),
                   tail_pad_us=tc.DEFAULT_TAIL_PAD_US, speed_min=0.9, speed_max=1.1)
        self.assertEqual(req.timeline_us, 4_120_000)
        self.assertEqual(req.usable_us, 10 * US - 300_000 - 200_000)   # 不含 tail_pad
        sol = tc.solve_window(req)
        self.assertEqual(sol.segment_us, 4_120_000)

    def test_handles_split_in_and_out(self):
        """修订 5：前/后把手分别从素材侧扣，且不互相顶替。"""
        base = _req(source_out_us=int(10 * US), audio_duration_us=int(4 * US),
                    speed_min=0.9, speed_max=1.1)
        with_in = tc.WindowRequest(**{**base.__dict__,
                                      "transition_in_handle_us": int(0.5 * US)})
        with_out = tc.WindowRequest(**{**base.__dict__,
                                       "transition_out_handle_us": int(0.5 * US)})
        self.assertEqual(with_in.usable_us, base.usable_us - int(0.5 * US))
        self.assertEqual(with_out.usable_us, base.usable_us - int(0.5 * US))
        self.assertEqual(with_in.lo_bound_us, base.lo_bound_us + int(0.5 * US))
        self.assertEqual(with_out.hi_bound_us, base.hi_bound_us - int(0.5 * US))

    def test_more_usable_material_keeps_handle_room(self):
        """余量如实报出，供转场重叠使用。"""
        sol = tc.solve_window(_req(source_out_us=int(10 * US),
                                   audio_duration_us=int(4 * US),
                                   speed_min=0.9, speed_max=1.1))
        self.assertTrue(sol.ok)
        self.assertGreater(sol.handle_room_us, 0)

    def test_preferred_start_is_respected(self):
        sol = tc.solve_window(_req(source_out_us=int(10 * US),
                                   audio_duration_us=int(4 * US),
                                   preferred_start_us=int(3 * US),
                                   speed_min=1.0, speed_max=1.0))
        self.assertTrue(sol.ok)
        self.assertEqual(sol.source_start_us, int(3 * US))

    def test_selected_speed_is_closest_to_one(self):
        """修订 4：可行集里挑最接近 1.0 的解，而不是单一公式。"""
        sol = tc.solve_window(_req(source_out_us=int(10 * US),
                                   audio_duration_us=int(4 * US),
                                   speed_min=0.5, speed_max=2.0))
        self.assertEqual(sol.speed, 1.0)


class InvariantTests(unittest.TestCase):
    """⑤：不变量防线（结构化返回，不用 assert）。"""

    def _item(self, **kwargs):
        # 基准夹具必须**几何自洽**：speed=1.0 时源窗口 == 段长（1.3.27.1 起速度
        # 换算
        # segment ≈ (source_end − source_start) / speed 是硬校验，自相矛盾的
        # 夹具会撞上 DURATION_MISMATCH 而失焦）。
        base = dict(index=0, video="a.mov", audio_mode="tts",
                    source_start_us=0, source_end_us=4_120_000,
                    segment_us=4_120_000, audio_duration_us=4_000_000,
                    tail_pad_us=tc.DEFAULT_TAIL_PAD_US)
        base.update(kwargs)
        return base

    def test_clean_item_has_no_issues(self):
        self.assertEqual(tc.check_invariants([self._item()]), [])

    def test_duration_mismatch_is_caught(self):
        """⑤ 人为把段长改成 0.2s → DURATION_MISMATCH（换算与音频两路都会报）。"""
        issues = tc.check_invariants([self._item(segment_us=200_000)])
        self.assertEqual({i["code"] for i in issues}, {tc.INV_DURATION_MISMATCH})
        self.assertGreater(abs(issues[0]["delta_us"]), tc.frame_us())
        self.assertEqual(issues[0]["video"], "a.mov")

    def test_one_frame_tolerance_is_allowed(self):
        off = tc.frame_us()          # 恰好一帧 → 放行
        self.assertEqual(
            tc.check_invariants([self._item(segment_us=4_120_000 + off)]), [])

    def test_native_mode_is_exempt_from_duration_equality(self):
        """native 段由现场原声锚定：窗口==段长，音频等式豁免（ audio+tail ≠ 段长）。"""
        issues = tc.check_invariants([self._item(audio_mode="native",
                                                 source_end_us=1_000_000,
                                                 segment_us=1_000_000)])
        self.assertEqual(issues, [])

    def test_window_out_of_bounds_is_caught(self):
        issues = tc.check_invariants([self._item(source_end_us=int(12 * US),
                                                 video_duration_us=int(10 * US))])
        self.assertIn(tc.INV_WINDOW_OUT_OF_BOUNDS, [i["code"] for i in issues])

    def test_negative_start_is_caught(self):
        issues = tc.check_invariants([self._item(source_start_us=-500_000)])
        self.assertIn(tc.INV_WINDOW_OUT_OF_BOUNDS, [i["code"] for i in issues])

    def test_evidence_not_covered_is_caught(self):
        issues = tc.check_invariants([self._item(
            evidence_start_us=int(2 * US), evidence_end_us=int(6 * US))])
        self.assertIn(tc.INV_EVIDENCE_NOT_COVERED, [i["code"] for i in issues])

    def test_speed_out_of_role_range_is_caught(self):
        # 窗口随速度同步缩放（4.12s × 0.7），保证只有 SPEED 一条违规。
        issues = tc.check_invariants([self._item(video_speed=0.7,
                                                 source_end_us=2_884_000,
                                                 speed_range=(0.85, 1.20))])
        self.assertEqual([i["code"] for i in issues], [tc.INV_SPEED_OUT_OF_RANGE])

    def test_aperture_mismatch_after_reread(self):
        """草稿写完后回读 → 目标时长与请求片段不等（旧 min(...) 截断的症状）。"""
        issues = tc.check_invariants([self._item(actual_video_us=3_745_000)])
        self.assertEqual([i["code"] for i in issues], [tc.INV_APERTURE_MISMATCH])

    def test_issues_carry_frame_deltas(self):
        issues = tc.check_invariants([self._item(segment_us=4_120_000 - 1_000_000)])
        deltas = {i["delta_frames"] for i in issues}
        self.assertIn(-30.0, deltas)

    def test_invariant_codes_are_stable(self):
        self.assertEqual(len(tc.INVARIANT_CODES), 5)
        self.assertEqual(len(set(tc.INVARIANT_CODES)), 5)
        self.assertEqual(len(tc.FAIL_REASONS), len(set(tc.FAIL_REASONS)))


class VoiceClipSourceStartGuardTests(unittest.TestCase):
    """voice_tts.generate_batch 的 source_start 守卫（B 步多镜的前置安全垫）。"""

    def test_negative_source_start_rejected_before_tts_call(self):
        from orchestrator import voice_tts
        with self.assertRaises(ValueError):
            voice_tts.generate_batch(
                [{"text": "测试", "source_start": -0.5}], Path("/tmp/nonexistent_tts"))
        with self.assertRaises(ValueError):
            voice_tts.generate_batch(
                [{"text": "测试", "source_start_us": -1}], Path("/tmp/nonexistent_tts"))


class CertGateTests(unittest.TestCase):
    """⑥ 修订 10：7A 临时状态必须用明确字段，不能只靠 fully_gated=None。"""

    def _coverage(self, **shot):
        base = {"role": "direct_evidence", "has_subject": True,
                "action": "过水", "setup_id": "s1", "action_phase": "hold"}
        base.update(shot)
        return demo_actions.gate_coverage([base])

    def test_uncertified_fields_present(self):
        cov = self._coverage()
        self.assertIs(cov["fully_gated"], False)
        self.assertEqual(cov["coverage_status"], "RULEPACK_MISSING")
        self.assertIsNone(cov["validation_passed"])
        self.assertEqual(cov["certification"], "UNCERTIFIED")
        self.assertEqual(cov["uncertified_reasons"], ["RULEPACK_MISSING"])

    def test_fully_gated_is_bool_not_truthy_count(self):
        cov = self._coverage()
        self.assertIsInstance(cov["fully_gated"], bool)
        self.assertNotEqual(cov["fully_gated"], 1)

    def test_counts_still_reported_alongside(self):
        cov = self._coverage()
        for key in ("shots", "with_role", "with_has_subject", "with_action",
                    "with_setup_phase"):
            self.assertIn(key, cov)

    def test_visual_role_accessor(self):
        self.assertEqual(demo_actions.visual_role({"role": "usage_demo"}), "usage_demo")
        self.assertEqual(demo_actions.visual_role({"visual_role": "context"}), "context")
        self.assertEqual(demo_actions.visual_role({}), "")
        self.assertEqual(demo_actions.visual_role(None), "")


class VoiceLockTests(unittest.TestCase):
    """修订 1：音色只决议一次并回写锁定；引擎后续不得再选。"""

    def test_manifest_voice_wins(self):
        from orchestrator import voice_catalog as vc
        m = {"voice": vc.DEFAULT_VOICE_KEY if hasattr(vc, "DEFAULT_VOICE_KEY") else "male_energetic"}
        preset = vc.resolve_voice_once(script="随便一段剧情", manifest=m, persist=True)
        self.assertEqual(preset.key, m["voice"])
        self.assertEqual(m[vc.VOICE_LOCKED_FROM_KEY], "operator")
        self.assertIs(m[vc.VOICE_LOCKED_KEY], True)

    def test_empty_manifest_gets_locked_once(self):
        from orchestrator import voice_catalog as vc
        m = {}
        preset = vc.resolve_voice_once(manifest=m, persist=True)
        self.assertTrue(m.get(vc.VOICE_LOCK_KEY))
        self.assertEqual(m[vc.VOICE_LOCKED_FROM_KEY], "default")
        self.assertIs(m[vc.VOICE_LOCKED_KEY], True)
        # 第二次调用读的是锁，不再重新决议
        again = vc.resolve_voice_once(manifest=m, persist=True)
        self.assertEqual(again.key, preset.key)

    def test_recommendation_is_locked_into_manifest(self):
        from orchestrator import voice_catalog as vc
        m = {}
        preset = vc.resolve_voice_once(script="悬挂式抽纸 HelloKitty 联名 湿水不破",
                                       manifest=m, persist=True)
        self.assertTrue(m.get(vc.VOICE_LOCK_KEY))
        self.assertIn(m[vc.VOICE_LOCKED_FROM_KEY], ("recommended", "default"))
        self.assertEqual(preset.sami, vc.resolve_voice(m[vc.VOICE_LOCK_KEY]).sami)

    def test_operator_label_is_preserved_verbatim(self):
        from orchestrator import voice_catalog as vc
        m = {}
        vc.resolve_voice_once("male_energetic1.6倍速", manifest=m, persist=True)
        self.assertEqual(m[vc.VOICE_LOCK_KEY], "male_energetic1.6倍速")

    def test_unknown_label_raises_instead_of_silently_switching(self):
        from orchestrator import voice_catalog as vc
        with self.assertRaises(vc.VoiceResolutionError):
            vc.resolve_voice_once("根本不存在的音色名", manifest={}, persist=False)

    def test_default_keyword_falls_back_to_default_voice(self):
        from orchestrator import voice_catalog as vc
        m = {}
        preset = vc.resolve_voice_once("默认", manifest=m, persist=True)
        self.assertEqual(m[vc.VOICE_LOCKED_FROM_KEY], "default")
        self.assertTrue(preset.sami)

    def test_no_pitch_field_added(self):
        """修订 2：不新增无效 pitch 字段（后端根本不接受）。"""
        from orchestrator import voice_catalog as vc
        self.assertFalse(hasattr(vc.VoicePreset, "pitch"))
        self.assertIn("speed", vc.VoicePreset.__dataclass_fields__)


class TtsCacheKeyTests(unittest.TestCase):
    """修订 2：缓存键只含实际影响音频字节的参数与后处理版本。"""

    def test_key_shape_and_stability(self):
        from orchestrator import voice_tts
        name = voice_tts._cache_name("文案", "zh_male_huoli")
        self.assertTrue(name.startswith("tts_") and name.endswith(".ogg"))
        self.assertEqual(name, voice_tts._cache_name("文案", "zh_male_huoli"))

    def test_pipeline_version_changes_key(self):
        from orchestrator import voice_tts
        a = voice_tts._cache_name("文案", "zh_male_huoli", pipeline="1")
        b = voice_tts._cache_name("文案", "zh_male_huoli", pipeline="2")
        self.assertNotEqual(a, b)

    def test_provider_changes_key(self):
        from orchestrator import voice_tts
        self.assertNotEqual(voice_tts._cache_name("文案", "s", provider="sami"),
                            voice_tts._cache_name("文案", "s", provider="edge"))

    def test_key_signature_has_no_video_speed(self):
        """video_speed 不属于缓存键（变速只动画面，音频字节不变）。"""
        import inspect
        from orchestrator import voice_tts
        params = set(inspect.signature(voice_tts._cache_name).parameters)
        self.assertEqual(params, {"text", "speaker", "provider", "pipeline"})


class FrameSpeedSnapTests(unittest.TestCase):
    """③ 修订 3：速度吸附到整数微秒档位，且**吸附后仍在角色区间内**。

    这两个断言都是防线自身的防线。吸附是为了让剪映
    ``round(素材 / 速度)`` 的回程误差落在一帧容差内；夹紧是为了让吸附后的值不被
    写入前的不变量校验误判 —— 栅格点天然会落在区间端点外侧一丁点
    （``frame_speed(0.85) = 0.8499985``，比 0.85 小 1.5e-6）。少了夹紧，
    合法草稿会在**最后一道闸**上被自己的防线拦死。
    """

    def test_snapshot_never_escapes_role_range(self):
        for role, (lo, hi) in tc.SPEED_RANGE_BY_ROLE.items():
            for probe in (lo, hi, (lo + hi) / 2, lo - 0.05, hi + 0.05):
                got = tc.frame_speed(probe, lo, hi)
                self.assertGreaterEqual(
                    got, lo - 1e-9, f"{role}: {probe} 吸附成 {got} 掉出下界 {lo}")
                self.assertLessEqual(
                    got, hi + 1e-9, f"{role}: {probe} 吸附成 {got} 冒过上界 {hi}")

    def test_raw_snap_would_have_escaped_without_bounds(self):
        """夹紧不是防御性编程，是实测缺了它会出事 —— 无界吸附确实越界。"""
        self.assertLess(tc.frame_speed(0.85), 0.85)
        self.assertGreater(tc.frame_speed(1.2), 1.2)
        self.assertGreaterEqual(tc.frame_speed(0.85, 0.85, 1.20), 0.85)
        self.assertLessEqual(tc.frame_speed(1.2, 0.85, 1.20), 1.2)

    def test_roundtrip_error_is_within_one_frame(self):
        """吸附后 ``round(round(T·v)/v)`` 与 T 的差 ≤ 1 帧（实测最大 1µs）。

        旧的 docstring 曾声称这里「误差恒为 0」，暴力枚举后推翻：整数微秒档位下
        仍有 4647 个非零点，最大 1µs（即 1/33333 帧）。**不是 0，但远在一帧内。**
        """
        tol = tc.frame_us()
        for role, (lo, hi) in tc.SPEED_RANGE_BY_ROLE.items():
            speed = lo
            while speed <= hi + 1e-12:
                v = tc.frame_speed(speed, lo, hi)
                for total in (1_000_000, 4_161_337, 4_280_000, 12_345_678):
                    material = int(round(total * v))
                    self.assertLessEqual(abs(round(material / v) - total), tol,
                                         f"{role} v={v} T={total}")
                speed += tc.SPEED_GRID_STEP

    def test_speed_is_snapped_to_integer_microseconds(self):
        """档位是「1 帧的 frame_us 分之一」，所以 v 与 1µs 刻度对齐。"""
        step = 1.0 / tc.frame_us()
        for value in (0.85, 0.9237, 1.0, 1.157, 1.2):
            snapped = tc.frame_speed(value)
            self.assertAlmostEqual(snapped / step, round(snapped / step), places=4)

    def test_bad_speed_falls_back_to_one(self):
        for bad in (-1, 0, "x", None, float("nan"), float("inf")):
            self.assertEqual(tc.frame_speed(bad), 1.0)

    def test_degenerate_bounds_do_not_crash(self):
        self.assertEqual(tc.frame_speed(0.9, 1.2, 0.85), tc.frame_speed(0.9, 0.85, 1.2))
        self.assertEqual(tc.frame_speed(0.9, "a", "b"), tc.frame_speed(0.9))


class UsOfTests(unittest.TestCase):
    """修订 3：秒→µs 的默认值只在 timing_contract 里定义，别处不许再写常量。"""

    def test_defaults_and_invalids(self):
        default = tc.DEFAULT_TAIL_PAD_US
        for bad in (None, "", "x", -1):
            self.assertEqual(tc.us_of(bad, default), default)
        self.assertEqual(tc.us_of(0.12, default), 120_000)
        self.assertEqual(tc.us_of(0, default), 0)          # 显式 0 是有效值
        self.assertEqual(tc.us_of("0.3", default), 300_000)

    def test_module_constants_agree_with_engine_defaults(self):
        """engine 从 manifest 取值时用的默认必须就是这里的常量（防两处漂移）。"""
        self.assertEqual(tc.DEFAULT_HEAD_GUARD_US, 300_000)
        self.assertEqual(tc.DEFAULT_TAIL_GUARD_US, 200_000)
        self.assertEqual(tc.DEFAULT_TAIL_PAD_US, 120_000)


class CandidateEvidenceIntervalTests(unittest.TestCase):
    """求解器与写段落处必须用**同一个**证据区间来源（定案第 4 条的隐藏前提）。

    两处口径一旦分叉，求解器会给出「合法但没罩住证据」的窗口，而写入前的不变量
    校验会在草稿组装到最后一步时才炸 —— 正是这次要消灭的失败模式。
    """

    def test_ai_intervals_win_and_take_union_span(self):
        from orchestrator import shot_analyzer as sa
        start, end, frame = sa.candidate_evidence_interval({
            "evidence_intervals": [{"start": 2.0, "end": 2.5},
                                   {"start": 3.0, "end": 3.4}],
            "frame_time": 2.2, "source_start": 0.0, "source_end": 8.0})
        self.assertEqual((start, end), (2.0, 3.4))
        self.assertEqual(frame, 2.2)

    def test_fallback_is_audited_frame_neighborhood_clamped_to_scene(self):
        from orchestrator import shot_analyzer as sa
        start, end, frame = sa.candidate_evidence_interval({
            "frame_time": 0.1, "source_start": 0.0, "source_end": 8.0})
        self.assertEqual(start, 0.0)                       # 夹在场景窗口内
        self.assertEqual(end, round(0.1 + sa.EVIDENCE_HALF_WINDOW, 3))
        self.assertEqual(frame, 0.1)

    def test_fallback_half_window_width_away_from_edges(self):
        from orchestrator import shot_analyzer as sa
        start, end, _ = sa.candidate_evidence_interval({
            "frame_time": 4.0, "source_start": 0.0, "source_end": 8.0})
        self.assertAlmostEqual(end - start, 2 * sa.EVIDENCE_HALF_WINDOW, places=3)


class SpanSolveTests(unittest.TestCase):
    """⑥ 定案第 6 条：一句话跨多镜（合格单镜 → 多镜组合 → 变速/尾帧 → MATERIAL_GAP）。

    组合求解不引入新的时间口径：它只把同一份音频按各段能力拆开，再对每段调用
    同一个 ``solve_window``。所以这里两条断言必须同时成立 ——
    **每段自己的窗口合法**，且**各段输出时长之和恰好等于音频+尾留白**（差 1µs
    都会让写入前的 DURATION_MISMATCH 闸门拦下整支）。
    """

    @staticmethod
    def _seg(out_s, speed_range=(1.0, 1.0), **kw):
        base = dict(source_in_us=0, source_out_us=int(out_s * US),
                    speed_min=speed_range[0], speed_max=speed_range[1])
        base.update(kw)
        return tc.SpanSegment(**base)

    def _check_parts_sum_and_legal(self, req, sol):
        self.assertTrue(sol.ok, f"应当有解，实际 {sol.reason} {sol.detail}")
        self.assertEqual(len(sol.parts), len(req.segments))
        total = sum(p.segment_us for p in sol.parts)
        self.assertEqual(total, req.timeline_us)       # 差 1µs 都不行
        for seg, part in zip(req.segments, sol.parts):
            self.assertTrue(seg.lo_bound_us <= part.source_start_us,
                            f"窗口下界越界：{part.source_start_us} < {seg.lo_bound_us}")
            self.assertTrue(part.source_end_us <= seg.hi_bound_us,
                            f"窗口上界越界：{part.source_end_us} > {seg.hi_bound_us}")
            self.assertTrue(seg.speed_min - 1e-9 <= part.speed <= seg.speed_max + 1e-9,
                            f"速度越出角色范围：{part.speed}")
            if seg.evidence_span_us is not None:
                self.assertTrue(part.source_start_us <= seg.evidence_start_us
                                and part.source_end_us >= seg.evidence_end_us,
                                "窗口没罩住证据区间")

    # ---- ③ 单镜装不下、多镜装得下（IMG_7950 真实参数）----
    def test_span_rescues_the_shot_single_window_cannot_hold(self):
        short = self._seg(3.945, (0.85, 1.20))
        neighbour = self._seg(5.0, (0.85, 1.20))
        # 先确认单镜确实无解 —— 否则这条用例证明不了「多镜是必需的」。
        self.assertFalse(tc.solve_window(short.request(
            audio_duration_us=4_160_000, tail_pad_us=120_000)).ok)
        req = tc.SpanRequest(segments=(short, neighbour),
                             audio_duration_us=4_160_000, tail_pad_us=120_000)
        self._check_parts_sum_and_legal(req, tc.solve_span(req))

    # ---- ④ 容量真不够 → 如实报 SOURCE_TOO_SHORT，不硬塞 ----
    # 两种「不够」要分开报，因为规划层的动作不同：
    #   scope=segment → 这一个镜头本身不可用，从候选里剔掉再试；
    #   scope=span    → 每个镜头单看都合格，加起来仍铺不满，只能 MATERIAL_GAP。
    def test_span_flags_a_degenerate_segment_by_index(self):
        """<0.5s 的镜头扣掉 0.3/0.2 保护区后可用素材恰好为 0，按段报出下标。"""
        req = tc.SpanRequest(segments=(self._seg(0.5, (0.85, 1.20)),
                                       self._seg(5.0, (0.85, 1.20))),
                             audio_duration_us=4_160_000, tail_pad_us=120_000)
        sol = tc.solve_span(req)
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)
        self.assertEqual(sol.detail["scope"], "segment")
        self.assertEqual(sol.detail["segment_index"], 0)
        self.assertEqual(sol.detail["hi_bound_us"] - sol.detail["lo_bound_us"], 0)

    def test_span_reports_collective_shortfall_instead_of_faking_a_solution(self):
        """每段单看都合格（usable 2.7s），两个加起来 5.4s < 6.12s 时间线。"""
        shots = (self._seg(3.2), self._seg(3.2))
        # 先确认单镜确实无解 —— 否则这条用例证明不了「多镜也救不了」。
        self.assertFalse(tc.solve_window(shots[0].request(
            audio_duration_us=6_000_000, tail_pad_us=120_000)).ok)
        req = tc.SpanRequest(segments=shots, audio_duration_us=6_000_000,
                             tail_pad_us=120_000)
        sol = tc.solve_span(req)
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)
        self.assertEqual(sol.detail["scope"], "span")
        self.assertIn("capacity_us", sol.detail)       # 报出容量而不是编一个解
        self.assertEqual(sol.detail["capacity_us"], 2 * 2_700_000)
        self.assertEqual(sol.detail["shortfall_us"], 6_120_000 - 2 * 2_700_000)

    # ---- 分配公平性：谁余量大谁多担，不是把剩下的塞给最后一个 ----
    def test_allocation_is_proportional_to_headroom(self):
        req = tc.SpanRequest(segments=(self._seg(3.2), self._seg(3.2), self._seg(6.4)),
                             audio_duration_us=9_000_000, tail_pad_us=120_000)
        sol = tc.solve_span(req)
        self._check_parts_sum_and_legal(req, sol)
        alloc = [p.segment_us for p in sol.parts]
        self.assertAlmostEqual(alloc[0] / alloc[2], 2.7 / 5.9, delta=0.02)

    # ---- 太碎：拆给这么多镜头会变成闪帧，宁可报无解 ----
    def test_span_refuses_to_shred_a_short_line_across_many_shots(self):
        req = tc.SpanRequest(segments=tuple(self._seg(8.0) for _ in range(12)),
                             audio_duration_us=1_200_000, tail_pad_us=120_000)
        sol = tc.solve_span(req)
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_SOURCE_TOO_SHORT)
        self.assertIn("minimum_us", sol.detail)

    # ---- 单段组合必须与单镜求解得出**同一个**结论（不引入第二套口径）----
    def test_single_segment_span_matches_solve_window_exactly(self):
        seg = self._seg(3.945, (0.85, 1.20))
        req = tc.SpanRequest(segments=(seg,), audio_duration_us=4_160_000,
                             tail_pad_us=120_000)
        single = tc.solve_window(seg.request(audio_duration_us=4_160_000,
                                             tail_pad_us=120_000))
        span = tc.solve_span(req)
        self.assertEqual(span.ok, single.ok)
        if single.ok:
            self.assertEqual(span.parts[0].source_start_us, single.source_start_us)
            self.assertEqual(span.parts[0].speed, single.speed)

    # ---- 证据越界在组合里也要拦（不能因为「反正是多镜」就放行）----
    def test_span_rejects_evidence_inside_guard_band(self):
        bad = self._seg(4.0, (0.85, 1.20), evidence_start_us=50_000,
                        evidence_end_us=200_000)       # 落进 0.3s 头保护区
        req = tc.SpanRequest(segments=(bad, self._seg(6.0, (0.85, 1.20))),
                             audio_duration_us=4_160_000, tail_pad_us=120_000)
        sol = tc.solve_span(req)
        self.assertFalse(sol.ok)
        self.assertEqual(sol.reason, tc.REASON_EVIDENCE_OUT_OF_RANGE)
        self.assertEqual(sol.detail["segment_index"], 0)

    # ---- 尾留白只能算一次（定案第 5 条：tail_pad 属于输出时间线）----
    def test_tail_pad_belongs_to_the_last_segment_only(self):
        req = tc.SpanRequest(segments=(self._seg(4.0, (1.0, 1.0)),
                                       self._seg(4.0, (1.0, 1.0))),
                             audio_duration_us=4_160_000, tail_pad_us=120_000)
        sol = tc.solve_span(req)
        self._check_parts_sum_and_legal(req, sol)
        # 前若干段的音频份额之和 = 各段输出时长之和（不含尾留白）；只有最后一段多 120ms
        self.assertEqual(sum(p.segment_us for p in sol.parts[:-1])
                         + (sol.parts[-1].segment_us - req.tail_pad_us),
                         req.audio_duration_us)


class EngineInvariantItemTests(unittest.TestCase):
    """⑧ 修订 7/8：写入前那道闸的**输入**必须取自真实来源，不能自证。"""

    def _engine(self):
        import tempfile
        from orchestrator.engine import OrchestrationEngine
        root = Path(tempfile.mkdtemp(prefix="tc_engine_"))
        return OrchestrationEngine({}, root, root / "report", capacity_guard=False)

    def test_solved_window_is_a_separate_field_from_planning_solution(self):
        """`solved_window` 不得覆盖 `window_solution`（后者是规划阶段的审计留痕）。"""
        from orchestrator.engine import Clip
        fields = Clip.__dataclass_fields__
        self.assertIn("solved_window", fields)
        self.assertIn("window_solution", fields)
        c = Clip(video="a.mov", source_start=0.0, duration=4.12,
                 window_solution={"ok": True, "speed": 1.0},
                 solved_window={"ok": True, "speed": 0.95})
        self.assertEqual(c.window_solution["speed"], 1.0)
        self.assertEqual(c.solved_window["speed"], 0.95)

    def test_invariant_items_prefer_recorded_solver_answer(self):
        """源出点取求解器原解，而不是用吸附后的速度反推（反推等于自证）。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=4.12, audio_mode="tts",
                 video_speed=0.95, solved_window={"source_end": 9.0})
        c.video_duration = 12.0
        c.runtime_audio_us = 4_000_000
        item = eng._invariant_items([c], [])[0]
        self.assertEqual(item["source_end_us"], 9_000_000)      # 不是 0 + 4.12×0.95

    def test_missing_recorded_answer_falls_back_to_arithmetic(self):
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=1.0, duration=4.12, audio_mode="tts",
                 video_speed=1.0)
        c.video_duration = 12.0
        c.runtime_audio_us = 4_000_000
        item = eng._invariant_items([c], [])[0]
        self.assertEqual(item["source_end_us"], 5_120_000)

    def test_audio_baseline_is_runtime_probe_not_requested_duration(self):
        """段长比对必须拿 ffprobe 真值；拿 c.duration 自己比自己恒成立。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=4.12, audio_mode="tts")
        c.video_duration = 12.0
        c.runtime_audio_us = 3_000_000          # 真配音比请求短 1.12s
        items = eng._invariant_items([c], [])
        self.assertEqual(items[0]["audio_duration_us"], 3_000_000)
        codes = [i["code"] for i in tc.check_invariants(items)]
        self.assertIn(tc.INV_DURATION_MISMATCH, codes)   # 写入前闸门确实会拦

    def test_clean_clip_passes_the_gate(self):
        """干净段落必须过闸 —— 防线假阳性会把所有草稿拦死。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=4.12, audio_mode="tts")
        c.video_duration = 12.0
        c.runtime_audio_us = 4_000_000
        items = eng._invariant_items([c], [])
        self.assertEqual(tc.check_invariants(items, fps=eng._fps()), [])

    def test_clamped_boundary_speed_passes_the_gate(self):
        """端点上取到的最慢速度不得被自己的防线误判 SPEED_OUT_OF_RANGE。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        lo, hi = tc.speed_range_for_role("product_display")
        c = Clip(video="a.mov", source_start=0.0, duration=4.28, audio_mode="tts",
                 visual_role="product_display")
        c.video_duration = 12.0
        c.runtime_audio_us = 4_160_000
        c.video_speed = tc.frame_speed(lo, lo, hi)      # 吸附到区间下端
        items = eng._invariant_items([c], [])
        self.assertEqual(tc.check_invariants(items, fps=eng._fps()), [])

    def test_draft_reread_duration_reaches_the_gate(self):
        """回读段长（最终渲染真正用的那个数）必须进闸门，否则 min() 截断发现不了。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=4.12, audio_mode="tts")
        c.video_duration = 12.0
        c.runtime_audio_us = 4_000_000
        items = eng._invariant_items([c], [{"index": 0, "duration_us": 3_745_000}])
        self.assertEqual(items[0]["actual_video_us"], 3_745_000)
        codes = [i["code"] for i in tc.check_invariants(items)]
        self.assertEqual(codes, [tc.INV_APERTURE_MISMATCH])


class NarrationSliceOffsetTests(unittest.TestCase):
    """v1.3.27.2 回归：单镜段旁白偏移必须恒为 0。

    1.3.27.1 的 preview5 在写入层炸出 ``截取的素材时间范围
    [start=7468375, end=9453875] 超出了素材时长(1899000)``，排查发现
    ``_audio_slice_us`` 对**单镜段**（``multi_shot=False``）返回了 ``c.start_us``
    —— 也就是把**时间线游标**（第 4 段游标 7,468,375µs）当成了配音里的偏移，
    喂给 ``add_audio_safe(source_start=...)`` 后 vendor 校验炸。前面 3 段只是
    「碰巧没炸」，其实旁白已经开始从句子中间播。约束在 docstring 里一直写着
    「单镜段落/未记账的段落返回 0」，实现漏了这条分支。这里把契约钉死。
    """

    def _engine(self):
        import tempfile
        from orchestrator.engine import OrchestrationEngine
        root = Path(tempfile.mkdtemp(prefix="tc_narr_"))
        return OrchestrationEngine({}, root, root / "report", capacity_guard=False)

    def test_single_shot_offset_is_zero_even_at_large_cursor(self):
        """单镜段落在第 7s 之后（游标 7,468,375），偏移仍必须是 0。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=1.9855,
                 audio_mode="tts")
        c.start_us = 7_468_375            # 模拟第 4 段游标
        c.multi_shot = False              # 单镜段
        self.assertEqual(eng._audio_slice_us(c), 0)

    def test_span_subclip_gets_sibling_delta(self):
        """多镜子段仍按基准相减：第二子段偏移 = 游标 − 父段起点。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=1.0,
                 audio_mode="tts")
        c.multi_shot = True
        c.start_us = 8_000_000            # 游标
        c.audio_clip_start_us = 5_000_000 # 父段旁白起点（_mark_audio_origin 记的）
        self.assertEqual(eng._audio_slice_us(c), 3_000_000)

    def test_first_span_subclip_offset_is_zero(self):
        """多镜首子段：基准 == 自身游标，偏移 0（从整句头播）。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=1.0,
                 audio_mode="tts")
        c.multi_shot = True
        c.start_us = 2_500_000
        c.audio_clip_start_us = 2_500_000
        self.assertEqual(eng._audio_slice_us(c), 0)

    def test_unmarked_single_shot_default_stays_zero(self):
        """默认 Clip（audio_clip_start_us 未写、multi_shot 未置）也要 0 ——
        不能靠「恰好 start_us==0」侥幸。"""
        from orchestrator.engine import Clip
        eng = self._engine()
        c = Clip(video="a.mov", source_start=0.0, duration=1.0,
                 audio_mode="tts", tts_path="x.ogg")
        c.start_us = 9_876_000
        self.assertEqual(eng._audio_slice_us(c), 0)


class PlanSpanLongAudioTests(unittest.TestCase):
    """⑬ 端到端规划用例：音频明显长于单镜 → 要么多镜、要么 MATERIAL_GAP，
    **绝不允许静默截断**。

    走 ``shot_analyzer.plan_span`` —— 这是引擎在「单镜无解」时真正调用的规划层
    （shot_analyzer 的语义判定那一步），不是只测 ``solve_span`` 的几何原语。
    电影里那句更短的口播是单镜可能讲不完 → 规划必须两条出路二选一：

    - **多镜**：把同一份音频按时长拆给多个镜头，各段输出之和**恰好**等于
      ``音频 + 尾留白``（差 1µs 都不行），从头到尾一句不缺；
    - **MATERIAL_GAP**：所有候选合起来也铺不满时，如实报 capacity/shortfall，
      ``parts`` 为空 —— 而不是硬凑一个「勉强能过」却在句尾截掉几秒配音的解。

    两种结局的共同前提：**没有哪条路可以让单镜把音频截短还宣称成功**。
    plan_span 的「单镜优先」分支只在 ``solve_window`` 真解出完整音频时才走
    （``solution.ok``），否则必须进多镜或缺口 —— 这正是本组断言要钉死的契约。
    """

    # 保护期与角色默认值来自生产 manifest/规则（与 shot_analyzer 同一份常量）。
    HEAD_TRIM = 0.3
    TAIL_TRIM = 0.2
    TAIL_PAD = 0.12
    FPS = 30
    FRAME_US = int(round(1e6 / FPS))

    @staticmethod
    def _shot(shot_id: str, source_end: float, *,
              source_start: float = 0.0, role: str = "product_display") -> dict:
        """一个真实形态的候选镜头（跟 analyze_manifest 产物字段齐）。

        ``source_end`` 无证据区间时由 ``candidate_evidence_interval`` 在场景中央
        生成 ±0.6s 邻域当作证据，这正是生产里的回退路径。
        """
        return {"shot_id": shot_id, "video": f"clip{shot_id}.mov",
                "source_start": source_start, "source_end": source_end,
                "role": role,
                "material_duration_us": int(round(source_end * 1_000_000)),
                "evidence_intervals": []}

    @staticmethod
    def _manifest(**extra) -> dict:
        base = {"fps": PlanSpanLongAudioTests.FPS,
                "head_trim_s": PlanSpanLongAudioTests.HEAD_TRIM,
                "tail_trim_s": PlanSpanLongAudioTests.TAIL_TRIM,
                "tail_pad_s": PlanSpanLongAudioTests.TAIL_PAD,
                "span_max_parts": 3}
        base.update(extra)
        return base

    # ---- 负向前置断言：单镜确实装不下 ----
    def test_single_shot_truly_cannot_hold_long_audio(self):
        """先证明「单镜确实无解」—— 否则整组用例证明不了多镜是必需的解。

        6s 口播（+120ms 尾留白）对 3.5s 可用素材的镜头，即使放到角色允许的
        最慢速度 0.85，也铺不满。plan_span 必须把它判成非单镜可解。
        """
        from orchestrator import shot_analyzer
        manifest = self._manifest()
        shots = [self._shot("P1", 4.0)]
        span = shot_analyzer.plan_span(shots, audio_duration_us=6_000_000,
                                       manifest=manifest)
        # 单镜可独立承担时 plan_span 走 scope=single；这里绝不允许。
        self.assertNotEqual(span.get("detail", {}).get("scope"), "single")
        self.assertFalse(span["ok"])

    # ---- 出路一：多镜 —— 音频从头到尾要一句不缺 ----
    def test_long_audio_goes_multi_shot_with_exact_total(self):
        """两个各 3.5s 可用素材的镜头，合起来装得下 5s 口播。

        断言多镜解的各段输出之和**恰好**等于 音频+尾留白 —— 这是「不静默截断」
        的证据：任何一段被掐短，总和都会小于时间线，闸门会拦下整支。
        """
        from orchestrator import shot_analyzer
        audio_us = 5_000_000
        manifest = self._manifest()
        shots = [self._shot("A1", 4.0), self._shot("B1", 4.0)]
        span = shot_analyzer.plan_span(shots, audio_duration_us=audio_us,
                                       manifest=manifest)
        self.assertTrue(span["ok"], f"应当有多镜解：{span.get('reason')} {span['detail']}")
        parts = span["parts"]
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual(span["detail"]["scope"], "span")
        timeline_us = audio_us + int(round(self.TAIL_PAD * 1_000_000))
        total = sum(p["solution"].segment_us for p in parts)
        self.assertEqual(total, timeline_us, "多镜各段之和必须恰好铺满时间线（差 1µs 都不行）")
        # 每段窗口必须落在自己镜头内，且连起来旁白不重不漏。
        for p in parts:
            sol = p["solution"]
            self.assertTrue(sol.ok)
            self.assertTrue(sol.source_start_us >= p["segment"].lo_bound_us,
                            f"{p['shot']['shot_id']} 窗口下界越界")
            self.assertTrue(sol.source_end_us <= p["segment"].hi_bound_us,
                            f"{p['shot']['shot_id']} 窗口上界越界")
        self.assertEqual(sum(p["solution"].segment_us for p in parts[:-1])
                         + (parts[-1]["solution"].segment_us
                            - int(round(self.TAIL_PAD * 1_000_000))),
                         audio_us, "各段音频份额之和 == 整句音频（尾留白只算一次）")

    # ---- 出路二：MATERIAL_GAP —— 如实报缺口，不给假解 ----
    def test_capacity_shortfall_is_a_plain_gap_not_a_truncation(self):
        """两个各 2.0s 可用素材的镜头，合起来 4.7s 仍铺不满 5s 口播。

        必须返回 SOURCE_TOO_SHORT（scope=span）并带 capacity/shortfall，
        ``parts`` 为空 —— 任何「从整句里截掉一段再配音」的行为都是静默截断，
        规划层给不出这种解。
        """
        from orchestrator import shot_analyzer
        audio_us = 5_000_000
        timeline_us = audio_us + int(round(self.TAIL_PAD * 1_000_000))
        manifest = self._manifest()
        shots = [self._shot("C1", 2.5), self._shot("D1", 2.5)]
        span = shot_analyzer.plan_span(shots, audio_duration_us=audio_us,
                                       manifest=manifest)
        self.assertFalse(span["ok"])
        self.assertEqual(span["reason"], tc.REASON_SOURCE_TOO_SHORT)
        detail = span["detail"]
        self.assertEqual(detail["scope"], "span")
        self.assertEqual(span["parts"], [])
        cap = int(detail["capacity_us"])
        self.assertLess(cap, timeline_us)
        self.assertEqual(int(detail["shortfall_us"]), timeline_us - cap)
        # 缺口理由里必须有运营可读的「还差多少秒」，而不是模棱两可的装不下。
        self.assertIn("shortfall_us", detail)


class ExplicitSpeedWritePathTests(unittest.TestCase):
    """修订 7：写入端删掉 min(...) 静默截断，越界时如实报错。

    ``core.media_ops`` 依赖 vendored 的 ``pyJianYingDraft``，而 Mac 编辑镜像的
    vendor 包是残缺的（只有 ``script_file.py``，连 ``__init__.py`` 都没有），
    所以这里**按源码校验**而不是导入执行 —— 代码结构是确定的，
    环境残缺不该伪装成代码缺陷。运行时行为在 Windows 活装（vendor 完整）上验收。
    """

    @staticmethod
    def _source(keep_comments: bool = False) -> str:
        """读源码，语法自证，并把注释剥掉。

        注释要剥掉：说明文字里会**提到** ``min()``（「这里不做 min() 静默截断」），
        不剥的话断言会打在散文上而不是代码上。
        """
        import ast
        import io
        import tokenize
        path = (Path(__file__).resolve().parents[2] / "jianying-editor" / "scripts"
                / "core" / "media_ops.py")
        text = path.read_text(encoding="utf-8")
        ast.parse(text)                     # 语法自证，坏文件不会静默通过
        if keep_comments:
            return text
        lines = text.splitlines()
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT and tok.start[0] == tok.end[0]:
                lines[tok.start[0] - 1] = lines[tok.start[0] - 1][:tok.start[1]]
        return "\n".join(lines)

    def test_guard_rejects_out_of_material_slice(self):
        """素材不够长时不得少取一截 —— 画面会跟不上配音。"""
        src = self._source(keep_comments=True)
        self.assertIn("变速切片越出素材范围", src)
        self.assertIn("不得静默截断", src)
        block = self._source().split("if speed is not None:")[1].split(
            "actual_duration =")[0]
        self.assertIn("raise ValueError", block)
        # 代码里（非注释）不得出现 min( —— 静默截断已删（用户定案第 7 条）。
        self.assertNotIn("min(", block)
        self.assertIn("src_start_us + material_us > phys_duration", block)

    def test_explicit_path_computes_material_from_speed(self):
        """显式通路的语义：素材取长 = 输出时长 × 速度（两个 range 不能混）。"""
        block = self._source().split("if speed is not None:")[1].split(
            "actual_duration =")[0]
        self.assertIn("material_us = int(round(segment_us * speed))", block)
        self.assertIn("trange(src_start_us, material_us)", block)
        self.assertIn("trange(start_us, segment_us)", block)

    def test_legacy_path_keeps_its_truncation(self):
        """旧通路（speed=None）保持原行为：min() 是它的历史契约，不动。"""
        src = self._source()
        tail = src.split("if speed is not None:")[1]
        self.assertIn("return min(req_us, phys_dur_available)", tail)


class PreviewDeliveryTests(unittest.TestCase):
    """B 步：preview/formal 分流 + 未认证可见性（定案第 9、10 条）。

    这组用例只 import ``orchestrator.engine`` / ``orchestrator.cli``，
    **不碰 vendored ``pyJianYingDraft``**，所以 Mac 编辑镜像的 vendor 残缺
    在这里不构成跳过理由 —— 每条都必须真的断言（见
    ``ExplicitSpeedWritePathTests`` 的教训：环境残缺不得伪装成通过）。
    """

    CERTIFIED = {"certification": "CERTIFIED", "coverage_status": "FULL",
                 "uncertified_reasons": []}
    UNCERTIFIED = {"certification": "UNCERTIFIED", "coverage_status": "RULEPACK_MISSING",
                   "uncertified_reasons": ["RULEPACK_MISSING"]}

    def _engine(self, manifest=None, reasons=()):
        import tempfile
        from orchestrator.engine import OrchestrationEngine
        root = Path(tempfile.mkdtemp(prefix="tc_preview_"))
        eng = OrchestrationEngine(dict(manifest or {}), root, root / "report",
                                  capacity_guard=False)
        eng.preview_uncertified_reasons.extend(reasons)
        return eng

    # ---- 交付模式归一化：默认必须是 formal ----
    def test_delivery_mode_normalization(self):
        from orchestrator.engine import OrchestrationEngine
        resolve = OrchestrationEngine._resolve_delivery_mode
        for raw in ("preview", "PREVIEW", "  Preview ", "preview_only", "预览"):
            self.assertEqual(resolve({"delivery_mode": raw}), "preview", raw)
        # 认不出来的一律 formal：少一次显式授权就少一次误发。
        for raw in ("formal", "", None, "junk", "preview_onlyx"):
            self.assertEqual(resolve({"delivery_mode": raw}), "formal", raw)
        self.assertEqual(resolve({}), "formal")

    # ---- 要不要打画面标注：只认「闸门未认证」 ----
    def test_uncertified_coverage_three_branches(self):
        self.assertIsNone(self._engine({})._uncertified_coverage())
        self.assertIsNone(self._engine({})._uncertified_coverage())
        self.assertIsNone(
            self._engine({"claim_gate_coverage": {}})._uncertified_coverage())
        self.assertIsNone(self._engine(
            {"claim_gate_coverage": self.CERTIFIED})._uncertified_coverage())
        cov = self._engine(
            {"claim_gate_coverage": self.UNCERTIFIED})._uncertified_coverage()
        self.assertEqual(cov["certification"], "UNCERTIFIED")
        # 缺 certification 的老 manifest 必须与「认证通过」分开。
        legacy = self._engine(
            {"claim_gate_coverage": {"coverage_status": "X"}})._uncertified_coverage()
        self.assertEqual(legacy, {"coverage_status": "X"})
        # 预览遗留理由**不能**让 `_uncertified_coverage` 变脸（那是上报口径的事）。
        self.assertIsNone(self._engine(
            {"claim_gate_coverage": self.CERTIFIED},
            reasons=["PREVIEW_TIMING_INFEASIBLE: 2 段时序问题未解决"]
        )._uncertified_coverage())

    # ---- 上报口径 ----
    def test_coverage_report_formal_delivery_allowed(self):
        self.assertIsNone(self._engine({})._coverage_report())
        self.assertIsNone(self._engine(
            {"claim_gate_coverage": self.CERTIFIED})._coverage_report())
        # 预览：即便闸门认证通过，也不是正式交付。
        rep = self._engine(
            {"claim_gate_coverage": self.CERTIFIED, "delivery_mode": "preview"}
        )._coverage_report()
        self.assertEqual(rep["delivery_mode"], "preview")
        self.assertIs(rep["formal_delivery_allowed"], False)
        # 预览理由必须出现在上报理由里（认证状态因此降为 UNCERTIFIED）。
        rep = self._engine(
            {"claim_gate_coverage": self.CERTIFIED, "delivery_mode": "preview"},
            reasons=["PREVIEW_AUDIO_VIDEO_MISMATCH: 1 段时序问题未解决"]
        )._coverage_report()
        self.assertEqual(rep["certification"], "UNCERTIFIED")
        self.assertIn("PREVIEW_AUDIO_VIDEO_MISMATCH: 1 段时序问题未解决",
                      rep["uncertified_reasons"])
        # 无 coverage、无预览理由的 preview 也要有上报（否则预览稿无从分辨）。
        rep = self._engine({"delivery_mode": "preview"})._coverage_report()
        self.assertEqual(rep["formal_delivery_allowed"], False)

    def test_formal_uncertified_gate_still_reports_allowed(self):
        """已知语义落差，锁住它以免被误当成 bug 修掉或误当成合规放过。

        `formal_delivery_allowed` 只表达「模式是 formal 且无预览遗留理由」，
        **不**表达「闸门已认证」—— 闸门状态另由 `certification` /
        `uncertified_reasons` 表达，成片另有 `Uncertified_Marks` 画面标注兜住，
        所以不是静默通过。这里把这条口径钉住：改它就等于改交付语义。
        """
        rep = self._engine({"claim_gate_coverage": self.UNCERTIFIED})._coverage_report()
        self.assertIs(rep["formal_delivery_allowed"], True)
        self.assertEqual(rep["certification"], "UNCERTIFIED")
        self.assertEqual(rep["uncertified_reasons"], ["RULEPACK_MISSING"])

    # ---- 成功合约的可见性（定案第 9 条：正式交付不得静默通过）----
    def test_compact_success_hides_everything_when_certified(self):
        from orchestrator import cli
        out = cli._compact_build_result({
            "status": "SUCCESS", "draft_name": "d", "draft_path": "",
            "delivery_mode": "formal", "coverage": self.CERTIFIED})
        self.assertEqual(sorted(out), sorted(
            ["草稿名称", "草稿路径", "草稿文件", "生成日志"]))

    def test_compact_success_flags_preview_and_uncertified(self):
        from orchestrator import cli
        out = cli._compact_build_result({
            "status": "SUCCESS", "draft_name": "d", "draft_path": "",
            "delivery_mode": "preview",
            "coverage": dict(self.UNCERTIFIED, delivery_mode="preview")})
        self.assertIn("preview", out["交付模式"])
        self.assertIn("不得用于正式交付", out["交付模式"])
        self.assertEqual(out["认证状态"], "UNCERTIFIED")
        self.assertEqual(out["未认证原因"], ["RULEPACK_MISSING"])

    def test_compact_success_flags_uncertified_without_preview_key(self):
        from orchestrator import cli
        out = cli._compact_build_result({
            "status": "SUCCESS", "draft_name": "d", "draft_path": "",
            "delivery_mode": "formal", "coverage": self.UNCERTIFIED})
        self.assertNotIn("交付模式", out)          # formal 稿不该自称预览
        self.assertEqual(out["认证状态"], "UNCERTIFIED")

    def test_compact_success_unchanged_without_coverage(self):
        from orchestrator import cli
        out = cli._compact_build_result({
            "status": "SUCCESS", "draft_name": "d", "draft_path": "",
            "delivery_mode": "preview"})
        self.assertEqual(sorted(out), sorted(
            ["草稿名称", "草稿路径", "草稿文件", "生成日志"]))

    # ---- 预览放宽只记理由，不改交付语义 ----
    def test_preview_tolerated_only_records_reason(self):
        eng = self._engine({"claim_gate_coverage": self.CERTIFIED,
                            "delivery_mode": "preview"})
        self.assertIsNone(eng._preview_tolerated("TIMING_INFEASIBLE",
                                                 [{"reason": "SOURCE_TOO_SHORT"}], "m"))
        self.assertEqual(len(eng.preview_uncertified_reasons), 1)
        self.assertIn("PREVIEW_TIMING_INFEASIBLE: 1", eng.preview_uncertified_reasons[0])
        self.assertIs(eng._coverage_report()["formal_delivery_allowed"], False)


def _fallback_shot(index, role, description, result="包装完整"):
    """构造一个「已分析、可用、未被复用约束卡住」的镜头。

    字段按 `match_manifest` 真正读取的键给全：`role`/`has_subject` 供
    `demo_actions.judge()`，`evidence_strength`/`result`/`object` 供
    `judge_claim()`，`setup_id`/`action_phase` 供反重复判据。
    """
    return {
        "shot_id": f"source_{index:03d}_shot_001",
        "video": f"\\\\nas\\IMG_79{index:02d}.MOV",
        "source_start": 0.0, "source_end": 8.0, "duration": 8.0,
        "frame_time": 4.0,
        "status": "ready_for_matching", "action_complete": True,
        "description": description, "visual_tags": [role],
        "evidence_tags": [], "role": role, "has_subject": True,
        "evidence_strength": "A", "result": result,
        "setup_id": f"s{index}", "action_phase": "static",
    }


def _fallback_manifest(text, *, mode="formal", seconds=1.5):
    return {
        "segments": [{"text": text, "audio_duration_us": int(seconds * US)}],
        "head_trim_s": 0.3, "tail_trim_s": 0.2, "fps": 30,
        "max_source_reuse": 2, "delivery_mode": mode,
    }


class FallbackPoolTests(unittest.TestCase):
    """兜底池的构造（v1.3.27 上线后第一次 preview 的 `NameError` 的解药）。

    2026-09-17 现场：`shot_analyzer.py` 兜底段里
    `eligible_good = max(fallback_pool, key=_priority, default=None)`
    的 `fallback_pool` **没有任何赋值** —— 1.3.27 重写这一段时只保留了使用处、
    丢掉了构造处（1.3.26 备份 L581 有完整实现）。

    这个 bug 从 `python3 -m py_compile` 和当时的 87 条测试底下**整个漏过去**：
    静态编译不查未定义名字，而本文件此前**没有任何一条测试调用
    `match_manifest`** —— 兜底分支从来没被走到过。所以下面这组用例不是
    「多一条覆盖率」，而是这条分支**唯一**的防线：任何一条都必须真的驱动
    `match_manifest` 走到兜底，而不是在它前面绕开。
    """

    # ---- ① 无相似可用镜头时：预览留黑，但保留旁白/字幕/文案 ----
    def test_preview_visual_missing_keeps_audio_and_operator_note(self):
        """「天哪！」命中不了任何卖点关键词，全池只有包装展示镜头。

        预览不能用 `max(ranked)` 绕过资格约束拿不相似画面充数；应生成一个
        visual_missing 段，交给引擎放黑色视频，同时把处理原因留给运营。
        """
        from orchestrator import shot_analyzer as sa
        shots = [_fallback_shot(i, "product_display", "产品包装摆在木台上")
                 for i in range(1, 5)]
        res = sa.match_manifest(_fallback_manifest("天哪！", mode="preview"),
                                {"shots": shots, "semantic_matches": []})

        self.assertTrue(res["ok"], "预览缺画面不应阻断整支")
        self.assertEqual(res["pending_items"], [])
        self.assertEqual(len(res["segments"]), 1)

        seg = res["segments"][0]
        self.assertTrue(seg["visual_missing"])
        self.assertIsNone(seg["video"])
        self.assertEqual(seg["selection_mode"], "visual_missing")
        self.assertGreater(seg["duration"], 0)
        self.assertIn("保留完整旁白", seg["operator_note"])
        self.assertEqual(res["visual_missing_count"], 1)
        self.assertEqual(res["degraded_count"], 0)

    def test_cta_product_display_fallback_is_preview_only_and_degraded(self):
        from orchestrator import shot_analyzer as sa
        shots = [_fallback_shot(i, "product_display", "产品包装摆在木台上")
                 for i in range(1, 5)]
        preview = _fallback_manifest("现在下单，优惠囤货！", mode="preview")
        preview["allow_ending_cta_product_display_fallback"] = True
        preview_result = sa.match_manifest(preview, {"shots": shots, "semantic_matches": []})
        picked = preview_result["segments"][0]
        self.assertTrue(picked["cta_product_display_fallback"])
        self.assertEqual(picked["selection_mode"], "cta_product_display_degraded")
        self.assertTrue(picked["degraded_no_match"])
        self.assertFalse(picked["cta_visual_evidence"]["ok"])
        self.assertEqual(preview_result["manifest"]["visual_matching_governance"][
            "ending_cta_product_display_fallback_preview_only"], True)

        formal = _fallback_manifest("现在下单，优惠囤货！", mode="formal")
        formal["allow_ending_cta_product_display_fallback"] = True
        formal_result = sa.match_manifest(formal, {"shots": shots, "semantic_matches": []})
        self.assertFalse(any(segment.get("cta_product_display_fallback", False)
                             for segment in formal_result["segments"]))

    # ---- ② 收紧生效：全句卖点都要求直接证据时不拿包装展示充数 ----
    def test_restrict_fallback_prefers_demo_over_display(self):
        """「超薄」= C-THIN，`evidence_required=True` → `restrict_fallback` 为真。

        池里同时有包装展示与演示镜头时，兜底**只能**捡演示类 ——
        「包装镜头配纸质卖点」正是 v1.3.18 用户点名要消灭的张冠李戴。
        """
        from orchestrator import shot_analyzer as sa
        shots = [
            _fallback_shot(1, "product_display", "包装摆在木台上"),
            _fallback_shot(2, "product_display", "包装正面"),
            _fallback_shot(3, "usage_demo", "手抚过巾体", "与手指同框对比"),
            _fallback_shot(4, "usage_demo", "手拿着纸巾", "与手指同框对比"),
        ]
        res = sa.match_manifest(_fallback_manifest("超薄", seconds=2.0),
                                {"shots": shots, "semantic_matches": []})
        self.assertEqual(len(res["segments"]), 1)
        picked = res["segments"][0]["temporary_shot_id"]
        self.assertIn(picked, {"source_003_shot_001", "source_004_shot_001"},
                      f"兜底捡回了包装展示镜头 {picked}，"
                      "`restrict_fallback` 未生效（demo_actions.fallback_allowed）")

    def test_related_product_closeup_is_explicit_degraded_fallback(self):
        """功能卖点没有动作证据时，相关产品特写可承接但必须留痕。"""
        from orchestrator import shot_analyzer as sa
        shots = [_fallback_shot(
            1, "product_display", "纸张面层特写，展示柔软的压花纹理",
            "包装完整")]
        res = sa.match_manifest(_fallback_manifest("面层柔软", seconds=2.0),
                                {"shots": shots, "semantic_matches": []})

        self.assertTrue(res["ok"])
        self.assertEqual(len(res["segments"]), 1)
        self.assertTrue(res["segments"][0]["degraded_no_match"])
        self.assertEqual(res["degraded_fallback_count"], 1)
        self.assertEqual(
            res["degraded_matches"][0]["fallback_reason"],
            "PRODUCT_SELLING_POINT_CLOSEUP_DEGRADED",
        )
        self.assertEqual(res["material_gaps"], [])

    # ---- ③ 收紧到底：池里只剩包装展示 → 报缺口，不是拿它凑 ----
    def test_restrict_fallback_blocks_instead_of_borrowing_packaging(self):
        """只有包装展示镜头时不得兜底：如实报 `SHOT_NO_MATCH` + 素材缺口。

        这一条是「宁可不生成也不充数」的那一半 —— 少了它，上面那两条收紧
        就会退化成「池子空了就随便捡」，等于把 v1.3.24 的教训重演一遍。
        """
        from orchestrator import shot_analyzer as sa
        shots = [_fallback_shot(i, "product_display", "包装摆在木台上")
                 for i in range(1, 5)]
        res = sa.match_manifest(_fallback_manifest("超薄", seconds=2.0),
                                {"shots": shots, "semantic_matches": []})

        self.assertFalse(res["ok"])
        self.assertEqual(res["segments"], [], "被禁止的镜头不得出现在成片里")
        self.assertEqual(res["degraded_fallback_count"], 0)
        self.assertEqual([p["type"] for p in res["pending_items"]], ["SHOT_NO_MATCH"])
        self.assertIn("包装展示/空镜已被禁止兜底", res["pending_items"][0]["message"])
        # 缺口按 claim 粒度登记，运营据此去补拍（C-THIN 的动作语料里根本没有）。
        self.assertEqual([g["claim_id"] for g in res["material_gaps"]], ["C-THIN"])

    # ---- ④ 复用约束优先于兜底：镜头用光时报 SHOT_NO_MATCH，不靠重复凑 ----
    def test_exhausted_pool_reports_no_match_rather_than_repeating(self):
        """两个镜头、`max_source_reuse=1`，两句都走兜底 → 第二句必须报缺口。

        兜底池只收 `eligible` 为真的行；池子空了就是素材用光，这正是
        `fallback_pool` 那个列表推导存在的意义 —— 若写成
        `[row for row in ranked]`，同一镜头会被反复捡回来。
        """
        from orchestrator import shot_analyzer as sa
        shots = [_fallback_shot(i, "product_display", "包装摆在木台上")
                 for i in range(1, 3)]
        manifest = {
            "segments": [{"text": "天哪！", "audio_duration_us": int(1.5 * US)},
                         {"text": "好可爱！", "audio_duration_us": int(1.5 * US)}],
            "head_trim_s": 0.3, "tail_trim_s": 0.2, "fps": 30,
            "max_source_reuse": 1, "delivery_mode": "formal",
        }
        res = sa.match_manifest(manifest, {"shots": shots, "semantic_matches": []})
        self.assertEqual(len(res["segments"]), 2)
        used = [s["temporary_shot_id"] for s in res["segments"]]
        self.assertEqual(len(set(used)), 2, f"兜底重复捡了同一镜头：{used}")


class MaterialDurationProbeTests(unittest.TestCase):
    """1.3.27.1：素材总长同源同值（容器 ≠ 视频轨）回归。

    2026-09-17 实测：iPhone 实拍 MOV 音轨比视频轨长，容器 4.990000s / 视频轨
    4.983333s。规划层按容器摆窗、写入层按视频轨零容差校验 → write_draft 炸
    「变速切片越出素材范围：… > 素材时长 4984000µs」。这里钉住：
    ① 探测**优先使用与写入层一致的视频素材时长**（pymediainfo / ffprobe 视频轨 /
       末帧 PTS，HIGH）；② **只有容器时长时进入保守降级**（减一帧安全边界，
       标记 LOW，不得用于生成「已认证」的候选窗口）；③ check_invariants 对
       `material_duration_us` 单列**零容差**硬边界，并按速度语义校验
       源窗口 ÷ video_speed ≈ segment_us（容差 1 帧）。
    """

    def test_pymediainfo_video_track_is_the_truth(self):
        """4.984s 视频轨一期到位，与容器 4.99 无关；来源/置信度必须如实带出。"""
        fake = _fake_pymediainfo(video_ms=4984)
        with mock.patch.dict(sys.modules, {"pymediainfo": fake}):
            got = tc.probe_material_duration_us(r"\\nas\a.MOV", ffprobe=None, ffmpeg=None)
        self.assertEqual(got["duration_us"], 4_984_000)
        self.assertEqual(got["source"], "pymediainfo_video_track")
        self.assertEqual(got["confidence"], tc.PROBE_CONFIDENCE_HIGH)

    def test_ffprobe_video_stream_fallback_truncates(self):
        """无 pymediainfo 时退 ffprobe v:0：4.983333s → 4983333µs（截断），HIGH。"""
        fake = _fake_pymediainfo(video_ms=None, have_video=False)
        runs = []

        def fake_run(cmd, **kwargs):
            runs.append(cmd)
            if "-select_streams" in cmd:
                return mock.Mock(stdout="4.983333\n", stderr="", returncode=0)
            return mock.Mock(stdout="4.990000\n", stderr="", returncode=0)

        with mock.patch.dict(sys.modules, {"pymediainfo": fake}), \
                mock.patch.object(tc.subprocess, "run", side_effect=fake_run):
            got = tc.probe_material_duration_us(r"\\nas\a.MOV", ffprobe="ffprobe", ffmpeg=None)
        self.assertEqual(got["duration_us"], 4_983_333)
        self.assertEqual(got["source"], "ffprobe_video_stream")
        self.assertEqual(got["confidence"], tc.PROBE_CONFIDENCE_HIGH)
        self.assertEqual(len(runs), 1, "视频轨命中即返，不再问容器/末帧 PTS")

    def test_last_video_pts_upgrades_to_high(self):
        """视频轨时长取不到但能读到末帧 PTS → HIGH（末帧起播点，天然 ≤ 真值）。"""
        fake = _fake_pymediainfo(video_ms=None, have_video=False)

        def fake_run(cmd, **kwargs):
            if "stream=duration" in cmd:
                return mock.Mock(stdout="\n", stderr="", returncode=0)
            if "packet=pts_time" in cmd:
                return mock.Mock(stdout="0.000000\n0.033333\n4.950000\n",
                                 stderr="", returncode=0)
            return mock.Mock(stdout="4.990000\n", stderr="", returncode=0)

        with mock.patch.dict(sys.modules, {"pymediainfo": fake}), \
                mock.patch.object(tc.subprocess, "run", side_effect=fake_run):
            got = tc.probe_material_duration_us(r"\\nas\a.MOV", ffprobe="ffprobe", ffmpeg=None)
        self.assertEqual(got["duration_us"], 4_950_000)
        self.assertEqual(got["source"], "last_video_pts")
        self.assertEqual(got["confidence"], tc.PROBE_CONFIDENCE_HIGH)

    def test_container_duration_degrades_low_with_frame_margin(self):
        """只剩容器时长 → 保守降级：减一帧安全边界、标记 LOW、来源如实落盘。

        容器 4.990000s − 一帧(30fps=33333µs) = 4_956_667µs ≤ 视频轨真值
        4_984_000µs —— 音轨比视频轨长出的那几毫秒被一帧边界整体盖住。
        """
        fake = _fake_pymediainfo(video_ms=None, have_video=False)

        def fake_run(cmd, **kwargs):
            if "stream=duration" in cmd:
                return mock.Mock(stdout="\n", stderr="", returncode=0)
            if "packet=pts_time" in cmd:
                return mock.Mock(stdout="\n", stderr="", returncode=0)
            return mock.Mock(stdout="4.990000\n", stderr="", returncode=0)

        with mock.patch.dict(sys.modules, {"pymediainfo": fake}), \
                mock.patch.object(tc.subprocess, "run", side_effect=fake_run):
            got = tc.probe_material_duration_us(r"\\nas\b.MOV", ffprobe="ffprobe", ffmpeg=None)
        self.assertEqual(got["duration_us"], 4_990_000 - 33_333)
        self.assertEqual(got["source"], "container_duration")
        self.assertEqual(got["confidence"], tc.PROBE_CONFIDENCE_LOW)
        self.assertLessEqual(got["duration_us"], 4_984_000,
                             "降级值必须落到视频轨真值之下，不给越界留空间")

    def test_probe_failure_is_conservative(self):
        """连保守降级都做不了 → duration_us=0/probe_failed，调用方报探测失败。"""
        fake = _fake_pymediainfo(video_ms=None, have_video=False)
        with mock.patch.dict(sys.modules, {"pymediainfo": fake}):
            got = tc.probe_material_duration_us(r"\\nas\x.MOV", ffprobe=None, ffmpeg=None)
        self.assertEqual(got["duration_us"], 0)
        self.assertEqual(got["source"], "probe_failed")
        self.assertEqual(got["confidence"], tc.PROBE_CONFIDENCE_LOW)

    def test_boundaries_us_never_rounded_up(self):
        """尾边界 = 实测 µs 本身；旧的 round(,3) 会把 4.9835 上舍成 4.984 → 越界。"""
        for duration_us, scenes in [(4_984_000, [4.59]), (3_883_333, [])]:
            wins = shot_analyzer._boundaries_us(duration_us, scenes, min_shot_s=0.45)
            self.assertTrue(wins, "有素材就该有窗口")
            self.assertEqual(wins[-1][1], duration_us,
                             "最后一格出点必须恒等于实测总长，不容舍入")
            for start, end in wins:
                self.assertLessEqual(end, duration_us)

    # ── check_invariants 边界（用户修正第 1 条要求的五条边界用例）────────

    def _legal_item(self, *, source_end_us: int, segment_us: int,
                    material_us: int = 4_984_000, speed: float = 1.0) -> dict:
        """构造除指定字段外全部自洽的条目：音频+尾垫 == 段长 == 窗口/速度。"""
        return {
            "index": 1, "video": "a.MOV",
            "source_start_us": 3_757_917, "source_end_us": source_end_us,
            "audio_duration_us": segment_us - 120_000, "tail_pad_us": 120_000,
            "segment_us": segment_us, "video_speed": speed,
            "material_duration_us": material_us,
        }

    def test_negative_case_reports_both_codes(self):
        """负向回归（用户点名的用例）：出点越界 + 源窗口与段长不换算，两个码都要报。

        fixture 即 20260917 炸写的那条解：source_end=5_050_000 > material
        4_984_000（WINDOW_OUT_OF_BOUNDS），且 source_end−source_start=1_292_083
        ≠ segment 1_232_083 × speed 1.0（DURATION_MISMATCH）。
        audio+tail 恰好等于 segment —— DURATION_MISMATCH **只**来自速度换算，
        证明换算校验独立生效，而不是搭「音频 vs 段长」的便车。
        """
        item = {
            "index": 1, "video": "a.MOV",
            "source_start_us": 3_757_917, "source_end_us": 5_050_000,
            "audio_duration_us": 1_112_083, "tail_pad_us": 120_000,
            "segment_us": 1_232_083, "video_speed": 1.0,
            "material_duration_us": 4_984_000,
        }
        issues = tc.check_invariants([item], fps=30)
        codes = [i["code"] for i in issues]
        self.assertIn(tc.INV_WINDOW_OUT_OF_BOUNDS, codes, "越界必须报 WINDOW_OUT_OF_BOUNDS")
        self.assertIn(tc.INV_DURATION_MISMATCH, codes, "窗口≠段长×速度必须报 DURATION_MISMATCH")
        oob = next(i for i in issues if i["code"] == tc.INV_WINDOW_OUT_OF_BOUNDS)
        self.assertIn("素材总长", oob["message"])
        self.assertEqual(oob["expected_us"], 4_984_000)
        self.assertEqual(oob["actual_us"], 5_050_000)

    def test_boundary_end_equals_material_passes(self):
        """边界：source_end == material_duration 精确贴边 → 通过（无任何 issue）。"""
        item = self._legal_item(source_end_us=4_984_000, segment_us=1_226_083)
        self.assertEqual(tc.check_invariants([item], fps=30), [])

    def test_boundary_end_material_plus_1us_fails(self):
        """边界：source_end == material + 1µs → 零容差失败（1 帧容差救不了它）。"""
        item = self._legal_item(source_end_us=4_984_001, segment_us=1_226_084)
        issues = tc.check_invariants([item], fps=30)
        self.assertEqual([i["code"] for i in issues],
                         [tc.INV_WINDOW_OUT_OF_BOUNDS])
        self.assertIn("零容差", issues[0]["message"])
        self.assertEqual(issues[0]["delta_us"], 1)

    def test_speed_conversion_mismatch_fails(self):
        """源窗口 ÷ video_speed 与 segment_us 差超 1 帧 → DURATION_MISMATCH（唯一码）。"""
        item = {
            "index": 2, "video": "b.MOV",
            "source_start_us": 0, "source_end_us": 1_232_083,
            "audio_duration_us": 1_172_083, "tail_pad_us": 120_000,
            "segment_us": 1_292_083, "video_speed": 1.0,
            "material_duration_us": 4_984_000,
        }
        issues = tc.check_invariants([item], fps=30)
        self.assertEqual([i["code"] for i in issues], [tc.INV_DURATION_MISMATCH])
        self.assertIn("变速", issues[0]["message"])

    def test_speed_conversion_within_one_frame_tolerated(self):
        """源窗口 ÷ 速度 与段长相差 ≤1 帧 → 放行（容差只属于这条换算等式）。"""
        item = {
            "index": 2, "video": "b.MOV",
            "source_start_us": 0, "source_end_us": 1_232_083,
            "audio_duration_us": 1_200_000, "tail_pad_us": 0,
            # 1 帧 = 33_333µs @30fps；窗口换算 1_232_083 vs 段长 1_232_084 差 1µs
            "segment_us": 1_232_084, "video_speed": 1.0,
            "material_duration_us": 4_984_000,
        }
        self.assertEqual(tc.check_invariants([item], fps=30), [])

    def test_sub_frame_material_fails(self):
        """极短素材（窗口不足一帧）→ 连一个可用切片都成不了，直接拦。"""
        item = {
            "index": 3, "video": "tiny.MOV",
            "source_start_us": 0, "source_end_us": 20_000,
            "audio_duration_us": 0, "tail_pad_us": 20_000,
            "segment_us": 20_000, "video_speed": 1.0,
            "material_duration_us": 20_000,
        }
        issues = tc.check_invariants([item], fps=30)
        self.assertEqual([i["code"] for i in issues],
                         [tc.INV_WINDOW_OUT_OF_BOUNDS])
        self.assertIn("不足一帧", issues[0]["message"])

    def test_unified_material_lets_boundary_solution_pass(self):
        """1.3.27.1 修好后，贴着边界的解（end == material）不越界、零容差通过。"""
        item = {
            "index": 1, "video": "a.MOV",
            "source_start_us": 3_757_917, "source_end_us": 4_984_000,
            "audio_duration_us": 1_112_083, "tail_pad_us": 120_000,
            "segment_us": 1_232_083, "video_duration_us": 4_984_000,
            "video_speed": 1.0,
            "material_duration_us": 4_984_000,
        }
        self.assertEqual(tc.check_invariants([item], fps=30), [])

    def test_legacy_video_duration_fallback_still_enforced(self):
        """旧形态条目（只有 video_duration_us、无 material_duration_us）保持兜底校验。"""
        item = {
            "index": 4, "video": "c.MOV",
            "source_start_us": 0, "source_end_us": 12_500_000,
            "audio_duration_us": 12_380_000, "tail_pad_us": 120_000,
            "segment_us": 12_500_000, "video_duration_us": 12_000_000,
            "video_speed": 1.0,
        }
        issues = tc.check_invariants([item], fps=30)
        self.assertIn(tc.INV_WINDOW_OUT_OF_BOUNDS, [i["code"] for i in issues])


if __name__ == "__main__":
    unittest.main(verbosity=2)
