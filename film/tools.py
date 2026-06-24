"""GA 工具适配层：把 film_sdk + workspace 包装成 do_xxx 方法。

挂载方式：用 inject_film_tools(handler) 给 GenericAgentHandler 实例添加方法。
agent_loop 通过 hasattr(self, f"do_{tool_name}") 找到工具，所以只要把方法绑到
handler 上即可，不用改 ga.py / agent_loop.py。
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

from agent_loop import StepOutcome
from . import workspace as ws
from . import film_sdk as sdk


# ============== 通用包装：把 SDK 函数变成 do_xxx ==============
def _wrap(tool_name: str, fn, log_keys=None):
    """把同步函数包装成 do_xxx generator。fn 接收 args dict，返回 dict。"""
    def do_method(self, args, response):
        # 复制一份去掉内部字段
        public_args = {k: v for k, v in args.items() if not k.startswith("_")}
        yield f"🎬 {tool_name}({_brief(public_args)})\n"
        try:
            result = fn(self, public_args)
            ws.log_tool_call(_active_pid(self), tool_name, public_args,
                             json.dumps(result, ensure_ascii=False, default=str))
            yield f"   ✅ {_brief(result)}\n"
            return StepOutcome(result, next_prompt="\n")
        except Exception as e:
            err = {"status": "error", "tool": tool_name, "msg": str(e)}
            ws.log_tool_call(_active_pid(self), tool_name, public_args, str(e))
            yield f"   ❌ {e}\n"
            return StepOutcome(err, next_prompt="\n")
    do_method.__name__ = f"do_{tool_name}"
    return do_method


# ============== 装饰器：一处声明 = 自动注册 + 自动生成 schema ==============
# 用 @film_tool(...) 标注的工具，无需再手动写 TOOL_REGISTRY 一行，也无需在
# tools_schema_film.json 里手写 function 定义——加新工具只改这一处。
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


def _project_path(handler, *parts) -> Path:
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    return ws.project_dir(pid).joinpath(*parts)


# ============== 项目工作区工具 ==============
@film_tool(
    name="project_create",
    desc="创建影视项目工作区，建 projects/<id>/ 目录并设置预算。返回 project_id（后续工具自动使用活跃项目）",
    params={
        "brief": {"type": str, "description": "用户原始需求一句话"},
        "max_seedance_calls": {"type": int, "description": "Seedance 调用次数统计上限，仅记账参考、不阻断（默认 0 = 不设限，可自由返工/重做）", "default": 0},
    },
    required=["brief"],
)
def _project_create(handler, args):
    brief = args.get("brief", "").strip()
    if not brief:
        raise ValueError("brief 必填，描述本次想做的视频")
    # 默认 0 = 无预算上限（仅记账不阻断），方便 agent 自由迭代审片
    budget = int(args.get("max_seedance_calls", 0))
    m = ws.project_create(brief, max_seedance_calls=budget)
    _set_active_pid(handler, m["project_id"])
    return {
        "project_id": m["project_id"],
        "project_dir": m["project_dir"],
        "phases": {k: v["status"] for k, v in m["phases"].items()},
        "budget": m["budget"],
    }


@film_tool(
    name="project_open",
    desc="切换活跃项目（用于继续之前的项目）",
    params={"project_id": str},
    required=["project_id"],
)
def _project_open(handler, args):
    pid = args["project_id"]
    m = ws.read_manifest(pid)
    _set_active_pid(handler, pid)
    return {"project_id": pid, "phases": m["phases"]}


# ============== 视觉生成 ==============
@film_tool(
    name="gen_image",
    desc="Seedream 文生图 / 图编辑 / 多图融合。带 ref_image_url（单图）或 ref_image_urls（多图，最多14张）即编辑/融合模式。watermark 默认 false（不加水印）。**落盘目录按 name 前缀自动区分**：name 以 `ref_` 开头的参考图（角色三视图/大头照/场景图/道具图）落 entities/，其余（关键帧等）落 shots/。返回 {path, url, name}。角色/道具/场景的参考图也用这个工具生成——怎么用它维护跨镜头一致性（白底三视图 / 面部特写 / 锁参考）见 skills/skill_video/SKILL.md",
    params={
        "prompt": {"type": str, "description": "图像描述"},
        "name": {"type": str, "description": "产物文件名（不含扩展名）。**参考图（角色/场景/道具，会被 reference_images 引用的）必须以 `ref_` 开头**，会落到 entities/；关键帧等其他图落 shots/"},
        "ref_image_url": {"type": str, "description": "[可选] 单张参考图（图编辑模式）。优先传本地 path（如上一张 gen_image 返回的 path，自动 base64 内嵌、最稳），也支持 http(s) url"},
        "ref_image_urls": {"type": "array", "items": {"type": "string"}, "description": "[可选] 多张参考图（最多 14 张，多图融合模式）。本地 path 或 url 均可，传了优先于 ref_image_url"},
        "size": {"type": str, "description": "尺寸，需 ≥ 360万像素（如 2048x2048、1920x1920），过小会被图像接口拒绝", "default": "2048x2048"},
        "watermark": {"type": bool, "description": "是否加「AI 生成」水印", "default": False},
    },
    required=["prompt", "name"],
)
def _gen_image(handler, args):
    """文生图 / 图编辑 / 多图融合（Seedream）。返回 {path, url, name}。
    落盘目录按 name 前缀自动区分：`ref_` 开头的参考图落 entities/，其余落 shots/。
    传 ref_image_urls（数组，最多 14 张）做多图融合；传单张也可。角色/道具/场景的参考图
    也用这个工具生成——怎么用它维护跨镜头一致性见 skills/skill_video/SKILL.md。"""
    prompt = args["prompt"]
    name = args.get("name", f"img_{int(time.time())}")
    # ref：数组优先，兼容单张 ref_image_url
    refs = args.get("ref_image_urls")
    if refs:
        ref = refs if len(refs) > 1 else refs[0]
    else:
        ref = args.get("ref_image_url")
    size = args.get("size", "2048x2048")
    watermark = bool(args.get("watermark", False))
    # 参考图（ref_ 前缀）落 entities/，关键帧等其余图落 shots/——别再混在一起
    subdir = "entities" if name.startswith("ref_") else "shots"
    save = _project_path(handler, subdir, f"{name}.png")
    save.parent.mkdir(parents=True, exist_ok=True)
    r = sdk.gen_image(prompt, save, ref_image_url=ref, size=size, watermark=watermark)
    ws.log_model_call(_active_pid(handler), sdk.MODEL_IMG, {
        "via_tool": "gen_image",
        "name": name,
        "prompt": prompt,
        "ref_image_url": ref,
        "size": size,
        "watermark": watermark,
        "result": {"path": r["path"], "url": r["url"]},
    }, raw_request=r.get("body"), raw_response=r.get("raw"))
    return {"path": r["path"], "url": r["url"], "name": name}


def _resolve_reference_images(handler, args) -> tuple:
    """收集 reference_images（url 列表，可含字符串或 {url} dict）给 Seedance 当多模态参考。
    返回 (urls, sources)：urls 是纯 url 列表；sources 是每张图来源摘要（审计用）。
    """
    def normalize_ref(ref: str) -> str:
        s = str(ref).strip()
        if s.startswith(("http://", "https://", "data:")):
            return s
        p = Path(s)
        if p.exists():
            return str(p.resolve())
        # Agent 常会在项目目录语境下写 entities/foo.png / shots/foo.png。
        # 工具进程的 cwd 不一定是项目目录，这里用 active project 做一次兜底补全。
        if not p.is_absolute():
            pid = _active_pid(handler)
            if pid:
                candidate = ws.project_dir(pid) / p
                if candidate.exists():
                    return str(candidate.resolve())
        return s

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


def _resolve_reference_video(ref_video: Optional[str]) -> Optional[str]:
    """把 reference_video_url 入参兜底成 Seedance 能吃的云端 url。

    支持三种入参：
      - http(s) url → 直接返回
      - 本地 path → 反查同名 sidecar `<path>.url.txt` 拿云端 url；
        sidecar 不存在 → 抛 RuntimeError，提示先 query_video_task
      - 空 → 返回 None
    """
    if not ref_video:
        return None
    s = str(ref_video).strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    p = Path(s)
    sidecar = p.with_suffix(".url.txt")
    if sidecar.exists():
        url = sidecar.read_text(encoding="utf-8").strip()
        if url:
            return url
    raise RuntimeError(
        f"reference_video_url 收到本地路径 '{s}' 但找不到云端 url（sidecar {sidecar.name} 不存在）。"
        f"链式段必须用上一段 query_video_task 返回的 video_url，或确保 query_video_task "
        f"已经成功落盘并写出 .url.txt sidecar 文件。"
    )


@film_tool(
    name="gen_video_t2v",
    desc="Seedance 2.0 视频生成（唯一入口）。异步任务立即返回 task_id（不要等！），后续用 query_video_task 轮询。只走多模态参考模式：reference_images / reference_video_url（最多 9 张图 + 1 段视频）。⛔ 开拍门槛：本 shot 出现的所有角色/关键道具/主场景，必须已用 gen_image 出好参考图并 vlm 过审，再把这些图放进 reference_images（**直接传 gen_image 返回的本地 path 即可，无需 url**）。**链式衔接、配乐策略、跨镜头一致性详见 skills/skill_video/SKILL.md**",
    params={
        "prompt": {"type": str, "description": "视频描述。链式段必须显式承接上段（'承接上段视频，...'）。需要锁首/尾帧画面用文字暗示：'opening frame: ...; ending frame: ...'"},
        "name": {"type": str, "description": "产物文件名（不含扩展名）"},
        "duration": {"type": int, "description": "视频时长 4-15 秒", "minimum": 4, "maximum": 15},
        "generate_audio": {"type": bool, "description": "Seedance 2.0 原生同步音频。配乐策略见 skills/skill_video/SKILL.md", "default": False},
        "reference_images": {"type": "array", "items": {"type": "string"}, "description": "[可选] 参考图列表（最多 9 张）。把本 shot 角色/道具/场景的参考图都放进来锁一致性。**强烈建议直接传 gen_image 返回的本地 path**（短、好转抄，工具会自动 base64 内嵌）；不要转抄那条很长的带签名 url——签名 query 一旦被你截断，Seedance 服务端就会报 resource download failed"},
        "reference_video_url": {"type": str, "description": "[链式段（≥2 段视频中第 N≥2 段）必传] 上一段视频。可传 query_video_task 返回的 video_url（云端 url），也可传 path（本地 mp4 路径，工具会自动反查同名 .url.txt sidecar 取云端 url）。最多 1 段"},
        "resolution": {"type": str, "enum": ["480p", "720p", "1080p"], "default": "720p"},
        "ratio": {"type": str, "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"], "default": "16:9"},
        "seed": {"type": int, "description": "[可选] 随机种子（-1 随机）。传同一 seed + 同 prompt 可复现近似画面，用于锁定/微调"},
        "camera_fixed": {"type": bool, "description": "[可选] 是否固定机位（不运镜）。固定机位镜头建议传 true，比只在 prompt 里写更稳"},
    },
    required=["prompt", "name"],
)
def _gen_video_t2v(handler, args):
    """Seedance 视频生成（仅多模态参考模式）。
    参考媒体：reference_images（最多 9 张 url）/ reference_video_url（链式段承接上一段）。
    本工具是**唯一**的视频生成入口——已删除 i2v / 首尾帧模式（互斥 reference 不划算）。
    """
    prompt = args["prompt"]
    generate_audio = bool(args.get("generate_audio", False))

    name = args.get("name", f"video_{int(time.time())}")
    pid = _active_pid(handler)
    if pid:
        ws.bump_budget(pid, "seedance", 1)
    ref_urls, ref_sources = _resolve_reference_images(handler, args)
    ref_video_in = args.get("reference_video_url")
    ref_video = _resolve_reference_video(ref_video_in)
    seed = args.get("seed")
    camera_fixed = args.get("camera_fixed")
    r = sdk.submit_video_task(
        prompt,
        reference_images=ref_urls or None,
        reference_video_url=ref_video,
        duration=args.get("duration"),
        generate_audio=generate_audio,
        resolution=args.get("resolution", "720p"),
        ratio=args.get("ratio", "16:9"),
        seed=seed,
        camera_fixed=camera_fixed,
    )
    ws.log_seedance_call(pid, {
        "tool": "gen_video_t2v", "name": name, "task_id": r["task_id"],
        "model": r["model"], "prompt": prompt[:200],
        "reference_video_url": ref_video,
        "reference_video_url_input": ref_video_in,
        "reference_count": len(ref_urls),
        "reference_sources": ref_sources,
    })
    ws.log_model_call(pid, sdk.MODEL_VIDEO, {
        "via_tool": "gen_video_t2v",
        "name": name,
        "task_id": r["task_id"],
        "model": r["model"],
        "prompt": prompt,                            # 完整 prompt 不截断（已含 lint 后版本）
        "duration": args.get("duration"),
        "ratio": args.get("ratio", "16:9"),
        "resolution": args.get("resolution", "720p"),
        "generate_audio": generate_audio,
        "seed": seed,
        "camera_fixed": camera_fixed,
        "reference_video_url": ref_video,
        "reference_video_url_input": ref_video_in,
        "reference_images_count": len(ref_urls),
        "reference_sources": ref_sources,
    }, raw_request=r.get("body"), raw_response=r.get("raw"))
    return {"task_id": r["task_id"], "model": r["model"], "name": name,
            "reference_count": len(ref_urls),
            "reference_video_url": ref_video,
            "reference_sources": ref_sources,
            "hint": "异步任务已提交，请用 query_video_task 轮询 status。预计 200-300s 完成。"}


# 进度条跨调用记忆：{task_id: start_ts}
# 同一个 task_id 多次 query 时复用首次开始时间，进度条连续从上一次的百分比继续，
# 而不是每次新调用都从 0% 起。
_VIDEO_TASK_STARTS = {}


@film_tool(
    name="query_video_task",
    desc="查询 Seedance 任务状态。默认阻塞等到 succeeded/failed，**自动按 duration 估 ETA + 动态轮询间隔 + 打印进度条**。建议传 duration（视频时长秒数），ETA 会算成 60 + 15 * duration（10s 视频约 210s，15s 视频约 285s）。succeeded 时返回 {path?, video_url}（path: save_name 给了的话；video_url: 云端 url 可直接当下一段 reference_video_url）。同时把 url 写到 <save_name>.url.txt sidecar 方便本地 path 反查。串行 vs 并行调度策略见 skills/skill_video/SKILL.md",
    params={
        "task_id": {"type": str, "description": "gen_video_t2v 返回的 task_id"},
        "save_name": {"type": str, "description": "[可选] succeeded 时落盘的文件名（不含扩展名）"},
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
        if st in ("succeeded", "failed") or not wait:
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

    # 用户中断：本地放弃轮询，云端任务自生自灭（Seedance 不支持真正的取消）
    if aborted:
        return {"status": "aborted", "task_id": task_id,
                "hint": "用户中断了轮询，本地已放弃。云端任务可能仍在跑，"
                        "稍后可用同 task_id 再次 query_video_task 取回结果。"}

    out = {"status": last["status"], "task_id": task_id}
    if last["status"] == "succeeded":
        _VIDEO_TASK_STARTS.pop(task_id, None)  # 任务终结，清掉进度条记忆
        video_url = last["video_url"]
        out["video_url"] = video_url
        if save_name:
            save = _project_path(handler, "shots", f"{save_name}.mp4")
            sdk._download(video_url, save)
            out["path"] = str(save)
            # 同步把 url 写到 sidecar，方便后续链式段引用 / 跨会话回查
            try:
                sidecar = save.with_suffix(".url.txt")
                sidecar.write_text(video_url, encoding="utf-8")
            except Exception:
                pass
    elif last["status"] == "failed":
        _VIDEO_TASK_STARTS.pop(task_id, None)  # 任务终结，清掉进度条记忆
        out["error"] = last["raw"].get("error") or last["raw"]
    else:
        out["hint"] = f"任务仍在运行中，已等待 {max_wait}s 仍未完成。再次调用本工具继续等待。"
    return out


@film_tool(
    name="cancel_video_task",
    desc="尝试取消 Seedance 任务（DELETE）。⚠️ 仅当任务处于 queued / pending 等未启动状态时可取消；一旦 status=running 则会返回 409 InvalidAction.RunningTaskDeletion——平台不支持中途打断。succeeded / 不存在也会 graceful 返回。最佳实践：用户中断后立即调一次本工具，能取消的就取消，不能取消的就只能等它跑完（已经计费）",
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
    name="video_concat",
    desc="ffmpeg 硬拼接（无重编码，秒级）。要求所有 clip 编码参数一致",
    params={
        "clips": {"type": "array", "items": {"type": "string"}, "description": "视频文件路径列表"},
        "name": {"type": str, "description": "输出文件名"},
    },
    required=["clips", "name"],
)
def _video_concat(handler, args):
    clips = args["clips"]
    name = args.get("name", f"concat_{int(time.time())}")
    save = _project_path(handler, "composed", f"{name}.mp4")
    return sdk.video_concat(clips, save)


@film_tool(
    name="video_crossfade",
    desc="ffmpeg 交叉溶解（视频+音频同步淡入淡出）。会重编码，慢于 concat",
    params={
        "clip_a": str,
        "clip_b": str,
        "name": str,
        "duration": {"type": float, "description": "过渡时长(秒)", "default": 1.0},
        "offset": {"type": float, "description": "过渡开始的时间点(从 clip_a 起算)", "default": 4.0},
        "transition": {"type": str, "description": "xfade 转场类型：fade/wipeleft/wiperight/slideup/slidedown/circleopen/circleclose/dissolve/radial 等，见 ffmpeg xfade", "default": "fade"},
        "preset": {"type": str, "description": "[可选] x264 编码档位（ultrafast..veryslow），慢=画质好体积小", "default": "ultrafast"},
    },
    required=["clip_a", "clip_b", "name"],
)
def _video_crossfade(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'crossfade')}.mp4")
    return sdk.video_crossfade(args["clip_a"], args["clip_b"], save,
                               duration=float(args.get("duration", 1.0)),
                               offset=float(args.get("offset", 4.0)),
                               transition=args.get("transition", "fade"),
                               preset=args.get("preset", "ultrafast"))


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
    name="video_fade",
    desc="头尾黑场（音视频同步）",
    params={
        "clip": str,
        "name": str,
        "fade_in": {"type": float, "default": 0.5},
        "fade_out": {"type": float, "default": 0.5},
        "total_duration": {"type": float, "description": "[可选] 视频总时长，不传会用 ffprobe 探测"},
        "preset": {"type": str, "description": "[可选] x264 编码档位", "default": "ultrafast"},
    },
    required=["clip", "name"],
)
def _video_fade(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'fade')}.mp4")
    return sdk.video_fade(args["clip"], save,
                          fade_in=float(args.get("fade_in", 0.5)),
                          fade_out=float(args.get("fade_out", 0.5)),
                          total_duration=args.get("total_duration"),
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
                                   duration=args.get("duration_mode", "shortest"))

    if action == "amix":
        save = _project_path(handler, "composed", f"{name}.mp4")
        return sdk.audio_amix(args["base_video"], args["bgm_audio"], save,
                              bgm_volume=float(args.get("bgm_volume", 0.2)),
                              base_volume=float(args.get("base_volume", 1.0)),
                              duration=args.get("duration_mode", "first"))

    raise ValueError(f"未知 audio_process action: {action}")


@film_tool(
    name="tts",
    desc="豆包大模型语音合成。当前需 TTS_APP_ID/TTS_TOKEN，无 key 会报错",
    params={
        "text": str,
        "name": str,
        "voice": {"type": str, "default": "default"},
    },
    required=["text", "name"],
)
def _tts(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'tts')}.mp3")
    r = sdk.tts(args["text"], save, voice=args.get("voice", "default"))
    ws.log_model_call(_active_pid(handler), "volc-tts", {
        "via_tool": "tts",
        "name": args.get("name"),
        "text": args["text"],
        "voice": args.get("voice", "default"),
        "result": r,
    }, raw_request=r.get("body"), raw_response=r.get("raw"))
    return r


@film_tool(
    name="gen_audio_bgm",
    desc="豆包·音乐 GenBGM 生成纯音乐（异步，返回 task_id，需 query_audio_task 轮询）。当前需在 vibefilming.config.json 配置 volc.ak / volc.sk，无 key 会报错。配乐流程详见 skills/skill_video/SKILL.md",
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
    }, raw_request=r.get("body"), raw_response=r.get("raw"))
    return {"task_id": r["task_id"], "name": name, "duration": duration}


# 别名兜底：agent 调旧名 gen_bgm 也能 work，避免脑补"未配置密钥"。
# 只注入、不进 schema（不向模型暴露旧名，但旧名调用仍可路由到 gen_audio_bgm）。
_DECORATED_TOOLS["gen_bgm"] = _gen_audio_bgm


# 跟 _VIDEO_TASK_STARTS 对称：BGM 进度条跨调用记忆
_BGM_TASK_STARTS = {}


@film_tool(
    name="query_audio_task",
    desc="查询 BGM（gen_audio_bgm）任务状态，跟 query_video_task 对称。默认阻塞轮询到 succeeded/failed。succeeded 时返回 {path?, audio_url}（path: save_name 给了的话落到 audios/<save_name>.mp3）。⚠️ 查 BGM 任务必须用本工具，不要用 query_video_task（那是查 Seedance 视频的，task_id 不通用）。配乐流程详见 skills/skill_video/SKILL.md",
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
                "hint": "用户中断了轮询，本地已放弃。云端任务可能仍在跑，"
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
    desc="ffmpeg 抽帧（不调 VLM，纯抽帧用于 debug 或缩略图）",
    params={
        "clip": str,
        "name": str,
        "fps": {"type": float, "description": "每秒抽几张", "default": 1.0},
    },
    required=["clip", "name"],
)
def _extract_frames(handler, args):
    name = args.get("name", f"frames_{int(time.time())}")
    save_dir = _project_path(handler, "reviews", name)
    return sdk.extract_frames(args["clip"], save_dir, fps=float(args.get("fps", 1.0)))


@film_tool(
    name="burn_subtitle",
    desc="在视频上烧录文字字幕（drawtext，永久叠加）",
    params={
        "clip": str,
        "text": str,
        "name": str,
        "start": {"type": float, "default": 0},
        "end": {"type": float, "default": 3},
        "fontsize": {"type": int, "default": 32},
    },
    required=["clip", "text", "name"],
)
def _burn_subtitle(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'subtitled')}.mp4")
    return sdk.burn_subtitle(args["clip"], save, args["text"],
                             start=float(args.get("start", 0)),
                             end=float(args.get("end", 3)),
                             fontsize=int(args.get("fontsize", 32)))


@film_tool(
    name="vlm_understand",
    desc="开放式视觉理解：自己写 question，Doubao Seed 2.0 pro 回答。视频走原生理解（不抽帧），图片走多图理解。结果落到 reviews/<name>.json。**审 video 传 video，审图片传 images（二选一，至少传一个）**。**审片场景的提问规范、决策树、提问模板详见 skills/skill_video/SKILL.md，调用前必读**",
    params={
        "video": {"type": str, "description": "[审视频时传] 单个视频路径。与 images 二选一"},
        "images": {"type": "array", "items": {"type": "string"}, "description": "[审图片时传] 一张或多张图片路径（对比/多帧审查时传多张）。与 video 二选一", "default": None},
        "question": {"type": str, "description": "你想让 VLM 回答的问题，越具体越好；审片场景按 skills/skill_video/SKILL.md 模板写"},
        "system": {"type": str, "description": "[可选] 系统提示，设定 VLM 角色/输出格式（如 '你是严格的影视审片导演，只输出 JSON'）"},
        "mode": {"type": str, "enum": ["auto", "video", "frames"], "default": "auto", "description": "auto=视频走原生理解、图片走多图；frames=强制抽帧"},
        "fps": {"type": float, "default": 1.0},
        "max_frames": {"type": int, "description": "frames 模式下最多抽几帧送 VLM", "default": 16},
        "max_tokens": {"type": int, "default": 4096},
        "temperature": {"type": float, "default": 0.1},
        "name": {"type": str, "description": "归档文件名（不含扩展名）。**审片场景必须按规范命名**，便于事后翻查：审 entity 视图用 `review_entity_<entity名>_<view名>`（如 review_entity_dancer_girl_front）；审单镜头视频用 `review_shot_<shot_id>`（如 review_shot_s01）；审合成成片用 `review_compose_v<版本号>`（如 review_compose_v1）；非审片场景才用默认 understand 前缀", "default": "understand"},
    },
    required=["question"],
)
def _vlm_understand(handler, args):
    """开放式视觉理解：你自己写 question，VLM（Seed 2.0 pro）回答。
    支持图片或视频输入，模式自适应（视频走原生理解，图片走多图理解）。

    用途广泛：
      - 让 VLM 描述/讲解视频或图片内容
      - 让 VLM 找问题、给改进建议（替代以前的 vlm_review 打分）
      - 让 VLM 定位异常时间区间（"第几秒剑消失了"）
      - 让 VLM 对比两版（一次传两个 clip 用图片模式）

    审片场景的提问规范、决策树、提问模板详见 skills/skill_video/SKILL.md，
    agent 调用前先读 md 自己拼好 question。
    """
    pid = _active_pid(handler)
    if pid:
        ws.bump_budget(pid, "vlm", 1)

    video = args.get("video")
    images = args.get("images")
    question = args["question"]
    fps = float(args.get("fps", 1.0))
    name = args.get("name", f"understand_{int(time.time())}")
    system = args.get("system")
    max_frames = int(args.get("max_frames", 16))

    # video / images 二选一：传 video 走视频原生理解，传 images 走多图理解
    if video and images:
        raise ValueError("video 与 images 只能二选一，不要同时传")
    if not video and not images:
        raise ValueError("必须传 video（视频路径）或 images（图片路径列表）其一")
    if video:
        clip = video
        clip_is_list = False
        is_video = True
    else:
        # images 容错：允许误传单个字符串
        imgs = images if isinstance(images, list) else [images]
        clip = imgs
        clip_is_list = True
        is_video = False

    if is_video and args.get("mode", "auto") != "frames":
        try:
            resp_data = sdk.doubao_video_understand(clip, question,
                                              max_tokens=int(args.get("max_tokens", 4096)),
                                              temperature=float(args.get("temperature", 0.1)),
                                              fps=fps, system=system)
            fallback_reason = None
        except Exception as e:
            # 视频原生理解超时/网关抖动 → 自动降级到抽帧（frames）模式，避免任务整体失败
            msg = str(e).lower()
            is_timeout = ("timed out" in msg) or ("timeout" in msg) or isinstance(e, TimeoutError)
            if not is_timeout:
                raise
            fallback_reason = f"video_native_timeout: {e}"
            tmp = _project_path(handler, "reviews", name) if pid else Path(f"/tmp/{name}")
            try:
                fr = sdk.extract_frames(clip, tmp, fps=fps)
                paths = fr["paths"][:max_frames]
                resp_data = sdk.doubao_vlm(paths, question,
                                     max_tokens=int(args.get("max_tokens", 4096)),
                                     temperature=float(args.get("temperature", 0.1)),
                                     system=system)
            finally:
                # 清理抽帧产物，避免 reviews/ 累积成几百兆
                import shutil as _shutil
                try:
                    if tmp.exists():
                        _shutil.rmtree(tmp)
                except Exception:
                    pass
    else:
        fallback_reason = None
        if is_video:
            tmp = _project_path(handler, "reviews", name) if pid else Path(f"/tmp/{name}")
            try:
                fr = sdk.extract_frames(clip, tmp, fps=fps)
                paths = fr["paths"][:max_frames]
                resp_data = sdk.doubao_vlm(paths, question,
                                     max_tokens=int(args.get("max_tokens", 4096)),
                                     temperature=float(args.get("temperature", 0.1)),
                                     system=system)
            finally:
                import shutil as _shutil
                try:
                    if tmp.exists():
                        _shutil.rmtree(tmp)
                except Exception:
                    pass
        else:
            if clip_is_list:
                paths = clip
            else:
                paths = [clip]
            resp_data = sdk.doubao_vlm(paths, question,
                                 max_tokens=int(args.get("max_tokens", 4096)),
                                 temperature=float(args.get("temperature", 0.1)),
                                 system=system)

    raw = resp_data["raw"]
    payload = resp_data["body"]
    
    answer = raw["choices"][0]["message"]["content"].strip()

    # 归档到 reviews/<name>.json
    if pid:
        out = ws.project_dir(pid) / "reviews" / f"{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        archive = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "video": video, "images": images,
            "question": question, "answer": answer,
        }
        if fallback_reason:
            archive["fallback"] = fallback_reason
        out.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")

    ws.log_model_call(pid, sdk.MODEL_VLM, {
        "via_tool": "vlm_understand",
        "modality": "video" if is_video else "image",
        "name": name,
        "video": video,
        "images": images,
        "question": question,
        "answer": answer,
        "fps": int(args.get("fps", 1)),
        "mode": args.get("mode", "auto"),
        "max_tokens": int(args.get("max_tokens", 4096)),
        "temperature": float(args.get("temperature", 0.1)),
        "fallback": fallback_reason,
    }, raw_request=raw.get("body"), raw_response=raw.get("raw"))

    result = {"question": question, "answer": answer}
    if fallback_reason:
        result["fallback"] = fallback_reason
    return result


# ============== 注入到 handler ==============
# 所有 film 工具现在都用 @film_tool 装饰器声明（含别名 gen_bgm），自动登记到
# _DECORATED_TOOLS。TOOL_REGISTRY 保留为空 dict 仅作向后兼容的扩展点（手动塞特殊
# 工具时可用），常规加工具只需在函数上挂 @film_tool。
TOOL_REGISTRY = {}


def inject_film_tools(handler):
    """把所有 film 工具挂到 handler 实例上。
    包含两类：手写 TOOL_REGISTRY（兜底扩展点）+ 用 @film_tool 装饰器声明的（_DECORATED_TOOLS）。
    """
    import types
    merged = {**TOOL_REGISTRY, **_DECORATED_TOOLS}
    for name, fn in merged.items():
        method = _wrap(name, fn)
        setattr(handler, f"do_{name}", types.MethodType(method, handler))
    return handler
