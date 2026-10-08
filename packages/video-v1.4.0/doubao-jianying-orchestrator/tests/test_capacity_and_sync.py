import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import capacity, cli, draft_safety, migration, platform_env, script_polish
from orchestrator.engine import Clip, OrchestrationEngine


class CapacityTests(unittest.TestCase):
    def test_soft_memory_pressure_continues_with_lowest_profile(self):
        profile = {"name": "background_friendly", "analysis_workers": 2,
                   "tts_concurrency": 3, "staging_workers": 2, "process_priority": "below_normal"}
        with mock.patch("orchestrator.engine.capacity.resource_snapshot",
                        return_value={"memory_available_bytes": 768 * 1024 * 1024}), \
             mock.patch("orchestrator.engine.capacity.memory_pressure", return_value=None), \
             mock.patch("orchestrator.engine.time.sleep"):
            engine = OrchestrationEngine({"execution_profile": "background_friendly"},
                                         Path("."), Path("."), capacity_guard=False)
            engine.profile = type(engine.profile)(**profile)
            engine._drafts_root = Path(".")
            self.assertIsNone(engine._background_pressure("test"))
            self.assertTrue(engine._resource_degraded)
            self.assertEqual(engine.profile.analysis_workers, 1)

    def test_critical_memory_pressure_pauses(self):
        with mock.patch("orchestrator.engine.capacity.resource_snapshot",
                        return_value={"memory_available_bytes": 128 * 1024 * 1024}), \
             mock.patch("orchestrator.engine.capacity.memory_pressure",
                        return_value={"status": "SYSTEM_RESOURCE_PRESSURE", "sample": {}}), \
             mock.patch("orchestrator.engine.time.sleep"):
            engine = OrchestrationEngine({"execution_profile": "background_friendly"},
                                         Path("."), Path("."), capacity_guard=False)
            engine._drafts_root = Path(".")
            result = engine._background_pressure("test")
            self.assertEqual(result["status"], "SYSTEM_RESOURCE_PRESSURE")

    def test_critical_memory_pressure_also_protects_performance_profile(self):
        with mock.patch("orchestrator.engine.capacity.resource_snapshot",
                        return_value={"memory_available_bytes": 128 * 1024 * 1024}), \
             mock.patch("orchestrator.engine.capacity.memory_pressure",
                        return_value={"status": "SYSTEM_RESOURCE_PRESSURE", "sample": {}}):
            engine = OrchestrationEngine({"execution_profile": "performance"},
                                         Path("."), Path("."), capacity_guard=False)
            engine._drafts_root = Path(".")
            result = engine._background_pressure("test")
            self.assertEqual(result["status"], "SYSTEM_RESOURCE_PRESSURE")
    def test_package_receipt_matches_named_metadata_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "SKILL_PACKAGE.json"
            metadata.write_text('{"version":"v1.3.4"}', encoding="utf-8")
            digest = __import__("hashlib").sha256(metadata.read_bytes()).hexdigest()
            (root / "PACKAGE_CONTENTS.sha256").write_text(
                "deadbeef  DELIVERY_REPORT_v1.3.4.md\n"
                f"{digest}  SKILL_PACKAGE.json\n", encoding="ascii")
            with mock.patch.object(platform_env, "SKILLS_ROOT", root), \
                 mock.patch.object(platform_env, "SKILL_DIR", root / "doubao-jianying-orchestrator"), \
                 mock.patch.object(platform_env, "IS_WIN", False):
                report = platform_env.package_installation_report()
            self.assertTrue(report["integrity_ok"])

    def test_windows_install_discovery_allows_no_explicit_override(self):
        with mock.patch.object(platform_env, "IS_WIN", True), \
             mock.patch.dict(platform_env.os.environ, {"JY_JIANYING_INSTALL_ROOT": ""}, clear=False):
            self.assertIsInstance(platform_env._jianying_install_roots(), list)

    def test_versioned_windows_install_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "11.3.0.14362" / "JianyingPro.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"binary")
            with mock.patch.object(platform_env, "IS_WIN", True), \
                 mock.patch.object(platform_env, "IS_MAC", False), \
                 mock.patch.object(platform_env, "_jianying_install_roots", return_value=[root]), \
                 mock.patch.object(platform_env, "_windows_file_version", return_value="11.3.0.14362"), \
                 mock.patch.object(platform_env, "_jianying_process_count", return_value=0):
                report = platform_env.jianying_installation_report()
            self.assertEqual(report["executable"], str(exe.resolve()))
            self.assertEqual(report["version"], "11.3.0.14362")

    def test_windows_custom_draft_library_wins_over_local_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "JianyingPro Drafts"
            root.mkdir()
            output = "\n    currentCustomDraftPath    REG_SZ    " + str(root) + "\n"
            completed = mock.Mock(stdout=output)
            with mock.patch.object(platform_env, "IS_WIN", True), \
                 mock.patch.object(platform_env, "IS_MAC", False), \
                 mock.patch.object(platform_env.subprocess, "run", return_value=completed), \
                 mock.patch.dict(platform_env.os.environ,
                                 {"JY_DRAFTS_ROOT": "", "JY_PROJECTS_ROOT": ""}, clear=False):
                selected, source = platform_env.find_drafts_root_detail()
            self.assertEqual(selected, root.resolve())
            self.assertEqual(source, "windows_registry:currentCustomDraftPath")

    def test_draft_root_explicit_override_has_highest_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "operator-selected"
            root.mkdir()
            with mock.patch.dict(platform_env.os.environ, {"JY_DRAFTS_ROOT": str(root)}, clear=False):
                selected, source = platform_env.find_drafts_root_detail()
            self.assertEqual(selected, root.resolve())
            self.assertEqual(source, "env:JY_DRAFTS_ROOT")

    def test_sources_are_deduplicated_by_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "a.mov"
            video.write_bytes(b"video")
            summary = capacity.summarize_sources([video, root / "." / "a.mov"])
            self.assertEqual(summary["unique_source_count"], 1)
            self.assertEqual(summary["total_bytes"], 5)

    def test_profile_uses_eighty_percent_with_minimum_thirteen(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = capacity.create_profile(last_passing_count=26, storage_path=Path(tmp), samples=[])
            self.assertEqual(profile["safe_source_limit"], 20)
            minimum = capacity.create_profile(last_passing_count=13, storage_path=Path(tmp), samples=[])
            self.assertEqual(minimum["safe_source_limit"], 13)

    def test_admission_blocks_missing_profile_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "a.mov"
            video.write_bytes(b"video")
            with mock.patch("orchestrator.capacity.default_profile_path", return_value=root / "missing.json"), \
                 mock.patch("orchestrator.capacity._memory_bytes", return_value=(8 * 1024**3, 4 * 1024**3)):
                blocker, detail = capacity.admission_result([video], storage_path=root)
            self.assertEqual(blocker["status"], "CAPACITY_CALIBRATION_REQUIRED")
            self.assertEqual(detail["sources"]["unique_source_count"], 1)

    def test_admission_blocks_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            videos = []
            for name in ("a.mov", "b.mov"):
                path = root / name
                path.write_bytes(name.encode())
                videos.append(path)
            host = capacity.host_fingerprint(root)
            profile = {"schema": capacity.PROFILE_VERSION, "host": host, "safe_source_limit": 1}
            profile_path = root / "host_capacity.json"
            capacity.save_profile(profile, profile_path)
            with mock.patch("orchestrator.capacity._memory_bytes", return_value=(8 * 1024**3, 4 * 1024**3)), \
                 mock.patch("orchestrator.capacity.profile_state", return_value=("ready", {})):
                blocker, _ = capacity.admission_result(videos, storage_path=root, profile_path=profile_path)
            self.assertEqual(blocker["status"], "MATERIAL_BATCH_LIMIT_EXCEEDED")
            self.assertEqual(blocker["safe_source_limit"], 1)

    def test_upload_then_capacity_check_keeps_upload_and_blocks_before_tts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            videos = []
            for name in ("a.mov", "b.mov"):
                path = root / name
                path.write_bytes(name.encode())
                videos.append(path)
            profile = {"schema": capacity.PROFILE_VERSION,
                       "host": capacity.host_fingerprint(root),
                       "safe_source_limit": 1}
            profile_path = root / "host_capacity.json"
            capacity.save_profile(profile, profile_path)
            eng = OrchestrationEngine({"allow_silent": True}, root, root / "report",
                                      capacity_guard=True)
            eng._drafts_root = root
            clips = [Clip(video=str(v), source_start=0, duration=1, audio_mode="mute") for v in videos]
            with mock.patch("orchestrator.capacity.default_profile_path", return_value=profile_path), \
                 mock.patch("orchestrator.capacity._memory_bytes", return_value=(8 * 1024**3, 4 * 1024**3)), \
                 mock.patch("orchestrator.capacity.profile_state", return_value=("ready", {"fingerprint": "test"})):
                blocker = eng._upload_videos_and_check_capacity(clips)
            self.assertEqual(blocker["status"], "MATERIAL_BATCH_LIMIT_EXCEEDED")
            self.assertIn("上传数量单次只支持最多传 1 条", blocker["message"])
            self.assertTrue((root / "report" / "uploads" / "videos" / "a.mov").exists())


class AudioSyncTests(unittest.TestCase):
    def _validate_audio_end(self, *, track_name: str, audio_start: int, audio_duration: int):
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp)
            payload = {
                "materials": {"videos": [], "audios": []},
                "tracks": [
                    {"type": "video", "name": "Video_BRoll", "segments": [
                        {"target_timerange": {"start": 0, "duration": 1_000_000}}]},
                    {"type": "audio", "name": track_name, "segments": [
                        {"target_timerange": {"start": audio_start, "duration": audio_duration}}]},
                ],
            }
            (draft / "draft_info.json").write_text(json.dumps(payload), encoding="utf-8")
            return draft_safety.validate_audio_video_bounds(draft)

    def test_narration_after_picture_is_rejected(self):
        issues = self._validate_audio_end(track_name="Narration", audio_start=0,
                                          audio_duration=1_000_001)
        self.assertEqual(len(issues), 1)
        self.assertIn("超过画面结束", issues[0])

    def test_external_audio_after_picture_is_rejected(self):
        issues = self._validate_audio_end(track_name="External Audio", audio_start=100_000,
                                          audio_duration=900_001)
        self.assertEqual(len(issues), 1)
        self.assertIn("超过画面结束", issues[0])

    def test_bgm_after_full_timeline_is_rejected(self):
        issues = self._validate_audio_end(track_name="BGM", audio_start=0,
                                          audio_duration=1_000_001)
        self.assertEqual(len(issues), 1)
        self.assertIn("超过画面结束", issues[0])

    def test_audio_ending_with_picture_is_allowed(self):
        self.assertEqual(self._validate_audio_end(track_name="BGM", audio_start=0,
                                                  audio_duration=1_000_000), [])


class DraftRootSafetyTests(unittest.TestCase):
    def test_existing_drafts_root_is_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(draft_safety.drafts_root_access_issues(Path(tmp)), [])

    def test_file_is_not_accepted_as_drafts_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-a-directory"
            path.write_text("x", encoding="utf-8")
            issues = draft_safety.drafts_root_access_issues(path)
            self.assertEqual(len(issues), 1)
            self.assertIn("不是目录", issues[0])

    def test_new_draft_sidecar_matches_content_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            editor_root = Path(__file__).resolve().parents[2] / "jianying-editor"
            sys.path.insert(0, str(editor_root / "scripts"))
            from jy_wrapper import JyProject

            project = JyProject("metadata_probe", drafts_root=tmp, overwrite=True)
            project.save()
            draft = Path(project.draft_dir)
            info = json.loads((draft / "draft_info.json").read_text(encoding="utf-8"))
            meta = json.loads((draft / "draft_meta_info.json").read_text(encoding="utf-8"))
            self.assertTrue(info["id"])
            self.assertEqual(info["name"], "metadata_probe")
            self.assertEqual(meta["draft_id"], info["id"])
            self.assertEqual(meta["draft_name"], info["name"])
            self.assertEqual(Path(meta["draft_fold_path"]).resolve(), draft.resolve())
            self.assertEqual(draft_safety.post_write_validate(draft), [])


class TrackContractTests(unittest.TestCase):
    def _draft(self, root: Path, *, standard_text: bool, include_bgm: bool) -> Path:
        draft = root / "draft"
        draft.mkdir()
        video = root / "video.mov"
        narration = root / "narration.ogg"
        bgm = root / "bgm.m4a"
        for path in (video, narration, bgm):
            path.write_bytes(b"media")
        video_id, narration_id, bgm_id, text_id = "v", "n", "b", "t"
        if standard_text:
            text_material = {"id": text_id, "type": "text",
                             "content": json.dumps({"text": "测试字幕"}, ensure_ascii=False)}
        else:
            text_material = {"id": text_id, "type": "title", "text": "测试字幕",
                             "text_style": {"font_size": 12}}
        materials = {
            "videos": [{"id": video_id, "type": "video", "path": str(video)}],
            "audios": [
                {"id": narration_id, "type": "extract_music", "path": str(narration)},
                {"id": bgm_id, "type": "extract_music", "path": str(bgm)},
            ],
            "texts": [text_material],
        }
        one = lambda mid: {"material_id": mid,
                           "target_timerange": {"start": 0, "duration": 1_000_000}}
        tracks = [
            {"type": "video", "name": "Video_BRoll", "segments": [one(video_id)]},
            {"type": "audio", "name": "Narration", "segments": [one(narration_id)]},
            {"type": "text", "name": "Subtitles", "segments": [one(text_id)]},
        ]
        if include_bgm:
            tracks.append({"type": "audio", "name": "BGM", "segments": [one(bgm_id)]})
        payload = {"id": "draft-id", "name": "draft", "materials": materials,
                   "tracks": tracks}
        (draft / "draft_info.json").write_text(json.dumps(payload), encoding="utf-8")
        (draft / "draft_meta_info.json").write_text(json.dumps({
            "draft_id": "draft-id", "draft_name": "draft",
            "draft_fold_path": str(draft),
        }), encoding="utf-8")
        return draft

    def test_hand_written_title_text_without_bgm_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = self._draft(Path(tmp), standard_text=False, include_bgm=False)
            issues = draft_safety.post_write_validate(draft, expected_tracks={
                "video_segments": 1, "narration_segments": 1,
                "subtitle_segments": 1, "bgm_required": True, "bgm_segments": 1,
            })
            joined = " | ".join(issues)
            self.assertIn("BGM轨道片段数不符", joined)
            self.assertIn("字幕第 1 条素材类型不是标准", joined)
            self.assertIn("缺少可解析的 content.text", joined)

    def test_complete_standard_four_track_draft_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = self._draft(Path(tmp), standard_text=True, include_bgm=True)
            self.assertEqual(draft_safety.post_write_validate(draft, expected_tracks={
                "video_segments": 1, "narration_segments": 1,
                "subtitle_segments": 1, "bgm_required": True, "bgm_segments": 1,
            }), [])


class AudioSelectionGateTests(unittest.TestCase):
    def test_normal_mode_requires_explicit_confirmation_even_when_values_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eng = OrchestrationEngine({
                "voice": "female_sweet",
                "bgm": {"music_id": "music-1"},
            }, root, root / "report", capacity_guard=False)
            clip = Clip(video=str(root / "video.mov"), source_start=0,
                        duration=1, text="测试口播", audio_mode="tts")
            with mock.patch("orchestrator.engine.recommend_voices", return_value=[]), \
                 mock.patch("orchestrator.engine.voice_tts.generate_audition", return_value=[]), \
                 mock.patch("orchestrator.engine.bgm_selector.recommend_bgm",
                            return_value=[{"music_id": "music-1", "url": "https://example.test/a.m4a"}]), \
                 mock.patch("orchestrator.engine.bgm_selector.prepare_preview_tracks",
                            return_value=[{"music_id": "music-1", "preview_file": "/tmp/bgm.mp3",
                                           "preview_ready": True}]), \
                 mock.patch.object(eng, "_event"):
                result = eng.audio_selection_gate([clip])
            self.assertEqual(result["status"], "AUDIO_SELECTION_REQUIRED")
            self.assertEqual(result["reply_format"], "音色 <序号>，BGM <序号>")

    def test_confirmed_normal_mode_can_pass_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eng = OrchestrationEngine({
                "voice": "female_sweet",
                "bgm": {"music_id": "music-1"},
                "audio_selection_confirmed": True,
            }, root, root / "report", capacity_guard=False)
            clip = Clip(video=str(root / "video.mov"), source_start=0,
                        duration=1, text="测试口播", audio_mode="tts")
            self.assertIsNone(eng.audio_selection_gate([clip]))

    def test_bgm_without_direct_preview_is_a_blocking_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eng = OrchestrationEngine({}, root, root / "report", capacity_guard=False)
            clip = Clip(video=str(root / "video.mov"), source_start=0,
                        duration=1, text="测试口播", audio_mode="tts")
            with mock.patch("orchestrator.engine.recommend_voices", return_value=[]), \
                 mock.patch("orchestrator.engine.voice_tts.generate_audition", return_value=[]), \
                 mock.patch("orchestrator.engine.bgm_selector.recommend_bgm",
                            return_value=[{"music_id": "no-url", "url": ""}]), \
                 mock.patch.object(eng, "_event"):
                result = eng.audio_selection_gate([clip])
            self.assertEqual(result["status"], "BGM_PREVIEW_UNAVAILABLE")

    def test_selection_gate_runs_before_video_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eng = OrchestrationEngine({"voice": "female_sweet", "bgm": {"music_id": "music-1"}},
                                      root, root / "report", capacity_guard=False)
            clip = Clip(video=str(root / "video.mov"), source_start=0,
                        duration=1, text="测试口播", audio_mode="tts")
            gate = {"status": "AUDIO_SELECTION_REQUIRED"}
            with mock.patch.object(eng, "self_check", return_value=None), \
                 mock.patch.object(eng, "mode_confirmation_gate", return_value=None), \
                 mock.patch.object(eng, "build_clips", return_value=[clip]), \
                 mock.patch.object(eng, "voiceover_gate", return_value=None), \
                 mock.patch.object(eng, "audio_selection_gate", return_value=gate), \
                 mock.patch("orchestrator.engine.find_drafts_root", return_value=root), \
                 mock.patch.object(eng, "_upload_videos_and_check_capacity",
                                   side_effect=AssertionError("selection must precede upload")) as upload:
                result = eng.run()
            self.assertEqual(result, gate)
            upload.assert_not_called()


class OutputContractTests(unittest.TestCase):
    def test_success_output_is_exactly_four_fields(self):
        compact = cli._compact_build_result({
            "status": "SUCCESS", "draft_name": "demo",
            "draft_path": "/missing", "total_s": 9.9,
            "warnings": ["hidden"], "task_log": "/task",
        })
        self.assertEqual(list(compact), ["草稿名称", "草稿路径", "草稿文件", "生成日志"])


class MigrationTests(unittest.TestCase):
    def test_plain_draft_does_not_enter_migration_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "plain"
            draft.mkdir()
            (draft / "draft_info.json").write_text("{}", encoding="utf-8")
            result = migration.migrate_encrypted(draft, "copy")
            self.assertEqual(result["status"], "MIGRATION_NOT_REQUIRED")

    def test_encrypted_draft_requires_official_client(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(migration, "IS_WIN", False), \
             mock.patch.object(migration, "environment_report", return_value={}), \
             mock.patch.object(migration, "find_drafts_root", return_value=Path(tmp) / "drafts"):
            source = Path(tmp) / "encrypted"
            source.mkdir()
            (source / "draft_info.json").write_bytes(b"AES-CIPHERTEXT")
            result = migration.migrate_encrypted(source, "copy")
            self.assertEqual(result["status"], "OFFICIAL_COPY_UI_UNAVAILABLE")
            self.assertIn("crypto_key_store.dat", " ".join(result["instructions"]))


class IntelligentModeTests(unittest.TestCase):
    def test_smart_phrase_without_context_returns_short_prompt(self):
        with mock.patch.object(cli, "_emit", side_effect=lambda obj, code=0: (obj, code)):
            result = cli.main(["智能编排"])
        self.assertEqual(result[0]["status"], "INPUT_REQUIRED")
        self.assertEqual(result[0]["mode"], "intelligent")

    def test_storyboard_score_is_neutral_for_ordinary_material(self):
        score = script_polish.score_storyboard_shot("展示浴巾细节", duration_s=2.0,
                                                   video_name="bath-towel.MOV")
        self.assertGreaterEqual(score["score"], 0.7)
        self.assertEqual(score["adjustment"], "none")

    def test_storyboard_score_is_explainable_for_short_silent_shot(self):
        score = script_polish.score_storyboard_shot("", duration_s=0.4,
                                                   video_name="unknown.MOV")
        self.assertIn("未识别明确主体", score["reasons"])
        self.assertIn("镜头时长过短", score["reasons"])
        self.assertEqual(score["adjustment"], "保留素材，仅优化时长/转场/口播匹配")


if __name__ == "__main__":
    unittest.main()
