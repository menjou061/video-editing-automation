"""绿色免安装 ffmpeg 自动引导（专为“公司策略禁止安装 ffmpeg”的 Windows 环境设计）。

豆包/Agent 在 `run.py env` 发现缺 ffmpeg、或 winget/安装器被安全软件拦截时，直接调用
`run.py setup-ffmpeg` 即可：从官方静态构建下载压缩包，只抽取 ffmpeg(.exe)/ffprobe(.exe)
放到本 skill 的 bin/ 目录，不运行安装器、不写注册表、不需要管理员，最后自动复验。

零三方依赖（仅标准库 urllib/zipfile/shutil/subprocess）。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from .platform_env import IS_WIN, IS_MAC, SKILL_DIR, find_ffmpeg_detailed

# 官方静态构建（按顺序尝试，前一个失败自动换下一个）。只下载、解压取 exe，绝不执行安装器。
WIN_SOURCES = [
    {"name": "gyan.dev release-essentials",
     "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"},
    {"name": "BtbN master win64-gpl",
     "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"},
]
MAX_BYTES = 220 * 1024 * 1024  # 静态包上限保护，超出视为异常中止
TIMEOUT = 120


def _bin_dir() -> Path:
    d = SKILL_DIR / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (jianying-orchestrator-setup)",
        "Accept": "application/zip,*/*",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("下载体积超过上限，已中止")
    return data


def _extract_exes(zip_bytes: bytes, dest: Path) -> dict:
    """从静态构建 zip 中抽取 ffmpeg/ffprobe 可执行文件到 dest。"""
    want = {"ffmpeg.exe": None, "ffprobe.exe": None}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename).lower()
            if base in want and want[base] is None:
                with zf.open(info) as src, open(dest / base, "wb") as out:
                    shutil.copyfileobj(src, out)
                want[base] = dest / base
    return {"ffmpeg": want["ffmpeg.exe"], "ffprobe": want["ffprobe.exe"]}


def _version_of(exe: Path) -> str:
    try:
        r = subprocess.run([str(exe), "-version"], capture_output=True,
                           text=True, timeout=15)
        return (r.stdout or r.stderr or "").splitlines()[0][:120]
    except Exception:
        return ""


def bootstrap_ffmpeg(force: bool = False) -> dict:
    """自动准备绿色 ffmpeg。返回结构化结果（不抛栈，便于 CLI/Agent 决策）。"""
    detail = find_ffmpeg_detailed()
    if not force and detail["ffmpeg"]:
        return {"status": "ALREADY_READY", "ffmpeg": str(detail["ffmpeg"]),
                "ffprobe": str(detail["ffprobe"]) if detail["ffprobe"] else None,
                "ffmpeg_source": detail["ffmpeg_source"],
                "message": "已可用，无需重复下载；如需强制重装加 --force"}

    if not IS_WIN:
        # macOS/Linux：官方无统一免安装静态直链，开发机走包管理器即可
        tip = ("macOS 请用 `brew install ffmpeg`；Linux 用系统包管理器(apt/yum)。"
               "若也被限制，可自行下载静态构建后把 ffmpeg/ffprobe 放到 bin/，或设 FFMPEG_BIN。")
        return {"status": "MANUAL_NEEDED", "platform": sys.platform, "message": tip}

    dest = _bin_dir()
    errors = []
    for src in WIN_SOURCES:
        try:
            data = _download(src["url"])
            picked = _extract_exes(data, dest)
            if not picked["ffmpeg"]:
                raise RuntimeError("压缩包内未找到 ffmpeg.exe")
            # 复验
            recheck = find_ffmpeg_detailed()
            ver = _version_of(picked["ffmpeg"])
            if not recheck["ffmpeg"]:
                raise RuntimeError("放置后仍无法被引擎识别")
            return {
                "status": "READY", "source": src["name"],
                "ffmpeg": str(picked["ffmpeg"]),
                "ffprobe": str(picked["ffprobe"]) if picked["ffprobe"] else None,
                "ffmpeg_source": recheck["ffmpeg_source"],
                "size_mb": round(picked["ffmpeg"].stat().st_size / 1e6, 1),
                "version": ver,
                "message": "绿色 ffmpeg 已就位（未安装、未写注册表）；重跑 run.py env 应 ready=true",
            }
        except Exception as exc:  # noqa: BLE001  逐个源尝试，最终才失败
            errors.append(f"{src['name']}: {exc}")
            continue

    return {
        "status": "FAILED", "tried": [s["name"] for s in WIN_SOURCES], "errors": errors,
        "manual_fallback": [
            "1) 用可联网机器下载 ffmpeg-release-essentials.zip（gyan.dev）或 BtbN win64-gpl.zip；",
            "2) 解压取 bin/ffmpeg.exe、bin/ffprobe.exe，放到本 skill 的 bin/ 目录；",
            "3) 或设环境变量 FFMPEG_BIN/FFPROBE_BIN 指向已有绿色可执行文件；",
            "4) 若安全软件连绿色进程也拦截，向 IT 申请把 FFmpeg 加入白名单（开源、剪映同款组件）。",
        ],
    }
