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
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from . import video_models

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows local development fallback
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = Path(
    os.environ.get("VIBEFILMING_PROJECTS_ROOT") or ROOT / "projects"
).expanduser().resolve()
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
_SESSION_ACTIVE_PROJECT: Optional[str] = None

# manifest.json 串行化锁：Studio HTTP 线程（/api/upload）和 Agent 线程会同时
# "read → 改 → write" 同一个 manifest，无锁时后写者会整份覆盖前写者（丢预算/阶段状态）。
# 用 RLock 允许同线程重入：update_manifest 的 updater 里再调 read_manifest 不会自锁。
_manifest_lock = threading.RLock()
DEBUG_STOP_PHASES = ("breakdown", "story", "entity", "asset", "animate", "compose", "review")
_LEGACY_DEBUG_PHASES = {"plan": "entity", "storyboard": "asset"}
MAX_MATERIAL_BYTES = 500 * 1024 * 1024
MAX_REMAKE_SPEC_CHARS = 12000

DEFAULT_MATERIAL_TYPES = (
    {
        "id": "reference_video", "label": "参考视频", "media_kind": "video",
        "accept": ".mp4,.mov", "extensions": [".mp4", ".mov"],
        "max_bytes": 200 * 1024 * 1024, "max_items": 3, "entity_lock": False,
    },
    {
        "id": "product_image", "label": "产品图", "media_kind": "image",
        "accept": "image/*", "extensions": [".png", ".jpg", ".jpeg", ".webp", ".gif"],
        "max_bytes": 20 * 1024 * 1024, "max_items": 0, "entity_lock": True,
    },
    {
        "id": "character_image", "label": "人物图", "media_kind": "image",
        "accept": "image/*", "extensions": [".png", ".jpg", ".jpeg", ".webp", ".gif"],
        "max_bytes": 20 * 1024 * 1024, "max_items": 0, "entity_lock": True,
    },
    {
        "id": "reference_image", "label": "其他参考图", "media_kind": "image",
        "accept": "image/*", "extensions": [".png", ".jpg", ".jpeg", ".webp", ".gif"],
        "max_bytes": 20 * 1024 * 1024, "max_items": 0, "entity_lock": False,
    },
    {
        "id": "reference_audio", "label": "音频参考", "media_kind": "audio",
        "accept": ".mp3,.wav", "extensions": [".mp3", ".wav"],
        "max_bytes": 15 * 1024 * 1024, "max_items": 3, "entity_lock": False,
    },
    {
        "id": "document", "label": "脚本/文档", "media_kind": "document",
        "accept": ".pdf,.txt,.md,.docx,.pptx,.xlsx,.csv,.json,.srt",
        "extensions": [".pdf", ".txt", ".md", ".docx", ".pptx", ".xlsx", ".csv", ".json", ".srt"],
        "max_bytes": 50 * 1024 * 1024, "max_items": 0, "entity_lock": False,
    },
)

DEFAULT_PHASE_DEFINITIONS = (
    {"id": "breakdown", "label": "拆镜"},
    {"id": "story", "label": "剧本"},
    {"id": "entity", "label": "编导方案"},
    {"id": "asset", "label": "参考资产与故事板"},
    {"id": "animate", "label": "视频段"},
    {"id": "compose", "label": "合成"},
    {"id": "review", "label": "审查"},
)

DEFAULT_CANVAS_STAGES = (
    {"id": "breakdown", "label": "拆镜"},
    {"id": "story", "label": "剧本"},
    {"id": "plan", "label": "编导方案"},
    {"id": "assets", "label": "参考资产"},
    {"id": "storyboard", "label": "故事板"},
    {"id": "video", "label": "视频段"},
    {"id": "review", "label": "审查"},
    {"id": "compose", "label": "合成成片"},
)


def _definition_list(value, fallback) -> list[dict]:
    """Normalize manifest-provided UI definitions while preserving declared order."""
    source = value if isinstance(value, list) else fallback
    result: list[dict] = []
    seen: set[str] = set()
    for raw in source:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id", "")).strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        item = dict(raw)
        item["id"] = item_id
        item["label"] = str(item.get("label") or item_id)
        result.append(item)
    return result


def material_type_definitions(manifest: dict | None = None) -> list[dict]:
    configured = (manifest or {}).get("material_types")
    definitions = _definition_list(configured, DEFAULT_MATERIAL_TYPES)
    profile = video_models.resolve_manifest(manifest)
    seedance_defaults = {
        item["id"]: dict(item)
        for item in DEFAULT_MATERIAL_TYPES
        if item["id"] in {"reference_video", "reference_audio"}
    }
    seedance_defaults["reference_video"]["max_items"] = profile.max_videos
    seedance_defaults["reference_audio"]["max_items"] = profile.max_audios
    for item in definitions:
        if item["id"] in seedance_defaults:
            item.update(seedance_defaults[item["id"]])
        media_kind = str(item.get("media_kind") or "file")
        item["media_kind"] = media_kind if media_kind in {"video", "image", "audio", "document", "file"} else "file"
        item["accept"] = str(item.get("accept") or "")
        item["extensions"] = [
            str(ext).lower() for ext in (item.get("extensions") or []) if str(ext)
        ]
        try:
            item["max_bytes"] = min(
                MAX_MATERIAL_BYTES,
                max(1, int(item.get("max_bytes") or 100 * 1024 * 1024)),
            )
        except (TypeError, ValueError):
            item["max_bytes"] = 100 * 1024 * 1024
        try:
            item["max_items"] = max(0, int(item.get("max_items") or 0))
        except (TypeError, ValueError):
            item["max_items"] = 0
        item["entity_lock"] = bool(item.get("entity_lock", False))
    known = {item["id"] for item in definitions}
    for category in ((manifest or {}).get("materials") or {}):
        category = str(category).strip()
        if category and category not in known:
            definitions.append({
                "id": category, "label": category, "media_kind": "file",
                "accept": "", "extensions": [], "max_bytes": 100 * 1024 * 1024,
                "max_items": 0, "entity_lock": False,
            })
            known.add(category)
    return definitions


def workflow_phase_definitions(manifest: dict | None = None) -> list[dict]:
    workflow = (manifest or {}).get("workflow") or {}
    definitions = _definition_list(workflow.get("phases"), DEFAULT_PHASE_DEFINITIONS)
    known = {item["id"] for item in definitions}
    for phase in ((manifest or {}).get("phases") or {}):
        phase = str(phase).strip()
        if phase and phase not in known:
            definitions.append({"id": phase, "label": phase})
            known.add(phase)
    return definitions


def canvas_stage_definitions(manifest: dict | None = None) -> list[dict]:
    workflow = (manifest or {}).get("workflow") or {}
    return _definition_list(workflow.get("stages"), DEFAULT_CANVAS_STAGES)


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def normalize_remake_spec(value) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_REMAKE_SPEC_CHARS:
        raise ValueError(
            f"复刻规范最多 {MAX_REMAKE_SPEC_CHARS} 字，当前为 {len(text)} 字")
    return text


def _safe_id(brief: str) -> str:
    """从用户 brief 生成项目 ID：日期 + 简短描述"""
    import re
    short = re.sub(r"[^\w\u4e00-\u9fff]+", "_", brief.strip())[:20]
    return f"p{time.strftime('%Y%m%d_%H%M%S')}_{short}".rstrip("_")


def _validate_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    raw = Path(value)
    if (
        not value
        or raw.is_absolute()
        or len(raw.parts) != 1
        or raw.name in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", value)
    ):
        raise ValueError(f"非法 project_id：{project_id!r}")
    return value


def project_create(brief: str, max_seedance_calls: int = 0,
                   execution_mode: str | None = None,
                   project_id: str | None = None,
                   remake_spec: str = "",
                   video_model: str | None = None) -> dict:
    """创建一个新项目目录 + 初始 manifest。返回 manifest dict。

    max_seedance_calls=0（默认）= **无预算上限**（只记账不阻断），便于 agent 自由迭代。
    需要硬上限时显式传一个正整数。
    """
    from . import execution_mode as execution

    execution_state = execution.state(
        execution_mode, source="explicit" if execution_mode is not None else "default")
    selected_video_model = video_models.resolve(
        video_model, strict=video_model is not None)
    pid = _validate_project_id(project_id) if project_id else _safe_id(brief)
    pdir = PROJECTS_ROOT / pid
    try:
        pdir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(f"项目已存在：{pdir}")
    try:
        for sub in ("entities", "shots", "composed", "reviews", "logs", "audios", "materials"):
            (pdir / sub).mkdir()
        manifest = {
        "project_id": pid,
        "project_dir": str(pdir),
        "brief": brief,
        "remake_spec": normalize_remake_spec(remake_spec),
        "created_at": _now_str(),
        "execution_mode": execution_state,
        "video_model": selected_video_model.model_id,
        "material_types": material_type_definitions({
            "video_model": selected_video_model.model_id,
        }),
        "workflow": {
            "phases": [dict(item) for item in DEFAULT_PHASE_DEFINITIONS],
            "stages": [dict(item) for item in DEFAULT_CANVAS_STAGES],
        },
        "phases": {
            "breakdown": {"status": "pending"},   # 参考视频逆向拆镜（写剧本之前）
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
    except Exception:
        shutil.rmtree(pdir, ignore_errors=True)
        raise
    return manifest


def project_dir(project_id: str) -> Path:
    safe_id = _validate_project_id(project_id)
    root = PROJECTS_ROOT.resolve()
    p = (root / safe_id).resolve()
    try:
        p.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"project_id 越出会话项目根目录：{project_id!r}") from exc
    if not p.exists():
        raise FileNotFoundError(f"项目不存在：{p}")
    return p


def manifest_path(project_id: str) -> Path:
    return project_dir(project_id) / "manifest.json"


@contextmanager
def _manifest_file_lock(project_id: str, exclusive: bool):
    lock_path = project_dir(project_id) / ".manifest.lock"
    with lock_path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def project_operation_lock(project_id: str, name: str):
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("._")
    if not safe_name:
        raise ValueError("操作锁名称不能为空")
    lock_path = project_dir(project_id) / "logs" / f".{safe_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_manifest_unlocked(project_id: str) -> dict:
    return json.loads(manifest_path(project_id).read_text(encoding="utf-8"))


def _write_manifest_unlocked(project_id: str, manifest: dict):
    target = project_dir(project_id) / "manifest.json"
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".manifest.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def read_manifest(project_id: str) -> dict:
    with _manifest_lock, _manifest_file_lock(project_id, exclusive=False):
        return _read_manifest_unlocked(project_id)


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
        try:
            phases = manifest.get("phases") if isinstance(manifest.get("phases"), dict) else {}
            phase_status = {
                str(name): str(value.get("status", "pending")) if isinstance(value, dict) else str(value)
                for name, value in phases.items()
            }
            projects.append({
                "project_id": manifest.get("project_id") or pdir.name,
                "brief": manifest.get("brief") or pdir.name,
                "remake_spec": normalize_remake_spec(
                    manifest.get("remake_spec", "")),
                "created_at": manifest.get("created_at") or "",
                "updated_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(modified_at)),
                "phases": phase_status,
                "done_phases": sum(
                    status == "done" for status in phase_status.values()),
                "total_phases": len(phase_status),
                "execution_mode": execution_state_from_manifest(manifest),
                "video_model": video_models.state(manifest),
            })
        except (TypeError, ValueError):
            continue
    projects.sort(key=lambda item: item["updated_at"], reverse=True)
    return projects[:max(0, int(limit))]


def write_manifest(project_id: str, manifest: dict):
    with _manifest_lock, _manifest_file_lock(project_id, exclusive=True):
        _write_manifest_unlocked(project_id, manifest)


def update_manifest(project_id: str, updater: Callable[[dict], None]) -> dict:
    """以 "read → mutate → write" 为一个原子事务更新 manifest，返回更新后的 dict。

    updater 就地修改传入的 manifest dict（返回值忽略）。所有跨线程的
    读改写路径都必须走这里，否则并发下会整份覆盖别人的改动。
    updater 抛错时不落盘，manifest 保持原样。
    """
    with _manifest_lock, _manifest_file_lock(project_id, exclusive=True):
        m = _read_manifest_unlocked(project_id)
        updater(m)
        _write_manifest_unlocked(project_id, m)
        return m


def set_remake_spec(project_id: str, value) -> str:
    normalized = normalize_remake_spec(value)
    update_manifest(
        project_id,
        lambda manifest: manifest.__setitem__("remake_spec", normalized),
    )
    return normalized


def video_model_state(manifest: dict | None = None) -> dict:
    return video_models.state(manifest)


def set_video_model(project_id: str, selection: str | None) -> dict:
    profile = video_models.resolve(selection, strict=True)

    def _apply(manifest: dict):
        manifest["video_model"] = profile.model_id
        manifest["material_types"] = material_type_definitions({
            **manifest,
            "video_model": profile.model_id,
        })

    manifest = update_manifest(project_id, _apply)
    return video_models.state(manifest)


def execution_state_from_manifest(manifest: dict) -> dict:
    """Read current mode; legacy projects remain human-gated."""
    from . import execution_mode as execution

    current = manifest.get("execution_mode")
    raw_mode = current.get("mode") if isinstance(current, dict) else current
    if not raw_mode:
        return execution.state(execution.HUMAN_GATED, source="legacy_default")
    try:
        normalized = execution.state(raw_mode, source=(
            current.get("source", "project") if isinstance(current, dict) else "project"))
    except ValueError:
        normalized = execution.state(execution.HUMAN_GATED, source="invalid_fallback")
    if isinstance(current, dict) and current.get("updated_at"):
        normalized["updated_at"] = current["updated_at"]
    return normalized


def normalize_execution_mode(project_id: str) -> dict:
    """Persist a canonical state for legacy or partially written manifests."""
    current_manifest = read_manifest(project_id)
    normalized = execution_state_from_manifest(current_manifest)
    if current_manifest.get("execution_mode") != normalized:
        update_manifest(project_id, lambda m: m.__setitem__("execution_mode", normalized))
    return normalized


def set_execution_mode(project_id: str, mode: str, *, source: str = "user") -> dict:
    from . import execution_mode as execution

    selected = execution.state(mode, source=source)
    selected["updated_at"] = _now_str()

    def _apply(manifest: dict):
        manifest["execution_mode"] = selected
        if selected["mode"] == execution.AUTONOMOUS:
            for phase in (manifest.get("phases") or {}).values():
                if not isinstance(phase, dict):
                    continue
                note = str(phase.get("note") or "")
                if "awaiting_human_approval" in note:
                    phase["note"] = "execution_mode=autonomous; continue after machine validation"
        else:
            approvals = manifest.setdefault("execution_approvals", {})
            breakdown = (manifest.get("phases") or {}).get("breakdown") or {}
            materials = manifest.get("materials") or {}
            needs_breakdown = (
                bool(materials.get("reference_video"))
                or breakdown.get("source") == "reference_video"
            )
            if needs_breakdown and breakdown.get("status") == "done":
                current = approvals.get("breakdown_approval") or {}
                if current.get("actor") != "human":
                    approvals["breakdown_approval"] = {
                        "status": "pending",
                        "actor": "system",
                        "evidence": "执行模式切换为人工介入，需要人工确认现有拆镜",
                        "updated_at": _now_str(),
                    }
            script = approvals.get("script_approval") or {}
            if script.get("status") == "approved" and script.get("actor") != "human":
                approvals["script_approval"] = {
                    "status": "pending",
                    "actor": "system",
                    "evidence": "执行模式切换为人工介入，需要人工确认现有剧本",
                    "updated_at": _now_str(),
                }

    update_manifest(project_id, _apply)
    return selected


def execution_gate_state(manifest: dict, gate: str) -> dict:
    value = (manifest.get("execution_approvals") or {}).get(gate)
    return dict(value) if isinstance(value, dict) else {"status": "missing"}


def set_execution_gate(project_id: str, gate: str, status: str, *,
                       actor: str, evidence: str = "") -> dict:
    from . import execution_mode as execution

    if gate not in execution.HUMAN_GATES:
        raise ValueError(f"未知执行闸门：{gate}")
    if status not in ("pending", "approved"):
        raise ValueError(f"闸门状态只支持 pending/approved：{status}")
    value = {
        "status": status,
        "actor": actor,
        "evidence": str(evidence or ""),
        "updated_at": _now_str(),
    }

    def _apply(manifest: dict):
        manifest.setdefault("execution_approvals", {})[gate] = value
        if gate == "breakdown_approval" and status == "approved":
            manifest.setdefault("execution_approvals", {}).setdefault(
                "script_approval", {
                    "status": "pending",
                    "actor": "system",
                    "evidence": "formal script must be reviewed after it is written",
                    "updated_at": _now_str(),
                })

    update_manifest(project_id, _apply)
    return value


def execution_gate_approved(project_id: str, gate: str) -> bool:
    return execution_gate_state(read_manifest(project_id), gate).get("status") == "approved"


def update_phase(project_id: str, phase: str, **kwargs):
    """部分更新某 phase 的状态字段，并自动维护耗时时间戳。

    状态进入 in_progress/running 时记 started_at；进入 done/failed/blocked 时记
    finished_at 并按 started_at 计算 duration_s。改状态前先调用本函数打时间戳，
    供画布状态条与耗时复盘使用。
    """
    def _apply(m: dict):
        ph = m.setdefault("phases", {}).setdefault(phase, {})
        old_status = ph.get("status")
        new_status = kwargs.get("status")
        ph.update(kwargs)
        _stamp_phase_timing(ph, old_status, new_status)

    return update_manifest(project_id, _apply)


def normalize_debug_state(project_id: str) -> dict:
    """把历史 debug.stop_after 迁移到 manifest 的真实阶段名。"""
    current = read_manifest(project_id).get("debug") or {}
    stop_after = _LEGACY_DEBUG_PHASES.get(current.get("stop_after"), current.get("stop_after"))
    if stop_after not in DEBUG_STOP_PHASES:
        stop_after = "asset"
    normalized = {"enabled": bool(current.get("enabled", False)), "stop_after": stop_after}
    if current != normalized:
        update_manifest(project_id, lambda manifest: manifest.__setitem__("debug", normalized))
    return normalized


def _stamp_phase_timing(phase: dict, old_status, new_status) -> None:
    """按状态迁移打时间戳；done/failed/blocked 且无 started_at 时用 finished 时刻兜底。"""
    if new_status in (None, old_status):
        return
    now = _now_str()
    if new_status in ("in_progress", "running"):
        phase.setdefault("started_at", now)
    elif new_status in ("done", "failed", "blocked"):
        started = phase.get("started_at")
        if not started:
            started = phase["started_at"] = now
        phase["finished_at"] = now
        try:
            start_t = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
            end_t = time.mktime(time.strptime(now, "%Y-%m-%d %H:%M:%S"))
            phase["duration_s"] = max(0, round(end_t - start_t, 1))
        except ValueError:
            pass


def bump_budget(project_id: str, kind: str, n: int = 1) -> dict:
    """递增预算用量。kind ∈ seedance / vlm。返回更新后的 budget。

    纯记账，不阻断——上限字段仅作参考统计，超额不抛错（避免卡死返工/审核重做）。
    """
    def _apply(m: dict):
        key_used = f"{kind}_used"
        m["budget"][key_used] = m["budget"].get(key_used, 0) + n

    return update_manifest(project_id, _apply)["budget"]


def log_tool_call(project_id: Optional[str], tool: str, args: dict, result_brief: str,
                  duration_s: float | None = None):
    """把每次工具调用都追加到 logs/tool_calls.jsonl（含耗时）。"""
    if not project_id:
        return
    try:
        log_path = project_dir(project_id) / "logs" / "tool_calls.jsonl"
        entry: dict = {
            "ts": _now_str(),
            "tool": tool,
            "args": args,
            "result": result_brief[:300],
        }
        if duration_s is not None:
            entry["duration_s"] = round(duration_s, 2)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
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


def _compact_model_media(value):
    """保留完整提示词与参数，以长度和摘要记录内嵌媒体，避免日志复制数百 MB。"""
    if isinstance(value, dict):
        return {k: _compact_model_media(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compact_model_media(v) for v in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        prefix, data = value.split(",", 1)
        return {
            "inline_media": prefix,
            "base64_length": len(data),
            "sha256_base64": hashlib.sha256(data.encode()).hexdigest(),
        }
    return value


def log_model_call(project_id: Optional[str], model_kind: str, detail: dict, raw_request: dict = None,
                   raw_response: dict = None, duration_s: float | None = None):
    """统一记录所有"调用云端模型"的请求详情，给用户做审计/复盘用。

    model_kind ∈ {seedream, seedance, vlm_video, vlm_image, gen_audio_bgm}
    detail 应包含：name / prompt（完整不截断）/ 关键参数（duration、ratio、ref_*、size、...）
                  + result_brief（task_id 或 url 等）
    raw_request: 实际发给 API 的原始 HTTP 请求体 (body/json)
    raw_response: API 返回的原始 HTTP 响应体
    duration_s: 本次调用耗时（秒），有就写进日志，用于复盘各环节耗时

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
            if duration_s is not None:
                log_data["duration_s"] = round(duration_s, 2)
            if raw_request is not None:
                log_data["raw_request"] = raw_request
            if raw_response is not None:
                log_data["raw_response"] = raw_response
                
            f.write(json.dumps(_compact_model_media(log_data), ensure_ascii=False, default=str) + "\n")
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
