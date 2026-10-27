"""Local chat + workflow canvas studio for VibeFilming."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import media_validation
from . import workspace as ws
from . import workflow_canvas
from .server_config import (
    bind_http_server,
    require_explicit_remote_bind,
    resolve_server_settings,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8764

# 上传能力由 manifest.material_types 声明；这里只保留请求体绝对上限。
MAX_UPLOAD_BYTES = ws.MAX_MATERIAL_BYTES
_MULTIPART_SLACK = 1 * 1024 * 1024   # multipart 头部/边界的额外开销余量

_STATE: dict[str, Any] = {
    "agent": None,
    "server": None,
    "thread": None,
    "url": None,
    "jobs": {},
    "canvas_project": None,
    "canvas_url": None,
    "host": DEFAULT_HOST,
    # project_id -> list[str]：上传成功后待注入下一轮对话的素材提示
    "pending_notes": {},
}
_LOCK = threading.Lock()
# 串行提交、项目切换和素材入库；GET 状态与后台生成不占此锁。
_MUTATION_LOCK = threading.RLock()

# 参考视频预览副本：把 H.265/HEVC 等浏览器不兼容的编码转成 H.264 + faststart，
# 落盘到 materials/previews/，manifest 用 material_previews 映射原始路径 -> 预览路径。
PREVIEW_DIR_NAME = "previews"
PREVIEW_SUFFIX = ".preview.mp4"
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_INFLIGHT: set[str] = set()   # 已触发后台转码的源文件绝对路径（防每 2s 轮询重复转码）


_INTERNAL_BLOCK_RE = re.compile(
    r"<(?:summary|thinking|think|tool_use|tool_call)>[\s\S]*?</(?:summary|thinking|think|tool_use|tool_call)>",
    re.IGNORECASE,
)
_TURN_HEADER_RE = re.compile(
    r"^\s*(?:\*\*)?(?:LLM Running \(Turn \d+\)|Turn \d+) \.\.\.(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TOOL_LINE_RE = re.compile(r"^\s*🛠️(?:\s+Tool:)?[\s\S]*$", re.IGNORECASE)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _public_reply(item: dict[str, Any]) -> str:
    """Return only the final user-facing answer from an agent task result."""
    outputs = item.get("outputs")
    candidates = reversed(outputs) if isinstance(outputs, list) else [item.get("done", "")]
    for candidate in candidates:
        parts = _TURN_HEADER_RE.split(str(candidate or ""))
        text = _INTERNAL_BLOCK_RE.sub("", parts[-1])
        lines = []
        in_tool_dump = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("`````"):
                in_tool_dump = not in_tool_dump
                continue
            if in_tool_dump or _TURN_HEADER_RE.match(line) or _TOOL_LINE_RE.match(line):
                continue
            if stripped.startswith(("[Info]", "[Warn]", "[System]", "[AutoContinue]")):
                continue
            lines.append(line)
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if cleaned:
            return cleaned
    return "本轮任务已完成，项目画布已更新。"


# 从 agent 输出中按原始顺序提取 summary 与工具调用。
_SUMMARY_RE = re.compile(r"<summary>([\s\S]*?)</summary>", re.IGNORECASE)
_TOOL_ACTIVITY_RE = re.compile(
    r"^[ \t]*🛠️\s*(?:Tool:\s*`?)?([A-Za-z_]\w*)`?([^\r\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_TOOL_LABELS = {
    "project_create": "📁 创建项目工作区",
    "project_open": "📂 切换项目",
    "project_update_phase": "📌 更新项目进度",
    "workflow_canvas": "🗺️ 刷新工作流画布",
    "gen_image": "🎨 生成画面素材",
    "gen_video_t2v": "🎬 生成视频镜头",
    "query_video_task": "⏳ 等待视频渲染",
    "cancel_video_task": "🛑 取消视频任务",
    "video_concat": "🎞️ 拼接视频段",
    "video_trim": "✂️ 裁剪视频",
    "video_speed": "⏩ 调整视频速度",
    "video_overlay": "🖼️ 画中画叠加",
    "video_portrait": "📱 横屏转竖屏",
    "audio_process": "🔊 处理音频",
    "gen_audio_bgm": "🎵 生成配乐",
    "query_audio_task": "⏳ 等待配乐生成",
    "extract_frames": "🖼️ 抽取视频帧",
    "code_run": "⚙️ 运行脚本",
    "file_read": "📖 查阅资料",
    "file_write": "📝 写入文件",
    "file_patch": "✏️ 修改文件",
    "web_scan": "🌐 浏览网页",
    "web_execute_js": "🌐 操作网页",
    "update_working_checkpoint": "🧠 记录工作要点",
    "ask_user": "❓ 向你确认",
    "start_long_term_update": "🧠 更新长期记忆",
}


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, f"🔧 {name}")


def _compact_trace_text(text: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _tool_detail(raw_suffix: str) -> str:
    """Extract a compact argument preview from the non-verbose tool line."""
    detail = str(raw_suffix or "").strip()
    if detail.startswith("(") and detail.endswith(")"):
        detail = detail[1:-1].strip()
    elif "args:" in detail:
        # verbose 模式的参数在后续代码块里，不把装饰文字当参数展示。
        detail = ""
    return _compact_trace_text(detail)


def _extract_trace_events(text: str) -> list[dict[str, str]]:
    """Extract model summaries and tool calls in their original output order."""
    found: list[tuple[int, dict[str, str]]] = []
    source = str(text or "")
    for match in _SUMMARY_RE.finditer(source):
        summary = _compact_trace_text(match.group(1))
        if summary:
            found.append((match.start(), {
                "type": "trace",
                "kind": "summary",
                "text": summary,
            }))
    for match in _TOOL_ACTIVITY_RE.finditer(source):
        name = match.group(1)
        found.append((match.start(), {
            "type": "trace",
            "kind": "tool",
            "text": _tool_label(name),
            "tool": name,
            "detail": _tool_detail(match.group(2)),
        }))
    found.sort(key=lambda item: item[0])
    return [event for _, event in found]


def _append_event(job: dict[str, Any], event: dict[str, Any], status: str | None = None) -> None:
    with _LOCK:
        job["events"].append(event)
        job["updated_at"] = time.time()
        if status:
            job["status"] = status


def _assert_idle() -> None:
    agent = _STATE.get("agent")
    pending = getattr(getattr(agent, "task_queue", None), "unfinished_tasks", 0)
    with _LOCK:
        busy = any(j.get("status") in {"queued", "running"} for j in _STATE["jobs"].values())
    if busy or pending or (agent and agent.is_running):
        raise RuntimeError("当前任务正在排队或运行，请停止或等待完成")


def _check_project(expected: str | None) -> None:
    if expected and expected != ws.get_active_project():
        raise RuntimeError("活跃项目已变化，请刷新后再操作")


def _reset_agent_context(agent) -> None:
    """Start a clean conversation context before creating or opening a project."""
    _assert_idle()
    if not agent:
        return
    agent.history = []
    agent.handler = None
    clients = getattr(agent, "llmclients", None) or [getattr(agent, "llmclient", None)]
    for client in clients:
        if client is None:
            continue
        try:
            client.backend.history = []
        except Exception:
            pass
        try:
            client.last_tools = ""
        except Exception:
            pass


def _activate_project(project_id: str) -> dict[str, Any]:
    """Switch both workspace and agent context to one explicit project."""
    manifest = ws.read_manifest(project_id)
    agent = _STATE.get("agent")
    _reset_agent_context(agent)
    ws.set_active_project(project_id)
    _STATE["canvas_project"] = None
    _STATE["canvas_url"] = None
    if agent:
        trace_path = str(ws.project_dir(project_id) / "logs" / "llm_trace.txt")
        for client in getattr(agent, "llmclients", None) or [getattr(agent, "llmclient", None)]:
            if client is not None:
                client.trace_path = trace_path
    return manifest


def _new_project(brief: str, execution_mode: str | None = None,
                 remake_spec: str = "",
                 video_model: str | None = None) -> dict[str, Any]:
    brief = str(brief or "").strip()
    if not brief:
        raise ValueError("请输入影片项目名称")
    agent = _STATE.get("agent")
    _reset_agent_context(agent)
    manifest = ws.project_create(
        brief,
        execution_mode=execution_mode,
        remake_spec=remake_spec,
        video_model=video_model,
    )
    _activate_project(manifest["project_id"])
    return manifest


def _ensure_active_project(
        brief_hint: str = "", execution_mode: str | None = None,
        remake_spec: str = "", video_model: str | None = None) -> str:
    """返回当前活跃项目；没有则用 brief_hint 自动建一个（上传/发消息即建项目）。"""
    pid = ws.get_active_project()
    if pid:
        if execution_mode:
            ws.set_execution_mode(pid, execution_mode, source="studio")
        if remake_spec:
            ws.set_remake_spec(pid, remake_spec)
        if video_model:
            ws.set_video_model(pid, video_model)
        return pid
    hint = str(brief_hint or "").strip()
    brief = hint[:40] if hint else "未命名项目"
    return _new_project(
        brief,
        execution_mode=execution_mode,
        remake_spec=remake_spec,
        video_model=video_model,
    )["project_id"]


def _active_canvas() -> dict[str, Any]:
    project_id = ws.get_active_project()
    if not project_id:
        return {"project_id": None, "canvas_url": None}
    try:
        project_dir = ws.project_dir(project_id)
        try:
            manifest = ws.read_manifest(project_id)
        except (OSError, json.JSONDecodeError):
            manifest = {}
        live = None
        if _STATE.get("canvas_project") == project_id and _STATE.get("canvas_url"):
            canvas_url = _STATE["canvas_url"]
        else:
            live = workflow_canvas.ensure_live_server(
                project_dir,
            )
            canvas_url = live["url"] if live else None
            _STATE["canvas_project"] = project_id
            _STATE["canvas_url"] = canvas_url
        return {
            "project_id": project_id,
            "project_brief": manifest.get("brief") or project_id,
            "remake_spec": ws.normalize_remake_spec(
                manifest.get("remake_spec", "")),
            "project_dir": str(project_dir),
            "canvas_url": canvas_url,
            "execution_mode": ws.execution_state_from_manifest(manifest),
        }
    except Exception as exc:
        return {"project_id": project_id, "canvas_url": None, "error": str(exc)}


def _which_tool(name: str) -> str | None:
    return shutil.which(name)


def _ffmpeg_tool() -> str | None:
    """Prefer system FFmpeg, then use the binary bundled by imageio-ffmpeg."""
    system = _which_tool("ffmpeg")
    if system:
        return system
    try:
        from .film_sdk import FFMPEG

        return str(FFMPEG) if Path(FFMPEG).is_file() else None
    except (ImportError, OSError, TypeError):
        return None


_CODEC_CACHE: dict[str, bool] = {}


def _video_codec_is_playable(path: Path) -> bool:
    """探测视频编码是否浏览器可直接播放（H.264/AVC）。探测失败按可播放处理，避免误转。

    结果按 (路径, mtime) 缓存，避免每 2s 状态轮询重复起 ffprobe。
    """
    ffprobe = _which_tool("ffprobe")
    key = f"{path}:{path.stat().st_mtime_ns if path.exists() else 0}"
    cached = _CODEC_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        if ffprobe:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            codec = (r.stdout or "").strip().lower()
        else:
            ffmpeg = _ffmpeg_tool()
            if not ffmpeg:
                return True
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            match = re.search(r"Video:\s*([^,\s]+)", r.stderr or "")
            codec = match.group(1).strip().lower() if match else ""
    except (OSError, subprocess.SubprocessError):
        return True
    playable = codec in ("h264", "avc1", "")
    _CODEC_CACHE[key] = playable
    return playable


def _transcode_preview(src: Path, dest: Path) -> bool:
    """把视频转成浏览器通用格式（H.264 + AAC + faststart）。成功返回 True。"""
    ffmpeg = _ffmpeg_tool()
    if not ffmpeg:
        return False
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(src),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart",
             "-max_muxing_queue_size", "1024",
             str(dest)],
            capture_output=True, timeout=3600,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return dest.is_file() and dest.stat().st_size > 0


def _register_preview(project_id: str, src: Path, preview: Path) -> None:
    """把预览副本路径登记进 manifest.material_previews。"""
    def _apply(m: dict):
        m.setdefault("material_previews", {})[str(src)] = str(preview)
    try:
        ws.update_manifest(project_id, _apply)
    except (OSError, json.JSONDecodeError):
        pass


def _ensure_video_preview(project_id: str, src: Path, materials_dir: Path) -> Path | None:
    """返回可预览的视频路径（优先 H.264 预览副本）。缺副本时后台转码并先回退原片。"""
    src_abs = str(src)
    preview = materials_dir / PREVIEW_DIR_NAME / f"{src.stem}{PREVIEW_SUFFIX}"
    if preview.is_file() and preview.stat().st_size > 0:
        return preview
    # 已在转码中：直接回退原片，等下一轮状态轮询拿到新副本
    with _PREVIEW_LOCK:
        if src_abs in _PREVIEW_INFLIGHT:
            return None
    if _video_codec_is_playable(src):
        return src
    # 需要转码：标记 in-flight 后起后台线程，避免每 2s 轮询重复转码
    with _PREVIEW_LOCK:
        if src_abs in _PREVIEW_INFLIGHT:
            return None
        _PREVIEW_INFLIGHT.add(src_abs)

    def _work():
        try:
            preview.parent.mkdir(parents=True, exist_ok=True)
            if _transcode_preview(src, preview):
                if src.is_file():
                    _register_preview(project_id, src, preview)
                else:
                    # 转码期间源视频已被删除：丢弃副本，避免留下孤儿登记
                    preview.unlink(missing_ok=True)
        finally:
            with _PREVIEW_LOCK:
                _PREVIEW_INFLIGHT.discard(src_abs)

    threading.Thread(target=_work, daemon=True).start()
    return None


def _project_materials_state(project_id: str | None) -> dict[str, list[dict[str, Any]]]:
    """把 manifest.materials 里的本地路径转成前端可预览的 /api/materials/ URL。

    参考视频优先返回浏览器兼容的 H.264 预览副本（materials/previews/）；副本缺失时
    触发后台转码并临时回退原片路径。
    """
    if not project_id:
        return {}
    try:
        manifest = ws.read_manifest(project_id)
    except (OSError, json.JSONDecodeError):
        return {}
    materials_dir = ws.project_dir(project_id) / "materials"
    previews = manifest.get("material_previews") or {}
    type_map = {item["id"]: item for item in ws.material_type_definitions(manifest)}
    result: dict[str, list[dict[str, Any]]] = {}
    for category, paths in (manifest.get("materials") or {}).items():
        material_type = type_map.get(category) or {}
        media_kind = str(material_type.get("media_kind") or "file")
        items = []
        for p in paths:
            path_obj = Path(p)
            if not path_obj.is_file():
                continue
            url_path: Path | None = None
            if media_kind == "video":
                registered = previews.get(str(p)) or previews.get(str(path_obj))
                if registered:
                    reg_path = Path(registered)
                    if reg_path.is_file() and reg_path.stat().st_size > 0:
                        url_path = reg_path
                    else:
                        url_path = _ensure_video_preview(project_id, path_obj, materials_dir)
                else:
                    url_path = _ensure_video_preview(project_id, path_obj, materials_dir)
            if url_path is None:
                url_path = path_obj
            try:
                rel = url_path.relative_to(materials_dir)
            except ValueError:
                # Manually authored manifests may reference trusted files outside
                # materials/. Never expose arbitrary filesystem paths through HTTP.
                items.append({
                    "filename": path_obj.name,
                    "url": "",
                    "path": str(path_obj),
                    "media_kind": media_kind,
                    "preview_unavailable": True,
                })
                continue
            items.append({
                "filename": path_obj.name,
                "url": f"/api/materials/{quote(project_id, safe='')}/{quote(rel.as_posix(), safe='/')}",
                "path": str(path_obj),
                "media_kind": media_kind,
            })
        if items:
            result[category] = items
    return result


def _material_types_state(project_id: str | None) -> list[dict[str, Any]]:
    manifest = None
    if project_id:
        try:
            manifest = ws.read_manifest(project_id)
        except (OSError, json.JSONDecodeError):
            manifest = None
    return ws.material_type_definitions(manifest)


# 调试模式：只推进到指定阶段就停（跳过最耗时的视频生成，快速验证前期流程）
DEBUG_STOP_POINTS = (
    ("breakdown", "拆镜"),
    ("story", "剧本"),
    ("entity", "编导方案"),
    ("asset", "参考资产与故事板"),
    ("animate", "视频段"),
    ("compose", "合成"),
    ("review", "审查"),
)
assert tuple(key for key, _ in DEBUG_STOP_POINTS) == ws.DEBUG_STOP_PHASES


def _debug_state(project_id: str | None) -> dict[str, Any]:
    if not project_id:
        return {}
    try:
        manifest = ws.read_manifest(project_id)
    except (OSError, json.JSONDecodeError):
        return {}
    if manifest.get("debug"):
        return ws.normalize_debug_state(project_id)
    return {}


def _execution_mode_state(project_id: str | None) -> dict[str, Any]:
    if not project_id:
        from . import execution_mode
        return execution_mode.state(None, source="default")
    try:
        return ws.normalize_execution_mode(project_id)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _video_model_state(project_id: str | None) -> dict[str, Any]:
    manifest = None
    if project_id:
        try:
            manifest = ws.read_manifest(project_id)
        except (OSError, json.JSONDecodeError):
            manifest = None
    return ws.video_model_state(manifest)


def _set_video_model(project_id: str | None, selection: str) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "no_active_project"}
    try:
        selected = ws.set_video_model(project_id, selection)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "video_model": selected,
        "material_types": _material_types_state(project_id),
    }


def _set_execution_mode(project_id: str | None, mode: str) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "no_active_project"}
    try:
        selected = ws.set_execution_mode(project_id, mode, source="studio")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "execution_mode": selected}


def _set_remake_spec(project_id: str | None, value) -> dict[str, Any]:
    if not project_id:
        return {"ok": False, "error": "no_active_project"}
    try:
        remake_spec = ws.set_remake_spec(project_id, value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "remake_spec": remake_spec}


def _set_debug(project_id: str | None, enabled: bool, stop_after: str) -> dict[str, Any]:
    """写 manifest.debug：enabled=True 时流程只推进到 stop_after 阶段即停。"""
    if not project_id:
        return {"ok": False, "error": "no_active_project"}
    if stop_after not in {p for p, _ in DEBUG_STOP_POINTS}:
        return {"ok": False, "error": f"stop_after 非法：{stop_after}"}

    def _apply(m: dict):
        m["debug"] = {"enabled": bool(enabled), "stop_after": stop_after}

    try:
        ws.update_manifest(project_id, _apply)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"更新 manifest 失败：{exc}"}
    return {"ok": True, "debug": _debug_state(project_id)}


def _project_timing(project_id: str | None) -> dict[str, Any]:
    """汇总项目耗时：各阶段 duration_s + 按工具聚合的调用耗时，供 Studio 耗时面板展示。"""
    if not project_id:
        return {}
    try:
        manifest = ws.read_manifest(project_id)
    except (OSError, json.JSONDecodeError):
        return {}
    phases = manifest.get("phases") or {}
    phase_items = []
    phase_definitions = ws.workflow_phase_definitions(manifest)
    phase_order = [item["id"] for item in phase_definitions]
    phase_labels = {item["id"]: item["label"] for item in phase_definitions}
    for name in phase_order:
        ph = phases.get(name) or {}
        if not isinstance(ph, dict):
            continue
        phase_items.append({
            "phase": name,
            "label": phase_labels.get(name, name),
            "status": str(ph.get("status", "pending")),
            "duration_s": ph.get("duration_s"),
        })
    # 阶段清单之外的 phase（如自定义扩展）也纳入，避免漏统计
    for name, ph in phases.items():
        if name in phase_order or not isinstance(ph, dict):
            continue
        phase_items.append({
            "phase": str(name),
            "label": str(name),
            "status": str(ph.get("status", "pending")),
            "duration_s": ph.get("duration_s"),
        })

    tool_stats: dict[str, dict[str, Any]] = {}
    log_path = ws.project_dir(project_id) / "logs" / "tool_calls.jsonl"
    if log_path.is_file():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dur = item.get("duration_s")
                if not isinstance(dur, (int, float)):
                    continue
                stat = tool_stats.setdefault(str(item.get("tool", "?")), {"count": 0, "duration_s": 0.0})
                stat["count"] += 1
                stat["duration_s"] += float(dur)
        except OSError:
            pass
    tools_sorted = sorted(tool_stats.items(), key=lambda kv: -kv[1]["duration_s"])
    total_tool_s = round(sum(v["duration_s"] for _, v in tools_sorted), 1)
    total_phase_s = round(sum(
        float(p.get("duration_s") or 0)
        for p in (phases or {}).values() if isinstance(p, dict)
    ), 1)
    return {
        "phases": phase_items,
        "tools": [
            {"tool": t, "count": v["count"], "duration_s": round(v["duration_s"], 1)}
            for t, v in tools_sorted
        ],
        "total_tool_s": total_tool_s,
        "total_phase_s": total_phase_s,
    }


def _delete_material(project_id: str | None, category: str, src_path: str) -> dict[str, Any]:
    """删除一个已归档素材：落盘文件 + manifest 登记（materials / entities / material_previews）。

    src_path 为 manifest.materials 里登记的源文件绝对路径（前端从 /api/state 的 path 字段回传）。
    参考视频会连带删除其预览副本；图片若带 entity 硬锁会一并解除该实体。
    """
    if not project_id:
        return {"ok": False, "error": "no_active_project"}
    src = Path(src_path)
    if not src.is_absolute():
        return {"ok": False, "error": "素材路径必须为绝对路径"}
    try:
        pdir = ws.project_dir(project_id)
        manifest = ws.read_manifest(project_id)
    except (ValueError, FileNotFoundError):
        return {"ok": False, "error": "项目不存在"}
    type_map = {item["id"]: item for item in ws.material_type_definitions(manifest)}
    material_type = type_map.get(category)
    if not material_type:
        return {"ok": False, "error": f"category 非法：{category or '(空)'}"}
    materials_dir = (pdir / "materials").resolve()
    src_resolved = src.resolve()
    try:
        src_resolved.relative_to(materials_dir)
    except ValueError:
        return {"ok": False, "error": "素材路径不在该项目 materials/ 下"}

    to_delete: list[Path] = []
    removed_entity: str | None = None

    def _apply(m: dict):
        nonlocal removed_entity
        # 1. 从 materials[category] 移除该路径
        mats = [p for p in (m.get("materials") or {}).get(category, [])]
        m.setdefault("materials", {})[category] = [
            p for p in mats if Path(p).resolve() != src_resolved
        ]
        # 2. 移除预览副本映射，并连带删除 preview 文件
        previews = m.get("material_previews") or {}
        preview_path = previews.pop(str(src), None)
        if preview_path:
            preview_obj = Path(preview_path)
            try:
                preview_obj.resolve().relative_to(materials_dir)
                to_delete.append(preview_obj)
            except ValueError:
                pass
        if previews:
            m["material_previews"] = previews
        else:
            m.pop("material_previews", None)
        # 3. 图片若被某实体硬锁，解除该实体并删除 entities/ 下的对应文件。
        #    实体文件由上传时按 `{entity_name}_{src.stem}{suffix}` 拷贝到 entities/，
        #    与源素材路径不同，因此用文件名规则反查（优先 source_material 精确匹配）。
        if material_type.get("entity_lock"):
            src_stem = src_resolved.stem
            for ename, edata in list((m.get("entities") or {}).items()):
                if not isinstance(edata, dict):
                    continue
                refs = [Path(p) for p in (edata.get("ref_pack") or [])]
                source = edata.get("source_material")
                matched = source and Path(str(source)).resolve() == src_resolved
                matched_refs = refs if matched else [r for r in refs if r.stem == f"{ename}_{src_stem}"]
                if matched_refs:
                    m.setdefault("entities", {}).pop(ename, None)
                    to_delete.extend(r for r in matched_refs if r.resolve() != src_resolved)
                    removed_entity = ename
        # 4. 重新计算硬锁标志
        entities = m.get("entities") or {}
        if any(isinstance(v, dict) and v.get("locked_from_upload") for v in entities.values()):
            m["_locked_no_new"] = True
        else:
            m.pop("_locked_no_new", None)

    try:
        ws.update_manifest(project_id, _apply)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"更新 manifest 失败：{exc}"}

    # 落盘删除放在 manifest 更新之后：先改登记，文件删除失败也不影响一致性
    for f in to_delete:
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass
    if src_resolved.is_file():
        try:
            src_resolved.unlink()
        except OSError:
            pass
    return {"ok": True, "removed_entity": removed_entity, "path": str(src_resolved)}


def _drain_job(job_id: str, display_queue):
    job = _STATE["jobs"][job_id]
    acc = ""          # 累积的中间输出，用于稳定地重扫追踪事件
    emitted = 0       # 已推送的追踪事件条数
    try:
        while True:
            item = display_queue.get()
            if "done" in item:
                # done 事件带的是完整全文，直接以它为准重扫（acc 是其增量子集，不叠加以免重复）
                final_text = item.get("done", "") or acc
                trace_events = _extract_trace_events(final_text)
                for event in trace_events[emitted:]:
                    _append_event(job, event)
                emitted = len(trace_events)
                _append_event(job, {"type": "done", "text": _public_reply(item)},
                              status="done")
                break
            # 中间 chunk：next 可能是增量片段，也可能是全量快照（取决于 agent.inc_out）。
            # 若新片段以已累积文本开头，说明是全量快照，直接替换；否则按增量追加。
            chunk = item.get("next", "")
            acc = chunk if chunk.startswith(acc) else acc + chunk
            safe = acc.rsplit("\n", 1)[0] if "\n" in acc else ""
            trace_events = _extract_trace_events(safe)
            for event in trace_events[emitted:]:
                _append_event(job, event)
            emitted = len(trace_events)
    except Exception as exc:
        _append_event(job, {"type": "error", "text": str(exc)}, status="error")



# ============== 素材上传：multipart 流式解析 ==============
_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)
_PART_NAME_RE = re.compile(r'name="([^"]*)"', re.IGNORECASE)
_PART_FILENAME_RE = re.compile(r'filename="([^"]*)"', re.IGNORECASE)
_UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


class _MultipartReader:
    """按块读取 multipart/form-data，文件体不进内存（500MB 视频也只占临时磁盘）。"""

    CHUNK = 256 * 1024

    def __init__(self, rfile, boundary: bytes, length: int):
        self._rfile = rfile
        self._remaining = max(0, length)
        self._buf = b""
        self._delim = b"--" + boundary

    def _pull(self) -> bool:
        if self._remaining <= 0:
            return False
        data = self._rfile.read(min(self.CHUNK, self._remaining))
        if not data:
            self._remaining = 0
            return False
        self._remaining -= len(data)
        self._buf += data
        return True

    def readline(self) -> bytes:
        while b"\r\n" not in self._buf:
            if not self._pull():
                line, self._buf = self._buf, b""
                return line
        idx = self._buf.find(b"\r\n")
        line, self._buf = self._buf[:idx], self._buf[idx + 2:]
        return line

    def skip_preamble(self) -> bool:
        """跳到第一个分隔行；返回是否还有 part。"""
        while True:
            line = self.readline()
            if line.startswith(self._delim):
                return not line[len(self._delim):].startswith(b"--")
            if not line and self._remaining <= 0 and not self._buf:
                return False

    def read_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = self.readline()
            if not line:
                return headers
            name, _, value = line.decode("utf-8", "replace").partition(":")
            headers[name.strip().lower()] = value.strip()

    def read_body(self, sink) -> bool:
        """把当前 part 的内容喂给 sink(bytes)；返回后面是否还有 part。"""
        needle = b"\r\n" + self._delim
        keep = len(needle) - 1
        while True:
            idx = self._buf.find(needle)
            if idx >= 0:
                sink(self._buf[:idx])
                self._buf = self._buf[idx + len(needle):]
                return not self.readline().startswith(b"--")
            if len(self._buf) > keep:
                sink(self._buf[:-keep])
                self._buf = self._buf[-keep:]
            if not self._pull():
                sink(self._buf)
                self._buf = b""
                return False


def _safe_upload_name(raw: str) -> str:
    """浏览器给的 filename 归一成安全的裸文件名。"""
    base = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_NAME_RE.sub("_", base).strip().lstrip(".")
    return base[:120] or "upload.bin"


def _read_upload(rfile, content_type: str, length: int, dest_dir: Path,
                 max_bytes: int) -> tuple[dict[str, str], Path | None, str]:
    """解析上传请求。返回 (文本字段, 落地的临时文件路径, 原始文件名)。"""
    match = _BOUNDARY_RE.search(content_type or "")
    if not match:
        raise ValueError("multipart 请求缺少 boundary")
    reader = _MultipartReader(rfile, match.group(1).strip().encode("utf-8"), length)
    fields: dict[str, str] = {}
    file_path: Path | None = None
    upload_name = ""
    has_part = reader.skip_preamble()
    while has_part:
        headers = reader.read_headers()
        disposition = headers.get("content-disposition", "")
        name_match = _PART_NAME_RE.search(disposition)
        file_match = _PART_FILENAME_RE.search(disposition)
        field_name = name_match.group(1) if name_match else ""
        if file_match and field_name == "file":
            upload_name = file_match.group(1)
            dest_dir.mkdir(parents=True, exist_ok=True)
            file_path = dest_dir / _safe_upload_name(upload_name)
            written = 0
            with open(file_path, "wb") as fh:
                def sink(chunk: bytes, _fh=fh):
                    nonlocal written
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
                    _fh.write(chunk)
                has_part = reader.read_body(sink)
        else:
            chunks: list[bytes] = []
            has_part = reader.read_body(chunks.append)
            if field_name:
                fields[field_name] = b"".join(chunks).decode("utf-8", "replace").strip()
    return fields, file_path, upload_name


def _upload_material(file_path: Path, category: str, entity_name: str) -> dict[str, Any]:
    """复用 upload_reference_material 工具的 handler 完成落盘 + 实体硬锁。"""
    from .tools import _DECORATED_TOOLS   # 延迟导入：避免 studio 模块加载期拉起全部工具

    handler_fn = _DECORATED_TOOLS.get("upload_reference_material")
    if handler_fn is None:
        raise RuntimeError("upload_reference_material 工具未注册")
    # handler=None：工具内部只用它取活跃项目，缺失时会回落到 ws.get_active_project()
    return handler_fn(None, {
        "file_path": str(file_path),
        "category": category,
        "entity_name": entity_name,
    })


def _push_pending_note(project_id: str, note: str) -> None:
    with _LOCK:
        _STATE["pending_notes"].setdefault(project_id, []).append(note)


def _take_pending_notes(project_id: str | None) -> list[str]:
    if not project_id:
        return []
    with _LOCK:
        return _STATE["pending_notes"].pop(project_id, [])


def _with_pending_notes(message: str) -> str:
    """把上传队列里的素材提示作为系统前缀拼进本轮用户消息（对齐飞书通道的机制）。"""
    notes = _take_pending_notes(ws.get_active_project())
    if not notes:
        return message
    header = "[System] 用户刚通过 Studio 上传了以下素材，已归档进项目 materials/ 并登记 manifest："
    return "\n".join([header, *notes, "", message])


def _with_remake_spec(message: str) -> str:
    project_id = ws.get_active_project()
    if not project_id:
        return message
    try:
        spec = ws.normalize_remake_spec(
            ws.read_manifest(project_id).get("remake_spec", ""))
    except (OSError, json.JSONDecodeError, ValueError):
        return message
    if not spec:
        return message
    return (
        "[System] 以下为用户为当前项目确认的爆款复刻规范。"
        "拆镜、改写、素材、生成、合成和审片都必须遵守；"
        "与普通创意偏好冲突时以本规范为准。\n"
        "[REMAKE_SPEC]\n"
        f"{spec}\n"
        "[/REMAKE_SPEC]\n\n"
        f"{message}"
    )


def _create_job(message: str) -> dict[str, Any]:
    _assert_idle()
    agent = _STATE.get("agent")
    if agent is None:
        raise RuntimeError("Agent 尚未就绪")
    message = _with_remake_spec(_with_pending_notes(message))
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "project_id": ws.get_active_project(),
        "status": "queued",
        "message": message,
        "events": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _LOCK:
        _STATE["jobs"][job_id] = job
    try:
        display_queue = agent.put_task(message, source="studio", project_id=job["project_id"])
    except Exception:
        job["status"] = "error"
        raise
    job["status"] = "running"
    threading.Thread(target=_drain_job, args=(job_id, display_queue), daemon=True).start()
    return {"job_id": job_id, "status": job["status"], "project_id": job["project_id"]}


def render_studio_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VibeFilming · 爆款视频裂变</title>
  <style>
    :root {
      --bg: #090b0e;
      --panel: #101318;
      --panel-soft: #151920;
      --line: rgba(255,255,255,.09);
      --text: #f2eee6;
      --muted: #8f969f;
      --gold: #d8aa58;
      --gold-soft: rgba(216,170,88,.14);
      --green: #6fca90;
      --blue: #77a8ff;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; min-height: 0; }
    body {
      margin: 0;
      overflow: hidden;
      color: var(--text);
      background:
        radial-gradient(circle at 12% 0%, rgba(216,170,88,.09), transparent 28%),
        radial-gradient(circle at 88% 10%, rgba(75,111,170,.12), transparent 30%),
        var(--bg);
      font-family: "Avenir Next", "PingFang SC", "Hiragino Sans GB", sans-serif;
    }
    button, textarea, input { font: inherit; }
    .app {
      display: grid;
      grid-template-columns: clamp(280px, 24vw, 380px) minmax(0, 1fr);
      height: 100vh;
      height: 100dvh;
      max-height: 100vh;
      max-height: 100dvh;
      min-height: 0;
      overflow: hidden;
    }
    .chat {
      min-width: 0;
      min-height: 0;
      height: 100%;
      overflow-x: hidden;
      overflow-y: auto;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto auto auto auto auto;
      border-right: 1px solid var(--line);
      background: rgba(12,15,19,.94);
      backdrop-filter: blur(22px);
      z-index: 2;
    }
    .brand {
      height: 74px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 22px;
      border-bottom: 1px solid var(--line);
    }
    .brand-lockup { display: flex; align-items: center; gap: 12px; }
    .mark {
      width: 34px; height: 34px; border-radius: 11px;
      display: grid; place-items: center;
      background: linear-gradient(145deg, #e1b76d, #9d6e2f);
      color: #17120a; font-weight: 900; box-shadow: 0 8px 24px rgba(216,170,88,.2);
    }
    .brand h1 { margin: 0; font-size: 16px; letter-spacing: -.01em; }
    .brand p { margin: 3px 0 0; color: var(--muted); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; }
    .live { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 11px; }
    .live::before { content:""; width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 12px var(--green); }
    .brand-actions { display: flex; align-items: center; gap: 6px; }
    .brand-btn {
      border: 1px solid var(--line); border-radius: 9px; padding: 6px 8px;
      color: #b9b4aa; background: rgba(255,255,255,.035); cursor: pointer; font-size: 10px;
    }
    .brand-btn:hover { color: var(--text); border-color: rgba(216,170,88,.4); }
    .brand-btn.primary { color: #17120a; background: var(--gold); border-color: transparent; font-weight: 700; }
    .messages {
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 22px 20px 32px;
      scroll-behavior: smooth;
    }
    .welcome {
      padding: 22px;
      border: 1px solid rgba(216,170,88,.18);
      border-radius: 20px;
      background: linear-gradient(145deg, rgba(216,170,88,.10), rgba(255,255,255,.025));
      margin-bottom: 22px;
    }
    .welcome .eyebrow { color: var(--gold); font-size: 10px; letter-spacing: .16em; text-transform: uppercase; }
    .welcome h2 { margin: 9px 0 8px; font-size: 20px; line-height: 1.35; }
    .welcome p { margin: 0; color: #b5b0a7; font-size: 13px; line-height: 1.65; }
    .msg { margin: 0 0 18px; }
    .msg-head { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; color: var(--muted); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
    .msg.user .msg-head { color: var(--gold); }
    .bubble {
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 5px 16px 16px 16px;
      background: var(--panel-soft);
      font-size: 13px;
      line-height: 1.65;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .msg.user .bubble {
      border-color: rgba(216,170,88,.20);
      background: var(--gold-soft);
      border-radius: 16px 5px 16px 16px;
    }
    .msg.running .bubble::after {
      content: ""; display: inline-block; width: 6px; height: 14px; margin-left: 4px;
      background: var(--gold); vertical-align: -2px; animation: blink 1s steps(1) infinite;
    }
    @keyframes blink { 50% { opacity: 0; } }
    /* 创作追踪：模型判断 + 工具调用，按原始顺序展示。 */
    .activity { margin: 0 0 12px; border: 1px solid var(--line); border-radius: 12px; background: rgba(255,255,255,.02); overflow: hidden; }
    .activity[hidden] { display: none !important; }
    .activity-head {
      display: flex; align-items: center; gap: 8px; width: 100%;
      padding: 9px 12px; cursor: pointer; border: 0; background: transparent;
      color: var(--muted); font-size: 11px; letter-spacing: .04em; text-align: left;
    }
    .activity-head:hover { color: var(--text); }
    .activity-head .caret { transition: transform .15s; font-size: 9px; color: var(--gold); }
    .activity.collapsed .caret { transform: rotate(-90deg); }
    .activity-head .spinner { width: 8px; height: 8px; border-radius: 50%; background: var(--gold); box-shadow: 0 0 8px var(--gold); animation: blink 1s steps(1) infinite; }
    .activity.done .spinner { display: none; }
    .activity-head .count { margin-left: auto; color: #707780; font-size: 10px; }
    .activity-steps { padding: 2px 12px 10px 14px; display: flex; flex-direction: column; gap: 10px; }
    .activity.collapsed .activity-steps { display: none; }
    .step { position: relative; padding-left: 14px; color: #c7c2b8; font-size: 12px; line-height: 1.45; min-width: 0; }
    .step::before { content: ""; position: absolute; left: 0; top: .55em; width: 5px; height: 5px; border-radius: 50%; background: var(--gold); opacity: .7; }
    .step.pending::before { background: var(--muted); animation: blink 1s steps(1) infinite; }
    .step-main { display: flex; align-items: baseline; flex-wrap: wrap; gap: 5px 7px; min-width: 0; }
    .step-kind {
      flex: none; padding: 1px 5px; border: 1px solid rgba(119,168,255,.25); border-radius: 5px;
      color: var(--blue); font-size: 9px; letter-spacing: .08em;
    }
    .step.tool .step-kind { border-color: rgba(216,170,88,.25); color: var(--gold); }
    .step-text { overflow-wrap: anywhere; }
    .step-tool {
      color: #818994; font: 10px/1.4 "SFMono-Regular", Consolas, monospace;
      overflow-wrap: anywhere;
    }
    .step-detail {
      margin-top: 4px; padding: 6px 8px; border-radius: 7px; background: rgba(0,0,0,.18);
      color: #8f969f; font: 10px/1.45 "SFMono-Regular", Consolas, monospace;
      white-space: pre-wrap; overflow-wrap: anywhere;
    }

    /* 素材面板：与飞书通道并存的 Studio 上传入口。 */
    .materials {
      position: relative;
      z-index: 3;
      border-top: 1px solid var(--line);
      background: var(--panel);
    }
    .materials[hidden] { display: none !important; }

    /* 耗时面板：每阶段 + 每工具调用耗时总览 */
    .timing {
      position: relative;
      z-index: 3;
      border-top: 1px solid var(--line);
      background: var(--panel);
    }
    .timing[hidden] { display: none !important; }
    .timing.collapsed .caret { transform: rotate(-90deg); }
    .timing.collapsed .timing-body { display: none; }
    .timing-body { padding: 0 18px 12px; display: flex; flex-direction: column; gap: 10px; max-height: 18vh; overflow: auto; }
    .timing-phases { display: flex; flex-wrap: wrap; gap: 6px; }
    .timing-phase {
      display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px;
      border: 1px solid var(--line); border-radius: 999px;
      color: #b9b4aa; font-size: 10px; background: rgba(255,255,255,.03);
    }
    .timing-phase .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--gold); flex: none; }
    .timing-phase.done .dot { background: var(--green); }
    .timing-phase.in_progress .dot, .timing-phase.running .dot { background: var(--blue); }
    .timing-phase.blocked .dot, .timing-phase.failed .dot { background: #e05656; }
    .timing-phase b { color: var(--text); font-weight: 600; }
    .timing-tools { display: flex; flex-direction: column; gap: 4px; }
    .timing-tool-row {
      display: flex; align-items: center; gap: 8px; font-size: 11px; color: #a9a49b;
    }
    .timing-tool-row .tname { flex: none; min-width: 150px; color: #d5cfc3; }
    .timing-tool-row .tbar-box { flex: 1; height: 6px; border-radius: 4px; background: rgba(255,255,255,.06); overflow: hidden; }
    .timing-tool-row .tbar { display: block; height: 100%; border-radius: 4px; background: linear-gradient(90deg, rgba(216,170,88,.75), var(--gold)); }
    .timing-tool-row .tcount { flex: none; color: #707780; font-size: 10px; }
    .timing-tool-row .tdur { flex: none; color: var(--gold); font-variant-numeric: tabular-nums; }
    .timing-total {
      padding: 7px 10px; border: 1px solid rgba(216,170,88,.22); border-radius: 10px;
      background: rgba(216,170,88,.06); color: #d5cfc3; font-size: 11px;
    }
    .timing-total b { color: var(--gold); font-variant-numeric: tabular-nums; }
    .timing-empty { color: #707780; font-size: 11px; padding: 2px 0; }

    /* 调试模式开关行 */
    .debug-row {
      display: flex; align-items: center; gap: 8px; padding: 7px 10px;
      border: 1px solid rgba(111,202,144,.25); border-radius: 10px;
      background: rgba(111,202,144,.05); font-size: 11px;
    }
    .debug-label { color: var(--green); font-weight: 600; flex: none; }
    .debug-row input[type="checkbox"] { accent-color: var(--green); cursor: pointer; }
    .debug-row select {
      flex: 1; min-width: 0; border: 1px solid var(--line); border-radius: 8px;
      padding: 4px 8px; color: var(--text); background: #171b21; font-size: 11px;
      outline: 0; cursor: pointer;
    }
    .debug-row select:focus { border-color: rgba(111,202,144,.5); }
    .debug-status { flex: none; color: #707780; font-size: 10px; white-space: nowrap; }
    .debug-status.on { color: var(--green); }
    .materials-head {
      display: flex; align-items: center; gap: 8px; width: 100%;
      padding: 10px 18px; border: 0; background: transparent; cursor: pointer;
      color: var(--muted); font-size: 11px; letter-spacing: .04em; text-align: left;
    }
    .materials-head:hover { color: var(--text); }
    .materials-head .caret { color: var(--gold); font-size: 9px; transition: transform .15s; }
    .materials.collapsed .caret { transform: rotate(-90deg); }
    .materials-head .materials-title { color: var(--gold); letter-spacing: .12em; text-transform: uppercase; font-size: 10px; }
    .materials-head .materials-hint { margin-left: auto; color: #707780; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .materials-body { padding: 0 18px 14px; max-height: 22vh; overflow: auto; }
    .materials.collapsed .materials-body { display: none; }
    .outputs-list { display: flex; flex-direction: column; gap: 6px; }
    .output-item {
      display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px;
      align-items: center; padding: 7px 9px; border-bottom: 1px solid var(--line);
      color: var(--text); text-decoration: none;
    }
    .output-item:hover { background: rgba(216,170,88,.06); }
    .output-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
    .output-meta { color: var(--muted); font-size: 9px; white-space: nowrap; }
    .outputs-empty { color: var(--muted); font-size: 11px; padding: 3px 0; }
    .materials-rows {
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 12px;
      max-height: 190px; overflow-y: auto; padding-right: 3px;
    }
    .materials-row { min-width: 0; }
    .materials-row-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .materials-row-head > span { min-width: 0; }
    .materials-row-label { font-size: 12px; color: var(--text); }
    .materials-row-hint { display: block; font-size: 10px; color: var(--muted); white-space: nowrap; }
    .mat-btn {
      flex: 0 0 auto; padding: 6px 10px; border: 1px solid rgba(216,170,88,.22); border-radius: 10px;
      color: #d9d3c8; background: rgba(216,170,88,.06); cursor: pointer;
      font-size: 11px; line-height: 1.3; white-space: nowrap;
      transition: border-color .15s, background .15s, color .15s;
    }
    .mat-btn:hover:not(:disabled) { color: var(--text); border-color: rgba(216,170,88,.6); background: var(--gold-soft); }
    .mat-btn.active { color: #17120a; border-color: transparent; background: var(--gold); font-weight: 700; }
    .mat-btn:disabled { opacity: .38; cursor: not-allowed; }
    .materials-previews {
      display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; min-height: 0;
    }
    .materials-previews:empty { display: none; }
    .materials-preview {
      position: relative; width: 72px; height: 72px; border-radius: 8px;
      overflow: hidden; border: 1px solid var(--line); background: rgba(0,0,0,.25);
      cursor: pointer;
    }
    .materials-preview:hover { border-color: rgba(216,170,88,.55); }
    .materials-preview img, .materials-preview video { width: 100%; height: 100%; object-fit: cover; }
    .materials-preview video { object-fit: contain; background: #000; pointer-events: none; }
    .material-file-icon {
      display: grid; width: 100%; height: 100%; place-items: center;
      color: var(--gold); background: #171b21; font-size: 10px; font-weight: 700;
      overflow-wrap: anywhere; text-align: center; padding: 6px;
    }
    .materials-preview .preview-name {
      position: absolute; bottom: 0; left: 0; right: 0; padding: 3px 20px 3px 5px;
      font-size: 9px; color: #b9b4aa; background: rgba(0,0,0,.72);
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .materials-preview .preview-del {
      position: absolute; top: 3px; right: 3px; width: 18px; height: 18px;
      display: grid; place-items: center; border: 0; border-radius: 50%;
      color: #e6c7c7; background: rgba(0,0,0,.68); font-size: 13px; line-height: 1;
      cursor: pointer; opacity: 0; transition: opacity .12s;
    }
    .materials-preview:hover .preview-del { opacity: 1; }
    .materials-preview .preview-del:hover { color: #fff; background: rgba(206,70,70,.9); }
    .entity-row { display: flex; gap: 7px; margin-top: 8px; }
    .entity-row[hidden] { display: none !important; }
    .entity-row input {
      min-width: 0; flex: 1; border: 1px solid var(--line); border-radius: 9px; outline: 0;
      padding: 8px 10px; color: var(--text); background: #171b21; font-size: 11px;
    }
    .entity-row input:focus { border-color: rgba(216,170,88,.55); }
    .entity-row .btn { padding: 6px 10px; font-size: 11px; white-space: nowrap; }
    .materials-status { min-height: 15px; margin-top: 8px; color: #8f969f; font-size: 10px; overflow-wrap: anywhere; }
    .materials-status.busy { color: var(--gold); }
    .materials-status.busy::before {
      content: ""; display: inline-block; width: 6px; height: 6px; margin-right: 6px;
      border-radius: 50%; background: var(--gold); box-shadow: 0 0 8px var(--gold);
      animation: blink 1s steps(1) infinite; vertical-align: 1px;
    }
    .materials-track { height: 2px; margin-top: 6px; border-radius: 2px; background: rgba(255,255,255,.07); overflow: hidden; }
    .materials-track[hidden] { display: none !important; }
    .materials-track i { display: block; height: 100%; width: 0; background: var(--gold); transition: width .2s; }
    .remake-spec-body { padding: 0 18px 12px; }
    .remake-spec-editor {
      min-height: 96px; max-height: 220px; resize: vertical;
      padding: 9px 10px; border: 1px solid var(--line); border-radius: 9px;
      color: var(--text); background: #171b21; font-size: 11px; line-height: 1.55;
    }
    .remake-spec-editor:focus { border-color: rgba(216,170,88,.55); }
    .remake-spec-actions {
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; margin-top: 8px;
    }
    .remake-spec-status { color: #707780; font-size: 10px; }
    .remake-spec-status.dirty { color: var(--gold); }
    .remake-spec-status.saved { color: var(--green); }
    .remake-spec-actions .btn { flex: none; }
    .toasts {
      position: fixed; right: 18px; bottom: 18px; z-index: 40;
      display: flex; flex-direction: column; gap: 8px; pointer-events: none;
    }
    .toast {
      max-width: 330px; padding: 10px 13px; border: 1px solid rgba(216,170,88,.28);
      border-radius: 11px; background: rgba(18,21,26,.97); color: var(--text);
      font-size: 12px; line-height: 1.5; overflow-wrap: anywhere;
      box-shadow: 0 16px 40px rgba(0,0,0,.45); animation: toast-in .22s ease-out;
    }
    .toast.err { border-color: rgba(232,134,134,.42); color: #f0b8b8; }
    .toast.ok { border-color: rgba(111,202,144,.35); }
    @keyframes toast-in { from { opacity: 0; transform: translateY(8px); } }

    .composer {
      position: relative;
      z-index: 3;
      padding: 14px 18px 18px;
      border-top: 1px solid var(--line);
      background: rgba(10,12,15,.96);
    }
    .execution-row.composer-mode {
      display: grid; grid-template-columns: minmax(0, 1fr);
      gap: 7px; margin-bottom: 10px; padding: 0 2px;
    }
    .composer-mode-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .composer-mode .debug-label { min-width: 0; white-space: nowrap; }
    .composer-mode .mode-picker { width: 100%; }
    .composer-mode .mode-picker label { padding: 7px 10px; }
    .video-model-row { padding-top: 9px; border-top: 1px solid var(--line); }
    .video-model-row .mode-picker label { padding: 7px 10px; }
    .input-wrap { border: 1px solid rgba(255,255,255,.12); border-radius: 17px; background: #15191f; padding: 12px 12px 9px; transition: border-color .15s, box-shadow .15s; }
    .input-wrap:focus-within { border-color: rgba(216,170,88,.6); box-shadow: 0 0 0 3px rgba(216,170,88,.07); }
    textarea { width: 100%; min-height: 66px; max-height: 180px; resize: none; border: 0; outline: 0; color: var(--text); background: transparent; line-height: 1.5; font-size: 13px; }
    textarea::placeholder { color: #707780; }
    .composer-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 7px; }
    .hint { color: #707780; font-size: 10px; }
    .actions { display: flex; gap: 8px; }
    .btn { border: 1px solid var(--line); border-radius: 10px; padding: 7px 12px; color: var(--text); background: rgba(255,255,255,.04); cursor: pointer; }
    .btn:hover { border-color: rgba(216,170,88,.45); }
    .btn.primary { color: #17120a; border-color: transparent; background: var(--gold); font-weight: 700; }
    .btn:disabled { opacity: .45; cursor: not-allowed; }
    .workspace { position: relative; min-width: 0; min-height: 0; overflow: hidden; background: #0b0d10; }
    .workspace-bar {
      height: 52px; display: flex; align-items: center; justify-content: space-between;
      padding: 0 18px; border-bottom: 1px solid var(--line); background: rgba(12,15,19,.96);
    }
    .project { min-width: 0; display: flex; align-items: center; gap: 10px; }
    .project strong { max-width: 52vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
    .project span { color: var(--muted); font-size: 10px; }
    .canvas-shell { position: absolute; inset: 52px 0 0; }
    iframe { width: 100%; height: 100%; border: 0; background: #0b0d10; }
    .empty-canvas {
      position: absolute; inset: 0; display: grid; place-items: center; text-align: center;
      background: radial-gradient(circle at 50% 40%, rgba(216,170,88,.08), transparent 32%);
    }
    .empty-canvas[hidden], iframe[hidden] { display: none !important; }
    .empty-canvas h3 { margin: 13px 0 5px; font-size: 18px; }
    .empty-canvas p { color: var(--muted); font-size: 12px; }
    .aperture { width: 62px; height: 62px; border: 1px solid rgba(216,170,88,.35); border-radius: 50%; display: grid; place-items: center; color: var(--gold); font-size: 26px; }
    .modal {
      position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 24px;
      background: rgba(3,5,8,.74); backdrop-filter: blur(8px);
    }
    .modal[hidden] { display: none !important; }
    .lightbox { z-index: 60; }
    .lightbox-content {
      position: relative; display: grid; place-items: center; max-width: 94vw; max-height: 94vh;
    }
    .lightbox-content img, .lightbox-content video {
      max-width: 94vw; max-height: 88vh; border-radius: 10px;
      background: #000; box-shadow: 0 30px 90px rgba(0,0,0,.6);
    }
    .lightbox-content audio { width: min(78vw, 720px); }
    .lightbox-caption {
      margin-top: 10px; max-width: 92vw; color: #c9c3b7; font-size: 12px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .lightbox-close {
      position: absolute; top: 18px; right: 22px; width: 40px; height: 40px;
      border: 1px solid rgba(255,255,255,.16); border-radius: 50%; background: rgba(0,0,0,.5);
      color: var(--text); font-size: 22px; line-height: 1; cursor: pointer;
    }
    .lightbox-close:hover { background: rgba(206,70,70,.8); border-color: transparent; }
    .project-panel {
      width: min(720px, 94vw); max-height: min(760px, 90vh); display: grid;
      grid-template-rows: auto auto auto minmax(0, 1fr); overflow: hidden;
      border: 1px solid rgba(255,255,255,.13); border-radius: 20px; background: #11151a;
      box-shadow: 0 30px 90px rgba(0,0,0,.5);
    }
    .project-panel-head {
      display: flex; align-items: center; justify-content: space-between; gap: 20px;
      padding: 18px 20px; border-bottom: 1px solid var(--line);
    }
    .project-panel-head h2 { margin: 0; font-size: 17px; }
    .project-panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 11px; }
    .modal-close { border: 0; color: var(--muted); background: transparent; font-size: 24px; cursor: pointer; }
    .new-project-form { display: flex; flex-direction: column; gap: 10px; padding: 14px 20px; border-bottom: 1px solid var(--line); }
    .new-project-row { display: flex; gap: 9px; }
    .new-project-form input[type="text"] {
      min-width: 0; flex: 1; border: 1px solid var(--line); border-radius: 10px; outline: 0;
      padding: 9px 11px; color: var(--text); background: #171b21; font-size: 12px;
    }
    .new-project-form input[type="text"]:focus { border-color: rgba(216,170,88,.55); }
    .new-project-form textarea {
      min-height: 92px; max-height: 180px; resize: vertical;
      border: 1px solid var(--line); border-radius: 10px; outline: 0;
      padding: 9px 11px; color: var(--text); background: #171b21; font-size: 12px;
    }
    .new-project-form textarea:focus { border-color: rgba(216,170,88,.55); }
    .mode-picker { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--line); }
    .mode-picker label { display: block; min-width: 0; padding: 8px 10px; color: #aaa59c; background: #171b21; cursor: pointer; }
    .mode-picker input { position: absolute; opacity: 0; pointer-events: none; }
    .mode-picker strong { display: block; color: inherit; font-size: 11px; letter-spacing: 0; }
    .mode-picker small { display: block; margin-top: 3px; color: #707780; font-size: 9px; line-height: 1.35; }
    .mode-picker label:has(input:checked) { color: #17120a; background: var(--gold); }
    .mode-picker label:has(input:checked) small { color: rgba(23,18,10,.72); }
    .execution-row { display: flex; align-items: center; gap: 8px; }
    .execution-row .debug-label { min-width: 52px; }
    .execution-row .mode-picker { flex: 1; }
    .project-error { min-height: 16px; padding: 0 20px; color: #e88686; font-size: 11px; }
    .project-list { min-height: 0; overflow-y: auto; padding: 8px 12px 16px; }
    .project-item {
      width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px;
      padding: 12px; border: 1px solid transparent; border-radius: 12px;
      color: var(--text); background: transparent; text-align: left; cursor: pointer;
    }
    .project-item:hover { border-color: var(--line); background: rgba(255,255,255,.035); }
    .project-item.active { border-color: rgba(216,170,88,.28); background: var(--gold-soft); }
    .project-brief { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 650; }
    .project-meta { margin-top: 4px; color: var(--muted); font-size: 10px; overflow-wrap: anywhere; }
    .project-progress { align-self: center; color: var(--gold); font-size: 10px; white-space: nowrap; }
    .project-empty { padding: 30px; color: var(--muted); text-align: center; font-size: 12px; }
    @media (max-width: 700px) {
      .brand { padding: 0 14px; }
      .brand p, .live { display: none; }
      .messages { padding-left: 14px; padding-right: 14px; }
      .composer { padding-left: 12px; padding-right: 12px; }
      .materials-head { padding-left: 12px; padding-right: 12px; }
      .materials-body { padding-left: 12px; padding-right: 12px; }
      .materials-head .materials-hint { display: none; }
      .hint { display: none; }
      .toasts { top: 70px; bottom: auto; left: 12px; right: 12px; }
      .toast { max-width: none; }
    }
    @media (max-width: 440px) {
      body { overflow: auto; }
      .app { grid-template-columns: 1fr; grid-template-rows: minmax(760px, 100dvh) 65dvh; height: auto; max-height: none; overflow: visible; }
      .chat { border-right: 0; border-bottom: 1px solid var(--line); }
      .brand { height: 62px; }
      .workspace-bar { height: 44px; }
      .canvas-shell { inset: 44px 0 0; }
    }
  </style>
</head>
<body>
  <div class="app">
    <section class="chat">
      <header class="brand">
        <div class="brand-lockup">
          <div class="mark">V</div>
          <div><h1>VibeFilming</h1><p>爆款视频裂变工作台</p></div>
        </div>
        <div class="brand-actions">
          <button id="projects" class="brand-btn">项目</button>
          <button id="new-project" class="brand-btn primary">＋ 新建</button>
        </div>
      </header>
      <main id="messages" class="messages">
        <div class="welcome">
          <div class="eyebrow">从参考视频开始，保留吸引力，生成新版本</div>
          <h2>这条视频，你想保留什么、替换什么？</h2>
          <p>填写复刻规范、上传参考片并直接描述变化即可开始；项目名称会根据需求自动生成。</p>
        </div>
      </main>
      <section id="timing" class="timing collapsed" hidden>
        <button id="timing-toggle" class="materials-head" type="button" aria-expanded="false">
          <span class="caret">▾</span>
          <span class="materials-title">制作耗时</span>
          <span>耗时总览</span>
          <span id="timing-hint" class="materials-hint"></span>
        </button>
        <div class="timing-body">
          <div class="debug-row" title="调试模式：流程只推进到所选阶段就停">
            <label class="debug-label">调试模式</label>
            <input type="checkbox" id="debug-enabled">
            <select id="debug-stop">
              <option value="breakdown">拆镜</option>
              <option value="story">剧本</option>
              <option value="entity">编导方案</option>
              <option value="asset" selected>参考资产与故事板</option>
              <option value="animate">视频段</option>
              <option value="compose">合成</option>
              <option value="review">审查</option>
            </select>
            <span id="debug-status" class="debug-status"></span>
          </div>
          <div id="timing-phases" class="timing-phases"></div>
          <div id="timing-tools" class="timing-tools"></div>
          <div id="timing-total" class="timing-total"></div>
        </div>
      </section>
      <section id="outputs-panel" class="materials collapsed" hidden>
        <button id="outputs-toggle" class="materials-head" type="button" aria-expanded="false">
          <span class="caret">▾</span>
          <span class="materials-title">交付产物</span>
          <span id="outputs-hint" class="materials-hint">暂无产物</span>
        </button>
        <div class="materials-body">
          <div id="outputs-list" class="outputs-list"></div>
        </div>
      </section>
      <section id="materials" class="materials">
        <button id="materials-toggle" class="materials-head" type="button" aria-expanded="true">
          <span class="caret">▾</span>
          <span class="materials-title">参考素材</span>
          <span>素材上传</span>
          <span id="materials-hint" class="materials-hint">上传即自动建项目</span>
        </button>
        <div class="materials-body">
          <div id="materials-rows" class="materials-rows"></div>
          <div id="entity-row" class="entity-row" hidden>
            <input id="entity-name" maxlength="40" autocomplete="off" placeholder="实体名（选填），如 小雨 / 产品A，填了就硬锁为唯一参考形象">
            <button id="entity-pick" class="btn primary" type="button">选择图片</button>
            <button id="entity-cancel" class="btn" type="button">取消</button>
          </div>
          <div id="materials-status" class="materials-status"></div>
          <div id="materials-track" class="materials-track" hidden><i id="materials-bar"></i></div>
        </div>
        <input id="materials-file" type="file" hidden>
      </section>
      <section id="remake-spec-panel" class="materials collapsed">
        <button id="remake-spec-toggle" class="materials-head" type="button" aria-expanded="false">
          <span class="caret">▾</span>
          <span class="materials-title">复刻规范</span>
          <span>世界观与元素</span>
          <span id="remake-spec-hint" class="materials-hint">未填写</span>
        </button>
        <div class="materials-body remake-spec-body">
          <textarea id="remake-spec" class="remake-spec-editor" maxlength="12000" placeholder="世界观、固定元素、核心资源、等级/UI术语、允许替换项、禁改项…"></textarea>
          <div class="remake-spec-actions">
            <span id="remake-spec-status" class="remake-spec-status"></span>
            <button id="remake-spec-save" class="btn primary" type="button">保存规范</button>
          </div>
        </div>
      </section>
      <footer class="composer">
        <div class="execution-row composer-mode" title="开始前选择执行模式；已有项目可随时切换">
          <div class="composer-mode-head">
            <span class="debug-label">执行模式</span>
            <span id="execution-mode-status" class="debug-status"></span>
          </div>
          <div class="mode-picker" id="active-mode-picker">
            <label><input type="radio" name="active-execution-mode" value="autonomous"><strong>无人干预</strong><small>机器校验后自动推进</small></label>
            <label><input type="radio" name="active-execution-mode" value="human_gated"><strong>人工介入</strong><small>拆镜、剧本后暂停</small></label>
          </div>
        </div>
        <div class="execution-row composer-mode video-model-row" title="按项目选择视频生成模型">
          <div class="composer-mode-head">
            <span class="debug-label">视频模型</span>
            <span id="video-model-status" class="debug-status"></span>
          </div>
          <div class="mode-picker" id="active-video-model-picker"></div>
        </div>
        <div class="input-wrap">
          <textarea id="input" placeholder="描述一条影片，或继续修改当前项目…"></textarea>
          <div class="composer-row">
            <span class="hint">Enter 发送 · Shift+Enter 换行</span>
            <div class="actions">
              <button id="stop" class="btn" disabled>停止</button>
              <button id="send" class="btn primary">发送</button>
            </div>
          </div>
        </div>
      </footer>
    </section>
    <section class="workspace">
      <header class="workspace-bar">
        <div class="project"><strong id="project-name">等待项目</strong><span id="canvas-state">画布将在创建项目后出现</span></div>
        <button id="reload-canvas" class="btn">刷新画布</button>
      </header>
      <div class="canvas-shell">
        <iframe id="canvas" title="VibeFilming Workflow Canvas" hidden></iframe>
        <div id="empty-canvas" class="empty-canvas">
          <div><div class="aperture">◎</div><h3>画布尚未开始</h3><p>在左侧发送第一条创作需求。</p></div>
        </div>
      </div>
    </section>
  </div>
  <div id="project-modal" class="modal" hidden>
    <section class="project-panel" role="dialog" aria-modal="true" aria-labelledby="project-panel-title">
      <header class="project-panel-head">
        <div>
          <h2 id="project-panel-title">影片项目</h2>
          <p>新建影片项目，或切换到历史项目查看当时的完整流水线。</p>
        </div>
        <button id="project-close" class="modal-close" aria-label="关闭">×</button>
      </header>
      <form id="new-project-form" class="new-project-form">
        <div class="mode-picker" id="new-project-mode">
          <label><input type="radio" name="new-execution-mode" value="autonomous"><strong>端到端无人干预</strong><small>机器校验通过后自动完成全部阶段</small></label>
          <label><input type="radio" name="new-execution-mode" value="human_gated" checked><strong>关键步骤人工介入</strong><small>拆镜和剧本定稿后等待确认</small></label>
        </div>
        <div class="mode-picker" id="new-video-model-picker"></div>
        <textarea id="project-remake-spec" maxlength="12000" placeholder="复刻规范（选填）：世界观、固定元素、核心资源、等级/UI术语、允许替换项、禁改项…"></textarea>
        <div class="new-project-row">
          <input id="project-brief" type="text" maxlength="200" placeholder="影片项目名称…" autocomplete="off">
          <button class="btn primary" type="submit">创建项目</button>
        </div>
      </form>
      <div id="project-error" class="project-error"></div>
      <div id="project-list" class="project-list"></div>
    </section>
  </div>
  <div id="lightbox" class="modal lightbox" hidden>
    <button id="lightbox-close" class="lightbox-close" aria-label="关闭">×</button>
    <div class="lightbox-content">
      <div id="lightbox-media"></div>
      <div id="lightbox-caption" class="lightbox-caption"></div>
    </div>
  </div>
  <div id="toasts" class="toasts"></div>
  <script>
    const messages = document.getElementById('messages');
    const input = document.getElementById('input');
    const send = document.getElementById('send');
    const stop = document.getElementById('stop');
    const frame = document.getElementById('canvas');
    const empty = document.getElementById('empty-canvas');
    const projectModal = document.getElementById('project-modal');
    const projectList = document.getElementById('project-list');
    const projectBrief = document.getElementById('project-brief');
    const projectRemakeSpec = document.getElementById('project-remake-spec');
    const projectError = document.getElementById('project-error');
    const outputsPanel = document.getElementById('outputs-panel');
    const outputsList = document.getElementById('outputs-list');
    const outputsHint = document.getElementById('outputs-hint');
    const remakeSpecPanel = document.getElementById('remake-spec-panel');
    const remakeSpec = document.getElementById('remake-spec');
    const remakeSpecHint = document.getElementById('remake-spec-hint');
    const remakeSpecStatus = document.getElementById('remake-spec-status');
    const remakeSpecSave = document.getElementById('remake-spec-save');
    const activeModeInputs = Array.from(
      document.querySelectorAll('input[name="active-execution-mode"]'));
    const executionModeStatus = document.getElementById('execution-mode-status');
    const activeVideoModelPicker = document.getElementById('active-video-model-picker');
    const newVideoModelPicker = document.getElementById('new-video-model-picker');
    const videoModelStatus = document.getElementById('video-model-status');
    let activeJob = null;
    let submitting = false;
    let modeSaving = null;
    let eventCursor = 0;
    let assistantTurn = null;
    let canvasUrl = null;
    let currentProject = null;
    let stateInFlight = false;
    let stateEpoch = 0;
    let newModeDirty = false;
    let pendingExecutionMode = null;
    let videoModelSaving = null;
    let activeVideoModelState = null;
    let pendingVideoModel = null;
    let remakeSpecDirty = false;
    let remakeSpecSaving = false;
    let outputsSignature = null;

    const studioBaseUrl = new URL(window.location.pathname.replace(/\/?$/, '/'), window.location.origin);
    function apiUrl(path) {
      return new URL(path.replace(/^\/api\//, 'api/'), studioBaseUrl).href;
    }
    function apiFetch(path, options) {
      return fetch(apiUrl(path), options);
    }

    // Bind addresses are not browser destinations. Normalize before comparing.
    function fixCanvasHost(value) {
      if (!value) return null;
      try {
        const url = new URL(value, window.location.href);
        if (['0.0.0.0', '127.0.0.1', 'localhost', '[::]', '[::1]'].includes(url.hostname)) {
          url.hostname = window.location.hostname;
        }
        return url.href;
      } catch (_) {
        return value;
      }
    }

    function resetChatForProject(data, mode) {
      messages.innerHTML = '';
      const welcome = document.createElement('div');
      welcome.className = 'welcome';
      const eyebrow = document.createElement('div');
      eyebrow.className = 'eyebrow';
      eyebrow.textContent = mode === 'new' ? '新建裂变项目' : '已恢复项目';
      const title = document.createElement('h2');
      title.textContent = data.project_brief || data.brief || data.project_id;
      const desc = document.createElement('p');
      desc.textContent = mode === 'new'
        ? '项目工作区已就绪。在下面描述你想拍什么，导演 Agent 就会从全新上下文开工。'
        : '已切换到该项目。右侧显示当时的完整流水线，你也可以在这里继续修改。';
      welcome.append(eyebrow, title, desc);
      messages.appendChild(welcome);
    }

    function applyProjectSwitch(data, mode) {
      // Ignore a pending state response from before this project switch.
      stateEpoch += 1;
      materialsSignature = null;
      outputsSignature = null;
      resetChatForProject(data, mode);
      currentProject = data.project_id || null;
      canvasUrl = fixCanvasHost(data.canvas_url);
      document.getElementById('project-name').textContent =
        data.project_brief || data.brief || data.project_id || '等待项目';
      document.getElementById('canvas-state').textContent =
        data.project_id ? '实时工作流画布' : '画布将在创建项目后出现';
      frame.hidden = !canvasUrl;
      empty.hidden = Boolean(canvasUrl);
      if (canvasUrl) {
        frame.src = canvasUrl + (canvasUrl.includes('?') ? '&' : '?') + 'ts=' + Date.now();
      } else {
        frame.removeAttribute('src');
      }
      projectModal.hidden = true;
      projectError.textContent = '';
      hasProject = Boolean(data.project_id);
      configureMaterialTypes(data.material_types);
      syncMaterials();
      renderMaterialsState(data.materials);
      renderOutputs(data.outputs, data.outputs_error);
      renderTiming(data.timing);
      renderDebug(data.debug);
      renderRemakeSpec(data.remake_spec, true);
      pendingExecutionMode = data.execution_mode?.mode || pendingExecutionMode;
      renderExecutionMode(data.execution_mode);
      renderVideoModel(data.video_model);
      input.focus();
    }

    async function projectRequest(path, payload) {
      const response = await apiFetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '项目操作失败');
      return data;
    }

    async function loadProjects(focusNew=false) {
      newModeDirty = false;
      projectError.textContent = '';
      projectList.innerHTML = '<div class="project-empty">正在读取项目…</div>';
      projectModal.hidden = false;
      try {
        const response = await apiFetch('/api/projects?ts=' + Date.now(), {cache:'no-store'});
        const data = await response.json();
        if (!newModeDirty) {
          const defaultMode = pendingExecutionMode
            || data.default_execution_mode?.mode || 'human_gated';
          document.querySelectorAll('input[name="new-execution-mode"]').forEach(input => {
            input.checked = input.value === defaultMode;
          });
        }
        renderNewVideoModel(data.video_model);
        projectList.innerHTML = '';
        if (!(data.projects || []).length) {
          projectList.innerHTML = '<div class="project-empty">还没有历史项目，先新建一部影片。</div>';
        }
        for (const project of data.projects || []) {
          const item = document.createElement('button');
          item.type = 'button';
          item.className = 'project-item' +
            (project.project_id === data.active_project_id ? ' active' : '');
          const info = document.createElement('div');
          const brief = document.createElement('div');
          brief.className = 'project-brief';
          brief.textContent = project.brief;
          const meta = document.createElement('div');
          meta.className = 'project-meta';
          const modeLabel = project.execution_mode?.label || '关键步骤人工介入';
          const videoLabel = project.video_model?.label || 'Seedance';
          meta.textContent = `${project.created_at || project.updated_at} · ${modeLabel} · ${videoLabel} · ${project.project_id}`;
          info.append(brief, meta);
          const progress = document.createElement('div');
          progress.className = 'project-progress';
          progress.textContent = `${project.done_phases}/${project.total_phases} 阶段`;
          item.append(info, progress);
          item.addEventListener('click', async () => {
            projectError.textContent = '';
            try {
              const opened = await projectRequest('/api/projects/open', {
                project_id: project.project_id
              });
              applyProjectSwitch(opened, 'open');
            } catch (err) {
              projectError.textContent = err.message;
            }
          });
          projectList.appendChild(item);
        }
        if (focusNew) setTimeout(() => projectBrief.focus(), 0);
      } catch (err) {
        projectList.innerHTML = '';
        projectError.textContent = err.message;
      }
    }

    function addMessage(role, text, running=false) {
      const box = document.createElement('article');
      box.className = `msg ${role} ${running ? 'running' : ''}`;
      box.innerHTML = `<div class="msg-head">${role === 'user' ? 'You' : 'VibeFilming'}</div><div class="bubble"></div>`;
      box.querySelector('.bubble').textContent = text;
      messages.appendChild(box);
      messages.scrollTop = messages.scrollHeight;
      return box;
    }

    // 一次助手回合 = 活动时间线（可折叠）+ 正在创作气泡；最终回复替换气泡文本，时间线保留。
    function addAssistantTurn() {
      const box = document.createElement('article');
      box.className = 'msg assistant running';
      box.innerHTML =
        '<div class="msg-head">VibeFilming</div>' +
        '<div class="activity" hidden>' +
        '  <button class="activity-head" type="button">' +
        '    <span class="spinner"></span><span class="caret">▾</span>' +
        '    <span class="activity-title">创作过程</span>' +
        '    <span class="count"></span>' +
        '  </button>' +
        '  <div class="activity-steps"></div>' +
        '</div>' +
        '<div class="bubble"></div>';
      const activity = box.querySelector('.activity');
      const steps = box.querySelector('.activity-steps');
      const bubble = box.querySelector('.bubble');
      bubble.textContent = '正在创作并更新项目画布…';
      box.querySelector('.activity-head').addEventListener('click', () => {
        activity.classList.toggle('collapsed');
      });
      messages.appendChild(box);
      messages.scrollTop = messages.scrollHeight;
      return {box, activity, steps, bubble};
    }

    function addStep(turn, event) {
      turn.activity.hidden = false;
      const prev = turn.steps.querySelector('.step.pending');
      if (prev) prev.classList.remove('pending');
      const step = document.createElement('div');
      const kind = event.kind === 'summary' ? 'summary' : 'tool';
      step.className = `step ${kind} pending`;

      const main = document.createElement('div');
      main.className = 'step-main';
      const badge = document.createElement('span');
      badge.className = 'step-kind';
      badge.textContent = kind === 'summary' ? '判断' : '工具';
      const text = document.createElement('span');
      text.className = 'step-text';
      text.textContent = event.text || '';
      main.append(badge, text);

      if (kind === 'tool' && event.tool) {
        const tool = document.createElement('code');
        tool.className = 'step-tool';
        tool.textContent = event.tool;
        main.appendChild(tool);
      }
      step.appendChild(main);
      if (kind === 'tool' && event.detail) {
        const detail = document.createElement('div');
        detail.className = 'step-detail';
        detail.textContent = event.detail;
        step.appendChild(detail);
      }
      turn.steps.appendChild(step);
      turn.activity.querySelector('.count').textContent = turn.steps.children.length + ' 条';
    }

    async function submit() {
      const message = input.value.trim();
      if (!message || activeJob || submitting) return;
      if (bottomUploader.isUploading()) {
        toast('素材正在入库，完成后即可开始', 'ok', 3000);
        return;
      }
      submitting = true;
      const mode = selectedExecutionMode();
      addMessage('user', message);
      input.value = '';
      assistantTurn = addAssistantTurn();
      send.disabled = true;
      stop.disabled = false;
      try {
      if (modeSaving) await modeSaving;
      if (hasProject && remakeSpecDirty) await saveRemakeSpec(false);
      const pendingRemakeSpec = hasProject ? undefined : remakeSpec.value;
      const response = await apiFetch('/api/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          message,
          project_id: currentProject,
          execution_mode: mode,
          remake_spec: pendingRemakeSpec,
          video_model: selectedVideoModel()
        })
      });
      const data = await response.json();
      if (!response.ok) {
        assistantTurn.bubble.textContent = data.error || '发送失败';
        assistantTurn.box.classList.remove('running');
        finishJob();
        return;
      }
      activeJob = data.job_id;
      currentProject = data.project_id || currentProject;
      if (!hasProject && currentProject) {
        hasProject = true;
        remakeSpecDirty = false;
        renderRemakeSpec(remakeSpec.value, true);
      }
      eventCursor = 0;
      pollJob();
      } catch (err) {
        assistantTurn.bubble.textContent = `发送失败：${err.message}`;
        assistantTurn.box.classList.remove('running');
        input.value = message;
        finishJob();
      } finally {
        submitting = false;
      }
    }

    async function pollJob() {
      if (!activeJob) return;
      try {
        const response = await apiFetch(`/api/jobs/${activeJob}?since=${eventCursor}`, {cache:'no-store'});
        const data = await response.json();
        for (const event of data.events || []) {
          eventCursor += 1;
          if (event.type === 'trace' || event.type === 'activity') {
            addStep(assistantTurn, event);
          } else if (event.type === 'done') {
            assistantTurn.steps.querySelectorAll('.step.pending')
              .forEach(s => s.classList.remove('pending'));
            assistantTurn.activity.classList.add('done');
            assistantTurn.bubble.textContent = event.text || '';
          } else if (event.type === 'progress') {
            assistantTurn.bubble.textContent = event.text || '正在创作并更新项目画布…';
          } else if (event.type === 'error') {
            assistantTurn.activity.classList.add('done');
            assistantTurn.bubble.textContent = `处理失败：${event.text}`;
          }
        }
        messages.scrollTop = messages.scrollHeight;
        if (data.status === 'done' || data.status === 'error' || data.status === 'aborted') {
          if (data.status === 'aborted') {
            assistantTurn.bubble.textContent = '已停止等待；远端任务可能仍在执行，请查看会话状态。';
          }
          assistantTurn.box.classList.remove('running');
          assistantTurn.activity.classList.add('done');
          finishJob();
          refreshState();
          return;
        }
      } catch (err) {
        console.warn(err);
      }
      setTimeout(pollJob, 450);
    }

    function finishJob() {
      activeJob = null;
      send.disabled = false;
      stop.disabled = true;
      input.focus();
    }

    async function refreshState() {
      if (stateInFlight) return;
      stateInFlight = true;
      const epoch = stateEpoch;
      try {
        const response = await apiFetch('/api/state?ts=' + Date.now(), {cache:'no-store'});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        if (epoch !== stateEpoch) return;
        const nextCanvasUrl = fixCanvasHost(data.canvas_url);
        const nextProject = data.project_id || null;
        if (nextProject !== currentProject) materialsSignature = null;
        hasProject = Boolean(data.project_id);
        configureMaterialTypes(data.material_types);
        syncMaterials();
        renderMaterialsState(data.materials);
        renderOutputs(data.outputs, data.outputs_error);
        renderTiming(data.timing);
        renderDebug(data.debug);
        renderRemakeSpec(data.remake_spec);
        renderExecutionMode(data.execution_mode);
        renderVideoModel(data.video_model);
        document.getElementById('project-name').textContent =
          data.project_brief || data.project_id || '等待项目';
        document.getElementById('canvas-state').textContent = data.project_id ? '实时工作流画布' : '画布将在创建项目后出现';
        const hasCanvas = Boolean(nextCanvasUrl);
        frame.hidden = !hasCanvas;
        empty.hidden = hasCanvas;
        if (!hasCanvas) {
          canvasUrl = null;
          currentProject = nextProject;
          if (frame.hasAttribute('src')) frame.removeAttribute('src');
        } else if (nextCanvasUrl !== canvasUrl || nextProject !== currentProject) {
          canvasUrl = nextCanvasUrl;
          currentProject = nextProject;
          frame.src = canvasUrl + (canvasUrl.includes('?') ? '&' : '?') + 'ts=' + Date.now();
          frame.hidden = false;
          empty.hidden = true;
        }
      } catch (err) {
        if (epoch === stateEpoch) {
          document.getElementById('canvas-state').textContent = '状态连接中…';
        }
      } finally {
        stateInFlight = false;
      }
    }

    // ============== 素材上传：能力由后端 material_types 声明 ==============
    const toasts = document.getElementById('toasts');
    const materialsRows = document.getElementById('materials-rows');
    let materialTypes = [];
    let materialTypesSignature = '';
    let materialsSignature = null;
    let hasProject = false;

    // ============== 耗时总览面板：每阶段 + 每工具调用耗时 ==============
    const timingPanel = document.getElementById('timing');
    const TOOL_LABELS = {
      analyze_reference_video: '拆镜', gen_image: '出图', gen_video_t2v: '视频生成',
      query_video_task: '视频轮询', vlm_understand: 'VLM审片', video_concat: '拼接',
      gen_audio_bgm: '配乐', query_audio_task: '配乐进度',
      audio_process: '音频处理', extract_frames: '抽帧',
      upload_reference_material: '素材入库', inject_breakdown_to_plan: '拆镜注入',
      project_update_phase: '阶段状态', workflow_canvas: '画布刷新'
    };

    function fmtDur(sec) {
      if (sec == null || isNaN(sec)) return '';
      if (sec < 60) return (Math.round(sec * 10) / 10) + 's';
      if (sec < 3600) return (sec / 60).toFixed(1) + 'm';
      return (sec / 3600).toFixed(1) + 'h';
    }

    function escapeHtml(text) {
      return String(text || '').replace(/[&<>"']/g, ch => ({
        '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
      })[ch]);
    }

    function renderTiming(timing) {
      if (!timing || !timing.phases || !timing.phases.length) {
        document.getElementById('timing-phases').innerHTML = '';
        document.getElementById('timing-tools').innerHTML = '';
        document.getElementById('timing-total').innerHTML = '';
        document.getElementById('timing-hint').textContent = '';
        return;
      }
      // 阶段行
      const phasesEl = document.getElementById('timing-phases');
      phasesEl.innerHTML = timing.phases.map(p => {
        const label = p.label || p.phase;
        const dur = fmtDur(p.duration_s);
        const durText = dur ? ` <b>${dur}</b>` : '';
        const status = p.status || 'pending';
        const statusText = {pending: '待开始', in_progress: '进行中', done: '已完成', blocked: '待处理'}[status] || status;
        return `<span class="timing-phase ${status}" title="${escapeHtml(p.phase + ' · ' + status)}">`
          + `<span class="dot"></span>${escapeHtml(label)} · ${escapeHtml(statusText)}${durText}</span>`;
      }).join('');
      // 工具行（耗时降序 + 比例条）
      const toolsEl = document.getElementById('timing-tools');
      const maxDur = timing.tools && timing.tools.length ? Math.max(...timing.tools.map(t => t.duration_s || 0)) : 0;
      if (timing.tools && timing.tools.length) {
        toolsEl.innerHTML = timing.tools.map(t => {
          const label = TOOL_LABELS[t.tool] || t.tool;
          const pct = maxDur > 0 ? Math.max(3, Math.round((t.duration_s || 0) / maxDur * 100)) : 0;
          return `<div class="timing-tool-row">`
            + `<span class="tname">${escapeHtml(label)}</span>`
            + `<span class="tbar-box"><span class="tbar" style="width:${pct}%"></span></span>`
            + `<span class="tcount">×${t.count}</span>`
            + `<span class="tdur">${fmtDur(t.duration_s) || '0s'}</span></div>`;
        }).join('');
      } else {
        toolsEl.innerHTML = '<div class="timing-empty">还没有工具耗时记录（重启 agent 后新执行才计时）</div>';
      }
      // 总计
      const totalEl = document.getElementById('timing-total');
      const phaseTotal = timing.total_phase_s || 0;
      const toolTotal = timing.total_tool_s || 0;
      totalEl.innerHTML = `工具调用总耗时 <b>${fmtDur(toolTotal) || '0s'}</b> · 阶段累计 <b>${fmtDur(phaseTotal) || '0s'}</b>`;
    }

    document.getElementById('timing-toggle').addEventListener('click', () => {
      const collapsed = timingPanel.classList.toggle('collapsed');
      document.getElementById('timing-toggle').setAttribute('aria-expanded', String(!collapsed));
      if (!collapsed) {
        materialsPanel.classList.add('collapsed');
        document.getElementById('materials-toggle').setAttribute('aria-expanded', 'false');
        outputsPanel.classList.add('collapsed');
        document.getElementById('outputs-toggle').setAttribute('aria-expanded', 'false');
        remakeSpecPanel.classList.add('collapsed');
        document.getElementById('remake-spec-toggle').setAttribute('aria-expanded', 'false');
      }
    });

    // ============== 调试模式：只推进到指定阶段就停 ==============
    const debugEnabled = document.getElementById('debug-enabled');
    const debugStop = document.getElementById('debug-stop');
    const debugStatus = document.getElementById('debug-status');
    const DEBUG_LABELS = {
      breakdown: '拆镜', story: '剧本', entity: '编导方案',
      asset: '参考资产与故事板', animate: '视频段', compose: '合成', review: '审查'
    };

    async function setDebug(enabled, stopAfter) {
      try {
        const resp = await apiFetch('/api/debug', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled, stop_after: stopAfter })
        });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          toast(enabled
            ? `调试模式已开启：只进行到「${DEBUG_LABELS[stopAfter] || stopAfter}」`
            : '调试模式已关闭', 'ok', 4600);
        } else {
          toast(`调试模式设置失败：${data.error || ''}`, 'err', 5200);
        }
      } catch (err) {
        toast('调试模式设置失败：连接中断', 'err', 5200);
      }
    }

    debugEnabled.addEventListener('change', () => setDebug(debugEnabled.checked, debugStop.value));
    debugStop.addEventListener('change', () => {
      if (debugEnabled.checked) setDebug(true, debugStop.value);
    });

    function renderDebug(debug) {
      const enabled = Boolean(debug && debug.enabled);
      const stopAfter = (debug && debug.stop_after) || 'asset';
      debugEnabled.checked = enabled;
      debugStop.value = stopAfter;
      debugStatus.textContent = enabled ? `只到「${DEBUG_LABELS[stopAfter] || stopAfter}」` : '关闭';
      debugStatus.className = 'debug-status' + (enabled ? ' on' : '');
    }

    function selectedExecutionMode() {
      return activeModeInputs.find(input => input.checked)?.value
        || pendingExecutionMode || 'human_gated';
    }

    async function setExecutionMode(mode) {
      stateEpoch += 1;
      pendingExecutionMode = mode;
      try {
        const resp = await apiFetch('/api/execution-mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({mode, project_id: currentProject})
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || '设置失败');
        modeSaving = null;
        renderExecutionMode(data.execution_mode);
        toast(mode === 'autonomous'
          ? '已切换为端到端无人干预模式'
          : '已切换为关键步骤人工介入模式', 'ok', 4600);
      } catch (err) {
        toast(`执行模式设置失败：${err.message}`, 'err', 5200);
        throw err;
      } finally {
        modeSaving = null;
        stateEpoch += 1;
        refreshState();
      }
    }

    function renderExecutionMode(state) {
      const stateMode = state?.mode || 'human_gated';
      const mode = modeSaving ? pendingExecutionMode
        : (hasProject ? stateMode : (pendingExecutionMode || stateMode));
      if (!hasProject) pendingExecutionMode = mode;
      activeModeInputs.forEach(input => {
        input.checked = input.value === mode;
        input.disabled = Boolean(activeJob || submitting || modeSaving || bottomUploader.isUploading());
      });
      executionModeStatus.textContent = hasProject
        ? (mode === 'autonomous' ? '自动推进' : '等待确认')
        : (mode === 'autonomous' ? '新项目自动推进' : '新项目等待确认');
      executionModeStatus.className = 'debug-status' + (mode === 'autonomous' ? ' on' : '');
    }

    activeModeInputs.forEach(input => input.addEventListener('change', () => {
      if (!input.checked) return;
      if (hasProject) {
        modeSaving = setExecutionMode(input.value);
        modeSaving.catch(() => {});
        renderExecutionMode({mode: input.value});
        return;
      }
      pendingExecutionMode = input.value;
      newModeDirty = true;
      document.querySelectorAll('input[name="new-execution-mode"]').forEach(item => {
        item.checked = item.value === pendingExecutionMode;
      });
      renderExecutionMode({mode: pendingExecutionMode});
    }));
    document.querySelectorAll('input[name="new-execution-mode"]').forEach(input => {
      input.addEventListener('change', () => {
        if (!input.checked) return;
        newModeDirty = true;
        pendingExecutionMode = input.value;
        if (!hasProject) renderExecutionMode({mode: input.value});
      });
    });

    function videoModelHint(option) {
      const caps = option?.capabilities || {};
      return `${caps.max_images || 0}图 · ${caps.max_videos || 0}视频 · `
        + `${caps.max_output_duration || 0}秒 · ${(caps.resolutions || []).join('/')}`;
    }

    function renderVideoModelPicker(container, name, state, selected, onChange) {
      container.innerHTML = '';
      const options = state?.options || [];
      container.style.gridTemplateColumns = `repeat(${Math.max(1, options.length)}, 1fr)`;
      for (const option of options) {
        const label = document.createElement('label');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = name;
        radio.value = option.model_id;
        radio.checked = option.model_id === selected;
        radio.disabled = Boolean(
          options.length === 1 || activeJob || submitting || videoModelSaving);
        label.setAttribute('role', 'radio');
        label.setAttribute('aria-checked', String(radio.checked));
        label.setAttribute('aria-disabled', String(radio.disabled));
        label.tabIndex = radio.disabled ? -1 : 0;
        const strong = document.createElement('strong');
        strong.textContent = option.label;
        const small = document.createElement('small');
        small.textContent = videoModelHint(option);
        const choose = () => {
          if (radio.disabled || radio.checked) return;
          radio.checked = true;
          label.setAttribute('aria-checked', 'true');
          onChange(option.model_id);
        };
        label.addEventListener('click', choose);
        label.addEventListener('keydown', event => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          choose();
        });
        label.append(radio, strong, small);
        container.appendChild(label);
      }
    }

    function selectedVideoModel() {
      return activeVideoModelPicker.querySelector('input:checked')?.value
        || pendingVideoModel || activeVideoModelState?.selected || '';
    }

    function renderNewVideoModel(state) {
      if (!state) return;
      pendingVideoModel = state.selected;
      renderVideoModelPicker(
        newVideoModelPicker, 'new-video-model', state, pendingVideoModel,
        value => {
          pendingVideoModel = value;
          if (!hasProject) renderVideoModel(state);
        });
    }

    async function setVideoModel(value) {
      stateEpoch += 1;
      try {
        const data = await projectRequest('/api/video-model', {
          project_id: currentProject,
          video_model: value
        });
        configureMaterialTypes(data.material_types);
        renderVideoModel(data.video_model);
        toast(`已切换为 ${data.video_model.label}`, 'ok', 4200);
      } catch (err) {
        toast(`视频模型设置失败：${err.message}`, 'err', 5200);
        throw err;
      } finally {
        videoModelSaving = null;
        stateEpoch += 1;
        refreshState();
      }
    }

    function renderVideoModel(state) {
      if (!state) return;
      activeVideoModelState = state;
      const selected = hasProject
        ? state.selected
        : (pendingVideoModel || state.selected);
      if (!hasProject) pendingVideoModel = selected;
      renderVideoModelPicker(
        activeVideoModelPicker, 'active-video-model', state, selected,
        value => {
          if (hasProject) {
            videoModelSaving = setVideoModel(value);
            videoModelSaving.catch(() => {});
          } else {
            pendingVideoModel = value;
            renderVideoModel(state);
            renderVideoModelPicker(
              newVideoModelPicker, 'new-video-model', state, value,
              next => {
                pendingVideoModel = next;
                renderVideoModel(state);
              });
          }
        });
      const option = (state.options || []).find(item => item.model_id === selected);
      videoModelStatus.textContent = (state.options || []).length === 1
        ? `${option?.label || state.label} · 自动`
        : (option?.label || state.label || '');
      videoModelStatus.className = 'debug-status on';
    }

    function toast(text, kind='ok', ttl=4200) {
      const box = document.createElement('div');
      box.className = `toast ${kind}`;
      box.textContent = text;
      toasts.appendChild(box);
      setTimeout(() => box.remove(), ttl);
    }

    function materialType(category) {
      return materialTypes.find(item => item.id === category) || {
        id: category, label: category, media_kind: 'file', accept: '',
        max_bytes: 100 * 1024 * 1024, max_items: 0, entity_lock: false
      };
    }

    function configureMaterialTypes(definitions) {
      const next = Array.isArray(definitions) ? definitions.filter(item => item && item.id) : [];
      const signature = JSON.stringify(next);
      if (signature === materialTypesSignature) return;
      materialTypes = next;
      materialTypesSignature = signature;
      materialsSignature = null;
      materialsRows.replaceChildren();
      for (const type of materialTypes) {
        const row = document.createElement('div');
        row.className = 'materials-row';
        row.dataset.category = type.id;
        const head = document.createElement('div');
        head.className = 'materials-row-head';
        const title = document.createElement('span');
        const label = document.createElement('span');
        label.className = 'materials-row-label';
        label.textContent = type.label || type.id;
        title.appendChild(label);
        if (Number(type.max_items) > 0) {
          const hint = document.createElement('span');
          hint.className = 'materials-row-hint';
          hint.textContent = `（限 ${type.max_items} 个）`;
          title.appendChild(hint);
        }
        const upload = document.createElement('button');
        upload.className = 'mat-btn';
        upload.type = 'button';
        upload.dataset.category = type.id;
        upload.textContent = '上传';
        const previews = document.createElement('div');
        previews.className = 'materials-previews';
        previews.dataset.category = type.id;
        head.append(title, upload);
        row.append(head, previews);
        materialsRows.appendChild(row);
      }
    }

    function getPreviewContainer(category) {
      return Array.from(document.querySelectorAll('.materials-previews'))
        .find(element => element.dataset.category === category) || null;
    }

    // ============== 素材预览：点击放大 + 删除 ==============
    const lightbox = document.getElementById('lightbox');
    const lightboxMedia = document.getElementById('lightbox-media');
    const lightboxCaption = document.getElementById('lightbox-caption');

    function openLightbox(url, filename, mediaKind) {
      url = apiUrl(url);
      lightboxMedia.innerHTML = '';
      let media;
      if (mediaKind === 'video') {
        media = document.createElement('video');
        media.src = url;
        media.controls = true;
        media.autoplay = true;
        media.playsInline = true;
      } else if (mediaKind === 'audio') {
        media = document.createElement('audio');
        media.src = url;
        media.controls = true;
        media.autoplay = true;
      } else {
        media = document.createElement('img');
        media.src = url;
        media.alt = filename;
      }
      lightboxMedia.appendChild(media);
      lightboxCaption.textContent = filename;
      lightbox.hidden = false;
    }

    function closeLightbox() {
      lightbox.hidden = true;
      lightboxMedia.innerHTML = '';
    }

    document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
    lightbox.addEventListener('click', event => {
      if (event.target === event.currentTarget) closeLightbox();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !lightbox.hidden) closeLightbox();
    });

    async function deleteMaterial(category, path, filename) {
      if (!path) { toast('缺少素材路径，无法删除', 'err'); return; }
      const label = materialType(category).label || category;
      if (!window.confirm(`删除${label}「${filename}」？此操作不可撤销。`)) return;
      try {
        const resp = await apiFetch('/api/materials/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category, path })
        });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          toast(`${label}「${filename}」已删除`, 'ok');
          refreshState();
        } else {
          toast(`删除失败：${data.error || ('HTTP ' + resp.status)}`, 'err', 5200);
        }
      } catch (err) {
        toast('删除失败：连接中断', 'err', 5200);
      }
    }

    function addMaterialPreview(category, url, filename, path, mediaKind) {
      url = apiUrl(url);
      const container = getPreviewContainer(category);
      if (!container) return;
      // An optimistic upload preview must be reconciled on the next state read.
      materialsSignature = null;
      const kind = mediaKind || materialType(category).media_kind || 'file';
      const box = document.createElement('div');
      box.className = 'materials-preview';
      box.title = filename;
      if (kind === 'video') {
        const video = document.createElement('video');
        video.src = url;
        video.preload = 'metadata';
        video.muted = true;
        video.playsInline = true;
        box.appendChild(video);
      } else if (kind === 'image') {
        const img = document.createElement('img');
        img.src = url;
        img.alt = filename;
        box.appendChild(img);
      } else {
        const fileIcon = document.createElement('span');
        fileIcon.className = 'material-file-icon';
        fileIcon.textContent = kind === 'audio'
          ? 'AUDIO'
          : (filename.split('.').pop() || 'FILE').toUpperCase();
        box.appendChild(fileIcon);
      }
      box.addEventListener('click', event => {
        if (event.target.closest('.preview-del')) return;
        if (kind === 'file' || kind === 'document') {
          window.open(url, '_blank', 'noopener');
        } else {
          openLightbox(url, filename, kind);
        }
      });
      const del = document.createElement('button');
      del.className = 'preview-del';
      del.type = 'button';
      del.title = '删除';
      del.textContent = '×';
      del.addEventListener('click', event => {
        event.stopPropagation();
        deleteMaterial(category, path, filename);
      });
      box.appendChild(del);
      const name = document.createElement('span');
      name.className = 'preview-name';
      name.textContent = filename;
      box.appendChild(name);
      container.appendChild(box);
    }

    function clearMaterialPreviews() {
      document.querySelectorAll('.materials-previews').forEach(el => { el.innerHTML = ''; });
    }

    function renderMaterialsState(materials) {
      const signature = JSON.stringify(materials || {});
      if (signature === materialsSignature) return;
      clearMaterialPreviews();
      for (const [category, items] of Object.entries(materials || {})) {
        if (!Array.isArray(items)) continue;
        for (const item of items) {
          addMaterialPreview(category, item.url, item.filename, item.path, item.media_kind);
        }
      }
      materialsSignature = signature;
    }

    function formatOutputBytes(value) {
      const bytes = Number(value || 0);
      if (!bytes) return '大小未知';
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    function renderOutputs(outputs, error) {
      const items = Array.isArray(outputs) ? outputs : [];
      const signature = JSON.stringify([error || '', items.map(item => [
        item.id, item.status, item.bytes, item.download_path || item.download_url
      ])]);
      if (signature === outputsSignature) return;
      outputsList.replaceChildren();
      outputsHint.textContent = error
        ? '读取未完成'
        : items.length ? `${items.length} 个可下载文件` : '暂无产物';
      if (error) {
        const warning = document.createElement('div');
        warning.className = 'outputs-empty';
        warning.textContent = `${error}；页面会自动重试。`;
        outputsList.appendChild(warning);
      }
      if (!items.length) {
        const emptyState = document.createElement('div');
        emptyState.className = 'outputs-empty';
        emptyState.textContent = error ? '暂时无法获取产物列表' : '完成的交付文件会显示在这里';
        outputsList.appendChild(emptyState);
      } else {
        for (const item of items) {
          const row = document.createElement('a');
          row.className = 'output-item';
          const url = item.download_path || item.download_url || '';
          row.href = url.startsWith('/api/outputs/')
            ? apiUrl(url) : /^https?:\/\//i.test(url) ? url : '#';
          row.target = '_blank';
          row.rel = 'noopener noreferrer';
          if (row.getAttribute('href') === '#') row.removeAttribute('target');
          const name = document.createElement('span');
          name.className = 'output-name';
          name.textContent = item.filename || item.id || '未命名产物';
          const meta = document.createElement('span');
          meta.className = 'output-meta';
          meta.textContent = `${formatOutputBytes(item.bytes)} · ${item.status || 'unknown'}`;
          row.append(name, meta);
          outputsList.appendChild(row);
        }
      }
      outputsSignature = signature;
    }

    // 上传器只消费 material_types，不内置业务类目。
    function createUploader(refs) {
      let uploading = false;
      let pickCategory = null;
      let pickEntity = '';

      function sync() {
        const ready = refs.ready();
        const locked = !ready || uploading;
        refs.btns.querySelectorAll('.mat-btn').forEach(btn => { btn.disabled = locked; });
        if (refs.hint) {
          refs.hint.textContent = uploading
            ? '正在上传…'
            : (hasProject ? '归档到项目 materials/' : '上传即自动建项目');
          refs.hint.classList.remove('actionable');
        }
        if (!ready) hideEntityRow();
      }

      function hideEntityRow() {
        refs.entityRow.hidden = true;
        refs.entityName.value = '';
        refs.btns.querySelectorAll('.mat-btn').forEach(btn => btn.classList.remove('active'));
      }

      function openPicker(category, entity) {
        const type = materialType(category);
        pickCategory = category;
        pickEntity = entity || '';
        refs.file.value = '';
        refs.file.multiple = Number(type.max_items || 0) !== 1;
        refs.file.accept = type.accept || '';
        refs.file.click();
      }

      function uploadOne(file, category, entity) {
        return new Promise(resolve => {
          const form = new FormData();
          form.append('category', category);   // 字段先于文件，后端流式解析时能提前拿到类型
          form.append('entity_name', entity || '');
          form.append('execution_mode', selectedExecutionMode());
          form.append('video_model', selectedVideoModel());
          if (currentProject) form.append('project_id', currentProject);
          if (!currentProject && remakeSpec.value.trim()) {
            form.append('remake_spec', remakeSpec.value);
          }
          form.append('file', file, file.name);
          refs.status.className = 'materials-status busy';
          const label = materialType(category).label || category;
          refs.status.textContent = `正在上传${label}：${file.name}`;
          refs.track.hidden = false;
          refs.bar.style.width = '0%';
          const xhr = new XMLHttpRequest();
          xhr.open('POST', apiUrl('/api/upload'));
          xhr.upload.addEventListener('progress', event => {
            if (event.lengthComputable && typeof event.total === 'number' && event.total > 0) {
              const percent = Math.round(event.loaded / event.total * 100);
              if (Number.isFinite(percent)) {
                refs.bar.style.width = percent + '%';
              }
            }
          });
          xhr.addEventListener('load', () => {
            let data = {};
            try { data = JSON.parse(xhr.responseText || '{}'); } catch (err) { data = {}; }
            if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
              refs.status.textContent = `已入库：${data.materials_path}`;
              const locked = data.entity_locked ? `，已硬锁实体「${data.entity_name}」` : '';
              toast(`${label}已归档${locked}。发下一条消息时会自动告知导演 Agent。`,
                    'ok', 5200);
              // 上传可能刚自动建了项目：同步项目名/画布/素材清单
              if (!hasProject && data.project_id) {
                currentProject = data.project_id;
                hasProject = true;
                remakeSpecDirty = false;
                renderRemakeSpec(remakeSpec.value, true);
                refreshState();
              }
              if (refs.onUploaded) {
                refs.onUploaded({
                  category,
                  filename: data.filename || file.name,
                  url: data.preview_url || '',
                  materials_path: data.materials_path || '',
                  media_kind: data.media_kind || materialType(category).media_kind,
                  entity_name: data.entity_name || '',
                  entity_locked: Boolean(data.entity_locked)
                });
              }
            } else {
              refs.status.textContent = '';
              const reason = data.error || `HTTP ${xhr.status}`;
              toast(`${file.name} 上传失败：${reason}`, 'err', 6200);
            }
            resolve();
          });
          xhr.addEventListener('error', () => {
            refs.status.textContent = '';
            toast(`${file.name} 上传失败：连接中断`, 'err', 6200);
            resolve();
          });
          xhr.addEventListener('abort', () => {
            refs.status.textContent = '';
            toast('上传已取消', 'err');
            resolve();
          });
          xhr.send(form);
        });
      }

      // 多选时串行上传：并发大文件既撑不满带宽，也会让进度条失真
      async function runQueue(files, category, entity) {
        uploading = true;
        sync();
        try {
          for (const file of files) {
            await uploadOne(file, category, entity);
          }
        } finally {
          uploading = false;
          refs.track.hidden = true;
          refs.bar.style.width = '0%';
          refs.status.className = 'materials-status';
          sync();
        }
      }

      refs.btns.addEventListener('click', event => {
        const btn = event.target.closest('.mat-btn');
        if (!btn || btn.disabled) return;
        const category = btn.dataset.category;
        const type = materialType(category);
        const maxItems = Number(type.max_items || 0);
        const currentItems = getPreviewContainer(category)?.children.length || 0;
        if (maxItems && currentItems >= maxItems) {
          toast(`${type.label || category}最多上传 ${maxItems} 个`, 'err', 5200);
          return;
        }
        hideEntityRow();
        if (!type.entity_lock) {
          openPicker(category, '');
          return;
        }
        btn.classList.add('active');
        pickCategory = category;
        refs.entityRow.hidden = false;
        refs.entityName.placeholder = `${type.label || '素材'}实体名（选填），填写后锁定为实体参考`;
        refs.entityName.focus();
      });
      refs.entityPick.addEventListener('click', () => {
        if (!pickCategory) return;
        openPicker(pickCategory, refs.entityName.value.trim());
      });
      refs.entityCancel.addEventListener('click', hideEntityRow);
      refs.entityName.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          if (pickCategory) openPicker(pickCategory, refs.entityName.value.trim());
        } else if (event.key === 'Escape') {
          hideEntityRow();
        }
      });
      refs.file.addEventListener('change', () => {
        const picked = Array.from(refs.file.files || []);
        const category = pickCategory;
        const entity = pickEntity;
        refs.file.value = '';
        if (!picked.length || !category) return;
        const type = materialType(category);
        if (entity && picked.length > 1) {
          toast('填写实体名时请单张上传，或清空实体名后批量上传。', 'err', 6800);
          return;
        }
        const files = [];
        const limit = Number(type.max_bytes || 100 * 1024 * 1024);
        for (const file of picked) {
          if (!file.size) {
            toast(`${file.name} 为空，已跳过`, 'err');
            continue;
          }
          if (file.size > limit) {
            toast(`${file.name} ${(file.size / 1024 / 1024).toFixed(1)}MB 超过 `
              + `${limit / 1024 / 1024}MB 上限，已跳过。`, 'err', 6200);
            continue;
          }
          files.push(file);
        }
        if (!files.length) return;
        hideEntityRow();
        runQueue(files, category, entity);
      });
      sync();
      return {sync, hideEntityRow, isUploading: () => uploading};
    }

    const materialsPanel = document.getElementById('materials');
    const bottomUploader = createUploader({
      btns: document.getElementById('materials-rows'),
      hint: document.getElementById('materials-hint'),
      entityRow: document.getElementById('entity-row'),
      entityName: document.getElementById('entity-name'),
      entityPick: document.getElementById('entity-pick'),
      entityCancel: document.getElementById('entity-cancel'),
      status: document.getElementById('materials-status'),
      track: document.getElementById('materials-track'),
      bar: document.getElementById('materials-bar'),
      file: document.getElementById('materials-file'),
      onUploaded: info => {
        if (info.url) {
          addMaterialPreview(
            info.category, info.url, info.filename, info.materials_path, info.media_kind
          );
        }
      },
      ready: () => !activeJob && !submitting && !modeSaving && !videoModelSaving
    });

    // applyProjectSwitch / refreshState 的统一入口
    function syncMaterials() {
      // 素材入口始终可见；首次上传可自动创建项目。
      materialsPanel.hidden = false;
      timingPanel.hidden = !hasProject;
      outputsPanel.hidden = !hasProject;
      bottomUploader.sync();
    }

    document.getElementById('outputs-toggle').addEventListener('click', () => {
      const collapsed = outputsPanel.classList.toggle('collapsed');
      document.getElementById('outputs-toggle')
        .setAttribute('aria-expanded', String(!collapsed));
      if (!collapsed) {
        timingPanel.classList.add('collapsed');
        document.getElementById('timing-toggle').setAttribute('aria-expanded', 'false');
        materialsPanel.classList.add('collapsed');
        document.getElementById('materials-toggle').setAttribute('aria-expanded', 'false');
        remakeSpecPanel.classList.add('collapsed');
        document.getElementById('remake-spec-toggle').setAttribute('aria-expanded', 'false');
      }
    });

    document.getElementById('materials-toggle').addEventListener('click', () => {
      const collapsed = materialsPanel.classList.toggle('collapsed');
      document.getElementById('materials-toggle').setAttribute('aria-expanded', String(!collapsed));
      if (!collapsed) {
        timingPanel.classList.add('collapsed');
        document.getElementById('timing-toggle').setAttribute('aria-expanded', 'false');
        outputsPanel.classList.add('collapsed');
        document.getElementById('outputs-toggle').setAttribute('aria-expanded', 'false');
        remakeSpecPanel.classList.add('collapsed');
        document.getElementById('remake-spec-toggle').setAttribute('aria-expanded', 'false');
      }
    });

    document.getElementById('remake-spec-toggle').addEventListener('click', () => {
      const collapsed = remakeSpecPanel.classList.toggle('collapsed');
      document.getElementById('remake-spec-toggle')
        .setAttribute('aria-expanded', String(!collapsed));
      if (!collapsed) {
        timingPanel.classList.add('collapsed');
        document.getElementById('timing-toggle').setAttribute('aria-expanded', 'false');
        outputsPanel.classList.add('collapsed');
        document.getElementById('outputs-toggle').setAttribute('aria-expanded', 'false');
        materialsPanel.classList.add('collapsed');
        document.getElementById('materials-toggle').setAttribute('aria-expanded', 'false');
      }
    });

    function renderRemakeSpec(value, force=false) {
      if (remakeSpecDirty && !force) return;
      const normalized = String(value || '');
      remakeSpec.value = normalized;
      remakeSpecDirty = false;
      remakeSpecHint.textContent = normalized
        ? (hasProject ? '已保存' : '待开始')
        : '未填写';
      remakeSpecStatus.textContent = hasProject
        ? (normalized ? '项目级规范已生效' : '')
        : (normalized ? '将在首次任务中保存' : '可直接填写后开始生成');
      remakeSpecStatus.className =
        'remake-spec-status' + (normalized && hasProject ? ' saved' : '');
      remakeSpecSave.textContent = hasProject ? '保存规范' : '随任务保存';
      remakeSpec.disabled = Boolean(activeJob || submitting || remakeSpecSaving);
      remakeSpecSave.disabled = Boolean(
        activeJob || submitting || remakeSpecSaving);
    }

    remakeSpec.addEventListener('input', () => {
      remakeSpecDirty = true;
      remakeSpecHint.textContent = hasProject ? '未保存' : '待开始';
      remakeSpecStatus.textContent = hasProject
        ? `${remakeSpec.value.length}/12000`
        : `将在首次任务中保存 · ${remakeSpec.value.length}/12000`;
      remakeSpecStatus.className = 'remake-spec-status dirty';
    });

    async function saveRemakeSpec(showToast=true) {
      if (remakeSpecSaving) return;
      if (!hasProject) {
        remakeSpecDirty = true;
        remakeSpecHint.textContent = remakeSpec.value.trim()
          ? '待开始' : '未填写';
        remakeSpecStatus.textContent = remakeSpec.value.trim()
          ? '将在首次任务中保存'
          : '可直接填写后开始生成';
        if (showToast && remakeSpec.value.trim()) {
          toast('复刻规范会随首次生成自动保存。', 'ok', 4200);
        }
        return;
      }
      remakeSpecSaving = true;
      remakeSpec.disabled = true;
      remakeSpecSave.disabled = true;
      remakeSpecStatus.textContent = '保存中…';
      try {
        const data = await projectRequest('/api/remake-spec', {
          project_id: currentProject,
          remake_spec: remakeSpec.value
        });
        renderRemakeSpec(data.remake_spec, true);
        if (showToast) {
          toast('复刻规范已保存，后续所有阶段自动使用。', 'ok', 4600);
        }
      } catch (err) {
        remakeSpecStatus.textContent = err.message;
        remakeSpecStatus.className = 'remake-spec-status dirty';
        throw err;
      } finally {
        remakeSpecSaving = false;
        remakeSpec.disabled = Boolean(activeJob || submitting);
        remakeSpecSave.disabled = Boolean(activeJob || submitting);
      }
    }

    remakeSpecSave.addEventListener('click', () => {
      saveRemakeSpec().catch(() => {});
    });

    // ============== 新建项目，不发任何消息 ==============
    send.addEventListener('click', submit);
    document.getElementById('projects').addEventListener('click', () => loadProjects(false));
    document.getElementById('new-project').addEventListener('click', () => loadProjects(true));
    document.getElementById('project-close').addEventListener('click', () => {
      projectModal.hidden = true;
    });
    projectModal.addEventListener('click', event => {
      if (event.target === projectModal) projectModal.hidden = true;
    });
    document.getElementById('new-project-form').addEventListener('submit', async event => {
      event.preventDefault();
      const brief = projectBrief.value.trim();
      if (!brief) {
        projectError.textContent = '请输入影片项目名称';
        return;
      }
      projectError.textContent = '';
      try {
        const mode = document.querySelector(
          'input[name="new-execution-mode"]:checked')?.value || 'human_gated';
        pendingExecutionMode = mode;
        const created = await projectRequest('/api/projects/new', {
          brief,
          execution_mode: mode,
          remake_spec: projectRemakeSpec.value,
          video_model: newVideoModelPicker.querySelector('input:checked')?.value
            || pendingVideoModel
        });
        newModeDirty = false;
        projectBrief.value = '';
        projectRemakeSpec.value = '';
        // 只建工作区：切进去就行，素材走左下角面板，需求由用户自己在对话框发
        applyProjectSwitch(created, 'new');
        toast('项目已创建。需要参考素材就在左下角上传，然后在下面描述你想拍什么。', 'ok', 5600);
      } catch (err) {
        projectError.textContent = err.message;
      }
    });
    stop.addEventListener('click', async () => {
      await apiFetch('/api/abort', {method:'POST'});
      stop.disabled = true;
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        submit();
      }
    });
    document.getElementById('reload-canvas').addEventListener('click', () => {
      if (canvasUrl) frame.src = canvasUrl + (canvasUrl.includes('?') ? '&' : '?') + 'ts=' + Date.now();
      refreshState();
    });
    setInterval(refreshState, 2000);
    refreshState();
    input.focus();
  </script>
</body>
</html>"""


class StudioHandler(BaseHTTPRequestHandler):
    def _send_json(self, value: Any, status: int = 200):
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _reject_upload(self, error: str, status: int):
        """上传提前拒绝：请求体可能还在传，回包后直接断连，不拉完大文件。"""
        self.close_connection = True
        self._send_json({"error": error}, status)

    def _handle_upload(self):
        project_id = ws.get_active_project()
        material_types = _material_types_state(project_id)
        type_map = {item["id"]: item for item in material_types}
        request_limit = max(
            (int(item.get("max_bytes") or 0) for item in material_types),
            default=MAX_UPLOAD_BYTES,
        )
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type.lower():
            self._reject_upload("请求必须为 multipart/form-data", 400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._reject_upload("请求体为空", 400)
            return
        if length > request_limit + _MULTIPART_SLACK:
            self._reject_upload(f"文件超过 {request_limit // 1024 // 1024}MB 上限", 413)
            return

        temp_dir = Path(tempfile.mkdtemp(prefix="studio_upload_"))
        try:
            fields, file_path, upload_name = _read_upload(
                self.rfile, content_type, length, temp_dir, request_limit)
            category = fields.get("category", "")
            entity_name = fields.get("entity_name", "")
            material_type = type_map.get(category)
            if not material_type:
                self._send_json({"error": f"category 非法：{category or '(空)'}"}, 400)
                return
            if file_path is None or not file_path.is_file() or file_path.stat().st_size == 0:
                self._send_json({"error": "未收到文件（file 字段必填）"}, 400)
                return
            size = file_path.stat().st_size
            max_bytes = int(material_type.get("max_bytes") or MAX_UPLOAD_BYTES)
            if size > max_bytes:
                self._send_json({
                    "error": f"文件超过 {max_bytes // 1024 // 1024}MB 上限",
                }, 413)
                return
            extensions = {
                str(ext).lower() for ext in (material_type.get("extensions") or []) if str(ext)
            }
            if extensions and Path(upload_name).suffix.lower() not in extensions:
                self._send_json({
                    "error": f"{material_type.get('label', category)}不支持该文件格式",
                }, 400)
                return
            detected_format = media_validation.validate_upload_content(
                file_path,
                upload_name,
                str(material_type.get("media_kind") or "file"),
            )

            # 输入合法后才自动建项目，避免错误上传留下空项目。
            _assert_idle()
            _check_project(fields.get("project_id"))
            project_id = _ensure_active_project(
                execution_mode=fields.get("execution_mode") or None,
                remake_spec=fields.get("remake_spec") or "",
                video_model=fields.get("video_model") or None,
            )
            manifest = ws.read_manifest(project_id)
            max_items = int(material_type.get("max_items") or 0)
            current_items = ((manifest.get("materials") or {}).get(category) or [])
            if max_items and len(current_items) >= max_items:
                self._send_json({
                    "error": f"{material_type.get('label', category)}最多上传 {max_items} 个",
                }, 409)
                return
            if not material_type.get("entity_lock"):
                entity_name = ""

            result = _upload_material(file_path, category, entity_name)
            materials_path = result.get("materials_path", "")
            note = (f"[Materials: saved to {materials_path}] category={category}"
                    + (f" entity_name={entity_name}" if entity_name else "")
                    + (" entity_locked=true（该实体已硬锁为唯一参考形象）"
                       if result.get("entity_locked") else ""))
            if category == "reference_video":
                note += "（参考视频：用多模态模型拆镜，按项目 execution_mode 推进；端到端模式不追加人工确认）"
            _push_pending_note(project_id, note)
            # 视频尽快启动 H.264 预览转码（后台线程）；前端轮询会自动换成可播放副本。
            if material_type.get("media_kind") == "video" and materials_path:
                _ensure_video_preview(project_id, Path(materials_path), Path(materials_path).parent)
            preview_url = ""
            if materials_path:
                rel = quote(Path(materials_path).name, safe="")
                preview_url = f"/api/materials/{quote(project_id, safe='')}/{rel}"
            self._send_json({
                "ok": True,
                "materials_path": materials_path,
                "category": category,
                "entity_name": result.get("entity_name", entity_name),
                "entity_locked": bool(result.get("entity_locked")),
                "filename": upload_name,
                "size": size,
                "preview_url": preview_url,
                "project_id": project_id,
                "media_kind": material_type.get("media_kind", "file"),
                "detected_format": detected_format,
            })
        except (ValueError, FileNotFoundError) as exc:
            # 超限/格式错误时 body 往往还没读完，必须断连：keep-alive 下残留字节会串到下一个请求
            self._reject_upload(str(exc), 400)
        except Exception as exc:
            self._reject_upload(str(exc), 500)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def do_GET(self):
        route = urlsplit(self.path).path
        if route in ("", "/"):
            body = render_studio_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/api/state":
            agent = _STATE.get("agent")
            project_id = ws.get_active_project()
            self._send_json({
                **_active_canvas(),
                "agent_running": bool(agent and agent.is_running),
                "material_types": _material_types_state(project_id),
                "materials": _project_materials_state(project_id),
                "timing": _project_timing(project_id),
                "debug": _debug_state(project_id),
                "execution_mode": _execution_mode_state(project_id),
                "video_model": _video_model_state(project_id),
            })
            return
        if route == "/api/projects":
            self._send_json({
                "active_project_id": ws.get_active_project(),
                "default_execution_mode": _execution_mode_state(None),
                "video_model": _video_model_state(None),
                "projects": ws.list_projects(),
            })
            return
        if route.startswith("/api/jobs/"):
            job_id = route.rsplit("/", 1)[-1]
            try:
                since = int(dict(
                    part.split("=", 1) for part in urlsplit(self.path).query.split("&") if "=" in part
                ).get("since", "0"))
            except ValueError:
                since = 0
            with _LOCK:
                job = _STATE["jobs"].get(job_id)
                if not job:
                    self._send_json({"error": "任务不存在"}, 404)
                    return
                self._send_json({
                    "job_id": job_id,
                    "status": job["status"],
                    "events": job["events"][max(0, since):],
                })
            return
        if route.startswith("/api/materials/"):
            parts = route.split("/", 4)
            if len(parts) >= 5:
                project_id = unquote(parts[3])
                rel_path = unquote(parts[4])
                try:
                    project_dir = ws.project_dir(project_id)
                except (ValueError, FileNotFoundError):
                    self._send_json({"error": "Not found"}, 404)
                    return
                materials_dir = (project_dir / "materials").resolve()
                file_path = (materials_dir / rel_path).resolve()
                # 严防路径穿越：必须落在该项目 materials/ 下
                try:
                    file_path.relative_to(materials_dir)
                except ValueError:
                    self._send_json({"error": "Forbidden"}, 403)
                    return
                if not file_path.is_file():
                    self._send_json({"error": "Not found"}, 404)
                    return
                content_type, _ = mimetypes.guess_type(str(file_path))
                if not content_type:
                    content_type = "application/octet-stream"
                try:
                    file_size = file_path.stat().st_size
                except OSError as exc:
                    self._send_json({"error": str(exc)}, 500)
                    return

                def _serve_range():
                    # 视频播放/seek 依赖 HTTP Range：返回 206 + 指定字节区间，避免全量读内存
                    range_header = (self.headers.get("Range") or "").strip()
                    m = re.match(r"bytes=(\d*)-(\d*)$", range_header)
                    if not m:
                        return False
                    start = int(m.group(1)) if m.group(1) else 0
                    end = int(m.group(2)) if m.group(2) else file_size - 1
                    if start < 0 or start >= file_size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return True
                    end = min(end, file_size - 1)
                    length = end - start + 1
                    try:
                        with open(file_path, "rb") as fh:
                            fh.seek(start)
                            data = fh.read(length)
                    except OSError as exc:
                        self._send_json({"error": str(exc)}, 500)
                        return True
                    self.send_response(206)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return True

                if _serve_range():
                    return
                try:
                    with open(file_path, "rb") as fh:
                        data = fh.read()
                except OSError as exc:
                    self._send_json({"error": str(exc)}, 500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._send_json({"error": "Not found"}, 404)
            return
        self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        with _MUTATION_LOCK:
            self._do_post()

    def _do_post(self):
        route = urlsplit(self.path).path
        if route == "/api/chat":
            try:
                body = self._read_json()
                message = str(body.get("message", "")).strip()
                if not message:
                    self._send_json({"error": "消息不能为空"}, 400)
                    return
                _assert_idle()
                _check_project(body.get("project_id"))
                # 没有活跃项目时用这条需求自动建项目（“直接说需求”即建项目）。
                _ensure_active_project(
                    message,
                    execution_mode=body.get("execution_mode"),
                    remake_spec=body.get("remake_spec") or "",
                    video_model=body.get("video_model"),
                )
                self._send_json(_create_job(message), 202)
            except (RuntimeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, 409 if isinstance(exc, RuntimeError) else 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        if route == "/api/projects/new":
            try:
                body = self._read_json()
                manifest = _new_project(
                    body.get("brief", ""),
                    execution_mode=body.get("execution_mode"),
                    remake_spec=body.get("remake_spec") or "",
                    video_model=body.get("video_model"),
                )
                self._send_json({
                    "project_id": manifest["project_id"],
                    "brief": manifest["brief"],
                    **_active_canvas(),
                    "material_types": _material_types_state(manifest["project_id"]),
                    "materials": _project_materials_state(manifest["project_id"]),
                    "timing": _project_timing(manifest["project_id"]),
                    "debug": _debug_state(manifest["project_id"]),
                    "execution_mode": _execution_mode_state(manifest["project_id"]),
                    "video_model": _video_model_state(manifest["project_id"]),
                }, 201)
            except (ValueError, RuntimeError) as exc:
                self._send_json({"error": str(exc)}, 409 if isinstance(exc, RuntimeError) else 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        if route == "/api/projects/open":
            try:
                project_id = str(self._read_json().get("project_id", "")).strip()
                if not project_id:
                    raise ValueError("project_id 不能为空")
                manifest = _activate_project(project_id)
                self._send_json({
                    "project_id": manifest["project_id"],
                    "brief": manifest.get("brief", project_id),
                    **_active_canvas(),
                    "material_types": _material_types_state(manifest["project_id"]),
                    "materials": _project_materials_state(manifest["project_id"]),
                    "timing": _project_timing(manifest["project_id"]),
                    "debug": _debug_state(manifest["project_id"]),
                    "execution_mode": _execution_mode_state(manifest["project_id"]),
                    "video_model": _video_model_state(manifest["project_id"]),
                })
            except FileNotFoundError as exc:
                self._send_json({"error": str(exc)}, 404)
            except (ValueError, RuntimeError) as exc:
                self._send_json({"error": str(exc)}, 409 if isinstance(exc, RuntimeError) else 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        if route == "/api/upload":
            self._handle_upload()
            return
        if route == "/api/materials/delete":
            try:
                body = self._read_json()
                _assert_idle()
                _check_project(body.get("project_id"))
                result = _delete_material(
                    ws.get_active_project(),
                    str(body.get("category", "")),
                    str(body.get("path", "")),
                )
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if route == "/api/debug":
            try:
                body = self._read_json()
                result = _set_debug(
                    ws.get_active_project(),
                    bool(body.get("enabled", False)),
                    str(body.get("stop_after", "asset")),
                )
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if route == "/api/remake-spec":
            try:
                body = self._read_json()
                _assert_idle()
                _check_project(body.get("project_id"))
                result = _set_remake_spec(
                    ws.get_active_project(), body.get("remake_spec", ""))
                self._send_json(result, 200 if result.get("ok") else 400)
            except (RuntimeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    409 if isinstance(exc, RuntimeError) else 400,
                )
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if route == "/api/execution-mode":
            try:
                body = self._read_json()
                _assert_idle()
                _check_project(body.get("project_id"))
                result = _set_execution_mode(
                    ws.get_active_project(), str(body.get("mode", "")))
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if route == "/api/video-model":
            try:
                body = self._read_json()
                _assert_idle()
                _check_project(body.get("project_id"))
                result = _set_video_model(
                    ws.get_active_project(), str(body.get("video_model", "")))
                self._send_json(result, 200 if result.get("ok") else 400)
            except (RuntimeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    409 if isinstance(exc, RuntimeError) else 400,
                )
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if route == "/api/abort":
            agent = _STATE.get("agent")
            if agent:
                agent.abort()
            self._send_json({"ok": True})
            return
        self._send_json({"error": "Not found"}, 404)

    def log_message(self, *args, **kwargs):
        pass


def start_studio_server(agent, host: str | None = None, port: int | None = None,
                        public_url: str | None = None) -> dict[str, Any] | None:
    """Start one daemon studio server and attach it to the active agent."""
    settings = resolve_server_settings("studio", host=host, port=port, public_url=public_url)
    require_explicit_remote_bind("studio", settings.host)
    with _LOCK:
        _STATE["agent"] = agent
        if _STATE.get("server") is not None:
            return {"url": _STATE["url"], "port": _STATE["server"].server_port}
        # Explicit programmatic callers receive the same inheritance as CLI users.
        if host is not None:
            from .server_config import configure_server_overrides
            configure_server_overrides(studio=dict(host=settings.host, port=settings.port,
                                                   public_url=settings.public_url))
        _STATE["host"] = settings.host
        try:
            server = bind_http_server(settings, StudioHandler)
        except OSError:
            return None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = settings.browser_url(server.server_port)
        _STATE.update({"server": server, "thread": thread, "url": url})
        return {"url": url, "port": server.server_port}
