import os
import json
import re
import uuid
import pyJianYingDraft as draft

class MockVideoMaterial(draft.VideoMaterial):
    def __init__(self, material_id, duration, name, path):
        self.material_id = material_id
        self.duration = duration
        self.material_name = name
        self.path = path
    def serialize(self):
        return {
            "id": self.material_id, "type": "video", "name": self.material_name, "path": self.path,
            "duration": self.duration, "material_id": self.material_id
        }

class MockAudioMaterial(draft.AudioMaterial):
    def __init__(self, material_id, duration, name, path):
        self.material_id = material_id
        self.duration = duration
        self.material_name = name
        self.path = path
    def serialize(self):
        return {
            "id": self.material_id, "type": "audio", "name": self.material_name, "path": self.path,
            "duration": self.duration, "material_id": self.material_id
        }

class CompoundSegment:
    def __init__(self, material_id, target_timerange):
        self.material_id = material_id
        self.target_timerange = target_timerange
    def serialize(self):
        return {
             "id": str(uuid.uuid4()).upper(), "material_id": self.material_id,
             "target_timerange": self.target_timerange.serialize(),
             "render_index": 0, "type": "video"
        }

class MockingOpsMixin:
    """
    JyProject 的协议补丁与伪物料 Mixin。
    """
    # 剪映的调色资源包由宿主通过 JY_INSTALL_ROOT 或
    # JY_INSTALL_SEARCH_ROOTS 提供；发布包不携带机器专属安装路径。
    _ADJUST_BUNDLE_RELATIVE = ("Resources", "DefaultAdjustBundle", "combine_adjust")

    def _resolve_adjust_bundle_path(self) -> str:
        """定位剪映的 combine_adjust 资源包；找不到时回退到旧常量并告警。"""
        home = os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        configured = os.environ.get("JY_INSTALL_SEARCH_ROOTS", "")
        roots = [os.environ.get("JY_INSTALL_ROOT", "")]
        roots.extend(item for item in configured.split(os.pathsep) if item)
        roots.extend([
            os.path.join(local, "JianyingPro"),
            os.path.join(local, "JianyingPro", "Apps"),
        ])
        candidates = []
        for root in roots:
            if not root:
                continue
            # 受管版本按 <root>/<version>/JianyingPro.exe 布局，资源包在其同级。
            candidates.append(os.path.join(root, *self._ADJUST_BUNDLE_RELATIVE))
            try:
                children = sorted(os.listdir(root), reverse=True)
            except OSError:
                continue
            for child in children:
                if not re.match(r"^\d+(\.\d+)+$", child):
                    continue
                candidates.append(os.path.join(root, child, *self._ADJUST_BUNDLE_RELATIVE))
        # 用户数据目录里也有一份（剪映自己下载的），作为兜底
        candidates.append(os.path.join(
            local, "JianyingPro", "User Data", *self._ADJUST_BUNDLE_RELATIVE))
        for path in candidates:
            if os.path.isdir(path):
                return path.replace("\\", "/")
        fallback = "JY_INSTALL_ROOT_UNRESOLVED/Resources/DefaultAdjustBundle/combine_adjust"
        print(f"⚠️ 未找到剪映调色资源包，回退到旧路径（可能失效）: {fallback}")
        return fallback

    def _force_activate_adjustments(self):
        content_path = os.path.join(self.root, self.name, "draft_info.json")
        if not os.path.exists(content_path):
            content_path = os.path.join(self.root, self.name, "draft_content.json")
        if not os.path.exists(content_path): return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            has_modified = False
            materials = data.setdefault("materials", {})
            all_effects = materials.setdefault("effects", [])

            PROP_MAP = {"KFTypeBrightness": "brightness", "KFTypeContrast": "contrast", "KFTypeSaturation": "saturation"}
            jy_res_path = self._resolve_adjust_bundle_path()

            for track in data.get("tracks", []):
                for seg in track.get("segments", []):
                    kfs = seg.get("common_keyframes", [])
                    active_props = [kf.get("property_type") for kf in kfs if kf.get("property_type") in PROP_MAP]

                    if active_props:
                        seg["enable_adjust"] = True
                        seg["enable_color_correct_adjust"] = True
                        refs = seg.setdefault("extra_material_refs", [])

                        for prop in active_props:
                            mat_type = PROP_MAP[prop]
                            if not any(m.get("type") == mat_type and m["id"] in refs for m in all_effects):
                                new_id = str(uuid.uuid4()).upper()
                                shadow_mat = {
                                    "type": mat_type, "value": 0.0, "path": jy_res_path, "id": new_id,
                                    "apply_target_type": 0, "platform": "all", "source_platform": 0, "version": "v2"
                                }
                                all_effects.append(shadow_mat)
                                refs.append(new_id)
                                has_modified = True

            if has_modified:
                with open(content_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Force activation failed: {e}")

    def _patch_cloud_material_ids(self):
        if not self._cloud_audio_patches and not self._cloud_text_patches: return
        content_path = os.path.join(self.root, self.name, "draft_info.json")
        if not os.path.exists(content_path):
            content_path = os.path.join(self.root, self.name, "draft_content.json")
        if not os.path.exists(content_path): return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            has_modified = False
            materials = data.get("materials", {})
            audios = materials.get("audios", [])
            for mat in audios:
                path = mat.get("path", "")
                for dummy_path, patch_info in self._cloud_audio_patches.items():
                    if dummy_path in path:
                        if patch_info["type"] == "music":
                            mat["music_id"] = patch_info["id"]
                            mat["type"] = "music"
                            has_modified = True

            if has_modified:
                with open(content_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
