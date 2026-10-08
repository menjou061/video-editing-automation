"""跨平台运行环境解析：定位 jianying-editor skill、ffmpeg、剪映草稿目录，检测剪映进程。

零第三方依赖，同时支持 macOS 与 Windows。所有路径解析都遵循同一优先级：
显式环境变量 > 相对本 Skill 的兄弟目录 > 各平台常见安装位置 > 兜底默认值。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import copy
import hashlib
import json
import threading
import tempfile
from pathlib import Path
from typing import Optional


# 本文件位于 <skills>/doubao-jianying-orchestrator/orchestrator/platform_env.py
SKILL_DIR = Path(__file__).resolve().parents[1]
SKILLS_ROOT = SKILL_DIR.parent

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
_ENV_CACHE: dict | None = None
_ENV_CACHE_LOCK = threading.Lock()


def _exe(name: str) -> str:
    return f"{name}.exe" if IS_WIN else name


def _existing(*paths: Path) -> Optional[Path]:
    for p in paths:
        try:
            if p and p.exists():
                return p.resolve()
        except OSError:
            continue
    return None


def find_jianying_editor_root() -> Path:
    """定位 jianying-editor skill 根目录（其下应有 scripts/jy_wrapper.py）。"""
    candidates = []
    env = os.environ.get("JY_SKILL_ROOT")
    if env:
        candidates.append(Path(env).expanduser())
    # 1) 同级 skills 目录下的兄弟 skill（部署后的默认布局）
    candidates.append(SKILLS_ROOT / "jianying-editor")
    # 2) 各平台常见 skills 根
    home = Path.home()
    if IS_MAC:
        candidates += [
            home / "Doubao" / "skills" / "jianying-editor",
            home / "Library" / "Application Support" / "Doubao" / "Profile 1"
            / ".doubao" / "agent_mode" / "workspace" / ".skills" / "jianying-editor",
        ]
    else:
        candidates += [
            home / "Doubao" / "skills" / "jianying-editor",
            Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
            / "Doubao" / "skills" / "jianying-editor",
            Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
            / "Doubao" / "skills" / "jianying-editor",
        ]
    found = _existing(*[c / "scripts" / "jy_wrapper.py" for c in candidates])
    if found:
        return found.parents[1]
    # 找不到也返回首选候选，由上层抛出带安装指引的错误
    return candidates[0]


def _skill_local_bin_dirs() -> list[Path]:
    """随 Skill 走的免安装绿色 ffmpeg 目录（不安装、不写注册表，适合公司禁装环境）。"""
    dirs = [SKILL_DIR / "bin"]
    dirs.append(SKILL_DIR / "bin" / ("win" if IS_WIN else "mac"))
    return dirs


def _jianying_install_roots() -> list[Path]:
    """剪映/CapCut 安装根（其运行时捆绑 FFmpeg 组件，可免安装复用）。"""
    if not IS_WIN:
        return []
    home = Path.home()
    program = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    configured = os.environ.get("JY_JIANYING_INSTALL_ROOT", "").strip()
    roots = [
        program / "JianyingPro", program_x86 / "JianyingPro",
        local / "JianyingPro", local / "JianyingPro" / "Apps",
        program / "CapCut", program_x86 / "CapCut",
        local / "CapCut", local / "CapCut" / "Apps",
        # The managed Windows image installs versioned builds here. Keep this
        # explicit and shallow: a recursive disk scan is too costly at startup.
        Path(r"D:\\JianyingPro"),
    ]
    if configured:
        roots.insert(0, Path(configured).expanduser())
    out, seen = [], set()
    for r in roots:
        try:
            if r.exists() and str(r.resolve()) not in seen:
                seen.add(str(r.resolve())); out.append(r)
        except OSError:
            continue
    return out


def _windows_file_version(executable: Path) -> str | None:
    """Read a Windows executable version without requiring PowerShell or psutil."""
    if not IS_WIN or not executable.exists():
        return None
    try:
        import ctypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(executable), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(executable), 0, size, buf):
            return None
        value = ctypes.c_void_p()
        value_len = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(value), ctypes.byref(value_len)):
            return None
        fixed = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        # VS_FIXEDFILEINFO: dwFileVersionMS/DWFileVersionLS are slots 4/5.
        ms, ls = fixed[4], fixed[5]
        return f"{ms >> 16}.{ms & 0xffff}.{ls >> 16}.{ls & 0xffff}"
    except Exception:
        return None


def _jianying_process_count() -> int:
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, encoding="utf-8", errors="replace")
            return sum(1 for line in (out.stdout or "").splitlines()
                       if any(name in line.lower() for name in
                              ("jianyingpro.exe", "capcut.exe", "videofusion.exe")))
        out = subprocess.run(["pgrep", "-fl", "VideoFusion|JianyingPro|CapCut|lveditor|lvpro"],
                             capture_output=True, text=True, timeout=10)
        return len([line for line in (out.stdout or "").splitlines() if line.strip()])
    except Exception:
        return 0


def jianying_installation_report() -> dict:
    """Return observable Jianying binary/version/process facts without changing it."""
    candidates: list[Path] = []
    if IS_WIN:
        for root in _jianying_install_roots():
            candidates.extend([root / "JianyingPro.exe", root / "CapCut.exe"])
            # Managed releases are laid out as <root>/<version>/JianyingPro.exe.
            # Limit discovery to one level so every env check remains cheap.
            try:
                for child in root.iterdir():
                    if child.is_dir():
                        candidates.extend([child / "JianyingPro.exe", child / "CapCut.exe"])
            except OSError:
                continue
    elif IS_MAC:
        candidates.extend([Path("/Applications/JianyingPro.app"), Path("/Applications/CapCut.app")])
    exe = _existing(*candidates)
    return {
        "executable": str(exe) if exe else None,
        "version": _windows_file_version(exe) if exe else None,
        "running_processes": _jianying_process_count(),
    }


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def package_installation_report() -> dict:
    """Identify the running package and validate its lightweight integrity receipt.

    The report is informational for direct source-tree execution. Installed Windows
    packages are expected below a standard Doubao skills root; private Jianying
    DeepAgents skills are deliberately never treated as this delivery package.
    """
    metadata_path = SKILLS_ROOT / "SKILL_PACKAGE.json"
    metadata: dict = {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    own = str(SKILLS_ROOT).replace("/", "\\").lower()
    private_internal = "\\deepagents\\skills\\" in own
    home = Path.home()
    roots = [home / "Doubao" / "skills"]
    if IS_WIN:
        roots += [Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Doubao" / "skills",
                  Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "Doubao" / "skills"]
    standard = any(str(SKILLS_ROOT).lower().startswith(str(root).lower()) for root in roots)
    receipt = SKILLS_ROOT / "PACKAGE_CONTENTS.sha256"
    expected, actual = None, _sha256_file(metadata_path)
    try:
        # The manifest contains UTF-8 paths (including the localized release
        # documents), so ASCII decoding breaks environment checks on Windows.
        for line in receipt.read_text(encoding="utf-8-sig").splitlines():
            fields = line.split()
            if len(fields) >= 2:
                receipt_path = fields[-1].replace("\\", "/")
                if receipt_path.startswith("./"):
                    receipt_path = receipt_path[2:]
            else:
                receipt_path = ""
            if len(fields) >= 2 and receipt_path == "SKILL_PACKAGE.json":
                expected = fields[0]
                break
    except OSError:
        pass
    # The receipt covers the immutable metadata file. Package SHA is supplied by
    # the release report; this fast check catches an incomplete/mismatched folder.
    integrity = None if not expected else expected == actual
    return {
        "root": str(SKILLS_ROOT),
        "metadata_path": str(metadata_path),
        "name": metadata.get("name"),
        "version": metadata.get("version"),
        "metadata_sha256": actual,
        "integrity_receipt": str(receipt) if receipt.exists() else None,
        "integrity_ok": integrity,
        "location": "private_internal" if private_internal else "standard_doubao_skills" if standard else "other",
        "active_external_package": bool(metadata and not private_internal and (integrity is not False)),
    }


def _find_bundled_exe(tool: str) -> Optional[Path]:
    """在剪映安装目录内（限深、找到即停、跳过缓存大目录）搜索捆绑的 ffmpeg/ffprobe。"""
    target = _exe(tool).lower()
    skip = {"cache", "caches", "logs", "log", "crash", "temp", "tmp", "$recycle.bin",
            "gpucache", "code cache"}
    for root in _jianying_install_roots():
        base_depth = len(root.parts)
        try:
            for cur, dirs, files in os.walk(root):
                depth = len(Path(cur).parts) - base_depth
                if depth >= 5:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d.lower() not in skip]
                for fn in files:
                    if fn.lower() == target:
                        return Path(cur) / fn
        except OSError:
            continue
    return None


def find_ffmpeg_detailed() -> dict:
    """按优先级定位 ffmpeg/ffprobe，返回路径与命中来源（对“公司禁装 ffmpeg”友好）。

    优先级：环境变量 FFMPEG_BIN/FFPROBE_BIN > Skill 自带 bin（绿色免安装）
            > 剪映安装目录捆绑复用 > 系统 PATH > 包管理器常见目录。
    """
    if IS_MAC:
        common = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    else:
        program = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        program_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        common = [
            local / "Microsoft" / "WinGet" / "Links",
            Path(r"C:\ffmpeg\bin"), program / "ffmpeg" / "bin", program_x86 / "ffmpeg" / "bin",
            Path(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")) / "bin",
            Path.home() / "scoop" / "shims",
        ]

    def locate_one(tool: str, env_key: str):
        exe = _exe(tool)
        ev = os.environ.get(env_key)  # 1) 显式环境变量，可指向单个可执行文件
        if ev:
            p = Path(ev).expanduser()
            if p.exists():
                return p, f"env:{env_key}"
        for d in _skill_local_bin_dirs():  # 2) Skill 自带绿色 bin
            p = d / exe
            if p.exists():
                return p, "skill_bin(绿色免安装)"
        bundled = _find_bundled_exe(tool)  # 3) 复用剪映捆绑
        if bundled:
            return bundled, "jianying_bundled(复用剪映自带)"
        w = shutil.which(tool)  # 4) PATH
        if w:
            return Path(w), "PATH"
        for d in common:  # 5) 常见安装目录
            p = d / exe
            if p.exists():
                return p, "common_dir"
        return None, None

    ff, ff_src = locate_one("ffmpeg", "FFMPEG_BIN")
    fp, fp_src = locate_one("ffprobe", "FFPROBE_BIN")
    return {"ffmpeg": ff, "ffprobe": fp,
            "ffmpeg_source": ff_src, "ffprobe_source": fp_src}


def find_ffmpeg() -> tuple[Optional[Path], Optional[Path]]:
    """返回 (ffmpeg, ffprobe) 路径；兼容旧调用。"""
    d = find_ffmpeg_detailed()
    return d["ffmpeg"], d["ffprobe"]


def _windows_custom_drafts_root() -> Optional[Path]:
    """Return JianYing's configured custom draft library, if one is active.

    The Windows client stores a user-selected library outside ``LOCALAPPDATA``
    in this registry value.  Writing to the default location when this value is
    set produces a structurally valid draft that the active client never sees.
    Querying one known value is intentionally cheap and does not modify the
    client configuration.
    """
    if not IS_WIN:
        return None
    try:
        run = subprocess.run(
            ["reg", "query", r"HKCU\Software\Bytedance\JianyingPro\GlobalSettings\History",
             "/v", "currentCustomDraftPath"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (run.stdout or "").splitlines():
        if "REG_SZ" not in line:
            continue
        value = line.split("REG_SZ", 1)[1].strip()
        if value:
            candidate = Path(value).expanduser()
            try:
                if candidate.exists():
                    return candidate.resolve()
            except OSError:
                pass
    return None


def find_drafts_root_detail(editor_root: Optional[Path] = None) -> tuple[Path, str]:
    """Locate the active JianYing draft root and report why it was selected."""
    for env_key in ("JY_DRAFTS_ROOT", "JY_PROJECTS_ROOT"):
        env = os.environ.get(env_key, "").strip()
        if env:
            return Path(env).expanduser().resolve(), f"env:{env_key}"

    configured = _windows_custom_drafts_root()
    if configured:
        return configured, "windows_registry:currentCustomDraftPath"

    if editor_root is None:
        editor_root = find_jianying_editor_root()
    try:
        sys.path.insert(0, str(editor_root / "scripts"))
        from utils.formatters import get_default_drafts_root  # type: ignore
        root = Path(get_default_drafts_root())
        if root.exists():
            return root.resolve(), "jianying_editor_default"
    except Exception:
        pass

    home = Path.home()
    if IS_MAC:
        cands = [
            home / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
            home / "Movies" / "JianyingPro Drafts",
        ]
    else:
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        cands = [
            local / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
            local / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft",
        ]
    found = _existing(*cands)
    return (found, "platform_default") if found else (cands[0], "platform_fallback")


def find_drafts_root(editor_root: Optional[Path] = None) -> Path:
    """Compatibility wrapper returning only the active JianYing draft root."""
    return find_drafts_root_detail(editor_root)[0]


def inspect_draft_storage(root: Path) -> dict:
    """Report observable draft-storage signals without treating key presence as failure."""
    root = Path(root)
    key_store = root / "crypto_key_store.dat"
    plain_count = 0
    migrated_count = 0
    encrypted_count = 0
    if root.exists():
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                info = child / "draft_info.json"
                timelines = child / "Timelines"
                has_migrated_project = timelines.exists() and bool(
                    list(timelines.glob("**/project.json")))
                if has_migrated_project:
                    migrated_count += 1
                    continue
                if info.exists():
                    try:
                        raw = info.read_text(encoding="utf-8", errors="ignore")
                        if raw.lstrip().startswith("{"):
                            plain_count += 1
                        else:
                            encrypted_count += 1
                    except OSError:
                        encrypted_count += 1
        except OSError:
            pass
    return {
        "crypto_key_store_present": key_store.exists(),
        "plain_draft_count": plain_count,
        "migrated_draft_count": migrated_count,
        "unreadable_draft_count": encrypted_count,
        "writer_compatibility": "plain_or_migrated_only",
    }


def _drafts_root_access_issues(root: Path) -> list[str]:
    """Check that the selected draft library can accept a short-lived probe."""
    root = Path(root)
    if not root.exists():
        return [f"剪映草稿目录不存在：{root}"]
    if not root.is_dir():
        return [f"剪映草稿路径不是目录：{root}"]
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".jy_env_probe_",
                                         delete=True) as probe:
            probe.write(b"jianying-skill-access-check")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as exc:
        return [f"剪映草稿目录不可写：{root}（{exc}）"]
    return []


def is_jianying_running() -> bool:
    """检测剪映桌面端进程是否在运行（不区分是否打开具体草稿）。"""
    return _jianying_process_count() > 0


def bootstrap_path(ffmpeg_dir: Optional[Path]) -> None:
    """把 ffmpeg 所在目录注入当前进程 PATH（跨平台幂等）。"""
    if not ffmpeg_dir:
        return
    target = str(ffmpeg_dir)
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep)
    if target not in parts:
        os.environ["PATH"] = target + os.pathsep + cur


def check_python_deps() -> dict:
    """体检编排主链所需的 Python 包，分“必需/建议/可选”（跨平台）。"""
    import importlib.util
    # 必需：缺了主链无法运行
    required = {
        "pymediainfo": "pymediainfo",      # pyJianYingDraft 主链强依赖
        "websockets": "websockets",        # SAMI 在线 TTS
    }
    # 建议：缺了仍可跑，但失去兜底/便利（SAMI 失败回退、云曲库下载等）
    recommended = {
        "edge_tts": "edge-tts",            # SAMI 失败时的 TTS 兜底
        "requests": "requests",            # 云曲库在线下载（用本地 BGM 可不需要）
        "psutil": "psutil",                # 进程/系统辅助
    }
    # pyJianYingDraft imports uiautomation on Windows when constructing a draft.
    # Keep it Windows-only so macOS deployments do not gain an unnecessary dependency.
    required_win = {"uiautomation": "uiautomation"} if IS_WIN else {}
    optional_win = {}

    def _scan(group: dict) -> dict:
        out = {}
        for mod, pip_name in group.items():
            out[mod] = {"installed": importlib.util.find_spec(mod) is not None, "pip": pip_name}
        return out

    required_all = {**required, **required_win}
    status = {"required": _scan(required_all), "recommended": _scan(recommended),
              "optional": _scan(optional_win), "missing_required": [],
              "missing_recommended": []}
    status["missing_required"] = [v["pip"] for k, v in status["required"].items()
                                  if not v["installed"]]
    status["missing_recommended"] = [v["pip"] for k, v in status["recommended"].items()
                                     if not v["installed"]]
    for mod in status["optional"]:
        status["optional"][mod]["needed"] = "windows_auto_unlock"
    status["ready"] = not status["missing_required"]
    return status


def _environment_report_uncached() -> dict:
    """一次性汇总运行环境，供编排前自检与排障。"""
    editor_root = find_jianying_editor_root()
    ff_info = find_ffmpeg_detailed()
    ffmpeg, ffprobe = ff_info["ffmpeg"], ff_info["ffprobe"]
    drafts_root, drafts_root_source = find_drafts_root_detail(editor_root)
    draft_format = inspect_draft_storage(drafts_root)
    wrapper_ok = (editor_root / "scripts" / "jy_wrapper.py").exists()
    vendor_ok = (editor_root / "scripts" / "vendor" / "pyJianYingDraft").exists()
    deps = check_python_deps()
    jianying = jianying_installation_report()
    package = package_installation_report()
    drafts_access_issues = _drafts_root_access_issues(drafts_root)
    hints = []
    if not ffmpeg:
        hints.append(
            "未找到 ffmpeg。若公司安全策略禁止“安装”，不要用 winget/安装包，改用免安装方式："
            "①把绿色版 ffmpeg.exe、ffprobe.exe 放到本 skill 的 bin/ 目录（或 bin/win/）；"
            "②或设环境变量 FFMPEG_BIN/FFPROBE_BIN 指向绿色可执行文件；"
            "③引擎也会自动尝试复用剪映安装目录自带的 ffmpeg；④或向 IT 申请把 FFmpeg 加白名单。")
    elif not ffprobe:
        hints.append("找到 ffmpeg 但缺 ffprobe，绿色版压缩包内 ffmpeg.exe 与 ffprobe.exe 需放在同一目录。")
    return {
        "platform": "windows" if IS_WIN else "macos" if IS_MAC else sys.platform,
        "python": sys.version.split()[0],
        "jianying_editor_root": str(editor_root),
        "wrapper_available": wrapper_ok,
        "pyjianyingdraft_vendor": vendor_ok,
        "ffmpeg": str(ffmpeg) if ffmpeg else None,
        "ffprobe": str(ffprobe) if ffprobe else None,
        "ffmpeg_source": ff_info["ffmpeg_source"],
        "ffprobe_source": ff_info["ffprobe_source"],
        "drafts_root": str(drafts_root),
        "drafts_root_source": drafts_root_source,
        "drafts_root_index_exists": (drafts_root / "root_meta_info.json").exists(),
        "drafts_root_exists": drafts_root.exists(),
        "drafts_root_access_issues": drafts_access_issues,
        "draft_format": draft_format,
        "jianying": jianying,
        "jianying_running": bool(jianying["running_processes"]),
        "active_package": package,
        "python_deps": deps,
        "hints": hints,
        "ready": bool(wrapper_ok and vendor_ok and ffmpeg and ffprobe and
                      drafts_root.exists() and not drafts_access_issues and deps["ready"]
                      and package.get("active_external_package")),
    }


def clear_environment_cache() -> None:
    global _ENV_CACHE
    with _ENV_CACHE_LOCK:
        _ENV_CACHE = None


def environment_report(*, refresh: bool = False) -> dict:
    """一次性汇总运行环境；同一进程重复调用复用结果。"""
    global _ENV_CACHE
    with _ENV_CACHE_LOCK:
        if _ENV_CACHE is None or refresh:
            _ENV_CACHE = _environment_report_uncached()
        return copy.deepcopy(_ENV_CACHE)


if __name__ == "__main__":
    import json
    print(json.dumps(environment_report(), ensure_ascii=False, indent=2))
