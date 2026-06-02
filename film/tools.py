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


def _project_path(handler, *parts) -> Path:
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    return ws.project_dir(pid).joinpath(*parts)


# ============== 项目工作区工具 ==============
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


def _project_status(handler, args):
    pid = args.get("project_id") or _active_pid(handler)
    if not pid:
        return {"status": "no_active_project", "all_projects": ws.list_projects()}
    m = ws.read_manifest(pid)
    return {
        "project_id": m["project_id"],
        "brief": m["brief"],
        "phases": m["phases"],
        "budget": m["budget"],
        "storyboard_summary": m.get("storyboard_summary"),
        "entities": m.get("entities", {}),
    }


def _project_open(handler, args):
    pid = args["project_id"]
    m = ws.read_manifest(pid)
    _set_active_pid(handler, pid)
    return {"project_id": pid, "phases": m["phases"]}


# ============== Storyboard 分镜 ==============
def _storyboard_set(handler, args):
    """覆盖式写入分镜。args 即 board 本身（title/duration_total/ratio/style/synopsis/shots[]/entities_planned[]）。"""
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先 project_create")
    board = {k: v for k, v in args.items() if v is not None}
    saved = ws.storyboard_set(pid, board)
    return {
        "ok": True,
        "shots_count": len(saved.get("shots", [])),
        "entities_planned_count": len(saved.get("entities_planned", [])),
        "path": str(ws.storyboard_path(pid)),
        "hint": "分镜已落盘。下一步：先在思考里写 <self_review> 自审 5 条（节奏/衔接/entity完整性/镜头多样性/预算估算），改完再做 entity_register。",
    }


def _storyboard_get(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目")
    board = ws.storyboard_get(pid)
    if not board:
        return {"empty": True, "hint": "还没写分镜，请用 storyboard_set"}
    return board


# ============== Entity 档案库 ==============
def _entity_register(handler, args):
    """声明一个 entity（不出图）。先 register，后用 add_view 逐张生成视角/状态图。"""
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create")
    name = args["name"].strip()
    etype = args.get("type", "character")
    desc = args.get("description", "")
    # canonical 不传时按 type 默认（character->front / prop->default / scene->wide）
    canonical = args.get("canonical_view")
    if not canonical:
        canonical = ws.DEFAULT_VIEWS_NEEDED.get(etype, ["default"])[0]
    views_needed = args.get("views_needed")  # None 时由 workspace 按 etype 默认展开
    ref = ws.entity_register(pid, name, etype, desc,
                             canonical_view=canonical,
                             views_needed=views_needed)
    pending = ref.get("views_needed", [])
    return {
        "name": ref["name"], "type": ref["type"],
        "description": ref["description"],
        "canonical_view": ref["canonical_view"],
        "views_needed": pending,
        "hint": (f"已登记。**character 默认三视图 = {pending}**，请按这个清单"
                 f"逐张 entity_add_view（先出 {ref['canonical_view']} 当基准 → vlm 过审 → "
                 f"再出剩余视角）。后续 gen_video_t2v 时把这个 entity 名加进 reference_entities，"
                 f"会自动把所有 view 当多参考喂给 Seedance。"),
    }


def _entity_add_view(handler, args):
    """给已有 entity 加一张视角/状态图。
    - 第一次（views 为空）= 生成 canonical 基准图，**不带 ref**（除非外部传了 ref_image_url）
    - 之后每次自动用 canonical_view 的 url 当 ref_image_url，强制保持一致
    - 也支持显式覆盖 ref_image_url 用别的视角当参考
    - **character 类型自动追加白底素材规范**：
      - view='turnaround'（默认）→ 1 张图含 front/side/back 三视图（行业标准 character turnaround sheet）
      - view='face_closeup' → 仅面部特写（防 ID 漂移用）
      - view 含 front/side/back/3_4 等 → 单视角立绘（一般不需要，turnaround 默认就够）
    """
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目")
    entity_name = args["entity_name"]
    view = args["view"]
    prompt = args["prompt"]
    size = args.get("size", "1024x1024")

    ref = ws.entity_read(pid, entity_name)
    views = ref.get("views", {})

    # character 类型：根据 view 名自动追加不同的素材规范
    if ref.get("type") == "character":
        view_l = (view or "").lower()
        if "turnaround" in view_l or view_l in ("default", ""):
            # 行业标准三视图：一张白底图含 front + side + back 三个视角
            # 默认 1024x1024 太挤，自动改成横向 16:9 让三视图排得开
            if size == "1024x1024":
                size = "1920x1080"
            prompt = (f"{prompt}\n\n"
                      f"【素材规范 · character turnaround sheet · 严格遵守】"
                      f"一张图，**同一个角色的三视图**：左侧正面 front view、"
                      f"中间侧面 side view（朝画面右侧 90 度）、右侧背面 back view，"
                      f"三个视角左→中→右水平排列，**全部为同一角色**，"
                      f"造型/服装/发型/配饰完全一致；"
                      f"纯白色背景，无任何环境元素、无影子、无杂物；"
                      f"每个视角下角色全身入镜（头到脚），T-pose 或自然站姿，"
                      f"双手自然下垂或微微张开；光线均匀、无明显阴影；"
                      f"游戏角色立绘 / 概念设计稿 / character reference sheet 风格。")
        elif "face_closeup" in view_l or "face" in view_l or "closeup" in view_l:
            # 大头照：防 ID 漂移
            prompt = (f"{prompt}\n\n"
                      f"【素材规范 · 面部特写大头照 · 严格遵守】"
                      f"仅面部特写，精确裁剪到下颌底部以上，几乎不留颈部、肩部、背景；"
                      f"正面、表情中性、双眼直视镜头；"
                      f"纯白色背景；光线均匀、无明显阴影；"
                      f"高清人脸参考素材，用于视频模型 ID 锁定。")
        else:
            # 兼容旧的单视角写法（front/side/back/3_4/action_*）
            if "front" in view_l:
                angle = "正面 front view"
            elif "back" in view_l:
                angle = "背面 back view"
            elif "side" in view_l:
                angle = "侧面 side view（人物身体朝向画面左侧或右侧 90 度）"
            elif "three_quarter" in view_l or "3_4" in view_l:
                angle = "3/4 侧前方视角"
            else:
                angle = f"{view} 视角"
            prompt = (f"{prompt}\n\n"
                      f"【素材规范，严格遵守】纯白色背景，无任何环境元素、无影子、无杂物；"
                      f"人物全身入镜（头到脚），居中构图；"
                      f"{angle}；"
                      f"自然站姿，双手自然下垂或微微张开；"
                      f"光线均匀，无明显阴影；"
                      f"角色概念图风格，类似游戏角色立绘素材。")

    # 决定 ref_image_url：用户显式给的 > canonical view 的 url > 无（首张）
    ref_url = args.get("ref_image_url")
    if not ref_url:
        canonical = ref.get("canonical_view")
        if canonical and canonical in views:
            ref_url = views[canonical].get("url")

    save = ws.entity_dir(pid, entity_name) / f"{view}.png"
    save.parent.mkdir(parents=True, exist_ok=True)
    r = sdk.gen_image(prompt, save, ref_image_url=ref_url, size=size)
    ws.entity_record_view(pid, entity_name, view, r["path"], r["url"])
    ws.log_model_call(pid, "seedream", {
        "via_tool": "entity_add_view",
        "name": f"{entity_name}.{view}",
        "prompt": prompt,
        "ref_image_url": ref_url,
        "size": size,
        "result": {"path": r["path"], "url": r["url"]},
    })
    return {
        "entity": entity_name, "view": view,
        "path": r["path"], "url": r["url"],
        "used_ref": ref_url,
        "hint": "已登记到 entity 档案。后续 gen_video_t2v 想用这个视角时，把 entity 名加进 reference_entities，或从 entity_get 取 url 加进 reference_images。",
    }


def _entity_get(handler, args):
    """查 entity 档案。不传 entity_name 列出全部摘要；传了返回完整 ref.json（含每个 view 的 url）。"""
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目")
    name = args.get("entity_name")
    if not name:
        return {"entities": ws.entity_list(pid)}
    return ws.entity_read(pid, name)


# ============== 视觉生成 ==============
def _gen_image(handler, args):
    """通用图像生成（关键帧、概念图等）。**注意：entity 角色/道具图请用 entity_add_view，不要用这个工具。**"""
    prompt = args["prompt"]
    name = args.get("name", f"img_{int(time.time())}")
    ref = args.get("ref_image_url")
    size = args.get("size", "1024x1024")
    save = _project_path(handler, "shots", f"{name}.png")
    save.parent.mkdir(parents=True, exist_ok=True)
    r = sdk.gen_image(prompt, save, ref_image_url=ref, size=size)
    ws.log_model_call(_active_pid(handler), "seedream", {
        "via_tool": "gen_image",
        "name": name,
        "prompt": prompt,
        "ref_image_url": ref,
        "size": size,
        "result": {"path": r["path"], "url": r["url"]},
    })
    return {"path": r["path"], "url": r["url"], "name": name}


def _resolve_reference_images(handler, args) -> tuple:
    """合并 reference_images（直接 url 列表）+ reference_entities（取 entity 所有 view 的 url）。
    返回 (urls, sources)：
      urls: list[str] —— 给 sdk 用的纯 url 列表
      sources: list[dict] —— 每张图的来源摘要 [{url, from}]，用于审计日志，"from" 形如
        "entity:dancer_male.front" / "raw_arg" / "raw_arg_dict"
    """
    pid = _active_pid(handler)
    urls = []
    sources = []
    raw_imgs = args.get("reference_images") or []
    for x in raw_imgs:
        if isinstance(x, str):
            urls.append(x)
            sources.append({"url": x, "from": "raw_arg"})
        elif isinstance(x, dict) and x.get("url"):
            urls.append(x["url"])
            sources.append({"url": x["url"], "from": "raw_arg_dict"})
    ents = args.get("reference_entities") or []
    for ent in ents:
        if not pid:
            continue
        try:
            ref = ws.entity_read(pid, ent)
        except Exception:
            sources.append({"url": None, "from": f"entity:{ent}", "error": "entity_not_found"})
            continue
        for view_name, view in (ref.get("views") or {}).items():
            url = view.get("url")
            if url:
                urls.append(url)
                sources.append({"url": url, "from": f"entity:{ent}.{view_name}"})
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


# ===== BGM prompt linter（gen_video_t2v 调用前硬校验）=====
# Seedance 看 prompt 时靠 4 类括号识别音频意图：（）BGM / <>音效 / {}台词 / 【】字幕
# 本项目策略：BGM 走后期 gen_audio_bgm + audio_amix，禁止 Seedance 自己出 BGM。
# 详见 skills/skill_audio/skill_audio.md "4 类特殊字符规范"
import re as _re

_BGM_KEYWORDS = [
    "BGM", "bgm", "Bgm",
    "背景音乐", "背景配乐", "配乐", "音乐", "插曲", "片头曲", "片尾曲", "主题曲",
    "钢琴曲", "弦乐", "鼓点", "节拍", "旋律",
    "music", "Music", "MUSIC", "soundtrack", "score", "melody", "rhythm", "beat",
]
# 中英括号都要管 — Seedance 文档里 4 类符号规范写的就是中文（）；英文 () 也常被 agent 误用
_PAREN_PATTERN = _re.compile(r"[（(]([^（()）]{0,80})[）)]")


def _lint_no_inline_bgm(prompt: str) -> str:
    """检测 prompt 里"（…BGM/音乐/配乐…）"括号描述，发现就 raise。

    匹配策略：
      1. 中/英括号 () 内出现 BGM / 音乐 / 配乐 / soundtrack / melody 等关键词
      2. 或顶格出现 "BGM:" / "配乐：" / "音乐：" 等冒号声明
    返回原 prompt（未命中）；命中则 raise ValueError 让 agent 修。
    """
    # 检查 1：括号包裹的 BGM 描述
    for m in _PAREN_PATTERN.finditer(prompt):
        inside = m.group(1)
        for kw in _BGM_KEYWORDS:
            if kw in inside:
                raise ValueError(
                    f"BGM 闸门触发：prompt 中检测到括号 BGM 描述「{m.group(0)}」（命中关键词「{kw}」）。\n"
                    f"本项目 BGM 走后期 gen_audio_bgm + audio_amix，Seedance 不出 BGM。\n"
                    f"修法：① 删掉这段括号；② 想要的环境音改写成 <>音效（如 <远处沉闷鼓点声>）；\n"
                    f"③ prompt 末尾追加「无背景音乐，仅保留环境音效与人物对白」。\n"
                    f"详见 skills/skill_audio/skill_audio.md 的「4 类特殊字符规范」与「BGM prompt 编写要素」。"
                )

    # 检查 2：顶格 "BGM: xxx" / "配乐：xxx" 声明
    for line in prompt.splitlines():
        ls = line.strip()
        for prefix in ("BGM:", "BGM：", "bgm:", "bgm：",
                       "配乐:", "配乐：", "音乐:", "音乐：",
                       "Music:", "music:", "Soundtrack:", "soundtrack:"):
            if ls.startswith(prefix):
                raise ValueError(
                    f"BGM 闸门触发：prompt 中检测到 BGM 声明行「{ls[:60]}」。\n"
                    f"本项目 BGM 走后期 gen_audio_bgm + audio_amix，Seedance 不出 BGM。\n"
                    f"修法：删掉这一行；环境音改写成 <>音效；末尾加「无背景音乐」。"
                )
    return prompt


def _gen_video_t2v(handler, args):
    """Seedance 视频生成（仅多模态参考模式）。
    参考媒体三件套：reference_entities（按名展开 entity 所有 view）/ reference_images / reference_video_url。
    本工具是**唯一**的视频生成入口——已删除 i2v / 首尾帧模式（互斥 reference 不划算）。
    """
    prompt = args["prompt"]
    # ===== BGM 硬闸门（双层）=====
    # 闸门 1（代码层 lint）：禁止 prompt 写"（…BGM/音乐/配乐…）"括号描述
    #   原因：本项目 BGM 走后期 gen_audio_bgm + audio_amix，Seedance 出对白/音效就好。
    #   prompt 里写括号 BGM 描述 → 模型会自己脑补 BGM 撕裂 → 拼接后段间断裂
    #   详见 skills/skill_audio/skill_audio.md "4 类特殊字符规范"
    prompt = _lint_no_inline_bgm(prompt)
    # 闸门 2（兜底）：generate_audio=True 时若 prompt 没"无背景音乐"句，自动追加
    generate_audio = bool(args.get("generate_audio", False))
    if generate_audio and "无背景音乐" not in prompt and "无 BGM" not in prompt and "no bgm" not in prompt.lower():
        prompt = prompt.rstrip("。.\n") + "。无背景音乐，仅保留环境音效与人物对白。"

    name = args.get("name", f"video_{int(time.time())}")
    pid = _active_pid(handler)
    if pid:
        ws.bump_budget(pid, "seedance", 1)
    ref_urls, ref_sources = _resolve_reference_images(handler, args)
    ref_video_in = args.get("reference_video_url")
    ref_video = _resolve_reference_video(ref_video_in)
    r = sdk.submit_video_task(
        prompt,
        reference_images=ref_urls or None,
        reference_video_url=ref_video,
        duration=args.get("duration"),
        generate_audio=generate_audio,
        resolution=args.get("resolution", "720p"),
        ratio=args.get("ratio", "16:9"),
    )
    ws.log_seedance_call(pid, {
        "tool": "gen_video_t2v", "name": name, "task_id": r["task_id"],
        "model": r["model"], "prompt": prompt[:200],
        "reference_video_url": ref_video,
        "reference_video_url_input": ref_video_in,
        "reference_count": len(ref_urls),
        "reference_sources": ref_sources,
    })
    ws.log_model_call(pid, "seedance", {
        "via_tool": "gen_video_t2v",
        "name": name,
        "task_id": r["task_id"],
        "model": r["model"],
        "prompt": prompt,                            # 完整 prompt 不截断（已含 lint 后版本）
        "duration": args.get("duration"),
        "ratio": args.get("ratio", "16:9"),
        "resolution": args.get("resolution", "720p"),
        "generate_audio": generate_audio,
        "reference_video_url": ref_video,
        "reference_video_url_input": ref_video_in,
        "reference_entities": args.get("reference_entities") or [],
        "reference_images_count": len(ref_urls),
        "reference_sources": ref_sources,
    })
    return {"task_id": r["task_id"], "model": r["model"], "name": name,
            "reference_count": len(ref_urls),
            "reference_video_url": ref_video,
            "reference_sources": ref_sources,
            "hint": "异步任务已提交，请用 query_video_task 轮询 status。预计 200-300s 完成。"}


# 进度条跨调用记忆：{task_id: start_ts}
# 同一个 task_id 多次 query 时复用首次开始时间，进度条连续从上一次的百分比继续，
# 而不是每次新调用都从 0% 起。
_VIDEO_TASK_STARTS = {}


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
        out["hint"] = (
            "已拿到产物。⚠️ **下一步必须立即 vlm_understand 审片**（参考 skill_director_vlm.md），"
            f"name='review_shot_{save_name or '<shot_id>'}'。"
            "**不过 vlm 不许出下一段视频**——链式衔接对上一段质量极敏感，瑕疵会被放大。"
            "审过了再做下一段时，reference_video_url 既可传 video_url 也可传 path（本地文件会被自动上传）。"
        )
    elif last["status"] == "failed":
        _VIDEO_TASK_STARTS.pop(task_id, None)  # 任务终结，清掉进度条记忆
        out["error"] = last["raw"].get("error") or last["raw"]
    else:
        out["hint"] = f"任务仍在运行中，已等待 {max_wait}s 仍未完成。再次调用本工具继续等待。"
    return out


def _sleep(handler, args):
    """让 agent 主动等一段时间，常用于异步任务轮询间隔。"""
    seconds = float(args.get("seconds", 10))
    seconds = min(seconds, 120)  # 上限 2 分钟，防止误用
    time.sleep(seconds)
    return {"status": "ok", "slept_seconds": seconds}


def _cancel_video_task(handler, args):
    """主动取消 Seedance 云端任务（防止本地中断后云端继续烧预算）。"""
    task_id = args["task_id"]
    r = sdk.cancel_video_task(task_id)
    return {"task_id": task_id, **r}


# ============== 视频处理 ==============
def _video_concat(handler, args):
    clips = args["clips"]
    name = args.get("name", f"concat_{int(time.time())}")
    save = _project_path(handler, "composed", f"{name}.mp4")
    return sdk.video_concat(clips, save)


def _video_crossfade(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'crossfade')}.mp4")
    return sdk.video_crossfade(args["clip_a"], args["clip_b"], save,
                               duration=float(args.get("duration", 1.0)),
                               offset=float(args.get("offset", 4.0)))


def _video_trim(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'trim')}.mp4")
    return sdk.video_trim(args["clip"], save, float(args["start"]), float(args["end"]))


def _video_speed(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'speed')}.mp4")
    return sdk.video_speed(args["clip"], save, float(args["factor"]))


def _video_overlay(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'pip')}.mp4")
    return sdk.video_overlay(args["base"], args["pip"], save,
                             pip_scale=float(args.get("pip_scale", 0.33)),
                             position=args.get("position", "br"),
                             margin=int(args.get("margin", 20)))


def _video_fade(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'fade')}.mp4")
    return sdk.video_fade(args["clip"], save,
                          fade_in=float(args.get("fade_in", 0.5)),
                          fade_out=float(args.get("fade_out", 0.5)),
                          total_duration=args.get("total_duration"))


def _video_portrait(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'portrait')}.mp4")
    return sdk.video_portrait(args["clip"], save,
                              width=int(args.get("width", 720)),
                              height=int(args.get("height", 1280)))


# ============== 音频 ==============
def _audio_amix(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'amix')}.mp4")
    return sdk.audio_amix(args["base_video"], args["bgm_audio"], save,
                          bgm_volume=float(args.get("bgm_volume", 0.2)))


def _tts(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'tts')}.mp3")
    r = sdk.tts(args["text"], save, voice=args.get("voice", "default"))
    ws.log_model_call(_active_pid(handler), "tts", {
        "via_tool": "tts",
        "name": args.get("name"),
        "text": args["text"],
        "voice": args.get("voice", "default"),
        "result": r,
    })
    return r


def _gen_audio_bgm(handler, args):
    """提交 BigMusic GenBGM 任务（异步）。跟 gen_video_t2v 完全对称：只提交，立即回 task_id。

    必填：prompt（自然语言描述风格/情绪/乐器/场景/段落结构，建议 ≥50 字）
    选填：name（落盘文件名，默认 bgm_<ts>）/ duration（30-120 秒，默认 30）/
          segments（[{"Name":"intro","Duration":10},...]，传了优先级高于 duration）/
          enable_input_rewrite（让模型自动改写 prompt，默认 false）/
          base_video（成片路径；传了会自动 probe 时长并把 duration 覆写为
                      clamp(probe, 30, 120)，agent 不用自己算等长）

    返回：{task_id, name, duration, hint}
    后续：用 query_audio_task(task_id, save_name) 轮询并落盘。
    """
    prompt = args["prompt"]
    name = args.get("name", f"bgm_{int(time.time())}")
    duration = int(args.get("duration", 30))
    segments = args.get("segments")
    enable_input_rewrite = bool(args.get("enable_input_rewrite", False))

    # base_video 兜底：传了就自动 probe 算等长，省得 agent 自己 probe + clamp
    base_video = args.get("base_video")
    auto_duration_note = None
    if base_video and not segments:
        bv_path = _project_path(handler, base_video) if not Path(base_video).is_absolute() else Path(base_video)
        if bv_path.exists():
            probed = sdk.probe_duration(str(bv_path))
            new_duration = max(30, min(120, int(round(probed))))
            auto_duration_note = (
                f"base_video={bv_path.name} probe={probed:.2f}s -> "
                f"duration={new_duration}s (clamp 到火山 [30,120] 区间)"
            )
            duration = new_duration

    pid = _active_pid(handler)
    r = sdk.submit_bgm_task(
        prompt, duration=duration, segments=segments,
        enable_input_rewrite=enable_input_rewrite,
    )
    ws.log_model_call(pid, "gen_bgm", {
        "via_tool": "gen_audio_bgm",
        "name": name,
        "task_id": r["task_id"],
        "prompt": prompt,                          # 完整 prompt 不截断
        "duration": duration,
        "segments": segments,
        "enable_input_rewrite": enable_input_rewrite,
        "base_video": str(base_video) if base_video else None,
        "auto_duration_note": auto_duration_note,
    })
    out = {
        "task_id": r["task_id"],
        "name": name,
        "duration": duration,
        "hint": (
            "BGM 任务已提交。请用 query_audio_task(task_id, save_name='<name>') "
            "轮询并落盘到 projects/<pid>/audios/<save_name>.mp3。预计 30-90s 完成。"
            " amix 后必须 vlm_understand review_final_with_bgm 审过审，不过审重做 BGM prompt。"
        ),
    }
    if auto_duration_note:
        out["auto_duration_note"] = auto_duration_note
    return out


# 跟 _VIDEO_TASK_STARTS 对称：BGM 进度条跨调用记忆
_BGM_TASK_STARTS = {}


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
        out["hint"] = (
            "BGM 已落盘到 audios/。下一步：拼接好 video 后用 audio_amix("
            "base_video, bgm_audio=path, bgm_volume=0.15-0.25) 铺底成片。"
        )
    elif last["status"] == "failed":
        _BGM_TASK_STARTS.pop(task_id, None)
        out["error"] = last.get("error")
        if last.get("hint"):
            out["hint"] = last["hint"]
    else:
        out["hint"] = f"BGM 任务仍在运行中（已等待 {max_wait}s 仍未完成）。再次调用本工具继续等待。"
    return out


# ============== 评估归档 ==============
def _extract_frames(handler, args):
    name = args.get("name", f"frames_{int(time.time())}")
    save_dir = _project_path(handler, "reviews", name)
    return sdk.extract_frames(args["clip"], save_dir, fps=float(args.get("fps", 1.0)))


def _burn_subtitle(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'subtitled')}.mp4")
    return sdk.burn_subtitle(args["clip"], save, args["text"],
                             start=float(args.get("start", 0)),
                             end=float(args.get("end", 3)),
                             fontsize=int(args.get("fontsize", 32)))


def _vlm_understand(handler, args):
    """开放式视觉理解：你自己写 question，VLM（Seed 2.0 pro）回答。
    支持图片或视频输入，模式自适应（视频走原生理解，图片走多图理解）。

    用途广泛：
      - 让 VLM 描述/讲解视频或图片内容
      - 让 VLM 找问题、给改进建议（替代以前的 vlm_review 打分）
      - 让 VLM 定位异常时间区间（"第几秒剑消失了"）
      - 让 VLM 对比两版（一次传两个 clip 用图片模式）

    审片场景的提问规范、决策树、提问模板详见 skills/skill_director_vlm/skill_director_vlm.md，
    agent 调用前先读 md 自己拼好 question。
    """
    pid = _active_pid(handler)
    if pid:
        ws.bump_budget(pid, "vlm", 1)

    clip = args["clip"]
    question = args["question"]
    fps = float(args.get("fps", 1.0))
    name = args.get("name", f"understand_{int(time.time())}")

    # 判断是视频还是图片
    suffix = Path(clip).suffix.lower()
    is_video = suffix in (".mp4", ".mov", ".avi", ".mkv", ".webm")

    if is_video and args.get("mode", "auto") != "frames":
        raw = sdk.doubao_video_understand(clip, question,
                                          max_tokens=int(args.get("max_tokens", 4096)),
                                          temperature=float(args.get("temperature", 0.1)),
                                          fps=fps)
    else:
        if is_video:
            tmp = _project_path(handler, "reviews", name) if pid else Path(f"/tmp/{name}")
            fr = sdk.extract_frames(clip, tmp, fps=fps)
            paths = fr["paths"][:5]
        else:
            paths = [clip]
        raw = sdk.doubao_vlm(paths, question,
                             max_tokens=int(args.get("max_tokens", 4096)),
                             temperature=float(args.get("temperature", 0.1)))

    answer = raw["choices"][0]["message"]["content"].strip()

    # 归档到 reviews/<name>.json
    if pid:
        out = ws.project_dir(pid) / "reviews" / f"{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "clip": clip, "question": question, "answer": answer,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    ws.log_model_call(pid, "vlm_video" if is_video else "vlm_image", {
        "via_tool": "vlm_understand",
        "name": name,
        "clip": clip,
        "question": question,        # 完整 question 不截断
        "answer": answer,            # 完整 answer 不截断
        "fps": fps,
        "mode": args.get("mode", "auto"),
        "max_tokens": int(args.get("max_tokens", 4096)),
        "temperature": float(args.get("temperature", 0.1)),
    })

    return {"question": question, "answer": answer}


# ============== 注入到 handler ==============
TOOL_REGISTRY = {
    # 工作区
    "project_create": _project_create,
    "project_status": _project_status,
    "project_open":   _project_open,
    # 分镜
    "storyboard_set": _storyboard_set,
    "storyboard_get": _storyboard_get,
    # Entity 档案库
    "entity_register": _entity_register,
    "entity_add_view": _entity_add_view,
    "entity_get":      _entity_get,
    # 视觉生成
    "gen_image":         _gen_image,
    "gen_video_t2v":     _gen_video_t2v,
    "query_video_task":  _query_video_task,
    "cancel_video_task": _cancel_video_task,
    "sleep":             _sleep,
    # 视频处理
    "video_concat":    _video_concat,
    "video_crossfade": _video_crossfade,
    "video_trim":      _video_trim,
    "video_speed":     _video_speed,
    "video_overlay":   _video_overlay,
    "video_fade":      _video_fade,
    "video_portrait":  _video_portrait,
    # 音频
    "audio_amix":       _audio_amix,
    "tts":              _tts,
    "gen_audio_bgm":    _gen_audio_bgm,
    "gen_bgm":          _gen_audio_bgm,  # 别名兜底：agent 调旧名也能 work，避免脑补"未配置密钥"
    "query_audio_task": _query_audio_task,
    # 评估归档
    "extract_frames": _extract_frames,
    "vlm_understand": _vlm_understand,
    "burn_subtitle":  _burn_subtitle,
}


def inject_film_tools(handler):
    """把所有 film 工具挂到 handler 实例上。"""
    for name, fn in TOOL_REGISTRY.items():
        method = _wrap(name, fn)
        # 绑到实例
        import types
        setattr(handler, f"do_{name}", types.MethodType(method, handler))
    return handler
