"""项目工作区：manifest.json 读写 + 预算状态。

每个项目 = projects/<project_id>/  目录，结构：
  manifest.json          ← 项目状态（budget / entities / 元信息）
  director_plan.json     ← 全片唯一真相源：段清单 / 镜头节拍 / 资产对照 / 终版状态
  entities/              ← 角色/道具/场景等长期参考资产（gen_image category=entity）
  shots/                 ← 故事板/分镜草图图片、关键帧等镜头级素材（gen_image category=shot）+ 镜头视频（gen_video_t2v）
                          文件名由 agent 按项目语义自定，只要求稳定、可读、可回查；
                          工具层不规定固定后缀或固定命名模板。
  audios/                ← BGM 等音频产物（gen_audio_bgm 落盘到这里）
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
_SESSION_ACTIVE_PROJECT: Optional[str] = None


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


def list_projects(limit: int = 100) -> list[dict]:
    """返回可恢复的历史项目，按最近更新时间倒序。

    单个项目 manifest 损坏时跳过，避免一个旧目录拖垮整个项目管理页。
    """
    projects: list[dict] = []
    for pdir in PROJECTS_ROOT.iterdir():
        manifest_file = pdir / "manifest.json"
        if not pdir.is_dir() or not manifest_file.is_file():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            modified_at = manifest_file.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        phases = manifest.get("phases") if isinstance(manifest.get("phases"), dict) else {}
        phase_status = {
            str(name): str(value.get("status", "pending")) if isinstance(value, dict) else str(value)
            for name, value in phases.items()
        }
        projects.append({
            "project_id": manifest.get("project_id") or pdir.name,
            "brief": manifest.get("brief") or pdir.name,
            "created_at": manifest.get("created_at") or "",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(modified_at)),
            "phases": phase_status,
            "done_phases": sum(status == "done" for status in phase_status.values()),
            "total_phases": len(phase_status),
        })
    projects.sort(key=lambda item: item["updated_at"], reverse=True)
    return projects[:max(0, int(limit))]


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


def log_model_call(project_id: Optional[str], model_kind: str, detail: dict, raw_request: dict = None, raw_response: dict = None):
    """统一记录所有"调用云端模型"的请求详情，给用户做审计/复盘用。

    model_kind ∈ {seedream, seedance, vlm_video, vlm_image, gen_audio_bgm}
    detail 应包含：name / prompt（完整不截断）/ 关键参数（duration、ratio、ref_*、size、...）
                  + result_brief（task_id 或 url 等）
    raw_request: 实际发给 API 的原始 HTTP 请求体 (body/json)
    raw_response: API 返回的原始 HTTP 响应体

    写到 logs/model_calls.jsonl。这条日志是**人类可读的全量请求**，跟
    seedance_calls.jsonl（专项 Seedance 审计）和 tool_calls.jsonl（GA 工具调用）互补。
    """
    if not project_id:
        return
    try:
        log_path = project_dir(project_id) / "logs" / "model_calls.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            log_data = {
                "ts": _now_str(),
                "model_kind": model_kind,
                **detail,
            }
            if raw_request is not None:
                log_data["raw_request"] = raw_request
            if raw_response is not None:
                log_data["raw_response"] = raw_response
                
            f.write(json.dumps(log_data, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def get_active_project() -> Optional[str]:
    """返回当前进程会话显式选择的项目。

    不从 .active_project 自动恢复：新启动必须从空白画布开始。只有本会话调用
    project_create / project_open 后才会设置活跃项目，避免旧项目画布和日志串入新会话。
    """
    if _SESSION_ACTIVE_PROJECT and (PROJECTS_ROOT / _SESSION_ACTIVE_PROJECT).exists():
        return _SESSION_ACTIVE_PROJECT
    return None


def set_active_project(project_id: str):
    global _SESSION_ACTIVE_PROJECT
    _SESSION_ACTIVE_PROJECT = project_id
    # 仅记录最近使用项目，供显式恢复/排查；get_active_project 不会自动读取它。
    (PROJECTS_ROOT / ".active_project").write_text(project_id, encoding="utf-8")


def clear_active_project():
    """清空当前进程会话选择，不删除最近项目记录或任何项目文件。"""
    global _SESSION_ACTIVE_PROJECT
    _SESSION_ACTIVE_PROJECT = None
