"""项目工作区：manifest.json 读写 + 预算状态。

每个项目 = projects/<project_id>/  目录，结构：
  manifest.json          ← 项目状态（budget / entities / 元信息）
  storyboard.json        ← 分镜：title/duration/ratio/shots[]/entities_planned[]
  entities/              ← 角色/道具/场景的参考图（gen_image 中 name 以 ref_ 开头的落这里）
    ref_<主体>_<view>.png  ← 扁平命名，一张图一个文件：
                            ref_dancer_girl_front.png / ref_living_room_wide.png ...
                          （没有 ref.json 档案库、没有 per-entity 子目录——一个 entity 就是几张 ref_ 图，
                            agent 在上下文里自己记「主体名 → 图 url」，必要时 file_write 一个小 json 备查）
  shots/                 ← 关键帧（gen_image 非 ref_ 前缀）+ 镜头视频（gen_video_t2v）
  audios/                ← BGM / TTS 等音频产物（gen_audio_bgm 落盘到这里）
  composed/              ← 拼接/裁剪/字幕/audio_amix 等后期产物
  reviews/               ← vlm_understand 输出 + 抽帧
  logs/                  ← tool_calls.jsonl（每次调用都按时间戳留痕）
"""
import json
import os
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = ROOT / "projects"
PROJECTS_ROOT.mkdir(exist_ok=True)


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_id(brief: str) -> str:
    """从用户 brief 生成项目 ID：日期 + 简短描述"""
    import re
    short = re.sub(r"[^\w\u4e00-\u9fff]+", "_", brief.strip())[:20]
    return f"p{time.strftime('%Y%m%d_%H%M%S')}_{short}".rstrip("_")


def project_create(brief: str, max_seedance_calls: int = 0) -> dict:
    """创建一个新项目目录 + 初始 manifest。返回 manifest dict。

    max_seedance_calls=0（默认）= **无预算上限**（只记账不阻断），便于 agent 自由迭代。
    需要硬上限时显式传一个正整数。
    """
    pid = _safe_id(brief)
    pdir = PROJECTS_ROOT / pid
    if pdir.exists():
        raise FileExistsError(f"项目已存在：{pdir}")
    for sub in ("entities", "shots", "composed", "reviews", "logs", "audios"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    manifest = {
        "project_id": pid,
        "project_dir": str(pdir),
        "brief": brief,
        "created_at": _now_str(),
        "phases": {
            "story":   {"status": "pending"},
            "entity":  {"status": "pending"},
            "asset":   {"status": "pending", "shots_done": 0, "shots_total": 0},
            "animate": {"status": "pending", "shots_done": 0, "shots_total": 0},
            "compose": {"status": "pending"},
            "review":  {"status": "pending"},
        },
        "budget": {
            # 0 = 无上限（仅记账）；正整数 = 硬上限
            "max_seedance_calls": max_seedance_calls,
            "seedance_used": 0,
            "max_vlm_calls": 0,
            "vlm_used": 0,
        },
        "entities": {},
    }
    write_manifest(pid, manifest)
    return manifest


def project_dir(project_id: str) -> Path:
    p = PROJECTS_ROOT / project_id
    if not p.exists():
        raise FileNotFoundError(f"项目不存在：{p}")
    return p


def manifest_path(project_id: str) -> Path:
    return project_dir(project_id) / "manifest.json"


def read_manifest(project_id: str) -> dict:
    return json.loads(manifest_path(project_id).read_text(encoding="utf-8"))


def write_manifest(project_id: str, manifest: dict):
    p = PROJECTS_ROOT / project_id / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def update_phase(project_id: str, phase: str, **kwargs):
    """部分更新某 phase 的状态字段。"""
    m = read_manifest(project_id)
    m.setdefault("phases", {}).setdefault(phase, {}).update(kwargs)
    write_manifest(project_id, m)
    return m


def bump_budget(project_id: str, kind: str, n: int = 1) -> dict:
    """递增预算用量。kind ∈ seedance / vlm。返回更新后的 budget。

    纯记账，不阻断——上限字段仅作参考统计，超额不抛错（避免卡死返工/审核重做）。
    """
    m = read_manifest(project_id)
    key_used = f"{kind}_used"
    used = m["budget"].get(key_used, 0) + n
    m["budget"][key_used] = used
    write_manifest(project_id, m)
    return m["budget"]


def log_tool_call(project_id: Optional[str], tool: str, args: dict, result_brief: str):
    """把每次工具调用都追加到 logs/tool_calls.jsonl。"""
    if not project_id:
        return
    try:
        log_path = project_dir(project_id) / "logs" / "tool_calls.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now_str(),
                "tool": tool,
                "args": args,
                "result": result_brief[:300],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_seedance_call(project_id: Optional[str], detail: dict):
    """记录每次 gen_video_* 实际发给 Seedance 的输入清单（已经把 entity 名展开成具体 url）。
    专门用来事后审计：第 N 段视频到底参考了哪几张图、哪段视频。
    写到 logs/seedance_calls.jsonl。
    """
    if not project_id:
        return
    try:
        log_path = project_dir(project_id) / "logs" / "seedance_calls.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now_str(), **detail},
                               ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def log_model_call(project_id: Optional[str], model_kind: str, detail: dict):
    """统一记录所有"调用云端模型"的请求详情，给用户做审计/复盘用。

    model_kind ∈ {seedream, seedance, vlm_video, vlm_image, tts, gen_bgm}
    detail 应包含：name / prompt（完整不截断）/ 关键参数（duration、ratio、ref_*、size、voice、...）
                  + result_brief（task_id 或 url 等）

    写到 logs/model_calls.jsonl。这条日志是**人类可读的全量请求**，跟
    seedance_calls.jsonl（专项 Seedance 审计）和 tool_calls.jsonl（GA 工具调用）互补。
    """
    if not project_id:
        return
    try:
        log_path = project_dir(project_id) / "logs" / "model_calls.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now_str(),
                "model_kind": model_kind,
                **detail,
            }, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def get_active_project() -> Optional[str]:
    """读取 .active_project 标记文件，返回当前活跃项目 ID。"""
    f = PROJECTS_ROOT / ".active_project"
    if f.exists():
        pid = f.read_text(encoding="utf-8").strip()
        if (PROJECTS_ROOT / pid).exists():
            return pid
    return None


def set_active_project(project_id: str):
    (PROJECTS_ROOT / ".active_project").write_text(project_id, encoding="utf-8")
