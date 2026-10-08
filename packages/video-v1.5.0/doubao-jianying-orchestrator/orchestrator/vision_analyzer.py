"""Automatic task-local visual analysis and script-to-shot matching.

The first implementation asked the outer Codex turn to inspect hundreds of
frames manually.  That is not deterministic in a headless Windows run: the
turn can create the schema while leaving placeholder labels such as
``frame_visible``.  This module makes the visual step explicit.  It builds
labelled contact sheets, sends them to ``codex exec --image`` and validates
the returned JSON before the deterministic reuse/evidence gates run.

Only the current task's frames are sent.  Nothing is written to the reusable
material library.  A failed vision call remains a hard blocker; it is never
replaced by guessed tags.

v1.3.20: ``codex exec`` is an agent session, not a one-shot completion, and
that is what actually stalled the pipeline.  Given the shell and app tools,
the model answers a batch by cropping the contact sheet and re-viewing the
crops ("Let me crop to check details" -> powershell + PIL -> "Let me view
tiles 1-3"), a loop with no terminating condition.  Batches ran one to four
minutes and periodically crossed the 300 s timeout; before v1.3.17 the pipe
drain turned that into a permanent hang.  This version removes the tool
surface that feeds the loop and routes *every* codex call through one
file-backed runner so a timeout is always expressible as a process-tree kill.
"""
from __future__ import annotations

import json
import base64
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


_DEMO_ROLES = ("direct_evidence", "usage_demo", "visual_metaphor",
               "product_display", "context", "CTA")
_DEMO_PHASES = ("prep", "perform", "result", "static")
VISION_ANALYSIS_VERSION = "product-visual-v1"
_VISION_CACHE_FIELDS = (
    "description", "visual_description", "visual_tags", "evidence_tags",
    "action_complete", "head_waste", "tail_waste", "analysis_confidence",
    "analysis_source", "status", "analysis_error", "action", "object",
    "result", "role", "evidence_strength", "action_phase", "setup_id",
    "has_subject", "readable_claims", "packaging_claims", "pointed_claims",
    "pointing_to_text", "explicit_pointing", "pointing_action", "gesture",
)


def _demo_action_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """从视觉结果里摘出 DEMO_ACTIONS V0.2 要求的素材侧字段。

    只在模型**确实给了值**时才写键 —— `demo_actions.judge()` 用「键不存在」
    区分「这是旧分析结果（放行但要标注）」和「模型明确说了主体不存在/没有
    可见结果（要拦）」。写 None 会把后者也归进旧结果，闸门就失效了。

    `result` 是例外：它显式为 null 是有意义的信息（只有动作、没有可见结果），
    所以模型给了 null 就写 null 并把强度压到 C —— 这正是「抚摸面层不得当
    事实证明」那条规则的落点。
    """
    out: dict[str, Any] = {}
    action = str(raw.get("action") or "").strip()
    if action:
        out["action"] = action
    obj = str(raw.get("object") or "").strip()
    if obj:
        out["object"] = obj
    if "result" in raw:
        result = raw.get("result")
        result = str(result).strip() if result is not None else None
        out["result"] = result or None
    role = str(raw.get("role") or "").strip()
    if role in _DEMO_ROLES:
        out["role"] = role
    strength = str(raw.get("evidence_strength") or "").strip().upper()[:1]
    if strength in ("A", "B", "C"):
        # result 为空时不得为 A（DEMO_ACTIONS_V0.2 §5 门禁）：
        # 没有可见结果就没有直接证据，模型高报也要压回去。
        if strength == "A" and not out.get("result"):
            strength = "C"
        out["evidence_strength"] = strength
    phase = str(raw.get("action_phase") or "").strip().lower()
    if phase in _DEMO_PHASES:
        out["action_phase"] = phase
    setup = str(raw.get("setup_id") or "").strip()
    if setup:
        out["setup_id"] = setup
    if "has_subject" in raw:
        out["has_subject"] = raw.get("has_subject") is True
    for key in ("readable_claims", "packaging_claims", "pointed_claims"):
        if key in raw:
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                out[key] = [str(item).strip() for item in value if str(item).strip()]
            elif str(value or "").strip():
                out[key] = [str(value).strip()]
    for key in ("pointing_to_text", "explicit_pointing"):
        if key in raw:
            out[key] = raw.get(key) is True
    for key in ("pointing_action", "gesture"):
        value = str(raw.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def _evidence_intervals(raw: Any, shot: dict[str, Any], frame: dict[str, Any] | None,
                        *, evidence: bool) -> list[dict[str, Any]]:
    """Keep model-provided evidence windows only when they are auditable.

    The vision call may return a tighter interval than the representative
    frame.  It is still bounded to the detected shot window and discarded when
    it is malformed.  A representative-frame fallback keeps older model
    responses useful while preserving a traceable source timestamp.
    """
    if not evidence:
        return []
    start = _number(shot.get("source_start"))
    end = max(start, _number(shot.get("source_end"), start))
    intervals: list[dict[str, Any]] = []
    values = raw if isinstance(raw, list) else [raw]
    for value in values:
        if not isinstance(value, dict):
            continue
        left = max(start, _number(value.get("start"), start))
        right = min(end, _number(value.get("end"), end))
        if right <= left:
            continue
        record = {"start": round(left, 3), "end": round(right, 3)}
        if value.get("frame_path"):
            record["frame_path"] = str(value["frame_path"])
        intervals.append(record)
    if intervals:
        return intervals
    if frame:
        center = _number(frame.get("timestamp"))
        return [{"start": round(max(start, center - 0.25), 3),
                 "end": round(min(end, center + 0.25), 3),
                 "frame_path": frame["path"]}]
    return []


def _json_from_text(text: str) -> Any:
    """Parse a JSON array/object from a Codex final message."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("vision model returned an empty response")
    candidates = [text]
    candidates.extend(re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I))
    for candidate in candidates:
        candidate = candidate.strip()
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = candidate.find(opener), candidate.rfind(closer)
            if start >= 0 and end > start:
                try:
                    return json.loads(candidate[start:end + 1])
                except json.JSONDecodeError:
                    continue
    raise ValueError("vision model response did not contain valid JSON")


def _load_image(path: Path, width: int, height: int):
    """Load/resize a frame with OpenCV, falling back to Pillow."""
    try:
        import cv2  # type: ignore
        image = cv2.imread(str(path))
        if image is None:
            return None
        return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    except Exception:
        try:
            from PIL import Image  # type: ignore
            import numpy as np  # type: ignore
            image = Image.open(path).convert("RGB").resize((width, height))
            return np.asarray(image)[:, :, ::-1].copy()
        except Exception:
            return None


def _write_sheet(items: list[dict[str, Any]], output: Path, *, columns: int = 4,
                 tile_width: int = 320, tile_height: int = 220,
                 ffmpeg: str | None = None) -> bool:
    """Create one labelled contact sheet for a vision call."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        # Pillow is present in the Mac validation environment even when the
        # optional OpenCV wheel is absent.  Keep the sheet format identical so
        # Windows and Mac produce the same labelled visual input.
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
        except Exception:
            # Windows production Python intentionally has no NumPy/Pillow
            # requirement.  JianYing bundles ffmpeg, so use its image2/tile
            # filters as a dependency-free fallback and draw a numeric label
            # that is mapped to shot_id in the prompt.
            command = ffmpeg or os.environ.get("JY_FFMPEG") or shutil.which("ffmpeg")
            if not command:
                return False
            tile_dir = output.parent / (output.stem + "_tiles")
            try:
                tile_dir.mkdir(parents=True, exist_ok=True)
                for index, item in enumerate(items, 1):
                    shutil.copyfile(item["path"], tile_dir / f"tile_{index:04d}.jpg")
                rows = max(1, (len(items) + columns - 1) // columns)
                vf = (
                    f"scale={tile_width}:{tile_height - 34},"
                    f"drawbox=y={tile_height - 34}:h=34:color=black@1:t=fill,"
                    f"tile={columns}x{rows}"
                )
                proc = subprocess.run(
                    [str(command), "-y", "-hide_banner", "-loglevel", "error",
                     "-framerate", "1", "-i", str(tile_dir / "tile_%04d.jpg"),
                     "-vf", vf, "-frames:v", "1", str(output)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=120, check=False,
                )
                return proc.returncode == 0 and output.is_file() and output.stat().st_size > 0
            except (OSError, subprocess.SubprocessError, shutil.Error):
                return False
            finally:
                shutil.rmtree(tile_dir, ignore_errors=True)
        rows = max(1, (len(items) + columns - 1) // columns)
        canvas = Image.new("RGB", (columns * tile_width, rows * tile_height), (25, 25, 25))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        for index, item in enumerate(items):
            row, col = divmod(index, columns)
            x, y = col * tile_width, row * tile_height
            try:
                image = Image.open(item["path"]).convert("RGB").resize(
                    (tile_width, tile_height - 34))
                canvas.paste(image, (x, y))
            except Exception:
                draw.text((x + 8, y + 60), "FRAME_UNREADABLE", fill=(255, 0, 0), font=font)
            draw.rectangle((x, y + tile_height - 34, x + tile_width, y + tile_height), fill=(0, 0, 0))
            draw.text((x + 5, y + tile_height - 25), str(item["label"])[:42],
                      fill=(255, 255, 255), font=font)
        output.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output, quality=90)
        return output.is_file() and output.stat().st_size > 0
    rows = max(1, (len(items) + columns - 1) // columns)
    canvas = np.zeros((rows * tile_height, columns * tile_width, 3), dtype=np.uint8)
    canvas[:] = (25, 25, 25)
    for index, item in enumerate(items):
        row, col = divmod(index, columns)
        x, y = col * tile_width, row * tile_height
        image = _load_image(Path(item["path"]), tile_width, tile_height - 34)
        if image is None:
            cv2.putText(canvas, "FRAME_UNREADABLE", (x + 8, y + 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1, cv2.LINE_AA)
        else:
            canvas[y:y + tile_height - 34, x:x + tile_width] = image
        label = str(item["label"])
        cv2.rectangle(canvas, (x, y + tile_height - 34),
                      (x + tile_width, y + tile_height), (0, 0, 0), -1)
        cv2.putText(canvas, label[:42], (x + 5, y + tile_height - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.43, (255, 255, 255), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    return bool(cv2.imwrite(str(output), canvas))


# The loop needs a shell to manufacture new images to look at, so removing
# the shell removes the loop.  The sheet still reaches the model through
# --image, and view_image stays available for inspecting that one image.
_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "code_mode_host",
    "browser_use",
    "computer_use",
    "apps",
)

# The prompt already demands JSON-only output, but a prompt is a request and
# the model ignored it whenever it decided to investigate first.
# --output-schema would turn the shape into a constraint, but it is not
# universally available: the aijws relay answered 400 "<InvalidParameter:
# This response_format type is unavailable now>" and the batch never reached
# the model (measured 2026-09-16 against deepseek-v4.1-flash).  Not re-tested
# on official deepseek-flash, so the schema stays opt-in rather than default.  The
# parser in enrich_with_vision still accepts either the object or a bare
# array, so enabling it later is a one-variable change.
_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "shot_id": {"type": "string"},
                    "description": {"type": "string"},
                    "visual_tags": {"type": "array", "items": {"type": "string"}},
                    "evidence_tags": {"type": "array", "items": {"type": "string"}},
                    "action_complete": {"type": "boolean"},
                    "head_waste": {"type": "number"},
                    "tail_waste": {"type": "number"},
                    "evidence_frame": {"type": "string", "enum": ["mid", "end", ""]},
                    "confidence": {"type": "number"},
                    # DEMO_ACTIONS V0.2 素材侧字段（见 demo_actions.py）。
                    # 前四项是匹配的判据，后四项是反重复与空镜判据。
                    "action": {"type": "string"},
                    "object": {"type": "string"},
                    "result": {"type": ["string", "null"]},
                    "role": {"type": "string", "enum": [
                        "direct_evidence", "usage_demo", "visual_metaphor",
                        "product_display", "context", "CTA"]},
                    "evidence_strength": {"type": "string", "enum": ["A", "B", "C"]},
                    "action_phase": {"type": "string",
                                     "enum": ["prep", "perform", "result", "static"]},
                    "setup_id": {"type": "string"},
                    "has_subject": {"type": "boolean"},
                },
                # 新增的 8 项**不在 required 里**：旧模型/旧中转可能不产出它们，
                # 写进 required 会让整批校验失败、所有镜头回落 pending_analysis。
                # 缺失时 demo_actions.judge() 走 ungated 分支放行，覆盖率另写入
                # claim_gate_coverage，不会静默假装闸门生效。
                "required": ["shot_id", "description", "visual_tags", "evidence_tags",
                             "action_complete", "head_waste", "tail_waste",
                             "evidence_frame", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["shots"],
    "additionalProperties": False,
}


def _schema_enabled() -> bool:
    """Gate --output-schema; the relay rejects structured output by default."""
    return os.environ.get("JY_VISION_OUTPUT_SCHEMA", "").strip().lower() in (
        "1", "true", "yes", "on")


def _write_schema(directory: Path) -> Path | None:
    """Materialise the batch schema for --output-schema; never fatal."""
    path = directory / "vision_schema.json"
    try:
        path.write_text(json.dumps(_VISION_SCHEMA, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return None
    return path


def _vision_summary(profile: str, batches: list[Any], responses: list[dict[str, Any]],
                    errors: list[dict[str, Any]], *, partial: bool) -> dict[str, Any]:
    """Describe the batch run as it stands, including a partial one.

    The incremental persist writes this before each batch, so a file left
    behind by a failed run reports how many batches actually returned instead
    of carrying a stale count from an earlier attempt.
    """
    return {
        "provider": "codex_exec_image",
        "profile": profile,
        "batch_count": len(batches),
        "responses": list(responses),
        "errors": list(errors),
        "partial": partial,
    }


def _persist(report_dir: Path, analysis: dict[str, Any]) -> None:
    """Write shot_candidates.json incrementally, after every batch.

    v1.3.20: this used to be the single write after the last batch, so one
    batch that hung or timed out discarded every already-validated row from
    the batches before it.  Persisting per batch makes the surviving work
    usable even when a later batch fails.
    """
    try:
        (report_dir / "shot_candidates.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _vision_cache_path(report_dir: Path) -> Path:
    """Resolve the cross-task visual understanding cache.

    A task report remains the source of truth for its own result.  This cache is
    only a reusable accelerator, so it lives beside the queue work root and is
    invalidated by the source signature, shot window, profile, or analyzer
    version.  ``JY_VISION_CACHE_DIR`` lets the Windows worker place it on a
    persistent local/NAS cache without changing task manifests.
    """
    configured = str(os.environ.get("JY_VISION_CACHE_DIR") or "").strip()
    root = Path(configured) if configured else report_dir.parent.parent / ".cache"
    return root / "material_vision.json"


def _vision_cache_key(manifest: dict[str, Any], shot: dict[str, Any], profile: str) -> str:
    source = str(shot.get("video") or "").strip()
    if not source:
        return ""
    try:
        path = Path(source).expanduser().resolve()
        stat = path.stat()
        signature = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
        source = str(path)
    except OSError:
        return ""
    identity = {
        "version": VISION_ANALYSIS_VERSION,
        "profile": profile,
        "product": str(manifest.get("product_id") or manifest.get("product")
                        or manifest.get("brand") or "").strip(),
        "video": source,
        "signature": signature,
        "source_start_us": int(round(_number(shot.get("source_start")) * 1_000_000)),
        "source_end_us": int(round(_number(shot.get("source_end")) * 1_000_000)),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_vision_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_vision_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _cache_projection(row: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only stable visual facts; never cache task-local frame paths/status noise."""
    if row.get("status") != "ready_for_matching" or row.get("action_complete") is not True:
        return None
    return {field: row[field] for field in _VISION_CACHE_FIELDS if field in row}


def _apply_cached_visual(row: dict[str, Any], cached: dict[str, Any], key: str) -> None:
    for field in _VISION_CACHE_FIELDS:
        if field in cached:
            row[field] = cached[field]
    row["analysis_cache_hit"] = True
    row["analysis_cache_key"] = key


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a codex process tree, descendants before the cmd.exe wrapper.

    Killing the wrapper first reaps cmd.exe only; taskkill /T can then no
    longer enumerate the tree and the codex.exe/node.exe grandchildren leak as
    orphans.  Order matters.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=False)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def _run_codex_file(args: list[str], prompt_path: Path, stdout_path: Path,
                    stderr_path: Path, timeout: int) -> tuple[bool, int, str]:
    """Run codex with file-backed stdio and a tree-killing timeout.

    Never pipe codex stdio.  On Windows codex runs through a cmd.exe wrapper;
    killing the cmd on timeout leaves the orphaned codex.exe children holding
    the inherited pipe write handles, and the post-kill drain then blocks
    forever waiting for EOF that never comes.  A timeout has to be a real
    process-tree kill, which only works when nothing is draining a pipe.

    Every codex invocation in this module goes through here so that the rule
    cannot be honoured in one call site and forgotten in another.
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "w", encoding="utf-8", errors="replace") as fout, \
         open(stderr_path, "w", encoding="utf-8", errors="replace") as ferr, \
         open(prompt_path, "rb") as fin:
        try:
            proc = subprocess.Popen(args, stdin=fin, stdout=fout, stderr=ferr)
        except OSError as exc:
            return False, -1, str(exc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return False, -1, f"timed out after {timeout}s"
    return proc.returncode == 0, proc.returncode, ""


def _tail(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _clean_profile(value: Any) -> str:
    """Normalize a Windows-inherited Codex profile before CLI assembly.

    SSH/cmd wrappers can preserve one layer of surrounding quotes in an
    environment value. Passing that through makes Codex parse ``"flash`` as
    the profile name. Strip only whitespace and quote wrappers; never alter
    the profile's actual name.
    """
    return str(value or "").strip().strip('"').strip("'").strip()


def _codex_command(sheet: Path | None, response: Path, profile: str, *,
                   workdir: Path | None = None,
                   schema: Path | None = None) -> list[str]:
    profile = _clean_profile(profile)
    args = ["codex", "exec"]
    if profile:
        args.extend(["-p", profile])
    provider_override = os.environ.get("JY_VISION_PROVIDER", "").strip()
    if provider_override:
        args.extend(["-c", f"model_provider={provider_override}"])
    model_override = os.environ.get("JY_VISION_MODEL", "").strip()
    if model_override:
        args.extend(["-c", f"model={model_override}"])
    for feature in _DISABLED_FEATURES:
        args.extend(["--disable", feature])
    args.extend([
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check", "--ephemeral"])
    if sheet is not None:
        args.extend(["--image", str(sheet)])
    args.extend(["-o", str(response)])
    if workdir is not None:
        # Keep the agent's cwd inside the throwaway batch directory.  Without
        # this it inherits the task workdir and drops crop artefacts
        # (tmp_tile_*.png) into the production task folder.
        args.extend(["-C", str(workdir)])
    if schema is not None and _schema_enabled():
        # Single choke point: no caller can put --output-schema on the command
        # line while the relay still rejects structured output.
        args.extend(["--output-schema", str(schema)])
    args.append("-")
    if os.name == "nt":
        # npm installs Codex as a .cmd shim on Windows.  Going through cmd.exe
        # is the same rule used by run_task.template.ps1.
        import subprocess as _sp
        return ["cmd.exe", "/d", "/s", "/c", _sp.list2cmdline(args)]
    return args


def _ccswitch_env() -> dict[str, str]:
    """Load the CC Switch CodingPlan transport without logging credentials."""
    values = {
        "base_url": os.environ.get("ANTHROPIC_BASE_URL", "").strip(),
        "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip(),
        "model": os.environ.get("JY_VISION_MODEL", "").strip()
        or os.environ.get("ANTHROPIC_MODEL", "").strip(),
    }
    env_file = os.environ.get("JY_VISION_ANTHROPIC_ENV", "").strip()
    if env_file and Path(env_file).is_file():
        try:
            raw = json.loads(Path(env_file).read_text(encoding="utf-8"))
            for key in values:
                if str(raw.get(key) or "").strip():
                    values[key] = str(raw[key]).strip()
        except (OSError, ValueError, TypeError):
            pass
    return values


def _run_anthropic_messages(sheet: Path, prompt: str, output: Path,
                            *, timeout: int) -> tuple[bool, str]:
    """Run a one-shot image request against the CC Switch Anthropic provider.

    CC Switch's Agent Plan endpoint speaks Anthropic Messages, while Codex's
    native runner speaks OpenAI Responses.  Keeping this adapter here lets the
    Windows worker use the selected CodingPlan quota without pretending the
    endpoint is a Codex Responses provider.
    """
    cfg = _ccswitch_env()
    if not cfg["base_url"] or not cfg["auth_token"] or not cfg["model"]:
        return False, "CCSWITCH_ANTHROPIC_CONFIG_MISSING"
    try:
        image = base64.b64encode(sheet.read_bytes()).decode("ascii")
    except OSError as exc:
        return False, f"CCSWITCH_IMAGE_READ_FAILED:{exc}"
    media_type = mimetypes.guess_type(sheet.name)[0] or "image/jpeg"
    body = {
        "model": cfg["model"],
        "max_tokens": int(os.environ.get("JY_VISION_MAX_TOKENS", "4096")),
        # Volcengine GLM accepts enabled thinking with a bounded budget. An
        # unbounded/default budget can consume all output tokens and leave an
        # empty text block, while `disabled` is rejected by this model.
        "thinking": {"type": "enabled", "budget_tokens": int(
            os.environ.get("JY_VISION_THINKING_BUDGET", "2048"))},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": image,
            }},
        ]}],
    }
    url = cfg["base_url"].rstrip("/") + "/v1/messages"
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": cfg["auth_token"],
        }, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.HTTPError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read(800).decode("utf-8", "replace")
            return False, f"CCSWITCH_HTTP_{exc.code}:{detail[-500:]}"
        return False, f"CCSWITCH_REQUEST_FAILED:{exc}"
    parts = [item.get("text", "") for item in payload.get("content", [])
             if isinstance(item, dict) and item.get("type") == "text"]
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        return False, "CCSWITCH_EMPTY_RESPONSE"
    output.write_text(text, encoding="utf-8")
    return True, text


def _run_vision(sheet: Path, prompt: str, output: Path, *, profile: str,
                timeout: int, workdir: Path | None = None,
                schema: Path | None = None) -> tuple[bool, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = output.with_suffix(".stdout.log")
    stderr_path = output.with_suffix(".stderr.log")
    prompt_path = output.with_suffix(".prompt.txt")
    prompt_path.write_text(prompt, encoding="utf-8")
    if os.environ.get("JY_VISION_TRANSPORT", "").strip().lower() in {
            "anthropic", "ccswitch", "ccswitch_anthropic"}:
        return _run_anthropic_messages(sheet, prompt, output, timeout=timeout)
    ok, code, error = _run_codex_file(
        _codex_command(sheet, output, profile, workdir=workdir, schema=schema),
        prompt_path, stdout_path, stderr_path, timeout)
    if error:
        if error.startswith("timed out"):
            return False, f"vision batch {error}"
        return False, error
    # --output-last-message is the canonical final response.  Some older CLI
    # builds do not create it when the provider fails, so keep the log files
    # as a diagnostic fallback without treating logs as model JSON.
    if output.is_file() and output.stat().st_size:
        return ok, output.read_text(encoding="utf-8", errors="replace")
    return False, (_tail(stderr_path) or _tail(stdout_path) or f"codex exit {code}")


def _run_vision_with_retry(sheet: Path, prompt: str, output: Path, *,
                           profile: str, timeout: int,
                           workdir: Path | None = None,
                           schema: Path | None = None,
                           max_retries: int = 1) -> tuple[bool, str]:
    """Retry a transient provider failure at most once with short backoff.

    The legacy relay and Anthropic CC Switch adapter intermittently returned
    502/503/429 or empty responses and one failed batch used to hard-block the
    whole task. The default Volcengine CodingPlan route is provider-neutral
    to this guard and costs nothing when unused.
    Retry only when the failure text looks transient so deterministic failures
    and provider hangs stay fast.
    """
    transient = (
        "502", "503", "429", "too many requests", "temporarily unavailable",
        "timed out", "timeout", "connection reset", "connection refused", "network",
    )
    last: tuple[bool, str] = (False, "")
    for attempt in range(max_retries + 1):
        ok, text = _run_vision(sheet, prompt, output, profile=profile, timeout=timeout,
                               workdir=workdir, schema=schema)
        if ok:
            return ok, text
        last = (ok, text)
        lowered = text.casefold()
        if attempt >= max_retries or not any(t.casefold() in lowered for t in transient):
            break
        time.sleep(5 * (attempt + 1))
    return last


def _shot_frames(shot: dict[str, Any]) -> list[dict[str, Any]]:
    paths = shot.get("frame_paths") or [shot.get("frame_path")]
    result = []
    for index, raw in enumerate(paths):
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_file():
            continue
        start = _number(shot.get("source_start"))
        end = _number(shot.get("source_end"), start)
        if index == 0:
            timestamp = (start + end) / 2.0
            suffix = "mid"
        else:
            timestamp = max(start, end - 0.15)
            suffix = "end"
        result.append({"path": str(path), "timestamp": timestamp, "suffix": suffix})
    return result


def _vision_prompt(items: list[dict[str, Any]], batch_index: int, batch_count: int) -> str:
    mapping = "\n".join(
        f"- contact-sheet tile {index}（从左到右、从上到下；Windows 无数字叠字时按这个顺序计数）: {item['label']}; "
        f"shot_id={item['shot_id']}, source={item['source_start']:.3f}-{item['source_end']:.3f}s, frame={item['suffix']}"
        for index, item in enumerate(items)
    )
    return f"""你是当前商品素材的视觉分析器。现在处理第 {batch_index}/{batch_count} 个抽帧批次。
图片是带标签的 contact sheet，标签就是 shot_id 加 mid/end。只分析图片中确实看见的内容，不能读取或猜测脚本文案，不能把图片里没有的卖点写进 evidence_tags。

输入标签：
{mapping}

直接依据给出的 contact sheet 作答：不要裁剪、放大、重新查看图片，不要使用任何工具，也不要把中间过程写进输出。
    对每个 shot_id 输出一条 JSON。严格只输出 JSON 对象 {{"shots": [...]}}，不要 Markdown，不要额外字段。每条字段必须是：
    shot_id, description, visual_tags, evidence_tags, action_complete, head_waste, tail_waste, evidence_frame, confidence,
    action, object, result, role, evidence_strength, action_phase, setup_id, has_subject,
    readable_claims, pointing_to_text, pointing_action, gesture。
- description：一句客观画面描述。
- visual_tags：主体、动作、场景、物体等可见标签。
- evidence_tags：只写从画面直接观察到、能用于后续口播匹配的事实，例如“纸张接触水面”“手部擦拭锅面”“完整展示包装”；不能写“可能”“应该”“看不清”。
- action：画面里主体正在做的动作，必须是**动词短语**（如“倾倒”“按压”“拉伸”“抚摸”“拉开”“撕开再粘贴”“堆叠”“举起”“无动作”）。参考片的匹配单位是动词，不是名词，这一项是整个匹配的关键。
- object：这个动作作用在什么上（如“蓝色液体+巾体”“已吸水巾体”“裤腰”“粘扣”“真棉花球+巾体”）。必须与 action 对应，不能写“产品”这种笼统词。
- result：这个动作产生了什么**在画面里直接看得见**的结果（如“液体被吸入”“按压处无水渗出”“可见弹性形变”“立体护边立起”）。这是证据强度的唯一依据：
  * 有可见结果 → 填结果本身；
  * 动作看得见但结果看不见（例如抚摸面层、拿产品蹭手背、往身上比试——这类只有动作，柔软/不适/合身都是触觉或判断，画面里看不到）→ 必须填 null，不得编造结果。
- role：从这六个里选一个：direct_evidence（有可见结果的证明性动作）、usage_demo（演示用法/形态，结果不可见）、visual_metaphor（道具或装置示意，如真棉花球、玻璃杯）、product_display（陈列产品，无卖点证明）、context（量感/场景/促销）、CTA（引导语镜头）。
- evidence_strength：A / B / C 三选一，只按 result 判：
  * A = result 非 null 且结果在画面内直接可见（倾倒后液面下降、按压后接触面无水、拉伸后可见形变）；
  * B = 动作演示了用法或属性但结果需要推断（拆包取出、产品入包、举着包装对镜头）；
  * C = 仅陈列，或只有动作而结果不可见（抚摸面层、蹭手背、往身上比试），或用道具/装置示意。
  **result 为 null 时不得填 A。**
- action_phase：prep（准备/掏出/撕开）、perform（正在执行该动作）、result（展示动作结果状态）、static（静态陈列，无进行中的动作），四选一。
- setup_id：机位+构图标识。**同一台机器、同一角度、主体在画面中的位置与占比基本相同**的镜头必须填同一个 setup_id（自拟稳定短码，如“wood_table_front”“handheld_closeup_left”）。换景别、换角度、换主体位置就要换 setup_id。这一项用于防止同机位同构图连续重复。
    - has_subject：画面里是否存在本商品主体（true/false）。**整段都是背景、看不到产品本体时填 false**（空镜）。判断主体是否存在要看整段，不能只看某一帧；白色产品也算主体，不要因为颜色浅就判 false。
    - readable_claims：只有在包装文字/标识实际可读时逐字记录；看不清就填空数组。
    - pointing_to_text：只有手指/手势明确指向该可读包装文字或标识才为 true；普通拿包装、展示包装、手在旁边不算指向。
    - pointing_action/gesture：简短描述指向动作；没有明确指向时填空。包装文字只可支持脚本中同一条包装卖点，不能证明真实性或吸收、柔软等功能。
    - 对 CTA、促销、划算、囤货、数量话术，只有多包、成排、整箱、堆叠或清晰数量关系才填写对应量感标签；单包静态特写不得当作 CTA 量感证据。
- action_complete：判断这个镜头能否整段直接用作口播证据（画面是否处于稳定、完整、可用的展示状态）。判据是主体状态，不是画面里有没有人、有没有手：
  * 有人手拿着、举着、托着、指着、按着产品做展示——属于静态展示，只要产品主体完整可见就为 true；手入镜本身不构成 false 的理由。
  * 画面里没有人物，产品静置摆放（平铺、叠放、散落）——为 true。
  * 只有画面明显停在动作中途、或产品主体被遮挡/未完整入镜时才为 false（例如手正在伸入、正在放下、正在倾倒、正在擦拭且尚未到位，画面结束时仍处于该状态）。
  * 判断是否停在动作中途时，对比同一 shot 的 mid 与 end 两帧：两帧主体、位置、姿态基本一致→已静止或已完成→true；两帧明显不同且末帧仍不到位→false。
  * 不确定时按“产品主体是否完整可见、画面是否稳定”判断，不要因为有人手出现就判 false。
- head_waste/tail_waste：镜头开头/结尾属于无效画面的比例，0 到 1。无效指等待、空镜、动作尚未开始或已结束后的余量、机位调整、遮挡（例：整段都是有效展示填 0.0；开头约三成在等动作开始填 0.3）。只填确实观察到的废料比例，确认没有废料才填 0.0，不要用 0.0 表示“不确定”。
- evidence_frame：只能填 mid、end 或空字符串，选择最能证明 evidence_tags 的那一帧。
- confidence：0 到 1。任何不确定都降低置信度并减少 evidence_tags。

{mapping}
"""


def enrich_with_vision(manifest: dict[str, Any], analysis: dict[str, Any], report_dir: Path,
                      *, profile: str | None = None, batch_size: int = 12,
                      timeout: int = 300, ffmpeg: str | None = None) -> dict[str, Any]:
    """Use Codex image input to fill and validate every temporary shot row."""
    profile = _clean_profile(
        profile if profile is not None else os.environ.get("JY_VISION_PROFILE", "volc"))
    shots = [row for row in analysis.get("shots", []) if isinstance(row, dict)]
    if not shots:
        return analysis
    cache_path = _vision_cache_path(report_dir)
    cache = _load_vision_cache(cache_path)
    cache_hits = 0
    cache_misses = 0
    cache_keys: dict[str, str] = {}
    uncached_shots: list[dict[str, Any]] = []
    for shot in shots:
        shot_id = str(shot.get("shot_id") or "")
        key = _vision_cache_key(manifest, shot, profile)
        if key:
            cache_keys[shot_id] = key
        cached = cache.get(key) if key else None
        if (isinstance(cached, dict)
                and cached.get("status") == "ready_for_matching"
                and shot.get("frame_exists") is not False):
            _apply_cached_visual(shot, cached, key)
            cache_hits += 1
            continue
        shot["analysis_cache_hit"] = False
        if key:
            shot["analysis_cache_key"] = key
        cache_misses += 1
        uncached_shots.append(shot)
    shots_to_analyze = uncached_shots

    def _set_cache_summary(summary: dict[str, Any], *, partial: bool = False) -> None:
        summary.update({
            "analysis_version": VISION_ANALYSIS_VERSION,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_path": str(cache_path),
            "partial": partial,
        })
        analysis["vision_analysis"] = summary

    if not shots_to_analyze:
        _set_cache_summary(_vision_summary(profile, [], [], [], partial=False))
        analysis["pending_items"] = list(analysis.get("pending_items", []))
        analysis["ok"] = all(row.get("status") == "ready_for_matching" for row in shots)
        _persist(report_dir, analysis)
        return analysis
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for shot in shots_to_analyze:
        frames = _shot_frames(shot)
        if not frames:
            shot.setdefault("status", "pending_analysis")
            shot.setdefault("analysis_error", "no readable frame")
            continue
        for frame in frames:
            current.append({**frame, "shot_id": shot["shot_id"],
                            "source_start": _number(shot.get("source_start")),
                            "source_end": _number(shot.get("source_end")),
                            "label": f"{shot['shot_id']}|{frame['suffix']}"})
            if len(current) >= max(1, batch_size) * 2:
                batches.append(current)
                current = []
    if current:
        batches.append(current)
    # Keep a shot together in one response.  With two frames per shot this is
    # effectively batch_size shots per call.
    report_dir.mkdir(parents=True, exist_ok=True)
    by_id = {str(row.get("shot_id")): row for row in shots}
    vision_errors: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="jy_vision_", dir=str(report_dir)) as temp:
        temp_dir = Path(temp)
        schema = _write_schema(temp_dir)
        for batch_index, batch in enumerate(batches, 1):
            if batch_index > 1:
                # Rewrite the summary too: a partial file must not claim the
                # previous attempt's batch count, and must never claim ok.
                _set_cache_summary(
                    _vision_summary(profile, batches, responses, vision_errors, partial=True),
                    partial=True)
                analysis["ok"] = False
                _persist(report_dir, analysis)
            sheet = temp_dir / f"sheet_{batch_index:04d}.jpg"
            response_file = temp_dir / f"response_{batch_index:04d}.txt"
            if not _write_sheet(batch, sheet, ffmpeg=ffmpeg):
                vision_errors.append({"type": "VISION_SHEET_FAILED", "batch": batch_index})
                continue
            ok, text = _run_vision_with_retry(
                sheet,
                _vision_prompt(batch, batch_index, len(batches)),
                response_file,
                profile=profile,
                timeout=timeout,
                workdir=temp_dir,
                schema=schema,
            )
            if not ok:
                vision_errors.append({"type": "VISION_CALL_FAILED", "batch": batch_index,
                                      "message": text[-4000:]})
                continue
            try:
                payload = _json_from_text(text)
                rows = payload if isinstance(payload, list) else payload.get("shots", [])
                if not isinstance(rows, list):
                    raise ValueError("response JSON is not a shot array")
            except (ValueError, TypeError, AttributeError) as exc:
                vision_errors.append({"type": "VISION_JSON_INVALID", "batch": batch_index,
                                      "message": str(exc), "response": text[-2000:]})
                continue
            responses.append({"batch": batch_index, "shot_count": len(rows)})
            frame_map = {item["label"]: item for item in batch}
            seen: set[str] = set()
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                shot_id = str(raw.get("shot_id") or raw.get("shot_label") or "").strip()
                if shot_id not in by_id or shot_id in seen:
                    continue
                seen.add(shot_id)
                row = by_id[shot_id]
                description = str(raw.get("description") or "").strip()
                visual_tags = _as_list(raw.get("visual_tags"))
                evidence_tags = _as_list(raw.get("evidence_tags"))
                action_complete = raw.get("action_complete") is True
                confidence = max(0.0, min(1.0, _number(raw.get("confidence"), 0.0)))
                if not description or not visual_tags or not evidence_tags or confidence <= 0:
                    row["status"] = "pending_analysis"
                    row["analysis_error"] = "vision response lacks auditable description/tags/confidence"
                    continue
                row.update({
                    "description": description,
                    "visual_description": description,
                    "visual_tags": visual_tags,
                    "evidence_tags": evidence_tags,
                    "action_complete": action_complete,
                    "head_waste": max(0.0, min(1.0, _number(raw.get("head_waste")))),
                    "tail_waste": max(0.0, min(1.0, _number(raw.get("tail_waste")))),
                    "analysis_confidence": confidence,
                    "analysis_source": "codex_vision_contact_sheet",
                    "status": "ready_for_matching" if action_complete else "pending_analysis",
                    # DEMO_ACTIONS V0.2 素材侧字段。缺失时**不写键**（而不是写
                    # None）—— demo_actions.judge() 靠「键不存在」区分「旧分析结果」
                    # 与「模型明确说了没有动作/没有主体」，写 None 会把后者也当成
                    # 未覆盖而放行。
                    **_demo_action_fields(raw),
                })
                if not action_complete:
                    # 视觉结果本身已通过校验（description/tags/confidence 齐全），
                    # 这里只是动作状态不达标。必须显式写明原因，否则会被下面的
                    # setdefault 盖上 "no validated vision result" —— 那行字会让人
                    # 误以为视觉分析失败，实际不是。
                    row["analysis_error"] = (
                        "action_complete=false：模型判定该镜头动作未完成或画面非稳定展示状态，未进入匹配池")
                chosen = str(raw.get("evidence_frame") or "mid").strip().lower()
                frame = next((f for f in _shot_frames(row) if f["suffix"] == chosen), None)
                if frame is None:
                    frame = next(iter(_shot_frames(row)), None)
                row["evidence_intervals"] = _evidence_intervals(
                    raw.get("evidence_intervals"), row, frame, evidence=bool(evidence_tags))
                cache_key = cache_keys.get(shot_id, "")
                cached_projection = _cache_projection(row)
                if cache_key and cached_projection is not None:
                    cache[cache_key] = cached_projection
            # Persist after every completed vision batch so a later timeout
            # does not discard the understanding work that already finished.
            _save_vision_cache(cache_path, cache)
    # Do not silently retain the placeholder output produced by the old flow.
    for row in shots:
        if row.get("status") != "ready_for_matching":
            row["status"] = "pending_analysis"
            row.setdefault("analysis_error", "no validated vision result")
    analysis["shots"] = shots
    _set_cache_summary(_vision_summary(profile, batches, responses,
                                       vision_errors, partial=False), partial=False)
    analysis["pending_items"] = list(analysis.get("pending_items", [])) + vision_errors
    analysis["ok"] = bool(shots) and not analysis["pending_items"] and all(
        row.get("status") == "ready_for_matching" for row in shots)
    _persist(report_dir, analysis)
    return analysis


def match_with_codex(manifest: dict[str, Any], analysis: dict[str, Any], report_dir: Path,
                     *, profile: str | None = None, timeout: int = 300) -> dict[str, Any]:
    """Use the same observed shot descriptions to rank each spoken sentence."""
    profile = _clean_profile(
        profile if profile is not None else os.environ.get("JY_VISION_PROFILE", "volc"))
    claims = []
    text = str(manifest.get("full_script") or manifest.get("script") or "")
    parts = re.split(r"[。！？!?；;\n\r]+", text)
    if manifest.get("segments"):
        claims = [{"index": i, "text": str(s.get("text", s.get("caption", "")))}
                  for i, s in enumerate(manifest.get("segments", []), 1)]
    else:
        claims = [{"index": i, "text": p.strip()} for i, p in enumerate(parts, 1) if p.strip()]
    shots = [s for s in analysis.get("shots", []) if s.get("status") == "ready_for_matching"]
    if not claims or not shots:
        analysis["semantic_matches"] = []
        return analysis
    catalog = "\n".join(
        f"{s['shot_id']} | {s.get('source_start', 0):.3f}-{s.get('source_end', 0):.3f}s | "
        f"描述:{s.get('description','')} | 画面标签:{'、'.join(_as_list(s.get('visual_tags')))} | "
        f"证据:{'、'.join(_as_list(s.get('evidence_tags')))} | action_complete={s.get('action_complete')}"
        for s in shots
    )
    claim_text = "\n".join(f"{c['index']}. {c['text']}" for c in claims)
    prompt = f"""你是剪映脚本到镜头的匹配器。只使用下面已经由视觉模型观察到的镜头描述，不得编造描述或选择列表以外的 shot_id。
每句口播必须匹配真实画面：画面在展示什么，口播就应讲什么。对没有直接证据的句子返回空 top3，不要用产品无关空镜凑数。输出严格 JSON 数组：
[{{"index":1,"top3":[{{"shot_id":"...","score":0,"matched_claims":["..."],"reason":"..."}}]}}]
score 0-100；只有明确匹配并且证据足够才给 70 以上；`matched_claims` 必须写观察到的画面事实，不要把口播原文复制成证据。

口播句：
{claim_text}

已分析镜头：
{catalog}
"""
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jy_match_", dir=str(report_dir)) as temp:
        temp_dir = Path(temp)
        response = temp_dir / "response.txt"
        prompt_path = temp_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        # Text-only call: no image attachment.  v1.3.20 routes it through the
        # same file-backed runner as the vision batches.  It previously kept
        # the old stdout=PIPE pattern, which is the same deadlock v1.3.17
        # fixed in _run_vision and left behind here.
        ok, code, error = _run_codex_file(
            _codex_command(None, response, profile, workdir=temp_dir),
            prompt_path, temp_dir / "stdout.log", temp_dir / "stderr.log", timeout)
        if error:
            analysis["semantic_match_error"] = f"match call {error}"
            analysis["semantic_matches"] = []
        else:
            raw_text = (response.read_text(encoding="utf-8", errors="replace")
                        if response.is_file() and response.stat().st_size
                        else (_tail(temp_dir / "stderr.log") or _tail(temp_dir / "stdout.log")))
            try:
                payload = _json_from_text(raw_text)
                analysis["semantic_matches"] = payload if isinstance(payload, list) else payload.get("matches", [])
            except (ValueError, TypeError, AttributeError) as exc:
                analysis["semantic_match_error"] = str(exc)
                analysis["semantic_matches"] = []
    (report_dir / "shot_match_ai.json").write_text(
        json.dumps({"claims": claims, "matches": analysis.get("semantic_matches", []),
                    "error": analysis.get("semantic_match_error")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return analysis
