"""
JianYing Editor Skill - High Level Wrapper (Mixin Based)
旨在解决路径依赖、API 复杂度及严格校验问题。
"""

import os
import sys
import uuid
import json
import time
import tempfile
from typing import Union, Optional

# 环境初始化
from utils.env_setup import setup_env
setup_env()

# 导入工具函数
from utils.constants import SYNONYMS
from utils.formatters import (
    resolve_enum_with_synonyms, format_srt_time, safe_tim,
    get_duration_ffprobe_cached, get_default_drafts_root, get_all_drafts
)

# 导入基类与 Mixins
from core.project_base import JyProjectBase
from core.media_ops import MediaOpsMixin
from core.text_ops import TextOpsMixin
from core.vfx_ops import VfxOpsMixin
from core.mocking_ops import MockingOpsMixin

try:
    import pyJianYingDraft as draft
    from pyJianYingDraft import VideoSceneEffectType, TransitionType
except ImportError:
    draft = None

class JyProject(JyProjectBase, MediaOpsMixin, TextOpsMixin, VfxOpsMixin, MockingOpsMixin):
    """
    高层封装工程类。通过多重继承 Mixins 实现功能解耦。
    """
    def _resolve_enum(self, enum_cls, name: str):
        return resolve_enum_with_synonyms(enum_cls, name, SYNONYMS)

    def add_clip(self, media_path: str, source_start: Union[str, int], duration: Union[str, int],
                 target_start: Union[str, int] = None, track_name: str = "VideoTrack", **kwargs):
        """高层剪辑接口：从媒体指定位置裁剪指定长度，并放入轨道。"""
        if target_start is None:
            target_start = self.get_track_duration(track_name)
        return self.add_media_safe(media_path, target_start, duration, track_name, source_start=source_start, **kwargs)

    def save(self):
        """保存并执行质检报告。"""
        self.script.save()
        self._patch_cloud_material_ids()
        self._force_activate_adjustments()

        draft_path = os.path.join(self.root, self.name)
        # Keep the sidecar identity in lockstep with draft_info.json. Older
        # versions copied a placeholder metadata file, which could make the
        # JianYing library list the folder but fail to open it.
        meta_path = os.path.join(draft_path, "draft_meta_info.json")
        info_path = os.path.join(draft_path, "draft_info.json")
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta.update({
                "draft_id": info.get("id", meta.get("draft_id", "")),
                "draft_name": self.name,
                "draft_fold_path": os.path.abspath(draft_path),
                "draft_root_path": os.path.abspath(self.root),
                "draft_json_file": os.path.abspath(info_path),
                "draft_cover": "draft_cover.jpg" if os.path.exists(os.path.join(draft_path, "draft_cover.jpg")) else "",
                "tm_duration": int(info.get("duration", 0) or 0),
                "tm_draft_modified": int(time.time() * 1e6),
            })
            meta_parent = os.path.dirname(os.path.abspath(meta_path))
            fd, temp_meta = tempfile.mkstemp(prefix=".draft_meta.", suffix=".tmp", dir=meta_parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_meta, meta_path)
            finally:
                if os.path.exists(temp_meta):
                    os.unlink(temp_meta)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(f"草稿元数据同步失败: {exc}") from exc
        if os.path.exists(draft_path):
            os.utime(draft_path, None)
        print(f"✅ Project '{self.name}' saved and patched.")
        return {"status": "SUCCESS", "draft_path": draft_path}

# 导出工具函数以便向下兼容
__all__ = ["JyProject", "get_default_drafts_root", "get_all_drafts", "safe_tim", "format_srt_time"]

if __name__ == "__main__":
    # 测试代码
    try:
        project = JyProject("Refactor_Test_Project", overwrite=True)
        print("🚀 Refactored JyProject initialized successfully.")
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
