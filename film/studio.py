"""Local chat + workflow canvas studio for VibeFilming."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import workspace as ws
from . import workflow_canvas


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8764

_STATE: dict[str, Any] = {
    "agent": None,
    "server": None,
    "thread": None,
    "url": None,
    "jobs": {},
    "canvas_project": None,
    "canvas_url": None,
}
_LOCK = threading.Lock()


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
    "video_crossfade": "🎞️ 制作转场",
    "video_trim": "✂️ 裁剪视频",
    "video_speed": "⏩ 调整视频速度",
    "video_overlay": "🖼️ 画中画叠加",
    "video_portrait": "📱 横屏转竖屏",
    "audio_process": "🔊 处理音频",
    "gen_audio_bgm": "🎵 生成配乐",
    "query_audio_task": "⏳ 等待配乐生成",
    "extract_frames": "🖼️ 抽取视频帧",
    "burn_subtitle": "🔤 烧录字幕",
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


def _reset_agent_context(agent) -> None:
    """Start a clean conversation context before creating or opening a project."""
    if agent and agent.is_running:
        raise RuntimeError("当前任务仍在运行，请停止或等待完成后再切换项目")
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


def _new_project(brief: str) -> dict[str, Any]:
    brief = str(brief or "").strip()
    if not brief:
        raise ValueError("请输入影片名称或一句话创作需求")
    agent = _STATE.get("agent")
    _reset_agent_context(agent)
    manifest = ws.project_create(brief)
    _activate_project(manifest["project_id"])
    return manifest


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
            live = workflow_canvas.ensure_live_server(project_dir)
            canvas_url = live["url"] if live else None
            _STATE["canvas_project"] = project_id
            _STATE["canvas_url"] = canvas_url
        return {
            "project_id": project_id,
            "project_brief": manifest.get("brief") or project_id,
            "project_dir": str(project_dir),
            "canvas_url": canvas_url,
        }
    except Exception as exc:
        return {"project_id": project_id, "canvas_url": None, "error": str(exc)}


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



def _create_job(message: str) -> dict[str, Any]:
    agent = _STATE.get("agent")
    if agent is None:
        raise RuntimeError("Agent 尚未就绪")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "message": message,
        "events": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _LOCK:
        _STATE["jobs"][job_id] = job
    display_queue = agent.put_task(message, source="studio")
    job["status"] = "running"
    threading.Thread(target=_drain_job, args=(job_id, display_queue), daemon=True).start()
    return {"job_id": job_id, "status": job["status"]}


def render_studio_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VibeFilming Studio</title>
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
      overflow: hidden;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
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

    .composer {
      position: relative;
      z-index: 3;
      padding: 14px 18px 18px;
      border-top: 1px solid var(--line);
      background: rgba(10,12,15,.96);
    }
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
    .new-project-form { display: flex; gap: 9px; padding: 14px 20px; border-bottom: 1px solid var(--line); }
    .new-project-form input {
      min-width: 0; flex: 1; border: 1px solid var(--line); border-radius: 10px; outline: 0;
      padding: 9px 11px; color: var(--text); background: #171b21; font-size: 12px;
    }
    .new-project-form input:focus { border-color: rgba(216,170,88,.55); }
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
      .hint { display: none; }
    }
    @media (max-width: 440px) {
      .app { grid-template-columns: 1fr; grid-template-rows: 56vh 44vh; }
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
          <div><h1>VibeFilming</h1><p>Director Studio</p></div>
        </div>
        <div class="brand-actions">
          <button id="projects" class="brand-btn">项目</button>
          <button id="new-project" class="brand-btn primary">＋ 新建</button>
        </div>
      </header>
      <main id="messages" class="messages">
        <div class="welcome">
          <div class="eyebrow">Creative command center</div>
          <h2>说清你想拍什么，剩下的交给导演 Agent。</h2>
          <p>对话会驱动左侧创作过程，右侧画布会随项目产物自动更新。你也可以直接要求修改某个镜头、角色或成片。</p>
        </div>
      </main>
      <footer class="composer">
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
          <p>新建影片，或切换到历史项目查看当时的完整流水线。</p>
        </div>
        <button id="project-close" class="modal-close" aria-label="关闭">×</button>
      </header>
      <form id="new-project-form" class="new-project-form">
        <input id="project-brief" maxlength="200" placeholder="影片名称或一句话创作需求…" autocomplete="off">
        <button class="btn primary" type="submit">创建影片</button>
      </form>
      <div id="project-error" class="project-error"></div>
      <div id="project-list" class="project-list"></div>
    </section>
  </div>
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
    const projectError = document.getElementById('project-error');
    let activeJob = null;
    let eventCursor = 0;
    let assistantTurn = null;
    let canvasUrl = null;
    let currentProject = null;

    function resetChatForProject(data, mode) {
      messages.innerHTML = '';
      const welcome = document.createElement('div');
      welcome.className = 'welcome';
      const eyebrow = document.createElement('div');
      eyebrow.className = 'eyebrow';
      eyebrow.textContent = mode === 'new' ? 'New film project' : 'Project restored';
      const title = document.createElement('h2');
      title.textContent = data.project_brief || data.brief || data.project_id;
      const desc = document.createElement('p');
      desc.textContent = mode === 'new'
        ? '项目已建立，导演 Agent 将从全新上下文开始创作。'
        : '已切换到该项目。右侧显示当时的完整流水线，你也可以在这里继续修改。';
      welcome.append(eyebrow, title, desc);
      messages.appendChild(welcome);
    }

    function applyProjectSwitch(data, mode) {
      resetChatForProject(data, mode);
      currentProject = data.project_id || null;
      canvasUrl = data.canvas_url || null;
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
      input.focus();
    }

    async function projectRequest(path, payload) {
      const response = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '项目操作失败');
      return data;
    }

    async function loadProjects(focusNew=false) {
      projectError.textContent = '';
      projectList.innerHTML = '<div class="project-empty">正在读取项目…</div>';
      projectModal.hidden = false;
      try {
        const response = await fetch('/api/projects?ts=' + Date.now(), {cache:'no-store'});
        const data = await response.json();
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
          meta.textContent = `${project.created_at || project.updated_at} · ${project.project_id}`;
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
      if (!message || activeJob) return;
      addMessage('user', message);
      input.value = '';
      assistantTurn = addAssistantTurn();
      send.disabled = true;
      stop.disabled = false;
      const response = await fetch('/api/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message})
      });
      const data = await response.json();
      if (!response.ok) {
        assistantTurn.bubble.textContent = data.error || '发送失败';
        assistantTurn.box.classList.remove('running');
        finishJob();
        return;
      }
      activeJob = data.job_id;
      eventCursor = 0;
      pollJob();
    }

    async function pollJob() {
      if (!activeJob) return;
      try {
        const response = await fetch(`/api/jobs/${activeJob}?since=${eventCursor}`, {cache:'no-store'});
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
        if (data.status === 'done' || data.status === 'error') {
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
      try {
        const response = await fetch('/api/state?ts=' + Date.now(), {cache:'no-store'});
        const data = await response.json();
        document.getElementById('project-name').textContent =
          data.project_brief || data.project_id || '等待项目';
        document.getElementById('canvas-state').textContent = data.project_id ? '实时工作流画布' : '画布将在创建项目后出现';
        const hasCanvas = Boolean(data.canvas_url);
        frame.hidden = !hasCanvas;
        empty.hidden = hasCanvas;
        if (!hasCanvas) {
          canvasUrl = null;
          currentProject = data.project_id || null;
          frame.removeAttribute('src');
        } else if (data.canvas_url !== canvasUrl || data.project_id !== currentProject) {
          canvasUrl = data.canvas_url;
          currentProject = data.project_id;
          frame.src = canvasUrl + (canvasUrl.includes('?') ? '&' : '?') + 'ts=' + Date.now();
          frame.hidden = false;
          empty.hidden = true;
        }
      } catch (err) {
        document.getElementById('canvas-state').textContent = '状态连接中…';
      }
    }

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
        projectError.textContent = '请输入影片名称或一句话创作需求';
        return;
      }
      projectError.textContent = '';
      try {
        const created = await projectRequest('/api/projects/new', {brief});
        projectBrief.value = '';
        applyProjectSwitch(created, 'new');
        input.value = brief;
        await submit();
      } catch (err) {
        projectError.textContent = err.message;
      }
    });
    stop.addEventListener('click', async () => {
      await fetch('/api/abort', {method:'POST'});
      stop.disabled = true;
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        submit();
      }
    });
    document.getElementById('reload-canvas').addEventListener('click', () => {
      if (frame.src) frame.src = canvasUrl + (canvasUrl.includes('?') ? '&' : '?') + 'ts=' + Date.now();
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
            self._send_json({
                **_active_canvas(),
                "agent_running": bool(agent and agent.is_running),
            })
            return
        if route == "/api/projects":
            self._send_json({
                "active_project_id": ws.get_active_project(),
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
        self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        route = urlsplit(self.path).path
        if route == "/api/chat":
            try:
                message = str(self._read_json().get("message", "")).strip()
                if not message:
                    self._send_json({"error": "消息不能为空"}, 400)
                    return
                self._send_json(_create_job(message), 202)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        if route == "/api/projects/new":
            try:
                manifest = _new_project(self._read_json().get("brief", ""))
                self._send_json({
                    "project_id": manifest["project_id"],
                    "brief": manifest["brief"],
                    **_active_canvas(),
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
                })
            except FileNotFoundError as exc:
                self._send_json({"error": str(exc)}, 404)
            except (ValueError, RuntimeError) as exc:
                self._send_json({"error": str(exc)}, 409 if isinstance(exc, RuntimeError) else 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
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


def start_studio_server(agent, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any] | None:
    """Start one daemon studio server and attach it to the active agent."""
    with _LOCK:
        _STATE["agent"] = agent
        if _STATE.get("server") is not None:
            return {"url": _STATE["url"], "port": _STATE["server"].server_port}
        server = None
        for candidate in range(port, port + 20):
            try:
                server = ThreadingHTTPServer((host, candidate), StudioHandler)
                break
            except OSError:
                continue
        if server is None:
            return None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        actual_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
        url = f"http://{actual_host}:{server.server_port}/"
        _STATE.update({"server": server, "thread": thread, "url": url})
        return {"url": url, "port": server.server_port}
