import os
from typing import Union

import pyJianYingDraft as draft
from pyJianYingDraft import trange
from pyJianYingDraft.exceptions import SegmentOverlap
from utils.formatters import get_duration_ffprobe_cached, safe_tim
from utils.media_normalizer import normalize_webm_for_jianying


class MediaOpsMixin:
    """
    JyProject 的媒体处理 Mixin。
    """

    def add_media_safe(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = None,
        source_start: Union[str, int] = 0,
        speed: Union[float, None] = None,
        **kwargs,
    ):
        if not os.path.exists(media_path):
            print(f"❌ Media Missing: {media_path}")
            return None

        ext = os.path.splitext(media_path)[1].lower()
        # Normalize WEBM to a stable MP4 profile before import to avoid parser incompatibilities.
        if ext == ".webm":
            normalized_path = normalize_webm_for_jianying(media_path)
            if not normalized_path:
                print(f"❌ WEBM normalization failed: {media_path}")
                return None
            media_path = normalized_path
            ext = ".mp4"

        if ext in [".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"]:
            # `source_start` 必须一起转交：音频也走 `add_audio_safe` 的
            # `source_timerange`（多镜组合靠它把同一句配音切给不同镜头）。
            # 这里曾经把它丢掉 —— 调用方写了 source_start 也会被静默当 0，
            # 参数存在、路径存在、结果错。
            return self.add_audio_safe(
                media_path, start_time, duration, track_name or "AudioTrack",
                source_start=source_start,
            )

        return self._add_video_safe(
            media_path, start_time, duration, track_name or "VideoTrack",
            source_start=source_start, speed=speed
        )

    def add_audio_safe(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = "AudioTrack",
        source_start: Union[str, int] = 0,
        **kwargs,
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.audio, track_name)

        try:
            mat = draft.AudioMaterial(media_path)
            phys_duration = mat.duration
        except Exception:
            return None

        start_us = safe_tim(start_time)
        src_start_us = safe_tim(source_start)
        # 素材侧取用长度：从 src_start_us 起还剩多少可用（默认 0 时与旧行为完全一致）。
        # 多镜组合（v1.3.27 B 步）要把**同一句配音**分给多个镜头，每个镜头取配音的
        # 不同时间片，所以必须能把 source_timerange 的起点挪开 —— 旧版硬写 0，
        # 于是第二段必然从头再播一遍配音，音画互相打架。
        avail_us = max(phys_duration - src_start_us, 0)
        actual_duration = self._calculate_duration(duration, avail_us)

        seg = draft.AudioSegment(
            mat, trange(start_us, actual_duration),
            source_timerange=trange(src_start_us, actual_duration),
        )
        target_track = self._find_available_audio_track_name(track_name, seg)
        self.script.add_segment(seg, target_track)
        return seg

    def _add_video_safe(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = "VideoTrack",
        source_start: Union[str, int] = 0,
        speed: Union[float, None] = None,
        **kwargs,
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.video, track_name)

        try:
            fallback_duration_us = safe_tim(duration) * 10 if duration else None
            mat = draft.VideoMaterial(media_path, duration=fallback_duration_us)
            phys_duration = mat.duration
        except Exception:
            return None

        if not phys_duration or phys_duration <= 0:
            ff_dur = get_duration_ffprobe_cached(media_path)
            if ff_dur > 0:
                phys_duration = int(ff_dur * 1000000)
                mat.duration = phys_duration

        if not self._explicit_res and not self._first_video_resolved:
            if hasattr(mat, "width") and mat.width > 0:
                self.script.width, self.script.height = mat.width, mat.height
            self._first_video_resolved = True

        start_us = safe_tim(start_time)
        src_start_us = safe_tim(source_start)

        if speed is not None:
            # ── 显式变速通路（v1.3.27：由时序契约 solver 给出解，这里只执行）──
            #
            # 语义必须写清楚，否则素材长度会被悄悄改掉：
            #   `duration`（= segment_us）= 片段在**输出时间线**上占多长，由配音决定；
            #   `source_timerange`  = 从素材上**取多长**，即 segment_us × speed；
            #   `target_timerange`  = 输出时间线窗口，由 pyJianYingDraft 用
            #                         round(source_timerange.duration / speed) 回算。
            #
            # 注意这里**不做 min() 静默截断**（用户定案第 7 条）：素材不够长时
            # 少取一截，画面会跟不上配音；正确做法是让上游的窗口求解重新算，
            # 因此这里如实报错而不是悄悄改短。旧通路（speed=None）保持原行为不动。
            segment_us = safe_tim(duration) if duration else 0
            speed = float(speed)
            if speed <= 0:
                raise ValueError(f"非法变速 {speed}（必须 > 0）")
            if segment_us <= 0:
                raise ValueError("显式变速通路必须同时给出输出时长 duration")
            material_us = int(round(segment_us * speed))
            if src_start_us + material_us > phys_duration:
                raise ValueError(
                    f"变速切片越出素材范围：source_start={src_start_us}µs + "
                    f"material={material_us}µs（segment={segment_us}µs × speed={speed}）"
                    f" > 素材时长 {phys_duration}µs；应由窗口求解重算，不得静默截断")
            seg = draft.VideoSegment(
                mat,
                trange(start_us, segment_us),
                source_timerange=trange(src_start_us, material_us),
                speed=speed,
            )
            self.script.add_segment(seg, track_name)
            return seg

        actual_duration = self._calculate_duration(duration, phys_duration - src_start_us)

        seg = draft.VideoSegment(
            mat,
            trange(start_us, actual_duration),
            source_timerange=trange(src_start_us, actual_duration),
        )
        self.script.add_segment(seg, track_name)
        return seg

    def add_cloud_media(
        self,
        query: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = None,
    ):
        cm = self.cloud_manager
        local_path = cm.download_asset(query)
        if not local_path:
            return None

        asset_info = cm.find_asset(query)
        db_type = str(asset_info.get("type", "")).lower() if asset_info else ""
        source_db = str(asset_info.get("source_db", "")).lower() if asset_info else ""
        is_audio_db = source_db in {"cloud_music_library.csv", "cloud_sound_effects.csv"}
        is_audio_type = any(
            k in db_type for k in ["music", "audio", "sound", "bgm", "音效", "歌曲", "歌"]
        )
        is_audio = is_audio_db or is_audio_type

        if is_audio:
            default_track = (
                "BGM"
                if source_db == "cloud_music_library.csv"
                or any(k in db_type for k in ["music", "bgm", "歌曲", "歌"])
                else "AudioTrack"
            )
            return self.add_audio_safe(
                local_path, start_time, duration, track_name or default_track
            )
        return self.add_media_safe(local_path, start_time, duration, track_name or "VideoTrack")

    def add_cloud_music(
        self,
        query: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        name: str = None,
        duration_s: float = None,
        track_name: str = "BGM",
    ):
        """
        通过 Cloud ID 添加云端音乐。直接注入 Material ID 补丁。
        """
        if start_time is None:
            start_time = self.get_track_duration(track_name)

        # 优先使用真实本地缓存文件，避免生成虚拟路径导致“媒体丢失”提示。
        # 若下载失败，再回退到旧的 mock 注入模式。
        local_path = self.cloud_manager.download_asset(query)
        if local_path and os.path.exists(local_path):
            seg = self.add_audio_safe(
                local_path, start_time=start_time, duration=duration, track_name=track_name
            )
            if seg is not None:
                return seg

        # 1. 如果没给 duration_s，尝试查表
        actual_duration_s = duration_s
        if not actual_duration_s:
            actual_duration_s = self.cloud_manager.get_asset_duration(query)

        if not actual_duration_s:
            print(f"⚠️ Warning: Duration for cloud music '{query}' not found. Using fallback 3.0s")
            actual_duration_s = 3.0

        final_dur_us = safe_tim(duration) if duration else int(actual_duration_s * 1000000)

        # 2. 注入 Patch
        dummy_path = (
            local_path
            if (local_path and os.path.exists(local_path))
            else f"cloud_music_{query}.mp3"
        )
        self._cloud_audio_patches[dummy_path] = {"id": query, "type": "music"}

        # 3. 使用 Mock 素材
        from core.mocking_ops import MockAudioMaterial

        mat = MockAudioMaterial(query, final_dur_us, name or f"CloudMusic_{query}", dummy_path)
        seg = draft.AudioSegment(
            mat,
            draft.Timerange(safe_tim(start_time), final_dur_us),
            source_timerange=draft.Timerange(0, final_dur_us),
        )

        self._ensure_track(draft.TrackType.audio, track_name)
        target_track = self._find_available_audio_track_name(track_name, seg)
        self.script.add_segment(seg, target_track)
        return seg

    def _find_available_audio_track_name(self, base_track_name: str, segment) -> str:
        preferred = base_track_name or "AudioTrack"
        if self._track_accepts_segment(preferred, segment):
            return preferred

        index = 1
        while True:
            candidate = f"{preferred}_{index}"
            self._ensure_track(draft.TrackType.audio, candidate)
            if self._track_accepts_segment(candidate, segment):
                return candidate
            index += 1

    def _track_accepts_segment(self, track_name: str, segment) -> bool:
        track = self.script.tracks.get(track_name)
        if track is None:
            return False
        try:
            for existing in track.segments:
                if existing.overlaps(segment):
                    return False
            return True
        except SegmentOverlap:
            return False

    def _ensure_track(self, track_type, track_name):
        """确保轨道存在，不存在则创建。"""
        if not track_name:
            return
        if track_name not in self.script.tracks:
            self.script.add_track(track_type, track_name)

    def _calculate_duration(self, req_dur, phys_dur_available):
        """旧通路的时长兜底：请求时长超出素材可用长度时取素材长度。

        **`min()` 只允许用在音频上** —— 音频没有速度概念，多要的部分本来就
        播不出来，取素材长度是唯一合理解。

        视频不要走这里。视频「素材不够长」是有语义的失败：要么该换镜头，
        要么在允许范围内变速（`_add_video_safe` 的显式变速通路，越界直接
        `raise`）。按素材长度悄悄截短，画面就会跟不上配音，而草稿本身
        看不出来（v1.3.27 之前的实际故障形态）。流水线里视频一律显式传
        `speed`，所以走不到这一支；`vfx_ops` / `text_ops` / `add_cloud_media`
        几个历史调用点仍是旧口径（只给起点、不关心精确时长），保持原行为。
        """
        if req_dur:
            req_us = safe_tim(req_dur)
            return min(req_us, phys_dur_available) if phys_dur_available > 0 else req_us
        return phys_dur_available
