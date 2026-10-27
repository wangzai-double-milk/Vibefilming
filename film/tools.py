"""GA 工具适配层：把 film_sdk + workspace 包装成 do_xxx 方法。

挂载方式：用 inject_film_tools(handler) 给 GenericAgentHandler 实例添加方法。
agent_loop 通过 hasattr(self, f"do_{tool_name}") 找到工具，所以只要把方法绑到
handler 上即可，不用改 ga.py / agent_loop.py。
"""
from __future__ import annotations
import base64
import contextlib
import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlsplit

try:
    from agent_loop import StepOutcome
except ModuleNotFoundError:
    class StepOutcome:
        """Standalone CLI placeholder; the CLI calls tool bodies directly."""

        def __init__(self, result=None, next_prompt=None):
            self.result = result
            self.next_prompt = next_prompt

from . import artifact_contracts
from . import workspace as ws
from . import film_sdk as sdk
from . import storage
from . import video_models


# ============== 通用包装：把 SDK 函数变成 do_xxx ==============
def _wrap(tool_name: str, fn, log_keys=None):
    """把同步函数包装成 do_xxx generator。fn 接收 args dict，返回 dict。"""
    def do_method(self, args, response):
        # 复制一份去掉内部字段
        public_args = {k: v for k, v in args.items() if not k.startswith("_")}
        started = time.time()
        yield f"🎬 {tool_name}({_brief(public_args)})\n"
        try:
            result = fn(self, public_args)
            elapsed = time.time() - started
            pid = _active_pid(self)
            ws.log_tool_call(pid, tool_name, public_args,
                             json.dumps(result, ensure_ascii=False, default=str),
                             duration_s=elapsed)
            if pid and tool_name != "workflow_canvas":
                _refresh_workflow_canvas(pid)
            yield f"   ✅ {_brief(result)} ⏱ {elapsed:.1f}s\n"
            return StepOutcome(result, next_prompt="\n")
        except Exception as e:
            elapsed = time.time() - started
            err = {"status": "error", "tool": tool_name, "msg": str(e)}
            ws.log_tool_call(_active_pid(self), tool_name, public_args, str(e),
                             duration_s=elapsed)
            yield f"   ❌ {e} ⏱ {elapsed:.1f}s\n"
            return StepOutcome(err, next_prompt="\n")
    do_method.__name__ = f"do_{tool_name}"
    return do_method


# ============== 装饰器：一处声明 = 自动注册 + 自动生成 schema ==============
# 用 @film_tool(...) 标注的工具会自动注册并自动生成 schema。
# 加新工具只改这一处声明，不再维护手写 registry 或 schema 文件。
_DECORATED_TOOLS: dict = {}      # name -> fn（注入用）
_DECORATED_SCHEMAS: list = []    # OpenAI 风格 function schema（喂给模型用）

_PY2JSON = {str: "string", int: "integer", float: "number",
            bool: "boolean", list: "array", dict: "object"}


def film_tool(name: str, desc: str, params: dict | None = None,
              required: list | None = None):
    """声明一个 film 工具。一处即真相源：

    - 自动登记进 _DECORATED_TOOLS（inject_film_tools 会挂到 handler）
    - 自动生成 schema（build_film_schema 汇总后并入 TOOLS_SCHEMA）

    params: {参数名: spec}。spec 可以是 python 类型（str/int/float/bool/list/dict），
            也可以是 dict，如 {"type": float, "description": "...", "default": 1.0, "enum": [...]}。
    required: 必填参数名列表。
    """
    params = params or {}
    required = required or []

    def deco(fn):
        _DECORATED_TOOLS[name] = fn
        props = {}
        for pname, spec in params.items():
            if isinstance(spec, type):
                props[pname] = {"type": _PY2JSON.get(spec, "string")}
            elif isinstance(spec, dict):
                d = dict(spec)
                if isinstance(d.get("type"), type):
                    d["type"] = _PY2JSON.get(d["type"], "string")
                props[pname] = d
            else:
                props[pname] = {"type": "string"}
        _DECORATED_SCHEMAS.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"[Film] {desc}",
                "parameters": {"type": "object", "properties": props,
                               "required": required},
            },
        })
        return fn
    return deco


def build_film_schema() -> list:
    """返回所有用 @film_tool 声明的工具的 schema 列表（并入 TOOLS_SCHEMA）。"""
    return list(_DECORATED_SCHEMAS)


@film_tool(
    name="environment_check",
    desc="检查本地项目根目录、FFmpeg 和模型配置是否就绪。仅返回配置存在性，不返回凭证值，不发起模型请求。",
)
def _environment_check(handler, args):
    import subprocess
    try:
        probe = subprocess.run(
            [sdk.FFMPEG, "-version"], capture_output=True, text=True, timeout=15)
        ffmpeg_ready = probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ffmpeg_ready = False
    return {
        "contract": "vibefilming.environment/v1", "runtime": "local",
        "project_root": str(ws.PROJECTS_ROOT),
        "ffmpeg_ready": ffmpeg_ready,
        "ffmpeg_executable": sdk.FFMPEG if ffmpeg_ready else None,
        "ark_configured": bool(sdk.get_ark_key()),
        "tos_configured": storage.is_configured(),
        "delivery": "local_project_files",
        "reference_transport": {
            "video": ["public_url", "asset://", "private_sidecar"],
            "private_file_publisher": "publish_reference_file",
            "requires_tos": False,
        },
    }


@film_tool(
    name="project_file_list",
    desc="列出当前项目的实际产物路径和大小，核对本地交付。排除日志、凭证和私有参考 URL sidecar。",
)
def _project_file_list(handler, args):
    root = _project_path(handler)
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(root)
                or any(part.startswith(".") or part == "logs" for part in relative.parts)
                or path.name.endswith(".url.txt")
                or path.name.lower() in {"auth.json", "credentials.json", "secrets.json",
                                         "token.json", "vibefilming.config.json"}
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}):
            continue
        files.append({"project_path": relative.as_posix(),
                      "path": str(path), "bytes": path.stat().st_size})
    return {"project_id": _active_pid(handler), "project_dir": str(root), "files": files}


def _brief(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s[:160] + ("..." if len(s) > 160 else "")


def _active_pid(handler) -> Optional[str]:
    """从 handler 拿当前 project_id。"""
    pid = getattr(handler, "_film_active_project", None)
    if pid:
        return pid
    return ws.get_active_project()


def _set_active_pid(handler, pid: str):
    handler._film_active_project = pid
    ws.set_active_project(pid)
    _redirect_llm_log(handler, pid)


def _redirect_llm_log(handler, pid: str):
    """把 LLM 完整上下文 trace 指向 projects/<pid>/logs/llm_trace.txt。
    注意：只设独立的 trace_path，不动 log_path——temp/ 下的默认简洁日志保持原版格式，
    完整逐轮上下文 dump 只落到项目目录，避免 temp 文件爆量。"""
    try:
        log_dir = ws.project_dir(pid) / "logs"
        log_dir.mkdir(exist_ok=True)
        trace_path = str(log_dir / "llm_trace.txt")
        client = getattr(getattr(handler, "parent", None), "llmclient", None)
        if client is not None:
            client.trace_path = trace_path
    except Exception as e:
        print(f"[WARN] LLM trace 重定向失败: {e}")


def _refresh_workflow_canvas(pid: str):
    """Best-effort refresh of the read-only workflow canvas after tool progress."""
    try:
        from . import workflow_canvas

        project_dir = ws.project_dir(pid)
        graph = workflow_canvas.build_graph(project_dir)
        workflow_canvas.write_canvas(project_dir, graph)
    except Exception as e:
        print(f"[WARN] 工作流画布刷新失败: {e}")


def _project_path(handler, *parts) -> Path:
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    base = ws.project_dir(pid).resolve()
    clean_parts = []
    for raw in parts:
        part = Path(str(raw))
        if part.is_absolute() or ".." in part.parts:
            raise ValueError(f"项目内路径不允许绝对路径或 ..：{raw}")
        clean_parts.append(str(part))
    target = base.joinpath(*clean_parts).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"输出路径越出当前项目：{target}") from exc
    return target


_ARTIFACT_REFS_FILE = "logs/artifact_refs.json"


def _url_base(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"


def _artifact_registry(handler) -> dict:
    try:
        path = _project_path(handler, _ARTIFACT_REFS_FILE)
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value.setdefault("version", 1)
            value.setdefault("artifacts", {})
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"version": 1, "artifacts": {}}


def _register_artifact_ref(handler, local_path: str, source_url: str = "",
                           kind: str = "") -> str:
    project_dir = ws.project_dir(_active_pid(handler)).resolve()
    path = Path(local_path).expanduser().resolve()
    relative = path.relative_to(project_dir).as_posix()
    artifact_ref = f"project://{relative}"
    output = _project_path(handler, _ARTIFACT_REFS_FILE)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ws._manifest_lock, ws._manifest_file_lock(_active_pid(handler), exclusive=True):
        registry = _artifact_registry(handler)
        registry["artifacts"][artifact_ref] = {
            "project_path": relative,
            "kind": str(kind or ""),
            "source_url_base": _url_base(source_url),
        }
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=output.parent,
                prefix=".artifact_refs-", delete=False,
            ) as handle:
                temp = Path(handle.name)
                handle.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
            temp.replace(output)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    return artifact_ref


def _iter_artifact_log_pairs(handler):
    """兼容旧项目：从 gen_image 日志恢复 URL 到项目文件的映射。"""
    try:
        log_path = _project_path(handler, "logs", "model_calls.jsonl")
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in reversed(lines[-2000:]):
        try:
            root = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [root]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                path = item.get("path")
                url = item.get("url")
                if isinstance(path, str) and isinstance(url, str):
                    yield path, url
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)


def _validate_presigned_url(value: str) -> None:
    pairs = {key.lower(): unquote(val) for key, val in parse_qsl(
        urlsplit(value).query, keep_blank_values=True)}
    signature = (
        pairs.get("x-tos-signature")
        or pairs.get("x-amz-signature")
        or ""
    )
    if signature and not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        raise ValueError(
            "媒资预签名 URL 已损坏：signature 必须是 64 位十六进制。"
            "不要重试或手抄该 URL；改用 gen_image 返回的 project:// artifact_ref "
            "或项目内 path。"
        )


def _resolve_project_media_reference(handler, ref: str) -> str:
    """把 Agent 输入恢复为项目内文件，避免把长预签名 URL 继续传给模型。"""
    value = str(ref or "").strip()
    if not value:
        return value
    if value.startswith("data:"):
        return value
    if value.startswith("project://"):
        candidate = _project_path(handler, unquote(value[len("project://"):]))
        if not candidate.is_file():
            raise FileNotFoundError(f"项目产物不存在：{value}")
        return str(candidate)

    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        base = _url_base(value)
        for entry in _artifact_registry(handler)["artifacts"].values():
            if entry.get("source_url_base") != base:
                continue
            candidate = _project_path(handler, entry.get("project_path", ""))
            if candidate.is_file():
                return str(candidate)
        for logged_path, logged_url in _iter_artifact_log_pairs(handler) or ():
            if _url_base(logged_url) != base:
                continue
            candidate = Path(logged_path).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())

        pid = _active_pid(handler) or ""
        decoded_path = unquote(parsed.path)
        marker = f"/projects/{pid}/"
        if marker in decoded_path:
            relative = decoded_path.split(marker, 1)[1]
            candidate = _project_path(handler, relative)
            if candidate.is_file():
                return str(candidate)
        _validate_presigned_url(value)
        return value

    path = Path(value).expanduser()
    if not path.is_absolute() and _active_pid(handler):
        candidate = _project_path(handler, value)
        if candidate.is_file():
            return str(candidate)
    if path.is_file():
        return str(path.resolve())
    return value


# ============== 项目工作区工具 ==============
_SINGLE_VARIABLE_MARKERS = (
    "其他保持不变",
    "其它保持不变",
    "其余保持不变",
    "其他不变",
    "其余不变",
    "唯一变量",
    "只改",
    "仅改",
)
_LOCKED_LAYER_PATTERN = (
    r"(?:字幕|HUD|UI|CTA|图标|数字|数值|音轨|声音|音效|BGM|口播)"
)


def _validate_single_variable_remake_spec(brief: str, spec: str) -> None:
    """Reject internal scope expansion hidden behind a one-variable brief."""
    brief_text = str(brief or "").strip()
    spec_text = str(spec or "").strip()
    if not any(marker in brief_text for marker in _SINGLE_VARIABLE_MARKERS):
        return
    if not spec_text:
        raise ValueError(
            "SINGLE_VARIABLE_LOCK_VIOLATION：单变量完整复刻必须先写入 "
            "remake_spec，明确允许改变、必须保持和因果适配闭包。")

    explicit_locked_change = any(re.search(pattern, brief_text, re.I) for pattern in (
        rf"(?:改|替换|重写|更新|调整).{{0,10}}{_LOCKED_LAYER_PATTERN}",
        rf"{_LOCKED_LAYER_PATTERN}.{{0,10}}(?:也改|改成|替换|重写|更新|调整)",
    ))
    if explicit_locked_change:
        return

    clauses = [
        clause.strip()
        for clause in re.split(r"[\n。；]+", spec_text)
        if clause.strip()
    ]
    patterns = (
        rf"(?:允许改变|允许修改|可改变|可修改|可以改)"
        rf".{{0,160}}{_LOCKED_LAYER_PATTERN}",
        rf"{_LOCKED_LAYER_PATTERN}.{{0,120}}"
        rf"(?:除外|按因果闭包|可随|可以改|允许改|最小改写|"
        rf"最小替换|最小适配|重写|更新)",
    )
    for clause in clauses:
        if any(re.search(pattern, clause, re.I) for pattern in patterns):
            raise ValueError(
                "SINGLE_VARIABLE_LOCK_VIOLATION：用户未点名修改字幕/HUD/UI/"
                "数字/CTA/图标/音频，但 remake_spec 写入了例外："
                f"{clause[:180]}。删除该授权并将这些层逐字逐时锁定；"
                "只有用户明确点名才可改变。")


@film_tool(
    name="project_create",
    desc="创建影视项目工作区，设置执行模式与预算。execution_mode=autonomous 时机器校验通过后自动推进七阶段；human_gated 时在拆镜和剧本关键节点等待人工确认。",
    params={
        "brief": {"type": str, "description": "用户原始需求一句话"},
        "project_id": {
            "type": str,
            "description": "[可选] 稳定项目 ID；省略则自动生成",
            "default": None,
        },
        "execution_mode": {
            "type": str, "enum": ["autonomous", "human_gated"],
            "description": "执行模式；省略则读取 VIBEFILMING_EXECUTION_MODE / JSON 默认值",
            "default": None,
        },
        "remake_spec": {
            "type": str,
            "description": "项目级爆款复刻规范：世界观、固定元素、资源/UI术语、允许替换项和禁改项。后续所有阶段都必须遵守",
            "default": "",
            "maxLength": ws.MAX_REMAKE_SPEC_CHARS,
        },
        "video_model": {
            "type": str,
            "description": "项目视频模型，可传 2.0、2.5 或已配置的模型 ID；只配置一个模型时会自动使用它",
            "default": None,
        },
        "max_seedance_calls": {"type": int, "description": "Seedance 调用次数统计上限，仅记账参考、不阻断（默认 0 = 不设限，可自由返工/重做）", "default": 0},
    },
    required=["brief"],
)
def _project_create(handler, args):
    from . import workflow_canvas

    brief = args.get("brief", "").strip()
    if not brief:
        raise ValueError("brief 必填，描述本次想做的视频")
    # 默认 0 = 无预算上限（仅记账不阻断），方便 agent 自由迭代审片
    budget = int(args.get("max_seedance_calls", 0))
    remake_spec = args.get("remake_spec", "")
    _validate_single_variable_remake_spec(brief, remake_spec)
    m = ws.project_create(
        brief,
        max_seedance_calls=budget,
        execution_mode=args.get("execution_mode"),
        project_id=args.get("project_id"),
        remake_spec=remake_spec,
        video_model=args.get("video_model"),
    )
    _set_active_pid(handler, m["project_id"])
    project_dir = Path(m["project_dir"])
    return {
        "project_id": m["project_id"],
        "project_dir": m["project_dir"],
        "phases": {k: v["status"] for k, v in m["phases"].items()},
        "budget": m["budget"],
        "execution_mode": ws.execution_state_from_manifest(m),
        "video_model": ws.video_model_state(m),
        "remake_spec": m.get("remake_spec", ""),
        "canvas": workflow_canvas.canvas_launch_hint(project_dir),
    }


@film_tool(
    name="project_open",
    desc="切换活跃项目（用于继续之前的项目）",
    params={"project_id": str},
    required=["project_id"],
)
def _project_open(handler, args):
    from . import workflow_canvas

    pid = args["project_id"]
    m = ws.read_manifest(pid)
    m["debug"] = ws.normalize_debug_state(pid)
    _set_active_pid(handler, pid)
    return {
        "project_id": pid,
        "phases": m["phases"],
        "execution_mode": ws.normalize_execution_mode(pid),
        "video_model": ws.video_model_state(m),
        "remake_spec": ws.normalize_remake_spec(m.get("remake_spec", "")),
        "canvas": workflow_canvas.canvas_launch_hint(ws.project_dir(pid)),
    }


@film_tool(
    name="project_configure",
    desc="把用户当前确认的模型、执行模式、复刻规范和调试设置写入已存在的本地项目；配置变化时必须调用，不能只口头声明。",
    params={
        "video_model": {"type": str, "description": "2.0、2.5 或已配置模型 ID"},
        "execution_mode": {"type": str, "enum": ["autonomous", "human_gated"]},
        "remake_spec": {"type": str, "maxLength": ws.MAX_REMAKE_SPEC_CHARS},
        "debug": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "stop_after": {"type": "string", "enum": list(ws.DEBUG_STOP_PHASES)},
            },
            "required": ["enabled"],
        },
    },
)
def _project_configure(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    # Validate the full update before writing any field.
    profile = (
        video_models.resolve(args["video_model"], strict=True)
        if "video_model" in args else None
    )
    spec = (
        ws.normalize_remake_spec(args["remake_spec"])
        if "remake_spec" in args else None
    )
    if spec is not None:
        _validate_single_variable_remake_spec(
            ws.read_manifest(pid).get("brief", ""),
            spec,
        )
    mode = args.get("execution_mode")
    if "execution_mode" in args and mode not in {"autonomous", "human_gated"}:
        raise ValueError("execution_mode 非法")
    debug = args.get("debug")
    if "debug" in args:
        if not isinstance(debug, dict) or not isinstance(debug.get("enabled"), bool):
            raise ValueError("debug.enabled 必须为 boolean")
        debug = {"enabled": debug["enabled"], "stop_after": debug.get("stop_after", "asset")}
        if debug["stop_after"] not in ws.DEBUG_STOP_PHASES:
            raise ValueError("debug.stop_after 非法")
    if profile:
        ws.set_video_model(pid, profile.model_id)
    if spec is not None:
        ws.set_remake_spec(pid, spec)
    if mode and ws.execution_state_from_manifest(ws.read_manifest(pid))["mode"] != mode:
        ws.set_execution_mode(pid, mode, source="agent_or_user")
    if debug is not None:
        ws.update_manifest(pid, lambda manifest: manifest.__setitem__("debug", debug))
    manifest = ws.read_manifest(pid)
    return {
        "ok": True, "project_id": pid,
        "video_model": ws.video_model_state(manifest),
        "execution_mode": ws.execution_state_from_manifest(manifest),
        "remake_spec": manifest.get("remake_spec", ""),
        "debug": manifest.get("debug", {}),
    }


@film_tool(
    name="project_update_phase",
    desc="更新当前影视项目 manifest.json 的阶段状态。阶段 ID 取当前项目 workflow.phases；也允许新增自定义阶段。状态变化会自动打 started_at/finished_at 时间戳并计算耗时 duration_s。",
    params={
        "phase": {"type": "string", "description": "要更新的阶段 ID，优先使用当前项目 workflow.phases[].id"},
        "status": {"type": "string", "enum": ["pending", "in_progress", "done", "blocked"], "description": "阶段状态"},
        "note": {"type": str, "description": "当前阶段的简短证据或阻塞点", "default": ""},
        "artifact": {"type": str, "description": "本阶段关键产物路径，例如 director_plan.json、composed/final.mp4、reviews/final_review.json", "default": ""},
        "shots_done": {"type": int, "description": "可选：已完成镜头/片段数", "default": None},
        "shots_total": {"type": int, "description": "可选：总镜头/片段数", "default": None},
    },
    required=["phase", "status"],
)
def _project_update_phase(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    manifest = ws.read_manifest(pid)
    execution = ws.execution_state_from_manifest(manifest)
    if (args["phase"] == "story" and args["status"] == "done"
            and execution["mode"] == "human_gated"
            and not ws.execution_gate_approved(pid, "script_approval")):
        raise RuntimeError(
            "human_gated：正式剧本尚未获得后续人工确认。"
            "收到用户在剧本产出后的明确确认后，先调用 approve_execution_gate"
            "(gate=script_approval)，再把 story 标为 done。")
    note = str(args.get("note") or "")
    if execution["mode"] == "human_gated":
        if ("awaiting_human_approval: breakdown" in note
                and ws.execution_gate_state(
                    manifest, "breakdown_approval").get("status") == "missing"):
            ws.set_execution_gate(
                pid, "breakdown_approval", "pending",
                actor="system", evidence=note)
        if ("awaiting_human_approval: script" in note
                and ws.execution_gate_state(
                    ws.read_manifest(pid), "script_approval").get("status") == "missing"):
            ws.set_execution_gate(
                pid, "script_approval", "pending",
                actor="system", evidence=note)
    payload = {"status": args["status"], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if args.get("note"):
        payload["note"] = args["note"]
    if args.get("artifact"):
        payload["artifact"] = args["artifact"]
    for key in ("shots_done", "shots_total"):
        if args.get(key) is not None:
            payload[key] = int(args[key])
    m = ws.update_phase(pid, args["phase"], **payload)
    return {"project_id": pid, "phase": args["phase"], "state": m["phases"][args["phase"]]}


@film_tool(
    name="set_execution_mode",
    desc="切换当前项目执行模式。autonomous=端到端无人干预，机器校验通过后自动推进；human_gated=关键步骤人工介入，在拆镜和剧本定稿后暂停确认。不会绕过调试停止点、合规、安全、真实外部阻塞或最终失败。",
    params={
        "mode": {
            "type": str, "enum": ["autonomous", "human_gated"],
            "description": "项目执行模式",
        },
    },
    required=["mode"],
)
def _set_execution_mode(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    selected = ws.set_execution_mode(pid, args["mode"], source="agent_or_user")
    return {"ok": True, "project_id": pid, "execution_mode": selected}


@film_tool(
    name="approve_execution_gate",
    desc="记录 human_gated 项目的人工确认。只能在对应产物已经生成、且用户在产物生成后明确确认的后续消息中调用；不得用初始任务中的“完整执行”提前批准尚未看到的产物。",
    params={
        "gate": {
            "type": str,
            "enum": ["breakdown_approval", "script_approval"],
            "description": "breakdown_approval=确认拆镜；script_approval=确认正式剧本",
        },
        "evidence": {
            "type": str,
            "description": "用户本轮明确确认的原话或等价摘要",
        },
    },
    required=["gate", "evidence"],
)
def _approve_execution_gate(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    manifest = ws.read_manifest(pid)
    execution = ws.execution_state_from_manifest(manifest)
    if execution["mode"] != "human_gated":
        raise ValueError("approve_execution_gate 只用于 human_gated 项目")
    gate = args["gate"]
    current = ws.execution_gate_state(manifest, gate)
    if current.get("status") != "pending":
        raise RuntimeError(f"{gate} 当前不是待确认状态：{current.get('status')}")
    if gate == "breakdown_approval":
        if (manifest.get("phases") or {}).get("breakdown", {}).get("status") != "done":
            raise RuntimeError("拆镜尚未技术完成，不能提前确认")
    else:
        script_path = ws.project_dir(pid) / "script.md"
        if not script_path.is_file():
            raise RuntimeError("正式 script.md 尚未生成，不能提前确认")
    approved = ws.set_execution_gate(
        pid, gate, "approved", actor="human", evidence=args["evidence"])
    return {"ok": True, "project_id": pid, "gate": gate, "approval": approved}


@film_tool(
    name="set_debug_mode",
    desc="调试模式开关：开启后流程只推进到 stop_after 指定的阶段就停止。阶段名与 manifest 完全一致：breakdown/story/entity/asset/animate/compose/review；entity=编导方案，asset=参考资产与故事板。",
    params={
        "enabled": {"type": bool, "description": "True=开启调试模式；False=关闭，恢复正常完整流程"},
        "stop_after": {"type": "string", "enum": list(ws.DEBUG_STOP_PHASES),
                       "description": "停止点：breakdown=拆镜 / story=剧本 / entity=编导方案 / asset=参考资产与故事板 / animate=视频段 / compose=合成 / review=终审", "default": "asset"},
    },
    required=["enabled"],
)
def _set_debug_mode(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    enabled = bool(args.get("enabled", False))
    stop_after = str(args.get("stop_after", "asset")).strip()
    if stop_after not in ws.DEBUG_STOP_PHASES:
        raise ValueError(f"stop_after 非法：{stop_after}")

    def _apply(m: dict):
        m["debug"] = {"enabled": enabled, "stop_after": stop_after}

    ws.update_manifest(pid, _apply)
    return {"ok": True, "debug": {"enabled": enabled, "stop_after": stop_after}}


@film_tool(
    name="workflow_canvas",
    desc="生成当前项目的只读工作流画布：扫描 script/director_plan/assets/storyboards/videos/reviews/composed 和日志依赖，输出 workflow_graph.json 与 canvas.html，用来看 agent 流程走到哪里。",
    params={
        "project_id": {"type": str, "description": "[可选] 项目 id 或项目目录路径；不传则使用当前活跃项目", "default": ""},
    },
)
def _workflow_canvas(handler, args):
    from . import workflow_canvas

    target = (args.get("project_id") or _active_pid(handler) or "").strip()
    if not target:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open，或传 project_id")
    project_dir = workflow_canvas.resolve_project(target)
    graph = workflow_canvas.build_graph(project_dir)
    graph_path, html_path = workflow_canvas.write_canvas(project_dir, graph)
    return {
        "project_id": project_dir.name,
        "graph": str(graph_path),
        "html": str(html_path),
        "canvas": workflow_canvas.canvas_launch_hint(project_dir),
        "stats": graph["stats"],
    }


# ============== 视觉生成 ==============
@film_tool(
    name="gen_image",
    desc="Seedream 图片生成统一入口：文生图或参考图融合（reference_images 最多14张，本地 path 自动处理）。已定产品、角色、场景或道具参与故事板、分镜或关键帧时，应传对应终版参考图。category 决定落盘：entity→entities/，shot→shots/。返回 {path, project_path, artifact_ref, name}；后续工具必须传 path 或 artifact_ref，不传交付 URL。",
    params={
        "prompt": {"type": str, "description": "图像描述"},
        "name": {"type": str, "description": "产物文件名（不含扩展名），由 agent 按项目语义自定，要求稳定、可读、可回查；不要用单视角后缀把三视图误写成单视角图。文件放哪只由 category 决定"},
        "category": {"type": "string", "enum": ["entity", "shot"], "description": "输出类别：entity=长期参考资产（角色/场景/道具，落 entities/）；shot=镜头级素材（故事板/分镜草图/关键帧，落 shots/）"},
        "reference_images": {"type": "array", "items": {"type": "string"}, "description": "[可选] 参考图列表，最多 14 张；优先传本地 path"},
        "size": {"type": str, "description": "输出尺寸 WxH，总像素需 ≥ 3,686,400；16:9 至少 2560x1440，9:16 至少 1440x2560，1:1 至少 1920x1920。低于下限时工具会在调用前按原画幅自动放大，不先提交失败请求", "default": "2048x2048"},
        "watermark": {"type": bool, "description": "是否加「AI 生成」水印/标识。默认 true；只有用户明确要求无标识交付图时才传 false", "default": True},
    },
    required=["prompt", "name", "category"],
)
def _gen_image(handler, args):
    """文生图 / 图编辑 / 多图融合（Seedream）。
    落盘目录由 category 决定：entity 落 entities/，shot 落 shots/。
    传 reference_images（数组，最多 14 张）做图编辑/多图融合。"""
    def _coerce_refs(value):
        if not value:
            return []
        raw = value if isinstance(value, (list, tuple)) else [value]
        refs = []
        for item in raw:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(
                    item.get("path") or item.get("url") or item.get("image_url") or ""
                ).strip()
            else:
                value = str(item).strip()
            if value:
                refs.append(value)
        return refs

    prompt = args["prompt"]
    name = args.get("name", f"img_{int(time.time())}")
    refs = _coerce_refs(args.get("reference_images"))
    refs = [_resolve_project_media_reference(handler, value) for value in refs]
    if len(refs) > 14:
        raise ValueError("gen_image 最多支持 14 张参考图")
    ref = refs if len(refs) > 1 else (refs[0] if refs else None)
    requested_size = str(args.get("size") or "2048x2048")
    size = sdk.normalize_image_size(requested_size)
    watermark = bool(args.get("watermark", True))
    category = args["category"]
    if category not in ("entity", "shot"):
        raise ValueError("gen_image category 只能是 entity 或 shot")
    if category == "entity":
        subdir = "entities"
    else:
        subdir = "shots"
    save = _project_path(handler, subdir, f"{name}.png")
    save.parent.mkdir(parents=True, exist_ok=True)
    gen_started = time.time()
    r = sdk.gen_image(prompt, save, reference_images=ref, size=size, watermark=watermark)
    artifact_ref = _register_artifact_ref(
        handler, r["path"], r.get("url", ""), kind="image")
    project_path = artifact_ref[len("project://"):]
    ws.log_model_call(_active_pid(handler), r.get("body", {}).get("model", sdk.get_model("image", sdk.MODEL_IMG)), {
        "via_tool": "gen_image",
        "name": name,
        "prompt": prompt,
        "reference_images": refs,
        "reference_images_count": len(refs),
        "requested_size": requested_size,
        "size": size,
        "watermark": watermark,
        "result": {"path": r["path"], "url": r["url"]},
    }, raw_request=r.get("body"), raw_response=r.get("raw"),
        duration_s=time.time() - gen_started)
    return {
        "path": r["path"],
        "project_path": project_path,
        "artifact_ref": artifact_ref,
        "name": name,
        "requested_size": requested_size,
        "size": size,
        "size_adjusted": size != requested_size,
    }


def _resolve_reference_images(handler, args) -> tuple:
    """收集 reference_images（url 列表，可含字符串或 {url} dict）给 Seedance 当多模态参考。
    返回 (urls, sources)：urls 是纯 url 列表；sources 是每张图来源摘要（审计用）。
    """
    def normalize_ref(ref: str) -> str:
        return _resolve_project_media_reference(handler, ref)

    urls = []
    sources = []
    raw_imgs = args.get("reference_images") or []
    for x in raw_imgs:
        if isinstance(x, str):
            url = normalize_ref(x)
            urls.append(url)
            sources.append({"url": url, "input": x, "from": "raw_arg"})
        elif isinstance(x, dict) and x.get("url"):
            url = normalize_ref(x["url"])
            urls.append(url)
            sources.append({"url": url, "input": x["url"], "from": "raw_arg_dict"})
    return urls, sources


def _video_profile(handler=None):
    pid = _active_pid(handler) if handler is not None else None
    manifest = ws.read_manifest(pid) if pid else None
    return video_models.resolve_manifest(manifest)


def _reference_media_limits(profile, media_kind: str,
                            task_type: str = "") -> dict:
    count = (
        profile.max_videos if media_kind == "video"
        else profile.max_audios
    )
    minimum = 4.0 if media_kind == "video" and task_type == "edit" else 2.0
    return {
        "count": count,
        "extensions": (
            {".mp4", ".mov"} if media_kind == "video"
            else {".mp3", ".wav"}
        ),
        "single_bytes": (
            200 * 1024 * 1024 if media_kind == "video"
            else 15 * 1024 * 1024
        ),
        "single_duration": (minimum, float(profile.max_reference_duration)),
        "total_duration": float(profile.max_reference_duration),
    }


def _coerce_reference_values(value) -> list[str]:
    if not value:
        return []
    raw = [value] if isinstance(value, (str, dict)) else list(value)
    result = []
    for item in raw:
        if isinstance(item, str):
            ref = item.strip()
        elif isinstance(item, dict):
            ref = str(item.get("url") or item.get("path") or "").strip()
        else:
            ref = str(item).strip()
        if ref and ref not in result:
            result.append(ref)
    return result


def _seedance_call_records(project_id: Optional[str]) -> list[dict]:
    if not project_id:
        return []
    path = ws.project_dir(project_id) / "logs" / "seedance_calls.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _seedance_task_output_format(
    project_id: Optional[str], task_id: str,
) -> str:
    for record in reversed(_seedance_call_records(project_id)):
        if record.get("task_id") == task_id:
            value = str(record.get("output_format") or "").lower()
            if value in {"mp4", "mov"}:
                return value
    return ""


def _seedance_unit_aliases(
    project_id: Optional[str], unit_id: str,
) -> set[str]:
    aliases = {str(unit_id).strip()}
    if not project_id:
        return aliases
    plan_path = ws.project_dir(project_id) / "director_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return aliases
    artifact_contracts.validate(plan_path, plan)
    for collection in ("units", "segments", "shots"):
        items = plan.get(collection) or []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            identifiers = {
                str(item.get(key) or "").strip()
                for key in ("id", "unit_id", "name")
            }
            if unit_id not in identifiers:
                continue
            aliases.update(value for value in identifiers if value)
            for key in (
                "raw_output_path", "exact_output_path",
                "output_path", "clip_path",
            ):
                value = str(item.get(key) or "").strip()
                if value:
                    aliases.add(Path(value).stem)
            submissions = item.get("seedance_submissions") or []
            for submission in (
                submissions if isinstance(submissions, list) else []
            ):
                if isinstance(submission, dict) and submission.get("name"):
                    aliases.add(str(submission["name"]).strip())
    return aliases


MAX_SEEDANCE_SUBMISSIONS_PER_UNIT = 2


def seedance_submission_status(
    project_id: Optional[str], unit_id: str,
) -> dict:
    unit_id = str(unit_id or "").strip()
    if not unit_id:
        raise ValueError("unit_id 不能为空")
    aliases = _seedance_unit_aliases(project_id, unit_id)

    def matches(record: dict) -> bool:
        record_unit = str(record.get("unit_id") or "").strip()
        if record_unit:
            return record_unit == unit_id
        return str(record.get("name") or "").strip() in aliases

    def counts(record: dict) -> bool:
        if "counts_as_attempt" in record:
            return bool(record.get("counts_as_attempt"))
        return bool(record.get("task_id")) or record.get("status") in {
            "submitted", "accepted", "submission_failed_or_unknown",
        }

    records = [
        record for record in _seedance_call_records(project_id)
        if matches(record) and counts(record)
    ]
    submission_unknown = any(
        record.get("status") == "submission_failed_or_unknown"
        for record in records
    )
    attempts_used = len(records)
    retry_blocked = (
        submission_unknown
        or attempts_used >= MAX_SEEDANCE_SUBMISSIONS_PER_UNIT
    )
    reason = (
        "BLOCKED_SUBMISSION_UNKNOWN"
        if submission_unknown
        else "BLOCKED_RETRY_LIMIT"
        if attempts_used >= MAX_SEEDANCE_SUBMISSIONS_PER_UNIT
        else ""
    )
    return {
        "contract": artifact_contracts.GENERATION_UNIT_STATUS,
        "unit_id": unit_id,
        "attempts_used": attempts_used,
        "attempts_remaining": max(
            0, MAX_SEEDANCE_SUBMISSIONS_PER_UNIT - attempts_used),
        "max_submissions": MAX_SEEDANCE_SUBMISSIONS_PER_UNIT,
        "submission_unknown": submission_unknown,
        "retry_blocked": retry_blocked,
        "can_submit": not retry_blocked,
        "reason": reason,
        "task_ids": [
            str(record.get("task_id"))
            for record in records if record.get("task_id")
        ],
    }


def seedance_submission_states(
    project_id: Optional[str],
) -> dict[str, dict]:
    candidates = {
        str(record.get("unit_id") or record.get("name") or "").strip()
        for record in _seedance_call_records(project_id)
    }
    if project_id:
        plan_path = ws.project_dir(project_id) / "director_plan.json"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = {}
        for collection in ("units", "segments", "shots"):
            items = plan.get(collection) or []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                candidate = str(
                    item.get("unit_id")
                    or item.get("id")
                    or item.get("name")
                    or ""
                ).strip()
                if candidate:
                    candidates.add(candidate)
    return {
        candidate: seedance_submission_status(project_id, candidate)
        for candidate in sorted(candidates) if candidate
    }


@film_tool(
    name="generation_unit_status",
    desc="查询稳定生成单元的真实提交额度、未知提交结果和任务 ID；状态不因改名或恢复而重置。",
    params={"unit_id": str}, required=["unit_id"],
)
def _generation_unit_status(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先创建或打开项目")
    return seedance_submission_status(pid, args["unit_id"])


def _guard_seedance_submission(
    project_id: Optional[str], unit_id: str,
) -> None:
    state = seedance_submission_status(project_id, unit_id)
    if state["submission_unknown"]:
        raise RuntimeError(
            "BLOCKED_SUBMISSION_UNKNOWN：该 unit_id 已有结果未知的提交，"
            "可能已在云端计费。不要自动重提；先人工核对云端任务。"
        )
    if state["retry_blocked"]:
        raise RuntimeError(
            "BLOCKED_RETRY_LIMIT：该 unit_id 已达到执行器提交上限，"
            "禁止继续生成。")


def _validate_local_reference(path: Path, media_kind: str, profile=None,
                              task_type: str = "") -> dict:
    profile = profile or video_models.resolve()
    limits = _reference_media_limits(profile, media_kind, task_type)
    suffix = path.suffix.lower()
    if suffix not in limits["extensions"]:
        allowed = "/".join(sorted(ext.lstrip(".") for ext in limits["extensions"]))
        raise ValueError(
            f"{profile.label} 参考{media_kind}仅支持 {allowed}，"
            f"当前为 {suffix or '无扩展名'}")
    size = path.stat().st_size
    if size > limits["single_bytes"]:
        maximum = limits["single_bytes"] // 1024 // 1024
        raise ValueError(
            f"{profile.label} 单个参考{media_kind}不能超过 {maximum}MB：{path.name}")
    duration = float(sdk.probe_duration(str(path)))
    minimum, maximum = limits["single_duration"]
    if duration < minimum or duration > maximum:
        raise ValueError(
            f"{profile.label} 单个参考{media_kind}时长必须为 "
            f"{minimum:g}-{maximum:g} 秒，"
            f"{path.name} 为 {duration:.3f} 秒")
    metadata = {
        "path": str(path),
        "kind": media_kind,
        "duration": duration,
        "size_bytes": size,
    }
    if media_kind == "video":
        fps = float(sdk.probe_video_fps(str(path)))
        if fps < 24 or fps > 60:
            raise ValueError(
                f"{profile.label} 参考视频帧率必须为 24-60 FPS，"
                f"{path.name} 为 {fps:.3f} FPS")
        metadata["fps"] = fps
    return metadata


def _publish_reference_path(handler, path: Path) -> str:
    pid = _active_pid(handler) if handler is not None else None
    if pid and storage.is_configured():
        project_dir = ws.project_dir(pid).resolve()
        try:
            relative = path.resolve().relative_to(project_dir).as_posix()
        except ValueError:
            relative = (
                "materials/runtime_refs/"
                f"{path.stat().st_size}_{path.name}"
            )
        url = storage.publish(
            path,
            key=f"projects/{pid}/{relative}",
        )
        if url:
            return url

    sidecar = path.with_suffix(".url.txt")
    if sidecar.is_file():
        url = sidecar.read_text(encoding="utf-8").strip()
        if url.startswith(("http://", "https://", "asset://")):
            if url.startswith(("http://", "https://")):
                _validate_presigned_url(url)
            return url

    raise RuntimeError(
        "REFERENCE_TRANSPORT_UNAVAILABLE："
        f"本地视频 '{path}' 没有当前 Seedance 可接受的输入引用。"
        f"请提供公网 URL、asset://、同名 sidecar {sidecar.name}，"
        "或先调用 publish_reference_file 发布到方舟 Files 托管存储。"
        "本地产物仍可直接查看和交付，"
        "但 Seedance video_url 不接受本地路径或普通 File ID；"
        "这不代表必须配置 TOS。原片复刻必须保留原片输入，不能用生成片代替。"
    )


def _audio_data_uri(path: Path) -> str:
    subtype = {
        ".wav": "wav",
        ".mp3": "mpeg",
    }.get(path.suffix.lower())
    if not subtype:
        raise ValueError(f"音频 Base64 传输不支持格式：{path.suffix}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:audio/{subtype};base64,{encoded}"


def _files_api_request(path: str, *, data=None, content_type="application/json",
                       method="GET", timeout=60) -> dict:
    key = sdk.get_ark_key()
    if not key:
        raise RuntimeError(
            "FILES_TRANSPORT_AUTH_UNAVAILABLE：publish_reference_file "
            "需要本地配置 ark.api_key 或环境变量 ARK_API_KEY")
    request = urllib.request.Request(
        sdk.get_ark_base().rstrip("/") + "/" + path.lstrip("/"),
        data=data,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": content_type,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"Files API HTTP {exc.code}: {detail[:600]}") from exc
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict):
        raise RuntimeError("Files API 返回格式不是 JSON object")
    return value


def _publish_to_ark_files(path: Path) -> dict:
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("方舟托管 Files 单文件不得超过 512MB")
    boundary = "----vibefilming" + uuid.uuid4().hex
    crlf = b"\r\n"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = b"".join([
        b"--" + boundary.encode() + crlf,
        b'Content-Disposition: form-data; name="purpose"' + crlf + crlf,
        b"user_data" + crlf,
        b"--" + boundary.encode() + crlf,
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{path.name}"'
        ).encode("utf-8") + crlf,
        f"Content-Type: {mime}".encode() + crlf + crlf,
        path.read_bytes() + crlf,
        b"--" + boundary.encode() + b"--" + crlf,
    ])
    uploaded = _files_api_request(
        "files",
        data=payload,
        content_type=f"multipart/form-data; boundary={boundary}",
        method="POST",
        timeout=180,
    )
    file_id = str(uploaded.get("id") or "")
    if not file_id:
        raise RuntimeError("Files API 上传未返回 file_id")
    deadline = time.time() + 90
    current = uploaded
    while current.get("status") == "processing" and time.time() < deadline:
        time.sleep(1)
        current = _files_api_request(f"files/{file_id}", timeout=30)
    if current.get("status") != "active":
        error = current.get("error") or {}
        raise RuntimeError(
            "Files API 文件未进入 active："
            f"{current.get('status')} {error.get('code', '')} "
            f"{error.get('message', '')}".strip())
    url = str(current.get("download_url") or "")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("Files API active 文件未返回 download_url")
    _validate_presigned_url(url)
    return {
        "file_id": file_id,
        "download_url": url,
        "expire_at": current.get("expire_at"),
        "bytes": int(current.get("bytes") or path.stat().st_size),
        "mime_type": str(current.get("mime_type") or mime),
    }


@film_tool(
    name="publish_reference_file",
    desc="把当前项目中的本地视频或音频上传到方舟托管 Files，并在原文件旁写私有 URL sidecar，供 Seedance 后续读取。不需要自有 TOS，不把签名 URL 写入 stdout 或交付清单。",
    params={
        "file_path": {
            "type": str,
            "description": "项目内视频或音频路径；支持 project://、项目相对路径或工具返回的绝对 path",
        },
    },
    required=["file_path"],
)
def _publish_reference_file(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("项目尚未创建")
    project_dir = ws.project_dir(pid).resolve()
    resolved = _resolve_project_media_reference(
        handler, str(args["file_path"]))
    path = Path(resolved).expanduser().resolve()
    if not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError(f"待发布参考文件不存在或为空：{args['file_path']}")
    try:
        relative = path.relative_to(project_dir).as_posix()
    except ValueError as exc:
        raise ValueError("publish_reference_file 只允许当前项目内文件") from exc
    result = _publish_to_ark_files(path)
    sidecar = path.with_suffix(".url.txt")
    sidecar.write_text(result.pop("download_url"), encoding="utf-8")
    os.chmod(sidecar, 0o600)
    audit = project_dir / "logs" / "files_reference_transport.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "ts": time.time(),
            "project_path": relative,
            "file_id": result["file_id"],
            "bytes": result["bytes"],
            "expire_at": result["expire_at"],
            "transport": "ark_files_api_download_url",
        }, ensure_ascii=False) + "\n")
    return {
        "ok": True,
        "file_id": result["file_id"],
        "project_path": relative,
        "artifact_ref": f"project://{relative}",
        "bytes": result["bytes"],
        "mime_type": result["mime_type"],
        "expire_at": result["expire_at"],
        "reference_transport": "ark_files_api_download_url",
        "custom_tos_required": False,
        "url_sidecar_ready": True,
    }


def _resolve_reference_media(handler, refs, media_kind: str, profile=None,
                             task_type: str = "") -> tuple[list, list]:
    profile = profile or _video_profile(handler)
    limits = _reference_media_limits(profile, media_kind, task_type)
    values = _coerce_reference_values(refs)
    if len(values) > limits["count"]:
        raise ValueError(
            f"{profile.label} reference_{media_kind}_urls 最多 "
            f"{limits['count']} 段，当前为 {len(values)}")

    urls = []
    sources = []
    known_total = 0.0
    inline_audio_bytes = 0
    for value in values:
        if value.startswith(("http://", "https://")):
            _validate_presigned_url(value)
            url = value
            metadata = {"input": value, "kind": media_kind, "probe": "api"}
        elif value.startswith("asset://"):
            url = value
            metadata = {"input": value, "kind": media_kind, "probe": "api"}
        elif value.startswith("data:audio/") and media_kind == "audio":
            url = value
            metadata = {"input": "data:audio/...", "kind": media_kind,
                        "probe": "api"}
        else:
            resolved = (
                _resolve_project_media_reference(handler, value)
                if handler is not None else value
            )
            path = Path(resolved).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"参考{media_kind}不存在：{value}")
            path = path.resolve()
            metadata = _validate_local_reference(
                path, media_kind, profile=profile, task_type=task_type)
            known_total += metadata["duration"]
            if known_total > limits["total_duration"]:
                raise ValueError(
                    f"{profile.label} 本地参考{media_kind}总时长不能超过 "
                    f"{limits['total_duration']:.0f} 秒，"
                    f"当前为 {known_total:.3f} 秒")
            if media_kind == "audio":
                inline_audio_bytes += metadata["size_bytes"]
                if inline_audio_bytes > 45 * 1024 * 1024:
                    raise ValueError(
                        "本次本地参考音频原始数据超过 45MB；Base64 后可能超过 "
                        "Seedance 64MB 请求体限制，请减少音频数量或时长"
                    )
                url = _audio_data_uri(path)
                metadata["url_source"] = "sandbox_data_uri"
            else:
                url = _publish_reference_path(handler, path)
                metadata["url_source"] = "sidecar_or_optional_storage"
            metadata["input"] = value
        urls.append(url)
        sources.append(metadata)
    return urls, sources


def _resolve_reference_video(
    ref_video: Optional[str], handler=None,
) -> Optional[str]:
    """兼容旧版单视频调用；新代码使用 reference_video_urls。"""
    try:
        urls, _ = _resolve_reference_media(
            handler, [ref_video] if ref_video else [], "video")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{exc}。原片复刻必须传当前可访问的原片 URL、asset:// 或项目媒资引用。"
        ) from exc
    return urls[0] if urls else None


_EXTEND_INTENT_RE = re.compile(
    r"(?:向前|向后)延长.{0,20}(?:视频|@video|\bvideo\b)|"
    r"(?:延续|续写|继续(?:往前|往后)?(?:扩写|生成)?)"
    r".{0,20}(?:视频|@video|\bvideo\b)|"
    r"\bextend\b.{0,30}\bvideo\b|"
    r"\bcontinue\b.{0,30}\bvideo\b|"
    r"\bvideo\b.{0,30}\b(?:extend|continue)\b",
    re.IGNORECASE,
)


def _validate_video_generation_strategy(args: dict, profile) -> None:
    """Reject contradictory Seedance modes before publishing any references."""
    first_frame = str(args.get("first_frame") or "").strip()
    last_frame = str(args.get("last_frame") or "").strip()
    task_type = str(args.get("omni_reference_task_type") or "").strip()
    has_references = any((
        args.get("reference_images"),
        args.get("reference_video_urls"),
        args.get("reference_video_url"),
        args.get("reference_audio_urls"),
    ))
    if last_frame and not first_frame:
        raise ValueError("last_frame 不能单独输入，必须同时提供 first_frame")
    if (first_frame or last_frame) and has_references:
        raise ValueError("严格首帧/首尾帧不能与任何 reference_* 同时使用")
    if (first_frame or last_frame) and task_type:
        raise ValueError(
            "严格首帧/首尾帧不能同时设置 omni_reference_task_type")
    if first_frame and args.get("ratio") != "adaptive":
        raise ValueError("严格首帧/首尾帧必须使用 ratio=adaptive")
    if not task_type:
        return
    if profile.key != "2.5":
        raise ValueError(
            f"{profile.label} 不支持 omni_reference_task_type={task_type!r}")
    videos = _coerce_reference_values(args.get("reference_video_urls"))
    legacy_video = str(args.get("reference_video_url") or "").strip()
    if legacy_video and legacy_video not in videos:
        videos.insert(0, legacy_video)
    if task_type in {"edit", "extend"} and not videos:
        raise ValueError(f"{task_type} 任务必须提供 reference_video_urls")
    if task_type in {"edit", "extend"} and args.get("ratio") != "adaptive":
        raise ValueError(f"{task_type} 任务必须使用 ratio=adaptive")
    if task_type == "edit" and int(args.get("duration", 0)) != -1:
        raise ValueError("edit 任务必须使用 duration=-1")
    if (
        task_type == "extend"
        and not _EXTEND_INTENT_RE.search(str(args.get("prompt") or ""))
    ):
        raise ValueError(
            "extend 任务的 prompt 必须明确写向前/向后延长、延续或续写视频，"
            "不能只写“承接上段”"
        )


@film_tool(
    name="gen_video_t2v",
    desc="Seedance 2.0/2.5 视频生成统一入口。模型由当前项目选择；只配置一个时自动路由。提交后立即返回 task_id，用 query_video_task 轮询。",
    params={
        "prompt": {"type": str, "description": "视频描述。真正同一长镜头续写用 reference_video_urls + omni_reference_task_type=extend；普通串行同场景片段可把上一段真实末帧作为 first_frame，但严格首帧模式与所有 reference_* 互斥。原片硬切不做链式续写。有对白时用 {逐字台词}，环境音/动作声用 <具体音效>；需要背景音乐时写清情绪和对白避让。"},
        "name": {"type": str, "description": "产物文件名（不含扩展名）"},
        "unit_id": {"type": str, "description": "计划中的稳定逻辑单元 ID；重试或改文件名时保持不变，用于累计真实提交次数"},
        "duration": {"type": int, "description": "视频时长：2.0 为 4-15 秒，2.5 为 4-30 秒；-1 表示模型按任务自适应", "minimum": -1, "maximum": 30},
        "generate_audio": {"type": bool, "description": "原生同步音频，默认 true。用于原生对白/环境声/必要音效；是否加背景音乐由 prompt/audio_plan 决定。只有明确要全静音时才传 false", "default": True},
        "reference_images": {"type": "array", "items": {"type": "string"}, "maxItems": 30, "description": "[可选] 2.0 最多 9 张，2.5 最多 30 张；放本 shot 的产品、故事板、角色、场景和道具参考。优先传 gen_image 返回的本地 path"},
        "reference_video_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": "[可选] 2.0 最多 3 段且总长 15 秒，2.5 最多 10 段且总长 30 秒；mp4/mov、单段不超过 200MB、24-60 FPS。支持公网 URL、asset://、project:// 或项目路径。"},
        "reference_audio_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": "[可选] 2.0 最多 3 段且总长 15 秒，2.5 最多 10 段且总长 30 秒；wav/mp3、单段不超过 15MB。2.0 不能仅输入音频，2.5 支持。"},
        "reference_video_url": {"type": str, "description": "[兼容旧调用] 单个动态参考；新调用使用 reference_video_urls"},
        "first_frame": {"type": str, "description": "[可选] 严格首帧图片。串行同场景可传 extract_boundary_frame 返回的上一段真实末帧；与所有 reference_* 及 omni_reference_task_type 互斥，ratio 必须为 adaptive"},
        "last_frame": {"type": str, "description": "[可选] 严格尾帧图片。仅在终态已确定且与首帧同画幅时使用；必须同时传 first_frame，与其他参考及任务类型互斥"},
        "omni_reference_task_type": {"type": str, "enum": ["auto", "reference", "edit", "extend"], "description": "[仅 2.5] edit=编辑原视频，必须有4-30秒参考视频、ratio=adaptive、duration=-1；extend=从参考视频向前/后延长，ratio=adaptive。edit/extend 省略 output_format 时默认 mov"},
        "resolution": {"type": str, "enum": ["480p", "720p", "1080p", "4k"], "description": "视频清晰度。2.0 支持 4k；2.5 最高 1080p", "default": "720p"},
        "ratio": {"type": str, "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"], "description": "视频画幅比例，必须显式传入；edit/extend/首尾帧使用 adaptive"},
        "output_format": {"type": str, "enum": ["mp4", "mov"], "description": "[可选] 输出格式。2.0 仅 mp4；2.5 普通生成默认 mp4，edit/extend 默认 mov"},
    },
    required=["prompt", "name", "unit_id", "duration", "ratio"],
)
def _gen_video_t2v(handler, args):
    """Seedance 2.0/2.5 unified submission with project-level routing."""
    prompt = args["prompt"]
    profile = _video_profile(handler)
    unit_id = str(args.get("unit_id") or "").strip()
    if not unit_id:
        raise ValueError(
            "gen_video_t2v 必须显式传稳定 unit_id；"
            "返工改 name 时仍复用同一个 unit_id")
    if "duration" not in args:
        raise ValueError(
            "gen_video_t2v 必须显式传 duration（-1 或模型支持的秒数），"
            "不能省略让模型自定时长")
    duration = int(args["duration"])
    if (
        duration != -1
        and not profile.min_output_duration
        <= duration
        <= profile.max_output_duration
    ):
        raise ValueError(
            f"{profile.label} duration 只支持 -1 或 "
            f"{profile.min_output_duration}-{profile.max_output_duration} 秒，"
            f"当前为 {duration}")
    ratio = args.get("ratio")
    if ratio not in profile.ratios:
        raise ValueError(
            f"gen_video_t2v 必须显式传合法 ratio，当前为 {ratio!r}，"
            f"可选：{list(profile.ratios)}")
    resolution = args.get("resolution", "720p")
    if resolution not in profile.resolutions:
        raise ValueError(
            f"{profile.label} resolution 只支持 "
            f"{'/'.join(profile.resolutions)}，当前为 {resolution!r}"
        )
    task_type = str(args.get("omni_reference_task_type") or "").strip()
    if task_type and task_type not in profile.task_types:
        raise ValueError(
            f"{profile.label} 不支持 omni_reference_task_type={task_type!r}")
    _validate_video_generation_strategy(args, profile)
    output_format = str(
        args.get("output_format")
        or (
            "mov"
            if profile.key == "2.5" and task_type in {"edit", "extend"}
            else "mp4"
        )
    ).strip().lower()
    if output_format not in profile.output_formats:
        raise ValueError(
            f"{profile.label} output_format 只支持 "
            f"{'/'.join(profile.output_formats)}，当前为 {output_format!r}")
    reference_images = args.get("reference_images") or []
    if not isinstance(reference_images, list):
        raise ValueError("gen_video_t2v reference_images 必须是图片路径数组")
    if len(reference_images) > profile.max_images:
        raise ValueError(
            f"{profile.label} reference_images 最多 {profile.max_images} 张，"
            f"当前为 {len(reference_images)} 张")
    if args.get("seed") is not None:
        raise ValueError(f"{profile.label} 暂不支持 seed 参数，请删除 seed 后重试")
    generate_audio = bool(args.get("generate_audio", True))
    # A standalone 说 also matches technical words such as 说明 and 传说.
    # Require speech actions, direct quotation, or the explicit dialogue syntax.
    has_speech_intent = bool(re.search(
        r"[{}]|说话|说道|说出|说[：:「『“\"']|喊|大喊|开口|嘴唇|口型|"
        r"对白|台词|低语|朗读|念出|回答|询问|讨论", prompt))
    if not generate_audio and has_speech_intent:
        raise ValueError(
            "gen_video_t2v 检测到 prompt 含对白/口型语义，但 generate_audio=false。"
            "有说话、喊叫、嘴唇动作或 {逐字台词} 时必须使用 Seedance 原生音频。"
        )
    if generate_audio and "无任何音频" in prompt:
        raise ValueError(
            "gen_video_t2v 默认开启原生音频，但 prompt 写了“无任何音频”。"
            "若确实要全静音，请显式传 generate_audio=false 并删除所有口型/音效语义；"
            "否则改成“无背景音乐，仅保留环境音效/人物对白”。"
        )

    name = args.get("name", f"video_{int(time.time())}")
    pid = _active_pid(handler)
    first_frame_input = str(args.get("first_frame") or "").strip()
    last_frame_input = str(args.get("last_frame") or "").strip()
    first_frame = (
        _resolve_project_media_reference(handler, first_frame_input)
        if first_frame_input else None
    )
    last_frame = (
        _resolve_project_media_reference(handler, last_frame_input)
        if last_frame_input else None
    )
    continuity_strategy = (
        "strict_first_last"
        if last_frame_input else
        "strict_first"
        if first_frame_input else
        "video_extend"
        if task_type == "extend" else
        "video_edit"
        if task_type == "edit" else
        "multimodal_reference"
        if (
            reference_images
            or args.get("reference_video_urls")
            or args.get("reference_video_url")
            or args.get("reference_audio_urls")
        ) else
        "independent"
    )
    ref_urls, ref_sources = _resolve_reference_images(handler, args)
    ref_video_inputs = _coerce_reference_values(
        args.get("reference_video_urls"))
    legacy_ref_video = str(args.get("reference_video_url") or "").strip()
    if legacy_ref_video and legacy_ref_video not in ref_video_inputs:
        ref_video_inputs.insert(0, legacy_ref_video)
    ref_videos, ref_video_sources = _resolve_reference_media(
        handler, ref_video_inputs, "video", profile=profile,
        task_type=task_type)
    ref_audio_inputs = _coerce_reference_values(
        args.get("reference_audio_urls"))
    ref_audios, ref_audio_sources = _resolve_reference_media(
        handler, ref_audio_inputs, "audio", profile=profile,
        task_type=task_type)
    if ref_audios and not ref_urls and not ref_videos and not profile.audio_only:
        raise ValueError(
            f"{profile.label} 不支持仅输入参考音频；"
            "至少同时提供 1 张参考图或 1 段参考视频")
    submit_started = time.time()
    submission_lock = (
        ws.project_operation_lock(pid, "seedance_submit")
        if pid else contextlib.nullcontext()
    )
    with submission_lock:
        _guard_seedance_submission(pid, unit_id)
        if pid:
            ws.bump_budget(pid, "seedance", 1)
        try:
            r = sdk.submit_video_task(
                prompt,
                reference_images=ref_urls or None,
                reference_video_urls=ref_videos or None,
                reference_audio_urls=ref_audios or None,
                first_frame=first_frame,
                last_frame=last_frame,
                duration=duration,
                generate_audio=generate_audio,
                resolution=resolution,
                ratio=ratio,
                model=profile.model_id,
                omni_reference_task_type=task_type or None,
                output_format=output_format,
            )
        except sdk.VideoSubmissionError as exc:
            ws.log_seedance_call(pid, {
                "tool": "gen_video_t2v", "name": name, "unit_id": unit_id,
                "model": profile.model_id,
                "output_format": output_format,
                "continuity_strategy": continuity_strategy,
                "status": "submission_failed_or_unknown",
                "counts_as_attempt": True, "error": str(exc),
            })
            raise
        if r.get("task_id"):
            _VIDEO_TASK_FORMATS[r["task_id"]] = output_format
        ws.log_seedance_call(pid, {
            "tool": "gen_video_t2v", "name": name, "task_id": r["task_id"],
            "unit_id": unit_id, "status": "submitted",
            "counts_as_attempt": True,
            "model": r["model"], "model_version": profile.key,
            "output_format": output_format,
            "continuity_strategy": continuity_strategy,
            "prompt": prompt[:200],
            "omni_reference_task_type": task_type or None,
            "first_frame": first_frame_input or None,
            "last_frame": last_frame_input or None,
            "reference_video_urls": ref_videos,
            "reference_video_inputs": ref_video_inputs,
            "reference_audio_urls": ref_audios,
            "reference_audio_inputs": ref_audio_inputs,
            "reference_images_count": len(ref_urls),
            "reference_video_count": len(ref_videos),
            "reference_audio_count": len(ref_audios),
            "reference_sources": {
                "images": ref_sources,
                "videos": ref_video_sources,
                "audios": ref_audio_sources,
            },
        })
    ws.log_model_call(pid, r["model"], {
        "via_tool": "gen_video_t2v",
        "name": name,
        "task_id": r["task_id"],
        "model": r["model"],
        "model_version": profile.key,
        "prompt": prompt,                            # 完整 prompt 不截断（已含 lint 后版本）
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
        "output_format": output_format,
        "continuity_strategy": continuity_strategy,
        "omni_reference_task_type": task_type or None,
        "first_frame": first_frame_input or None,
        "last_frame": last_frame_input or None,
        "generate_audio": generate_audio,
        "reference_video_urls": ref_videos,
        "reference_video_inputs": ref_video_inputs,
        "reference_audio_urls": ref_audios,
        "reference_audio_inputs": ref_audio_inputs,
        "reference_images_count": len(ref_urls),
        "reference_video_count": len(ref_videos),
        "reference_audio_count": len(ref_audios),
        "reference_sources": {
            "images": ref_sources,
            "videos": ref_video_sources,
            "audios": ref_audio_sources,
        },
    }, raw_request=r.get("body"), raw_response=r.get("raw"),
        duration_s=time.time() - submit_started)
    return {"task_id": r["task_id"], "model": r["model"],
            "model_version": profile.key, "name": name,
            "output_format": output_format,
            "continuity_strategy": continuity_strategy,
            "first_frame": first_frame_input or None,
            "last_frame": last_frame_input or None,
            "reference_count": len(ref_urls),
            "reference_video_url": (
                ref_videos[0] if len(ref_videos) == 1 else None),
            "reference_video_urls": ref_videos,
            "reference_audio_urls": ref_audios,
            "reference_sources": {
                "images": ref_sources,
                "videos": ref_video_sources,
                "audios": ref_audio_sources,
            },
            "hint": "异步任务已提交，请用 query_video_task 轮询 status。预计 200-300s 完成。"}


# 进度条跨调用记忆：{task_id: start_ts}
# 同一个 task_id 多次 query 时复用首次开始时间，进度条连续从上一次的百分比继续，
# 而不是每次新调用都从 0% 起。
_VIDEO_TASK_STARTS = {}
_VIDEO_TASK_FORMATS = {}


@film_tool(
    name="query_video_task",
    desc="查询 Seedance 任务。默认等到 succeeded/failed；建议传 duration 估 ETA。成功后返回 {path?, video_url}，并写 <save_name>.url.txt，方便后续链式段用本地 path 反查任务 url。",
    params={
        "task_id": {"type": str, "description": "gen_video_t2v 返回的 task_id"},
        "save_name": {"type": str, "description": "[可选] succeeded 时落盘的文件名（不含扩展名）"},
        "output_format": {"type": str, "enum": ["mp4", "mov"], "description": "[可选] 落盘扩展名；默认沿用提交任务时的格式"},
        "duration": {"type": float, "description": "[强烈建议] 本视频时长（秒），用于估 ETA + 节流 query 调用"},
        "wait": {"type": bool, "description": "是否阻塞等待", "default": True},
        "max_wait": {"type": int, "description": "最长等待秒数；不传时根据 duration 自动算（ETA + 60s 缓冲）"},
    },
    required=["task_id"],
)
def _query_video_task(handler, args):
    """查询 Seedance 任务状态。
    默认行为：阻塞等待到 succeeded/failed。
    若传 wait=False，则只查一次立刻返回（用于多任务并行扫描）。

    ETA + 动态轮询：
      - 给 duration（秒，本视频的目标时长）→ 估算 ETA = 60 + 15 * duration
      - 前 60% 时间每 20s 查一次（节省 query 调用）
      - 后 40% 每 8s 查一次（临近完成密集探）
      - 超过 ETA 后每 5s 查一次（兜底）
      - sleep 都分 1s 一片，KeyboardInterrupt 1s 内响应

    打印一行进度条，方便用户/agent 估剩余时间。
    """
    task_id = args["task_id"]
    save_name = args.get("save_name")
    output_format = str(
        args.get("output_format")
        or _VIDEO_TASK_FORMATS.get(task_id)
        or _seedance_task_output_format(_active_pid(handler), task_id)
        or "mp4"
    ).lower()
    if output_format not in {"mp4", "mov"}:
        raise ValueError("query_video_task output_format 只支持 mp4/mov")
    wait = args.get("wait", True)

    # 估算 ETA。duration 没传就走 240s 默认（10s 视频典型值）
    duration = args.get("duration")
    if duration:
        eta = int(60 + 15 * float(duration))
    else:
        eta = 240
    max_wait = int(args.get("max_wait", max(eta + 60, 300)))  # 给 60s 缓冲

    start = _VIDEO_TASK_STARTS.setdefault(task_id, time.time())
    deadline = time.time() + max_wait
    last = None
    bar_printed = False
    aborted = False

    def _is_aborted():
        # handler.code_stop_signal 是 agentmain.abort() 注入的中断标志
        sig = getattr(handler, "code_stop_signal", None)
        return bool(sig)

    while True:
        if _is_aborted():
            aborted = True
            break
        r = sdk.query_video_task(task_id)
        last = r
        st = r["status"]
        if st in ("succeeded", "failed", "cancelled", "expired") or not wait:
            break
        if time.time() >= deadline:
            break

        # 进度条（原地刷新，单行）
        elapsed = int(time.time() - start)
        ratio = min(elapsed / eta, 0.99) if eta > 0 else 0.5
        bar_len = 20
        filled = int(bar_len * ratio)
        bar = "█" * filled + "░" * (bar_len - filled)
        line = f"   ⏳ {task_id[-8:]} {st:<10} [{bar}] {int(ratio*100):3d}% ({elapsed}s / ~{eta}s)"
        # \r 回到行首，末尾补空格清掉残留字符
        print(f"\r{line}   ", end="", flush=True)
        bar_printed = True

        # 动态 poll_interval
        if elapsed < eta * 0.6:
            poll_interval = 20
        elif elapsed < eta:
            poll_interval = 8
        else:
            poll_interval = 5

        # 1s 一片睡，让中断信号能尽快传达
        slept = 0
        while slept < poll_interval and time.time() < deadline:
            if _is_aborted():
                aborted = True
                break
            time.sleep(1)
            slept += 1
        if aborted:
            break

    # 单行进度条结束后补换行，避免后续输出粘连
    if bar_printed:
        print(flush=True)

    # 用户中断：当前客户端放弃轮询，云端任务继续运行（Seedance 不支持真正的取消）
    if aborted:
        return {"status": "aborted", "task_id": task_id,
                "hint": "用户中断了轮询，当前客户端已停止等待。云端任务可能仍在跑，"
                        "稍后可用同 task_id 再次 query_video_task 取回结果。"}

    out = {"status": last["status"], "task_id": task_id}
    if last["status"] == "succeeded":
        _VIDEO_TASK_STARTS.pop(task_id, None)  # 任务终结，清掉进度条记忆
        video_url = last["video_url"]
        out["video_url"] = video_url
        if save_name:
            save = _project_path(
                handler, "shots", f"{save_name}.{output_format}")
            try:
                sdk._download(video_url, save)
                out["path"] = str(save)
            except Exception as exc:
                out["download_error"] = str(exc)
                out["hint"] = "云端生成已成功，下载未完成；复用原 task_id 查询并下载，禁止重提生成。"
            # 同步把 url 写到 sidecar，方便后续链式段引用 / 跨会话回查
            try:
                sidecar = save.with_suffix(".url.txt")
                sidecar.write_text(video_url, encoding="utf-8")
            except Exception:
                pass
    elif last["status"] in {"failed", "cancelled", "expired"}:
        _VIDEO_TASK_STARTS.pop(task_id, None)  # 任务终结，清掉进度条记忆
        out["error"] = last["raw"].get("error") or last["raw"]
    else:
        out["hint"] = "任务尚未到终态；保存原 task_id，按轮询间隔再次非阻塞查询，禁止重新提交。"
    if last["status"] in {"succeeded", "failed", "cancelled", "expired"}:
        _VIDEO_TASK_FORMATS.pop(task_id, None)
    return out


@film_tool(
    name="cancel_video_task",
    desc="尝试取消 Seedance 任务。仅 queued/pending 通常可取消；running 平台不支持打断。用户中断后可调用一次，能取消就取消，不能取消只能稍后查询结果。",
    params={"task_id": {"type": str, "description": "Seedance 任务 id"}},
    required=["task_id"],
)
def _cancel_video_task(handler, args):
    """主动取消 Seedance 云端任务（防止本地中断后云端继续烧预算）。"""
    task_id = args["task_id"]
    r = sdk.cancel_video_task(task_id)
    return {"task_id": task_id, **r}


# ============== 视频处理 ==============
@film_tool(
    name="video_probe",
    desc="探测本地视频的时长、画幅、帧率、编码、音轨和文件大小。剪切前先探源片，剪切/拼接后必须再探输出，不能仅凭工具无报错判定成功。",
    params={
        "clip": {"type": str, "description": "要探测的视频路径"},
        "accurate_duration": {
            "type": bool,
            "description": "是否完整解码视频流取得精确画面时长；长视频默认false，终验关键成片时可设true",
            "default": False,
        },
    },
    required=["clip"],
)
def _video_probe(handler, args):
    return sdk.probe_media(
        args["clip"],
        accurate_duration=bool(args.get("accurate_duration", False)),
    )


@film_tool(
    name="video_concat",
    desc="按显式转场表合成终版片段；默认继承首段帧率，不再强制降到24fps。省略 transitions 时仅在 clips 与 director_plan.composition.clips 完全一致时读取计划，否则报错。每个切点需记录类型与理由；非硬切会扣除重叠时长。完成后调用 video_probe 复核。",
    params={
        "clips": {"type": "array", "items": {"type": "string"}, "description": "按顺序的视频文件路径列表"},
        "name": {"type": str, "description": "输出文件名"},
        "transitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "cut/source_cut/match_cut=画面硬切；或 fade/dissolve/fadeblack/fadewhite/wipeleft/wiperight/slideup/slidedown/circleopen/circleclose/radial 等 xfade 类型",
                    },
                    "duration": {
                        "type": "number",
                        "description": "非 cut 转场的重叠时长(秒)，通常 0.3-1.0",
                    },
                    "at": {
                        "type": "string",
                        "description": "切点标识，例如 S1->S2",
                    },
                    "reason": {
                        "type": "string",
                        "description": "选择该转场的剪辑理由",
                    },
                    "audio_handling": {
                        "type": "string",
                        "description": "原声保留、声音桥、短淡化或统一音轨等处理",
                    },
                    "source_time_s": {
                        "type": "number",
                        "description": "复刻项目中该切点对应的原片绝对时间",
                    },
                    "cut_class": {
                        "type": "string",
                        "enum": ["same_scene_continuous", "same_scene_elliptical", "scene_change"],
                        "description": "切点分类：同场景连续 / 同场景跳时 / 跨场景",
                    },
                    "left_end_state": {
                        "type": "object",
                        "description": "左段交接状态：人物位置/尺度/动作相位/道具/UI/机位",
                    },
                    "right_start_state": {
                        "type": "object",
                        "description": "右段承接状态，应与切点分类允许的变化一致",
                    },
                    "continuity_anchors": {
                        "type": "object",
                        "description": "跨切点锁定的主体位置、运动向量、构图线、光色和声音线索",
                    },
                    "generation_overlap_s": {
                        "type": "number",
                        "description": "仅用于生成层首尾咬合的参考视频把手时长；合成层仍可硬切",
                    },
                    "visual_bridge": {
                        "type": "object",
                        "description": "跨场景生成式视觉桥，记录进入、峰值、清除和前后片段职责",
                    },
                    "effect_source": {
                        "type": "string",
                        "description": "特效的画面触发来源，例如风雪、浪花、门体或能量命中",
                    },
                    "effect_peak": {
                        "type": "string",
                        "description": "切点处特效峰值或全遮挡状态",
                    },
                    "effect_clear": {
                        "type": "string",
                        "description": "新场景开头特效清除方式和最长耗时",
                    },
                    "seam_review": {
                        "type": "object",
                        "description": "extract_seam_frames 抽帧路径、审查状态和采用版本",
                    },
                    "fallback": {
                        "type": "object",
                        "description": "接缝验证失败时才允许启用的备用转场",
                    },
                },
                "required": ["type"],
            },
            "description": "逐切点转场表，数量=clips数减1。硬切也显式写 type=cut。可省略并读取与 clips 一致的 director_plan.composition。",
        },
        "crossfade": {"type": float, "description": "仅硬切点的音频淡出/淡入时长(秒)，默认 0.3s；画面叠化点的音频会按该点 duration 做 acrossfade", "default": 0.3},
        "fade_in": {"type": float, "description": "[可选] 整片开头画面和音频淡入时长(秒)", "default": 0.0},
        "fade_out": {"type": float, "description": "[可选] 整片结尾画面和音频淡出时长(秒)", "default": 0.0},
        "fps": {"type": float, "description": "[可选] 目标帧率；省略时继承首段帧率，只有交付规格明确要求时才覆盖"},
        "preset": {"type": str, "description": "[可选] x264 编码档位（ultrafast..veryslow）", "default": "ultrafast"},
    },
    required=["clips", "name"],
)
def _video_concat(handler, args):
    clips = args["clips"]
    name = args.get("name", f"concat_{int(time.time())}")
    save = _project_path(handler, "composed", f"{name}.mp4")
    transitions = args.get("transitions")
    if transitions is None and len(clips) > 1:
        pid = _active_pid(handler)
        plan_path = ws.project_dir(pid) / "director_plan.json" if pid else None
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path and plan_path.is_file() else {}
        if plan_path and plan_path.is_file():
            artifact_contracts.validate(plan_path, plan)
        composition = plan.get("composition") or {}
        if composition.get("clips") == clips:
            transitions = composition.get("transitions")
        if transitions is None:
            raise ValueError("请显式提供 transitions（硬切填 cut），或登记匹配 clips 的 composition 计划")
    return sdk.video_concat(
        clips, save,
        crossfade=float(args.get("crossfade", 0.3)),
        fade_in=float(args.get("fade_in", 0.0)),
        fade_out=float(args.get("fade_out", 0.0)),
        preset=args.get("preset", "ultrafast"),
        transitions=transitions,
        fps=(float(args["fps"]) if args.get("fps") is not None else None),
    )


@film_tool(
    name="video_trim",
    desc="裁剪视频 [start, end] 秒区间",
    params={
        "clip": str,
        "start": float,
        "end": float,
        "name": str,
        "preset": {"type": str, "description": "[可选] x264 编码档位", "default": "ultrafast"},
    },
    required=["clip", "start", "end", "name"],
)
def _video_trim(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'trim')}.mp4")
    return sdk.video_trim(args["clip"], save, float(args["start"]), float(args["end"]),
                          preset=args.get("preset", "ultrafast"))


@film_tool(
    name="video_fade",
    desc="给单个视频添加片头淡入和片尾淡出，画面与音频同步处理并写 .timeline.json。只在导演计划明确需要黑场开合时使用；段间转场仍用 video_concat。",
    params={
        "clip": {"type": str, "description": "输入视频路径"},
        "name": {"type": str, "description": "输出文件名（不含扩展名）"},
        "fade_in": {"type": float, "description": "片头淡入秒数", "default": 0.5},
        "fade_out": {"type": float, "description": "片尾淡出秒数", "default": 0.5},
        "total_duration": {"type": float, "description": "[可选] 已知总时长；省略时自动探测"},
        "preset": {"type": str, "description": "[可选] x264 编码档位", "default": "ultrafast"},
    },
    required=["clip", "name"],
)
def _video_fade(handler, args):
    fade_in = max(0.0, float(args.get("fade_in", 0.5)))
    fade_out = max(0.0, float(args.get("fade_out", 0.5)))
    total_duration = args.get("total_duration")
    if total_duration is not None:
        total_duration = float(total_duration)
    save = _project_path(handler, "composed", f"{args['name']}.mp4")
    return sdk.video_fade(
        args["clip"],
        save,
        fade_in=fade_in,
        fade_out=fade_out,
        total_duration=total_duration,
        preset=args.get("preset", "ultrafast"),
    )


@film_tool(
    name="video_speed",
    desc="视频变速（音视频同步）。factor>1 加速、<1 慢动作。⚠️ 严禁用 0.5 慢动作来凑分镜规定的总时长，那是作弊；正常用法是节奏调整 / 高速 / 慢动作镜头语言",
    params={
        "clip": str,
        "factor": {"type": float, "description": "1.0=原速，2.0=2x快，0.5=慢动作"},
        "name": str,
        "preset": {"type": str, "description": "[可选] x264 编码档位", "default": "ultrafast"},
    },
    required=["clip", "factor", "name"],
)
def _video_speed(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'speed')}.mp4")
    return sdk.video_speed(args["clip"], save, float(args["factor"]),
                           preset=args.get("preset", "ultrafast"))


@film_tool(
    name="video_overlay",
    desc="画中画。pip 缩到 pip_scale 后叠在 base 的指定角",
    params={
        "base": str,
        "pip": str,
        "name": str,
        "pip_scale": {"type": float, "default": 0.33},
        "position": {"type": str, "enum": ["tl", "tr", "bl", "br"], "default": "br"},
        "margin": {"type": int, "default": 20},
        "preset": {"type": str, "description": "[可选] x264 编码档位", "default": "ultrafast"},
    },
    required=["base", "pip", "name"],
)
def _video_overlay(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'pip')}.mp4")
    return sdk.video_overlay(args["base"], args["pip"], save,
                             pip_scale=float(args.get("pip_scale", 0.33)),
                             position=args.get("position", "br"),
                             margin=int(args.get("margin", 20)),
                             preset=args.get("preset", "ultrafast"))


@film_tool(
    name="video_portrait",
    desc="横转竖（如 1280x720 → 720x1280），居中裁剪不变形",
    params={
        "clip": str,
        "name": str,
        "width": {"type": int, "default": 720},
        "height": {"type": int, "default": 1280},
        "preset": {"type": str, "description": "[可选] x264 编码档位", "default": "ultrafast"},
    },
    required=["clip", "name"],
)
def _video_portrait(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'portrait')}.mp4")
    return sdk.video_portrait(args["clip"], save,
                              width=int(args.get("width", 720)),
                              height=int(args.get("height", 1280)),
                              preset=args.get("preset", "ultrafast"))


# ============== 音频 ==============
@film_tool(
    name="audio_process",
    desc=(
        "统一音频/音轨处理入口。用 action 选择原子动作："
        "add_silence=给视频补静音轨；strip=移除视频音轨；extract=抽取视频音轨；"
        "normalize=响度标准化；fade=音频淡入淡出；fit_duration=音频循环/补静音适配时长；"
        "set_audio=用指定音频替换视频音轨；amix=把 BGM 混入视频原音轨。"
        "这是纯执行工具，不负责选择配乐/转场/剪辑策略。"
    ),
    params={
        "action": {
            "type": str,
            "enum": ["add_silence", "strip", "extract", "normalize", "fade", "fit_duration", "set_audio", "amix"],
            "description": "要执行的音频处理动作",
        },
        "name": {"type": str, "description": "输出文件名（不含扩展名；视频输出默认 mp4，音频输出按 ext）"},
        "video": {"type": str, "description": "视频路径。add_silence/strip/extract/set_audio 使用"},
        "input_media": {"type": str, "description": "音频或视频路径。normalize 使用"},
        "audio": {"type": str, "description": "音频路径。fade/fit_duration/set_audio 使用"},
        "base_video": {"type": str, "description": "基础视频路径。amix 使用"},
        "bgm_audio": {"type": str, "description": "BGM 音频路径。amix 使用"},
        "ext": {"type": str, "enum": ["mp4", "mp3", "wav", "m4a"], "default": "mp4"},
        "duration": {"type": float, "description": "fit_duration 的目标秒数；或 set_audio/amix 的时长策略见 duration_mode"},
        "duration_mode": {"type": str, "enum": ["first", "shortest", "longest"], "default": "first"},
        "fit_mode": {"type": str, "enum": ["loop", "pad"], "default": "loop"},
        "fade_in": {"type": float, "default": 0.0},
        "fade_out": {"type": float, "default": 0.0},
        "total_duration": {"type": float, "description": "fade 的总时长；不传会探测"},
        "audio_volume": {"type": float, "description": "set_audio 的替换音轨音量倍率", "default": 1.0},
        "bgm_volume": {"type": float, "description": "amix 的 BGM 音量倍率", "default": 0.2},
        "base_volume": {"type": float, "description": "amix 的原视频音量倍率", "default": 1.0},
        "sample_rate": {"type": int, "default": 44100},
        "target_i": {"type": float, "description": "normalize 目标综合响度 LUFS", "default": -16.0},
        "target_tp": {"type": float, "description": "normalize 目标真峰值 dBTP", "default": -1.5},
        "target_lra": {"type": float, "description": "normalize 目标响度范围", "default": 11.0},
    },
    required=["action", "name"],
)
def _audio_process(handler, args):
    action = args["action"]
    name = args.get("name", action)

    if action == "add_silence":
        save = _project_path(handler, "composed", f"{name}.mp4")
        return sdk.video_add_silence(args["video"], save,
                                     sample_rate=int(args.get("sample_rate", 44100)))

    if action == "strip":
        save = _project_path(handler, "composed", f"{name}.mp4")
        return sdk.audio_strip(args["video"], save)

    if action == "extract":
        ext = args.get("ext", "mp3")
        save = _project_path(handler, "audios", f"{name}.{ext}")
        return sdk.audio_extract(args["video"], save)

    if action == "normalize":
        ext = args.get("ext", "mp4")
        subdir = "composed" if ext == "mp4" else "audios"
        save = _project_path(handler, subdir, f"{name}.{ext}")
        return sdk.audio_normalize(args["input_media"], save,
                                   target_i=float(args.get("target_i", -16.0)),
                                   target_tp=float(args.get("target_tp", -1.5)),
                                   target_lra=float(args.get("target_lra", 11.0)))

    if action == "fade":
        ext = args.get("ext", "mp3")
        save = _project_path(handler, "audios", f"{name}.{ext}")
        return sdk.audio_fade(args["audio"], save,
                              fade_in=float(args.get("fade_in", 0.5)),
                              fade_out=float(args.get("fade_out", 0.5)),
                              total_duration=args.get("total_duration"))

    if action == "fit_duration":
        ext = args.get("ext", "mp3")
        save = _project_path(handler, "audios", f"{name}.{ext}")
        return sdk.audio_fit_duration(args["audio"], save,
                                      duration=float(args["duration"]),
                                      mode=args.get("fit_mode", "loop"),
                                      fade_in=float(args.get("fade_in", 0.0)),
                                      fade_out=float(args.get("fade_out", 0.0)))

    if action == "set_audio":
        save = _project_path(handler, "composed", f"{name}.mp4")
        return sdk.video_set_audio(args["video"], args["audio"], save,
                                   audio_volume=float(args.get("audio_volume", 1.0)),
                                   duration=args.get("duration_mode", "first"))

    if action == "amix":
        save = _project_path(handler, "composed", f"{name}.mp4")
        return sdk.audio_amix(args["base_video"], args["bgm_audio"], save,
                              bgm_volume=float(args.get("bgm_volume", 0.2)),
                              base_volume=float(args.get("base_volume", 1.0)),
                              duration=args.get("duration_mode", "first"))

    raise ValueError(f"未知 audio_process action: {action}")


@film_tool(
    name="gen_audio_bgm",
    desc="豆包·音乐 GenBGM 生成纯音乐（异步，返回 task_id，需 query_audio_task 轮询）。当前需在 vibefilming.config.json 配置 volc.ak / volc.sk，无 key 会报错。",
    params={
        "prompt": {"type": str, "description": "音乐风格描述（自然语言写明风格/情绪/乐器/场景，建议 ≥50 字）"},
        "name": str,
        "duration": {"type": int, "description": "时长（秒），30-120", "minimum": 30, "maximum": 120, "default": 30},
        "segments": {"type": "array", "description": "[可选] 段落结构，传了优先级高于 duration。每段 {Name, Duration}，Name ∈ intro/verse/chorus/inst/bridge/outro，Duration 总和需 [30,120]", "items": {"type": "object", "properties": {"Name": {"type": "string", "enum": ["intro", "verse", "chorus", "inst", "bridge", "outro"]}, "Duration": {"type": "integer"}}, "required": ["Name", "Duration"]}},
        "enable_input_rewrite": {"type": bool, "description": "[可选] 让模型自动改写/丰富 prompt（版权校验失败时可尝试开启）", "default": False},
    },
    required=["prompt", "name"],
)
def _gen_audio_bgm(handler, args):
    """提交 BigMusic GenBGM 任务（异步）。跟 gen_video_t2v 完全对称：只提交，立即回 task_id。

    必填：prompt（自然语言描述风格/情绪/乐器/场景/段落结构，建议 ≥50 字）
    选填：name（落盘文件名，默认 bgm_<ts>）/ duration（30-120 秒，默认 30）/
          segments（[{"Name":"intro","Duration":10},...]，传了优先级高于 duration）/
          enable_input_rewrite（让模型自动改写 prompt，默认 false）

    返回：{task_id, name, duration}
    后续：用 query_audio_task(task_id, save_name) 轮询并落盘。
    """
    prompt = args["prompt"]
    name = args.get("name", f"bgm_{int(time.time())}")
    duration = int(args.get("duration", 30))
    segments = args.get("segments")
    enable_input_rewrite = bool(args.get("enable_input_rewrite", False))

    pid = _active_pid(handler)
    bgm_started = time.time()
    r = sdk.submit_bgm_task(
        prompt, duration=duration, segments=segments,
        enable_input_rewrite=enable_input_rewrite,
    )
    ws.log_model_call(pid, "volc-genbgm-v5.0", {
        "via_tool": "gen_audio_bgm",
        "name": name,
        "task_id": r["task_id"],
        "prompt": prompt,                          # 完整 prompt 不截断
        "duration": duration,
        "segments": segments,
        "enable_input_rewrite": enable_input_rewrite,
    }, raw_request=r.get("body"), raw_response=r.get("raw"),
        duration_s=time.time() - bgm_started)
    return {"task_id": r["task_id"], "name": name, "duration": duration}


# 跟 _VIDEO_TASK_STARTS 对称：BGM 进度条跨调用记忆
_BGM_TASK_STARTS = {}


@film_tool(
    name="query_audio_task",
    desc="查询 BGM（gen_audio_bgm）任务状态，跟 query_video_task 对称。默认阻塞轮询到 succeeded/failed。succeeded 时返回 {path?, audio_url}（path: save_name 给了的话落到 audios/<save_name>.mp3）。⚠️ 查 BGM 任务必须用本工具，不要用 query_video_task（那是查 Seedance 视频的，task_id 不通用）。",
    params={
        "task_id": {"type": str, "description": "gen_audio_bgm 返回的 task_id"},
        "save_name": {"type": str, "description": "[可选] succeeded 时落盘的文件名（不含扩展名），落到 audios/<save_name>.mp3"},
        "wait": {"type": bool, "description": "是否阻塞等待", "default": True},
        "max_wait": {"type": int, "description": "最长等待秒数，默认 180（BGM 比视频快）"},
    },
    required=["task_id"],
)
def _query_audio_task(handler, args):
    """查询 BGM 任务状态。跟 query_video_task 对称：默认阻塞轮询到 succeeded/failed。

    必填：task_id
    选填：save_name（拿到 audio_url 时落盘到 audios/<save_name>.mp3）/
          wait（默认 True；False=只查一次立刻回）/
          max_wait（默认 180s，BGM 比 seedance 快很多）

    返回：{status, task_id, audio_url?, path?, duration?, style_info?, error?}
    """
    task_id = args["task_id"]
    save_name = args.get("save_name")
    wait = args.get("wait", True)

    eta = 60  # BGM 一般 30-90s，给个 60s 估算
    max_wait = int(args.get("max_wait", 180))
    start = _BGM_TASK_STARTS.setdefault(task_id, time.time())
    deadline = time.time() + max_wait

    pid = _active_pid(handler)
    save_path: Optional[Path] = None
    if save_name and pid:
        save_path = _project_path(handler, "audios", f"{save_name}.mp3")

    bar_printed = False
    aborted = False

    def _is_aborted():
        return bool(getattr(handler, "code_stop_signal", None))

    while True:
        if _is_aborted():
            aborted = True
            break
        r = sdk.query_bgm_task(task_id, save_path=save_path)
        st = r["status"]
        if st in ("succeeded", "failed") or not wait:
            last = r
            break
        if time.time() >= deadline:
            last = r
            break

        elapsed = int(time.time() - start)
        ratio = min(elapsed / eta, 0.99) if eta > 0 else 0.5
        bar_len = 20
        filled = int(bar_len * ratio)
        bar = "█" * filled + "░" * (bar_len - filled)
        line = f"   ⏳ bgm {task_id[-8:]} {st:<10} [{bar}] {int(ratio*100):3d}% ({elapsed}s / ~{eta}s)"
        print(f"\r{line}   ", end="", flush=True)
        bar_printed = True

        # 轻量轮询，BGM 出得快
        slept = 0
        while slept < 5 and time.time() < deadline:
            if _is_aborted():
                aborted = True
                break
            time.sleep(1)
            slept += 1
        if aborted:
            break

    if bar_printed:
        print(flush=True)

    if aborted:
        return {"status": "aborted", "task_id": task_id,
                "hint": "用户中断了轮询，当前客户端已停止等待。云端任务可能仍在跑，"
                        "稍后可用同 task_id 再次 query_audio_task 取回结果。"}

    out = {"status": last["status"], "task_id": task_id}
    if last["status"] == "succeeded":
        _BGM_TASK_STARTS.pop(task_id, None)
        out["audio_url"] = last.get("audio_url")
        out["duration"] = last.get("duration")
        out["style_info"] = last.get("style_info")
        if last.get("path"):
            out["path"] = last["path"]
    elif last["status"] == "failed":
        _BGM_TASK_STARTS.pop(task_id, None)
        out["error"] = last.get("error")
        if last.get("hint"):
            out["hint"] = last["hint"]
    else:
        out["hint"] = f"BGM 任务仍在运行中（已等待 {max_wait}s 仍未完成）。再次调用本工具继续等待。"
    return out


# ============== 评估归档 ==============
@film_tool(
    name="extract_frames",
    desc="ffmpeg 固定 1fps 抽帧（不调 VLM，纯 debug 或缩略图；审片不要用它替代 vlm_understand 的原生视频理解）",
    params={
        "clip": str,
        "name": str,
    },
    required=["clip", "name"],
)
def _extract_frames(handler, args):
    name = args.get("name", f"frames_{int(time.time())}")
    save_dir = _project_path(handler, "reviews", name)
    return sdk.extract_frames(args["clip"], save_dir, fps=1.0)


@film_tool(
    name="extract_boundary_frame",
    desc="从已完成片段提取真实首帧或末帧 PNG。串行同场景片段可把上一段末帧作为下一次 gen_video_t2v 的 strict first_frame；它不能和 reference_* 同时传。真正同一长镜头续写仍应使用上一段完整视频 + extend。",
    params={
        "clip": {"type": str, "description": "已完成片段路径"},
        "name": {
            "type": str,
            "description": "稳定锚点名，例如 U03_to_U04",
        },
        "edge": {
            "type": str,
            "enum": ["first", "last"],
            "description": "提取首边界或末边界",
            "default": "last",
        },
        "offset_s": {
            "type": float,
            "description": "从边界向片内回退秒数；默认0取真实边界帧",
            "default": 0.0,
        },
    },
    required=["clip", "name"],
)
def _extract_boundary_frame(handler, args):
    edge = str(args.get("edge") or "last")
    clip = _resolve_project_media_reference(handler, args["clip"])
    save = _project_path(
        handler,
        "shots",
        "continuity",
        f"{args['name']}_{edge}.png",
    )
    return sdk.extract_boundary_frame(
        clip,
        save,
        edge=edge,
        offset_s=float(args.get("offset_s", 0.0)),
    )


@film_tool(
    name="extract_seam_frames",
    desc="抽取相邻终版片段的左末三帧和右初三帧，用于同场景跳变与跨场景特效桥审查；返回的六张图应一次传给 vlm_understand(images=...)，不能只看完整成片凭总体观感放行。",
    params={
        "left_clip": {"type": str, "description": "切点左侧终版片段路径"},
        "right_clip": {"type": str, "description": "切点右侧终版片段路径"},
        "name": {"type": str, "description": "reviews/ 下稳定的接缝目录名，例如 seam_U03__U04"},
        "window": {
            "type": float,
            "description": "两侧抽帧观察窗秒数，默认 0.3；通常 0.2-0.5",
            "default": 0.3,
        },
    },
    required=["left_clip", "right_clip", "name"],
)
def _extract_seam_frames(handler, args):
    save_dir = _project_path(handler, "reviews", args["name"])
    return sdk.extract_seam_frames(
        args["left_clip"],
        args["right_clip"],
        save_dir,
        window=float(args.get("window", 0.3)),
    )


@film_tool(
    name="vlm_understand",
    desc="Seed 2.1 开放式视觉理解：单视频传 video，一张或多张图片传 images，二者不能同时传；输出文本理解结果并落到 reviews/<name>.json。当前封装不支持一次输入多个视频。",
    params={
        "video": {"type": str, "description": "[审视频时传] 单个视频路径。与 images 二选一"},
        "images": {"type": "array", "items": {"type": "string"}, "description": "[审图片时传] 一张或多张图片路径（对比/多帧审查时传多张）。与 video 二选一", "default": None},
        "question": {"type": str, "description": "你想让 VLM 回答的问题，越具体越好；审片时把要核对的点逐条写清"},
        "system": {"type": str, "description": "[可选] 系统提示，设定 VLM 角色/输出格式（电商终审建议只检查产品、关键文字、使用动作、卖点证明、节奏和 CTA）"},
        "max_tokens": {"type": int, "default": 16384},
        "temperature": {"type": float, "default": 0.1},
        "name": {"type": str, "description": "归档文件名（不含扩展名），由 agent 按审查对象自定，要求稳定、可读、可回查；建议包含 review、对象类型/对象名、阶段或版本。不要用固定模板强塞单视角名，除非被审对象本身确实是单独视角图", "default": "understand"},
    },
    required=["question"],
)
def _vlm_understand(handler, args):
    """Seed 2.1 Pro 开放式视觉理解。
    当前封装支持单视频或多图片输入，输出文本回答。

    用途广泛：
      - 让 VLM 描述/讲解视频或图片内容
      - 让 VLM 找问题、给改进建议（替代以前的 vlm_review 打分）
      - 让 VLM 定位异常时间区间（"第几秒剑消失了"）
      - 多图之间做产品、人物或关键帧一致性比较
    """
    pid = _active_pid(handler)
    if pid:
        ws.bump_budget(pid, "vlm", 1)

    video = args.get("video")
    images = args.get("images")
    question = args["question"]
    name = args.get("name", f"understand_{int(time.time())}")
    system = args.get("system")

    # video / images 二选一：传 video 走视频原生理解，传 images 走多图理解
    if video and images:
        raise ValueError("video 与 images 只能二选一，不要同时传")
    if not video and not images:
        raise ValueError("必须传 video（视频路径）或 images（图片路径列表）其一")
    original_video = video
    original_images = images
    if video:
        video = _resolve_project_media_reference(handler, video)
    elif images:
        imgs = images if isinstance(images, list) else [images]
        images = [
            _resolve_project_media_reference(handler, item)
            for item in imgs
        ]
    vlm_started = time.time()
    if video:
        is_video = True
        resp_data = sdk.doubao_video_understand(
            video, question,
            max_tokens=int(args.get("max_tokens", 16384)),
            temperature=float(args.get("temperature", 0.1)),
            system=system,
        )
    else:
        # images 容错：允许误传单个字符串
        is_video = False
        resp_data = sdk.doubao_vlm(
            images, question,
            max_tokens=int(args.get("max_tokens", 16384)),
            temperature=float(args.get("temperature", 0.1)),
            system=system,
        )

    raw = resp_data["raw"]
    payload = resp_data["body"]
    
    prepared_video = resp_data.get("prepared_video")
    video_uploaded = resp_data.get("video_uploaded") or video
    incomplete = raw.get("_incomplete") if isinstance(raw, dict) else None
    answer = raw["choices"][0]["message"]["content"].strip()

    # 归档到 reviews/<name>.json
    if pid:
        out = _project_path(handler, "reviews", f"{name}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        archive = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "video": original_video, "images": original_images,
            "resolved_video": video if is_video else None,
            "resolved_images": images if not is_video else None,
            "video_uploaded": video_uploaded if is_video else None,
            "prepared_video": prepared_video,
            "incomplete": incomplete,
            "question": question, "answer": answer,
        }
        out.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")

    ws.log_model_call(pid, payload.get("model", sdk.get_model("vlm", sdk.MODEL_VLM)), {
        "via_tool": "vlm_understand",
        "modality": "video" if is_video else "image",
        "name": name,
        "video": video,
        "video_uploaded": video_uploaded if is_video else None,
        "prepared_video": prepared_video,
        "incomplete": incomplete,
        "images": images,
        "question": question,
        "answer": answer,
        "max_tokens": int(args.get("max_tokens", 16384)),
        "temperature": float(args.get("temperature", 0.1)),
    }, raw_request=payload, raw_response=raw,
        duration_s=time.time() - vlm_started)

    result = {"question": question, "answer": answer}
    if prepared_video:
        result["prepared_video"] = prepared_video
    if incomplete:
        result["incomplete"] = incomplete
    return result


# ============== 子模块工具注册 ==============
# 放在文件末尾：子模块要 import film_tool，此处必须等装饰器定义完再导入，避免循环导入。
from . import reference_breakdown  # noqa: E402,F401  导入即触发 @film_tool 注册


def _formal_script_write_block_reason(handler, args) -> str:
    """Hard gate for generic file_write/file_patch targeting formal script.md."""
    pid = _active_pid(handler)
    if not pid:
        return ""
    try:
        path = Path(handler._get_abs_path(args.get("path", ""))).resolve()
        formal_script = (ws.project_dir(pid) / "script.md").resolve()
        manifest = ws.read_manifest(pid)
    except Exception:
        return ""
    execution = ws.execution_state_from_manifest(manifest)
    if path != formal_script or execution["mode"] != "human_gated":
        return ""
    breakdown = (manifest.get("phases") or {}).get("breakdown") or {}
    materials = manifest.get("materials") or {}
    needs_breakdown = (
        bool(materials.get("reference_video"))
        or breakdown.get("source") == "reference_video"
    )
    if not needs_breakdown:
        return ""
    if ws.execution_gate_state(manifest, "breakdown_approval").get("status") == "approved":
        return ""
    return (
        "human_gated：拆镜产物尚未获得后续人工确认，禁止写正式 script.md。"
        "初始任务中的“完成全流程/继续写剧本”不能提前批准尚未看到的拆镜结果。"
        "请停止当前任务，等待用户确认后调用 "
        "approve_execution_gate(gate=breakdown_approval)。"
    )


def _formal_script_snapshot(handler) -> tuple[Optional[str], Optional[bytes]]:
    """Return the formal script path and bytes for post-tool gate invalidation."""
    pid = _active_pid(handler)
    if not pid:
        return None, None
    path = (ws.project_dir(pid) / "script.md").resolve()
    return str(path), path.read_bytes() if path.is_file() else None


def _mark_formal_script_changed(handler, before: Optional[bytes]) -> None:
    """Any real script change invalidates an earlier human script approval."""
    pid = _active_pid(handler)
    if not pid:
        return
    path = (ws.project_dir(pid) / "script.md").resolve()
    after = path.read_bytes() if path.is_file() else None
    if after == before:
        return
    manifest = ws.read_manifest(pid)
    if ws.execution_state_from_manifest(manifest)["mode"] != "human_gated":
        return
    ws.set_execution_gate(
        pid, "script_approval", "pending",
        actor="system", evidence="正式剧本已写入或修改，旧人工确认失效")
    if ((manifest.get("phases") or {}).get("story") or {}).get("status") == "done":
        ws.update_phase(
            pid, "story", status="in_progress",
            note="awaiting_human_approval: script；正式剧本已修改")


def _install_execution_guards(handler):
    """Wrap universal file tools so human gates cannot be bypassed by prompt drift."""
    import types

    for method_name in ("do_file_write", "do_file_patch"):
        original = getattr(handler, method_name, None)
        if original is None:
            continue

        def guarded(self, args, response, _original=original):
            reason = _formal_script_write_block_reason(self, args)
            if reason:
                yield f"[Status] ❌ {reason}\n"
                return StepOutcome({"status": "blocked", "msg": reason}, next_prompt="\n")
            script_path, before = _formal_script_snapshot(self)
            target = None
            try:
                target = str(Path(self._get_abs_path(args.get("path", ""))).resolve())
            except Exception:
                pass
            outcome = yield from _original(args, response)
            if script_path and target == script_path:
                _mark_formal_script_changed(self, before)
            return outcome

        setattr(handler, method_name, types.MethodType(guarded, handler))

    original_code_run = getattr(handler, "do_code_run", None)
    if original_code_run is not None:
        def guarded_code_run(self, args, response, _original=original_code_run):
            pid = _active_pid(self)
            script_path, before = _formal_script_snapshot(self)
            reason = ""
            if pid:
                reason = _formal_script_write_block_reason(
                    self, {"path": script_path or ""})
            outcome = yield from _original(args, response)
            if script_path:
                path = Path(script_path)
                after = path.read_bytes() if path.is_file() else None
                if after != before and reason:
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(before)
                    yield f"[Status] ❌ 已撤销绕过审批闸门的 script.md 修改：{reason}\n"
                    return StepOutcome(
                        {"status": "blocked", "msg": reason}, next_prompt="\n")
                _mark_formal_script_changed(self, before)
            return outcome

        handler.do_code_run = types.MethodType(guarded_code_run, handler)


def inject_film_tools(handler):
    """把所有用 @film_tool 声明的 film 工具挂到 handler 实例上。"""
    import types
    for name, fn in _DECORATED_TOOLS.items():
        method = _wrap(name, fn)
        setattr(handler, f"do_{name}", types.MethodType(method, handler))
    _install_execution_guards(handler)
    return handler
