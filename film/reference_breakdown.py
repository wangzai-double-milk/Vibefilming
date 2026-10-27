"""参考视频拆镜：素材收录 + VLM 逆向拆镜 + 拆镜驱动全流程。

三个工具（都用 @film_tool 声明，导入本模块即自动注册进 schema）：
  upload_reference_material  把用户上传的参考视频/产品图/人物图收录进 projects/<pid>/materials/
  analyze_reference_video    调 VLM 把参考视频拆成 Markdown 分镜表（写剧本之前的分析步骤），
                             落拆镜产物 + materials/script_from_breakdown.md 草稿，并登记写剧本模式
  inject_breakdown_to_plan   把拆镜结果注入 director_plan.json 的 segments[]

VLM 路径：一次多模态调用观察原片并输出分镜和结构分析；程序只做解析与校验。
仅在显式配置 DASHSCOPE_API_KEY 时启用备用多模态提供方。

产物：
  materials/storyboard_from_reference.md   VLM 原始 Markdown 分镜表（人类可读）
  materials/拆片表.md                        多模态观察表（分镜 + 镜头信息 + 全局结构，人类可读）
  materials/reference_breakdown.json       解析后的结构化 segments（机器可读）
  materials/script_from_breakdown.md       拆镜脚本草稿（front matter 标注 source/mode/draft），
                                           正式 script.md 由剧本阶段按 mode 创建
"""
from __future__ import annotations
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests

from . import film_sdk as sdk
from . import artifact_contracts
from . import workspace as ws
from .tools import film_tool, _active_pid, _DECORATED_TOOLS

MATERIALS_DIR_NAME = "materials"
BREAKDOWN_JSON_FILENAME = "reference_breakdown.json"
BREAKDOWN_MD_FILENAME = "storyboard_from_reference.md"

QWEN_VL_MODEL = "qwen-vl-max-latest"
QWEN_VL_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
# Qwen 兜底只支持 URL 或 <100MB 的本地视频（base64 内嵌），更大的本地文件直接报错让用户给 URL
QWEN_INLINE_MAX_BYTES = 100 * 1024 * 1024

_BREAKDOWN_PROMPT_PATH = Path(__file__).with_name(
    "reference_breakdown_prompt.md")
_PROMPT_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)


def _qwen_model() -> str:
    return os.environ.get("QWEN_VL_MODEL", QWEN_VL_MODEL).strip() or QWEN_VL_MODEL


def _qwen_endpoint() -> str:
    return (
        os.environ.get("QWEN_VL_ENDPOINT", QWEN_VL_ENDPOINT).strip()
        or QWEN_VL_ENDPOINT
    )


class BreakdownParseError(Exception):
    """拆镜结果解析/校验失败（供上层切兜底 provider 或直接报错）。"""


def _load_breakdown_prompt() -> str:
    """读取执行器自带的模型合同，不依赖任一业务 Skill 的安装路径。"""
    return _BREAKDOWN_PROMPT_PATH.read_text(encoding="utf-8")


def _analysis_signature() -> str:
    settings = {
        "prompt": _load_breakdown_prompt(),
        "model": sdk.get_model("vlm", sdk.MODEL_VLM),
        "fps": 4,
        "format_version": 2,
    }
    return hashlib.sha256(json.dumps(settings, ensure_ascii=False).encode()).hexdigest()


# ============== 列名归一 ==============
_FIELD_ALIASES = {
    "shot": ("分镜", "镜号", "镜头", "shot"),
    "shot_size": ("景别", "shot_size"),
    "camera": ("运镜", "camera"),
    "content": ("画面内容", "content"),
    "shooting": ("拍摄指导", "shooting"),
    "onscreen_text": ("画面文案/花字", "画面文案", "花字", "onscreen_text"),
    "dialogue": ("口播/台词", "口播", "台词", "dialogue"),
    "audio": ("音效/BGM", "音效", "BGM", "audio"),
    "start": ("开始时间", "start"),
    "end": ("结束时间", "end"),
    "duration": ("时长", "duration"),
}


def _field(seg: dict, key: str) -> str:
    """按别名表取字段值；缺失或占位符「-」都返回空串。"""
    for alias in _FIELD_ALIASES.get(key, (key,)):
        if alias in seg and seg[alias] is not None:
            value = str(seg[alias]).strip()
            if value and value not in ("-", "—", "无", "N/A"):
                return value
    return ""


def _norm_time(value: str) -> str:
    """保留毫秒表示精度；表示精度不等于视觉观察精度。"""
    secs = _to_seconds(value)
    if secs is None:
        return str(value).strip()
    millis = round(secs * 1000)
    whole, fraction = divmod(millis, 1000)
    suffix = f".{fraction:03d}".rstrip("0") if fraction else ""
    prefix = f"{whole // 3600:02d}:" if whole >= 3600 else ""
    return f"{prefix}{whole // 60 % 60:02d}:{whole % 60:02d}{suffix}"


def _to_seconds(value) -> Optional[float]:
    """时间/时长字符串 → 秒。无法识别返回 None。"""
    text = str(value if value is not None else "").strip().replace("，", "").replace(",", "")
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


# ============== 多模态观察的本地格式化（保留已有产物字段兼容性） ==============
SHOT_INFO_KEYS = ("shotAngle", "shotSize", "shotCameraMovement", "expressionStyle", "expressionPurpose")
# 拆片表中文表头：shot_info 5 字段 + script_summary 6 字段
_SHOT_INFO_LABELS = {
    "shotAngle": "机位角度",
    "shotSize": "景别",
    "shotCameraMovement": "运镜",
    "expressionStyle": "表达方式",
    "expressionPurpose": "表达目的",
}
_SUMMARY_LABELS = {
    "scriptStructureFormula": "结构公式",
    "scriptHookForm": "钩子形式",
    "scriptMainScenarios": "主打场景",
    "scriptUserPainPoints": "用户痛点",
    "scriptProductSellingPoints": "产品卖点",
    "scriptTargetAudience": "目标人群",
}
BREAKDOWN_TABLE_FILENAME = "拆片表.md"


def _observation_json(raw_text: str) -> dict:
    text = raw_text.strip()
    match = _PROMPT_BLOCK_RE.fullmatch(text)
    if match:
        text = match.group(1).strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _build_narration(segments: list[dict]) -> str:
    """各镜台词按 [MM:SS] 拼接旁白（med-ai-factory buildNarrationFromDialogues 移植）。

    时间戳取分镜开始时间，不同分镜间换行。
    """
    lines = []
    for seg in segments:
        text = _field(seg, "dialogue")
        if not text:
            continue
        start = _norm_time(_field(seg, "start"))
        if start:
            lines.append(f"[{start}] {text}")
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    """把内容压成单元格安全文本：去换行、转义竖线，空值填「-」。"""
    text = str(value or "").strip().replace("\n", " ").replace("|", "\\|")
    return text or "-"


def _build_breakdown_table(
    segments: list[dict],
    *,
    source_video: str,
    mode: str,
    total_duration: float,
    script_summary: dict,
) -> str:
    """把拆镜段 + shot_info + script_summary 汇成一张人类可读的拆片表 Markdown。

    把一次多模态观察中的镜头信息与全局结构渲染为中文表格。
    缺失字段保留空值，不补充推断。
    """
    lines = [
        "# 参考视频拆片表",
        "",
        f"- 来源视频：{source_video or '-'}",
        f"- 写剧本模式：{mode or 'pending（待用户选 仿写 imitate / 裂变 fission）'}",
        f"- 镜头数：{len(segments)}　总时长：{total_duration}s",
        "",
    ]

    if script_summary:
        lines.append("## 全局脚本分析")
        lines.append("")
        for key, label in _SUMMARY_LABELS.items():
            value = script_summary.get(key)
            if value:
                lines.append(f"- **{label}**：{value}")
        lines.append("")
    else:
        lines.append("> 全局脚本分析未生成（逐镜分析未全部成功，符合口径）。")
        lines.append("")

    lines.append("## 分镜表")
    lines.append("")
    header = [
        "镜号", "开始", "结束", "时长",
        "景别", "机位角度", "运镜",
        "画面内容", "拍摄指导", "口播/台词", "画面文案", "音效/BGM",
        "表达方式", "表达目的",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for i, seg in enumerate(segments, start=1):
        info = seg.get("shot_info") or {}
        row = [
            str(i),
            _norm_time(_field(seg, "start")),
            _norm_time(_field(seg, "end")),
            _field(seg, "duration"),
            _md_cell(info.get("shotSize") or _field(seg, "shot_size")),
            _md_cell(info.get("shotAngle")),
            _md_cell(info.get("shotCameraMovement") or _field(seg, "camera")),
            _md_cell(_field(seg, "content")),
            _md_cell(_field(seg, "shooting")),
            _md_cell(_field(seg, "dialogue")),
            _md_cell(_field(seg, "onscreen_text")),
            _md_cell(_field(seg, "audio")),
            _md_cell(info.get("expressionStyle")),
            _md_cell(info.get("expressionPurpose")),
        ]
        lines.append("| " + " | ".join(_md_cell(c) for c in row) + " |")
    lines.append("")
    return "\n".join(lines)


# ============== 工具：素材收录 ==============
def _validate_remote_material_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("远程素材只支持有效的 HTTP(S) URL")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0].split("%", 1)[0])
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or (
                    443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError) as exc:
        raise ValueError(f"远程素材域名无法安全解析：{parsed.hostname}") from exc
    if not addresses or any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    ):
        raise ValueError("远程素材 URL 不允许访问本机、内网或保留地址")


@film_tool(
    name="upload_reference_material",
    desc="把用户素材收录进 projects/<pid>/materials/ 并登记 manifest。本地路径和 HTTP(S)/TOS 预签名 URL 均可；可用 category 由当前项目 manifest.material_types 声明。",
    params={
        "file_path": {"type": str, "description": "素材本地路径或 HTTP(S)/TOS 预签名 URL"},
        "category": {"type": "string",
                     "description": "素材类型 ID，必须取当前项目 manifest.material_types[].id"},
        "entity_name": {"type": str, "description": "[图片时可选] 该图对应的角色/产品实体名，给了就会硬锁为该实体的唯一参考图", "default": ""},
    },
    required=["file_path", "category"],
)
def _upload_reference_material(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    category = str(args.get("category", "")).strip()
    manifest = ws.read_manifest(pid)
    material_types = {item["id"]: item for item in ws.material_type_definitions(manifest)}
    material_type = material_types.get(category)
    if not material_type:
        allowed = " / ".join(material_types)
        raise ValueError(f"category 非法：{category}（当前项目支持：{allowed}）")
    raw_source = str(args.get("file_path", "")).strip()
    if not raw_source:
        raise ValueError("file_path 必填")
    max_bytes = int(material_type.get("max_bytes") or ws.MAX_MATERIAL_BYTES)
    temporary_source = None
    if raw_source.startswith(("http://", "https://")):
        filename = Path(unquote(urlsplit(raw_source).path)).name or "remote_material.bin"
        suffix = Path(filename).suffix
        handle = tempfile.NamedTemporaryFile(
            prefix="vibefilming_material_", suffix=suffix, delete=False)
        temporary_source = Path(handle.name)
        size = 0
        try:
            current_url = raw_source
            response = None
            for _ in range(6):
                _validate_remote_material_url(current_url)
                response = requests.get(
                    current_url,
                    stream=True,
                    timeout=180,
                    allow_redirects=False,
                )
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location", "")
                    response.close()
                    if not location:
                        raise ValueError("远程素材重定向缺少 Location")
                    current_url = urljoin(current_url, location)
                    continue
                break
            else:
                raise ValueError("远程素材重定向次数超过 5 次")
            with handle, response:
                response.raise_for_status()
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(
                            f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
                    handle.write(chunk)
            src = temporary_source
        except Exception:
            if not handle.closed:
                handle.close()
            if response is not None:
                response.close()
            temporary_source.unlink(missing_ok=True)
            raise
    else:
        src = Path(raw_source).expanduser()
        filename = src.name
        if not src.is_file():
            raise FileNotFoundError(f"素材文件不存在：{src}")
    extensions = {
        str(ext).lower() for ext in (material_type.get("extensions") or []) if str(ext)
    }
    try:
        if extensions and Path(filename).suffix.lower() not in extensions:
            raise ValueError(
                f"{material_type.get('label', category)}不支持该文件格式："
                f"{Path(filename).suffix}")
        if src.stat().st_size > max_bytes:
            raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
        max_items = int(material_type.get("max_items") or 0)
        current_items = ((manifest.get("materials") or {}).get(category) or [])
        if max_items and len(current_items) >= max_items:
            raise ValueError(
                f"{material_type.get('label', category)}最多上传 {max_items} 个")

        pdir = ws.project_dir(pid)
        materials_path = _copy_no_overwrite(
            src, pdir / MATERIALS_DIR_NAME, filename)
        if (
            raw_source.startswith(("http://", "https://"))
            and material_type.get("media_kind") in {"video", "audio"}
        ):
            materials_path.with_suffix(".url.txt").write_text(
                raw_source, encoding="utf-8")

        entity_name = str(args.get("entity_name", "")).strip()
        if not material_type.get("entity_lock"):
            entity_name = ""
        entity_locked = False
        entity_path = None
        if material_type.get("entity_lock") and entity_name:
            entity_path = _copy_no_overwrite(
                src,
                pdir / "entities",
                f"{entity_name}_{Path(filename).stem}{Path(filename).suffix}",
            )
            entity_locked = True

        def _apply(m: dict):
            m.setdefault("materials", {}).setdefault(category, []).append(
                str(materials_path))
            if entity_locked:
                entity = m.setdefault("entities", {}).setdefault(entity_name, {})
                ref_pack = entity.setdefault("ref_pack", [])
                if str(entity_path) not in ref_pack:
                    ref_pack.append(str(entity_path))
                source_materials = entity.setdefault("source_materials", [])
                if str(materials_path) not in source_materials:
                    source_materials.append(str(materials_path))
                entity["locked_from_upload"] = True
                entity.setdefault("source_material", str(materials_path))

        ws.update_manifest(pid, _apply)
        if entity_locked:
            _lock_entities_no_new_rule(pid)

        return {
            "ok": True,
            "materials_path": str(materials_path),
            "source_url": raw_source if raw_source.startswith(("http://", "https://")) else "",
            "entity_name": entity_name,
            "entity_locked": entity_locked,
        }
    finally:
        if temporary_source is not None:
            temporary_source.unlink(missing_ok=True)


def _copy_no_overwrite(src: Path, dest_dir: Path, filename: str) -> Path:
    """copy 到 dest_dir，同名时加 _1/_2 后缀，绝不覆盖既有素材。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(str(filename)).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError(f"素材文件名非法：{filename!r}")
    dest = dest_dir / safe_name
    stem, suffix = dest.stem, dest.suffix
    i = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{i}{suffix}"
        i += 1
    shutil.copy2(src, dest)
    return dest


# ============== 工具：参考视频拆镜（写剧本之前的逆向拆解步骤） ==============
BREAKDOWN_MODES = ("imitate", "fission")   # 仿写 / 裂变


def _sync_breakdown_execution_gate(pid: str, evidence: str, *,
                                   reset_human_gate: bool = False) -> dict:
    manifest = ws.read_manifest(pid)
    mode = ws.execution_state_from_manifest(manifest)["mode"]
    current = ws.execution_gate_state(manifest, "breakdown_approval")
    if reset_human_gate:
        ws.set_execution_gate(
            pid, "script_approval", "pending",
            actor="system", evidence="拆镜或创作模式已变化，旧剧本确认失效")
    if mode == "autonomous":
        return ws.set_execution_gate(
            pid, "breakdown_approval", "approved",
            actor="machine", evidence=evidence)
    if reset_human_gate or current.get("status") == "missing":
        return ws.set_execution_gate(
            pid, "breakdown_approval", "pending",
            actor="system", evidence=evidence)
    return current


@film_tool(
    name="analyze_reference_video",
    desc="一次多模态分析参考视频的分镜、动作、节奏与结构；输出 reference_breakdown.json、拆片表.md 和脚本草稿。程序校验时间轴，不调用纯文本模型补造视觉事实。mode=imitate 仿写或 fission 裂变；人工介入由项目 execution_mode 控制。可能耗时数分钟，调用前告知用户。",
    params={
        "video_path": {"type": str, "description": "参考视频路径或 URL；留空则自动取 manifest.materials.reference_video[0]", "default": ""},
        "mode": {"type": "string", "enum": ["", "imitate", "fission"],
                 "description": "imitate 保留参考结构换内容；fission 衍生变体。留空继承项目选择；端到端模式默认 fission 并记录。", "default": ""},
        "force": {"type": bool, "description": "强制重新分析。来源、规则或模型变化也会自动重拆。", "default": False},
    },
)
def _analyze_reference_video(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    pdir = ws.project_dir(pid)
    materials_dir = pdir / MATERIALS_DIR_NAME
    breakdown_path = materials_dir / BREAKDOWN_JSON_FILENAME
    mode = str(args.get("mode", "")).strip().lower()
    if mode not in ("", *BREAKDOWN_MODES):
        raise ValueError(f"mode 非法：{mode}（只支持 imitate=仿写 / fission=裂变，或留空表示待用户确认）")
    force = bool(args.get("force", False))
    manifest = ws.read_manifest(pid)
    mode = mode or str((manifest.get("phases", {}).get("story") or {}).get("mode") or "")
    if mode not in BREAKDOWN_MODES:
        mode = ("fission" if ws.execution_state_from_manifest(manifest).get("mode")
                == "autonomous" else "")
    video_path = str(args.get("video_path", "")).strip()
    if not video_path:
        refs = (manifest.get("materials") or {}).get("reference_video") or []
        if not refs:
            raise ValueError("未指定 video_path，且 manifest.materials.reference_video 为空："
                             "请先调用 upload_reference_material 收录参考视频")
        video_path = str(refs[0])
    source_identity = _video_source_identity(video_path)
    signature = _analysis_signature()
    cached = {}
    if breakdown_path.is_file():
        try:
            cached = json.loads(breakdown_path.read_text(encoding="utf-8"))
            artifact_contracts.validate(breakdown_path, cached)
        except (ValueError, OSError):
            cached = {}
    reusable = (
        isinstance(cached, dict)
        and cached.get("analysis_signature") == signature
        and cached.get("source_identity") == source_identity
        and source_identity.get("kind") == "file"
    )
    if reusable and not force:
        segments = cached.get("segments", [])
        cached_duration = cached.get("source_duration")
        cached_video = str(cached.get("source_video") or "").strip()
        cached_identity = cached.get("source_identity")
        if source_identity.get("kind") == "url":
            raise RuntimeError("远程 URL 无法确认内容未变化，请使用 force=true 重新拆镜")
        if cached_identity:
            if cached_identity != source_identity:
                raise RuntimeError("当前参考视频与拆镜缓存来源不一致，请使用 force=true 重新拆镜")
        elif _canonical_video_source(cached_video) != _canonical_video_source(video_path):
            raise RuntimeError("当前参考视频与旧拆镜缓存来源不一致，请使用 force=true 重新拆镜")
        else:
            cached["source_identity"] = source_identity
        if cached_duration is None:
            cached_duration = _probe_reference_duration(video_path)
            cached["source_duration"] = cached_duration
        _validate_segments(segments, expected_duration=cached_duration)
        story_phase = (manifest.get("phases") or {}).get("story") or {}
        manifest_mode = str(
            story_phase.get("mode") or ""
        ).strip().lower()
        effective_mode = mode or (manifest_mode if manifest_mode in BREAKDOWN_MODES else "")
        persisted_mode = effective_mode or "pending"
        mode_changed = cached.get("mode") != persisted_mode
        if (mode_changed
                or cached.get("source_identity") != source_identity
                or cached.get("source_duration") != cached_duration):
            cached["mode"] = persisted_mode
            cached["source_identity"] = source_identity
            cached["source_duration"] = cached_duration
            breakdown_path.write_text(
                json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        script_path = pdir / "script.md"
        script_meta = _read_front_matter(script_path) if script_path.is_file() else {}
        source_ref = str(script_meta.get("source_breakdown") or "")
        confirmed = (
            story_phase.get("status") == "done"
            and not mode_changed
            and effective_mode in BREAKDOWN_MODES
            and script_meta.get("source") == "reference_video"
            and str(script_meta.get("mode") or "").lower() == effective_mode
            and source_ref in {
                "materials/拆片表.md",
                str(materials_dir / BREAKDOWN_TABLE_FILENAME),
            }
        )
        gate = _sync_breakdown_execution_gate(
            pid,
            f"cached breakdown: {len(segments)} segments, duration={cached_duration}s",
            reset_human_gate=mode_changed,
        )
        if not confirmed:
            duration_note = (
                "总时长须与参考片相差 < 5s"
                if effective_mode == "imitate"
                else "总时长按 brief、平台与电商节奏重新确定"
                if effective_mode == "fission"
                else "待用户确认仿写或裂变"
            )
            gate_note = (
                "awaiting_human_approval: breakdown；禁止写正式 script.md"
                if gate.get("status") == "pending"
                else f"breakdown_approval={gate.get('status')}；{duration_note}"
            )
            ws.update_phase(
                pid, "story", status="in_progress", source="reference_video",
                mode=persisted_mode,
                artifact=str(materials_dir / "script_from_breakdown.md"),
                note=(f"参考视频拆镜 {len(segments)} 段（缓存复用），"
                      f"{gate_note}"),
            )
        # 缓存复用时也从结构化数据补出拆片表（纯本地渲染，不重跑 LLM）
        cached_mode = effective_mode
        breakdown_table_path = materials_dir / BREAKDOWN_TABLE_FILENAME
        breakdown_table_path.write_text(
            _build_breakdown_table(
                segments,
                source_video=cached.get("source_video", ""),
                mode=cached_mode if cached_mode != "pending" else "",
                total_duration=cached.get("total_duration", 0),
                script_summary=cached.get("script_summary") or {},
            ),
            encoding="utf-8",
        )
        script_path = _write_script_from_breakdown(
            pid,
            segments,
            mode=cached_mode if cached_mode != "pending" else "",
            script_summary=cached.get("script_summary") or {},
        )
        return {
            "ok": True,
            "cached": True,
            "mode": effective_mode or "pending",
            "segments_count": len(segments),
            "script_path": str(script_path),
            "breakdown_path": str(breakdown_path),
            "breakdown_table": str(breakdown_table_path),
            "provider_used": cached.get("provider_used", ""),
            "total_duration": cached.get("total_duration", 0),
            "execution_gate": gate,
        }

    source_duration = _probe_reference_duration(video_path)

    # 拆镜开始：breakdown 阶段标 in_progress（画布/状态条上可见"拆镜进行中"）
    def _mark_breakdown_started(mm: dict):
        ph = mm.setdefault("phases", {}).setdefault("breakdown", {})
        ws._stamp_phase_timing(ph, ph.get("status"), "in_progress")
        ph.update({
            "status": "in_progress",
            "source": "reference_video",
            "note": f"正在拆镜：{Path(video_path).name}",
        })
    ws.update_manifest(pid, _mark_breakdown_started)

    # 拆镜各环节分项计时：定位"到底慢在哪一环"（VLM 主拆 / 兜底 / 逐镜分析 / 全局总结）
    timing: dict[str, float] = {}
    _t0 = time.time()

    ws.bump_budget(pid, "vlm", 1)
    raw_text, provider_used = _vlm_breakdown(video_path, project_id=pid)
    timing["vlm_primary"] = round(time.time() - _t0, 2)

    _t1 = time.time()
    try:
        segments = _parse_breakdown(raw_text, expected_duration=source_duration)
        timing["parse"] = round(time.time() - _t1, 2)
    except BreakdownParseError as primary_err:
        if provider_used != "doubao" or not os.environ.get("DASHSCOPE_API_KEY", "").strip():
            raise  # 已经是 Qwen 兜底路径解析失败，无处可切，直接上抛
        # 豆包有内容但格式/时间轴不合规 → 按规划切 Qwen 兜底重拆一次
        try:
            raw_text = _qwen_vl_fallback(
                video_path, _load_breakdown_prompt())
            provider_used = _qwen_model()
            segments = _parse_breakdown(raw_text, expected_duration=source_duration)
        except Exception as fallback_err:
            raise RuntimeError(
                f"参考视频拆镜失败：豆包返回内容解析不合规（{primary_err}）；"
                f"阿里 {_qwen_model()} 兜底也失败（{fallback_err}）"
            ) from fallback_err
        timing["parse"] = round(time.time() - _t1, 2)
    total_duration = _to_seconds(_field(segments[-1], "end")) or 0

    # 全部观察来自同一次多模态响应；本地仅归一格式，不补充视觉推断。
    _t2 = time.time()
    for seg in segments:
        info = seg.get("shot_info")
        info = info if isinstance(info, dict) else {}
        seg["shot_info"] = {k: str(info[k]) for k in SHOT_INFO_KEYS if info.get(k)}
        seg["shot_info"].setdefault("shotSize", _field(seg, "shot_size"))
        seg["shot_info"].setdefault("shotCameraMovement", _field(seg, "camera"))
    observed = _observation_json(raw_text)
    summary = observed.get("script_summary")
    script_summary = (
        {k: summary[k] for k in _SUMMARY_LABELS if k in summary}
        if isinstance(summary, dict) else {}
    )
    timing["local_formatting"] = round(time.time() - _t2, 2)
    narration = _build_narration(segments)

    materials_dir.mkdir(parents=True, exist_ok=True)
    breakdown_path.write_text(json.dumps({
        "contract": artifact_contracts.REFERENCE_BREAKDOWN,
        "source": "reference_video",
        "source_video": video_path,
        "source_identity": source_identity,
        "analysis_signature": signature,
        "analysis_method": "multimodal",
        "sampling_fps": 4,
        "uncertainties": observed.get("uncertainties", []),
        "provider_used": provider_used,
        "mode": mode or "pending",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "segments_count": len(segments),
        "total_duration": total_duration,
        "source_duration": source_duration,
        "narration": narration,
        "script_summary": script_summary,
        "registries": {"characters": [], "props": [], "scenes": []},
        "timing": timing,
        "segments": segments,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (materials_dir / "breakdown_model_output.txt").write_text(raw_text, encoding="utf-8")
    breakdown_table_path = materials_dir / BREAKDOWN_TABLE_FILENAME
    breakdown_table_path.write_text(
        _build_breakdown_table(
            segments,
            source_video=video_path,
            mode=mode,
            total_duration=total_duration,
            script_summary=script_summary,
        ),
        encoding="utf-8",
    )
    (materials_dir / BREAKDOWN_MD_FILENAME).write_text(
        breakdown_table_path.read_text(encoding="utf-8"), encoding="utf-8")

    script_path = _write_script_from_breakdown(pid, segments, mode=mode, script_summary=script_summary)
    gate = _sync_breakdown_execution_gate(
        pid,
        f"fresh breakdown: {len(segments)} segments, duration={total_duration}s, "
        f"provider={provider_used}",
        reset_human_gate=True,
    )

    # 拆镜只是"写剧本之前"的分析步骤：breakdown 标 done，story 标 in_progress。
    # mode 留空 = 用户还没选：写剧本前必须先问用户仿写还是裂变，不得默认。
    def _mark_story_ready(m: dict):
        phases = m.setdefault("phases", {})
        bd = phases.setdefault("breakdown", {})
        ws._stamp_phase_timing(bd, bd.get("status"), "done")
        bd.update({
            "status": "done",
            "source": "reference_video",
            "artifact": str(breakdown_path),
            "note": f"参考视频拆镜 {len(segments)} 段完成（provider={provider_used}，总时长 {total_duration}s）",
        })
        st = phases.setdefault("story", {})
        ws._stamp_phase_timing(st, st.get("status"), "in_progress")
        story = {
            "status": "in_progress",
            "source": "reference_video",
            "artifact": str(script_path),
        }
        if mode:
            story["mode"] = mode
            duration_note = (
                "总时长须与参考片相差 < 5s"
                if mode == "imitate"
                else "总时长按 brief、平台与电商节奏重新确定"
            )
            story["note"] = (
                f"参考视频拆镜 {len(segments)} 段完成（provider={provider_used}）；"
                + ("awaiting_human_approval: breakdown；禁止写正式 script.md"
                   if gate.get("status") == "pending"
                   else f"auto_approved: breakdown；待按 {mode} 模式写剧本（{duration_note}）")
            )
        else:
            story["mode"] = "pending"
            story["note"] = (
                f"参考视频拆镜 {len(segments)} 段完成（provider={provider_used}）。"
                + ("awaiting_human_approval: breakdown；禁止写正式 script.md；"
                   if gate.get("status") == "pending"
                   else "auto_approved: breakdown；")
                + "待按 execution_mode 确定 imitate/fission。"
            )
        st.update(story)

    ws.update_manifest(pid, _mark_story_ready)

    return {
        "ok": True,
        "mode": mode or "pending",
        "segments_count": len(segments),
        "script_path": str(script_path),
        "breakdown_path": str(breakdown_path),
        "storyboard_md": str(materials_dir / BREAKDOWN_MD_FILENAME),
        "breakdown_table": str(breakdown_table_path),
        "provider_used": provider_used,
        "total_duration": total_duration,
        "timing": timing,
        "execution_gate": gate,
    }


_analyze_reference_video_impl = _analyze_reference_video


def _analyze_reference_video(handler, args):
    try:
        return _analyze_reference_video_impl(handler, args)
    except Exception as exc:
        pid = _active_pid(handler)
        if pid:
            def mark_failed(manifest: dict):
                phase = manifest.setdefault(
                    "phases", {}).setdefault("breakdown", {})
                if phase.get("status") != "in_progress":
                    return
                ws._stamp_phase_timing(
                    phase, phase.get("status"), "failed")
                phase.update({
                    "status": "failed",
                    "source": "reference_video",
                    "note": f"拆镜失败：{str(exc)[:300]}",
                })
            ws.update_manifest(pid, mark_failed)
        raise


_DECORATED_TOOLS["analyze_reference_video"] = _analyze_reference_video


# ============== 工具：拆镜注入编导方案 ==============
# 结构字段（锁段/锁时长/锁内容）：仿写整表照搬；裂变默认不搬，避免污染新结构。
_STRUCTURAL_FIELDS = (
    "duration", "content", "dialogue", "onscreen_text", "audio",
    "time_start", "time_end",
)
# 技法字段（拍摄手法参考库）：两种模式都可搬。
_TECHNIQUE_FIELDS = ("shot_type", "camera", "shot_angle", "shot_camera_movement",
                     "expression_style", "expression_purpose")


def _read_front_matter(path: Path) -> dict[str, str]:
    """Parse the flat YAML front matter used by project Markdown artifacts."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


@film_tool(
    name="inject_breakdown_to_plan",
    desc=("把 materials/reference_breakdown.json 的段清单深合并进 director_plan.json 的 segments[]，"
          "并登记 materials/拆片表.md 为编导来源，"
          "已存在的人工字段不覆盖。仿写(imitate)强制整表注入（ID/景别/运镜/时长/台词/画面内容/拍摄指导/画面文案/音效/镜头角度/表达方式/表达目的），"
          "缺失段会按参考段补齐；裂变(fission)默认只搬拍摄技法字段(technique_only)且只更新 plan 里已存在的段(only_existing_ids)，"
          "并要求 segment_mapping 显式声明新段到母片段的技法映射，不按同名段号猜。"),
    params={
        "plan_path": {"type": str, "description": "director_plan.json 路径；留空则用当前项目的 director_plan.json", "default": ""},
        "technique_only": {"type": bool, "description": "[可选] 只注入拍摄技法字段，不搬时长/台词/内容。裂变模式强制为 True；仿写模式默认 False", "default": None},
        "only_existing_ids": {"type": bool, "description": "[可选] 只更新 plan 已有段，不追加参考段。裂变模式强制为 True；仿写模式默认 False", "default": None},
        "segment_mapping": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "[裂变必填] 覆盖全部目标段：target_segment_id -> source_breakdown_segment_id；原创段填 original",
            "default": {},
        },
    },
)
def _inject_breakdown_to_plan(handler, args):
    pid = _active_pid(handler)
    if not pid:
        raise RuntimeError("尚无活跃项目，请先调用 project_create / project_open")
    pdir = ws.project_dir(pid)
    breakdown_path = pdir / MATERIALS_DIR_NAME / BREAKDOWN_JSON_FILENAME
    breakdown_table_path = pdir / MATERIALS_DIR_NAME / BREAKDOWN_TABLE_FILENAME
    if not breakdown_path.is_file():
        raise FileNotFoundError(f"拆镜产物不存在：{breakdown_path}，请先调用 analyze_reference_video")
    if not breakdown_table_path.is_file():
        raise FileNotFoundError(f"拆片表不存在：{breakdown_table_path}，请先重新执行拆镜")
    breakdown = json.loads(breakdown_path.read_text(encoding="utf-8"))
    artifact_contracts.validate(breakdown_path, breakdown)
    segments = breakdown.get("segments") or []
    if not segments:
        raise ValueError(f"拆镜产物里没有 segments：{breakdown_path}")
    manifest = ws.read_manifest(pid)
    story_phase = (manifest.get("phases") or {}).get("story") or {}
    story_mode = str(story_phase.get("mode") or "").strip().lower()
    if story_phase.get("status") != "done":
        raise RuntimeError("剧本尚未完成人工确认：story.status 必须为 done 后才能进入编导")
    if story_mode not in BREAKDOWN_MODES:
        raise RuntimeError("仿写/裂变模式尚未确认：story.mode 必须为 imitate 或 fission")
    breakdown_mode = str(breakdown.get("mode") or "").strip().lower()
    if breakdown_mode != story_mode:
        raise RuntimeError(
            f"拆镜模式与 manifest 不一致：breakdown={breakdown_mode or 'pending'}，"
            f"story={story_mode}；请先同步拆镜产物"
        )
    expected_table = _build_breakdown_table(
        segments,
        source_video=str(breakdown.get("source_video") or ""),
        mode=story_mode,
        total_duration=breakdown.get("total_duration", 0),
        script_summary=breakdown.get("script_summary") or {},
    )
    actual_table = breakdown_table_path.read_text(encoding="utf-8", errors="replace")
    if actual_table.strip() != expected_table.strip():
        raise RuntimeError("拆片表与 reference_breakdown.json 不一致，请先重新同步拆镜产物")
    script_path = pdir / "script.md"
    if not script_path.is_file():
        raise FileNotFoundError(f"正式剧本不存在：{script_path}")
    script_meta = _read_front_matter(script_path)
    if script_meta.get("source") != "reference_video":
        raise RuntimeError("正式剧本缺少 source: reference_video")
    if str(script_meta.get("mode") or "").lower() != story_mode:
        raise RuntimeError(
            f"正式剧本 mode 与 manifest 不一致：script={script_meta.get('mode') or 'missing'}，"
            f"story={story_mode}"
        )
    raw_source_breakdown = str(script_meta.get("source_breakdown") or "").strip()
    if not raw_source_breakdown:
        raise RuntimeError("正式剧本缺少 source_breakdown: materials/拆片表.md")
    source_path = Path(raw_source_breakdown)
    if not source_path.is_absolute():
        source_path = pdir / source_path
    if source_path.resolve() != breakdown_table_path.resolve():
        raise RuntimeError(
            f"正式剧本 source_breakdown 未指向当前拆片表：{raw_source_breakdown}"
        )
    if str(script_meta.get("draft") or "").lower() == "true":
        raise RuntimeError(f"正式剧本仍标记为 draft：{script_path}")

    raw_plan_path = str(args.get("plan_path", "")).strip()
    plan_path = Path(raw_plan_path).resolve() if raw_plan_path else (pdir / "director_plan.json").resolve()
    try:
        plan_path.relative_to(pdir.resolve())
    except ValueError as exc:
        raise ValueError(f"plan_path 必须位于当前项目内：{plan_path}") from exc
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        artifact_contracts.validate(plan_path, plan)
    else:
        plan = {
            "contract": artifact_contracts.DIRECTOR_PLAN,
            "project": manifest.get("brief", pid),
            "version": "1.0",
            "created": time.strftime("%Y-%m-%d"),
            "source": "reference_video",
            "total_duration": breakdown.get("total_duration", 0),
            "segments": [],
        }
    source_breakdown = plan.setdefault("source_breakdown", {})
    if not isinstance(source_breakdown, dict):
        raise ValueError("director_plan.source_breakdown 必须为对象")
    source_breakdown["json"] = str(breakdown_path)
    source_breakdown["markdown"] = str(breakdown_table_path)
    source_breakdown["mode"] = story_mode
    plan_segments = plan.setdefault("segments", [])
    if not isinstance(plan_segments, list):
        raise ValueError(f"director_plan.segments 不是数组，拒绝注入：{plan_path}")

    # 注入范围：默认随拆镜产物 mode 自动判定，显式传参可覆盖。
    # 裂变(fission)默认只搬技法字段、不 append 参考段，保护 script.md 的新结构；
    # 仿写(imitate)整表照搬、缺段补齐；未确认模式已在上方拒绝。
    is_fission = story_mode == "fission"
    technique_only = args.get("technique_only")
    if technique_only is None:
        technique_only = is_fission
    only_existing_ids = args.get("only_existing_ids")
    if only_existing_ids is None:
        only_existing_ids = is_fission
    technique_only = bool(technique_only)
    only_existing_ids = bool(only_existing_ids)
    if is_fission and (not technique_only or not only_existing_ids):
        raise ValueError(
            "裂变模式强制 technique_only=true 且 only_existing_ids=true，"
            "不得把母片结构段覆盖进裂变计划"
        )
    if not is_fission and (technique_only or only_existing_ids):
        raise ValueError(
            "仿写模式强制整表注入：technique_only=false 且 only_existing_ids=false"
        )

    injected = 0
    skipped_new = 0
    pairs: list[tuple[dict, dict]] = []
    if is_fission:
        mapping = args.get("segment_mapping") or {}
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("裂变模式必须提供非空 segment_mapping，禁止按同名段号猜技法来源")
        source_by_id = {
            _segment_id(seg, idx): seg for idx, seg in enumerate(segments)
        }
        target_by_id = {
            str(seg.get("id")): seg for seg in plan_segments
            if isinstance(seg, dict) and seg.get("id")
        }
        missing_targets = sorted(set(target_by_id) - {str(key) for key in mapping})
        extra_targets = sorted({str(key) for key in mapping} - set(target_by_id))
        if missing_targets or extra_targets:
            raise ValueError(
                "segment_mapping 必须覆盖全部目标段；"
                f"缺失={missing_targets or '无'}，未知={extra_targets or '无'}。"
                "原创段请显式映射为 original"
            )
        adopted = []
        original_targets = []
        for target_id, source_id in mapping.items():
            target = target_by_id.get(str(target_id))
            if target is None:
                raise ValueError(f"segment_mapping 目标段不存在：{target_id}")
            if str(source_id).lower() == "original":
                original_targets.append(str(target_id))
                continue
            source = source_by_id.get(str(source_id))
            if source is None:
                raise ValueError(f"segment_mapping 母片段不存在：{source_id}")
            pairs.append((source, target))
            adopted.append({"target_segment": str(target_id), "source_segment": str(source_id)})
        adopted_sources = {item["source_segment"] for item in adopted}
        source_breakdown["adopted"] = adopted
        source_breakdown["original"] = original_targets
        source_breakdown["discarded"] = [
            source_id for source_id in source_by_id if source_id not in adopted_sources
        ]
    else:
        source_breakdown["adopted"] = [
            {"target_segment": _segment_id(seg, idx), "source_segment": _segment_id(seg, idx)}
            for idx, seg in enumerate(segments)
        ]
        source_breakdown["discarded"] = []
        source_breakdown["original"] = []
        ordered_targets = []
        for idx, seg in enumerate(segments):
            seg_id = _segment_id(seg, idx)
            target = next(
                (item for item in plan_segments
                 if isinstance(item, dict) and item.get("id") == seg_id),
                None,
            )
            if target is None:
                target = {"id": seg_id}
            ordered_targets.append(target)
            pairs.append((seg, target))
        # 仿写严格锁定母片段数与顺序；移除拆镜中不存在的额外段。
        plan_segments[:] = ordered_targets
        plan["total_duration"] = breakdown.get("total_duration", 0)

    for seg, target in pairs:
        fields = {
            "shot_type": _field(seg, "shot_size"),
            "camera": _field(seg, "camera"),
            "duration": _to_seconds(_field(seg, "duration")),
            "dialogue": _field(seg, "dialogue"),
            "content": _field(seg, "content"),
            "shooting": _field(seg, "shooting"),
            "onscreen_text": _field(seg, "onscreen_text"),
            "audio": _field(seg, "audio"),
            "time_start": _norm_time(_field(seg, "start")),
            "time_end": _norm_time(_field(seg, "end")),
            "source": "reference_video",
        }
        # 已有多模态镜头观察也注入，供编导直接用
        info = seg.get("shot_info") or {}
        if info:
            fields.update({
                "shot_angle": info.get("shotAngle", ""),
                "shot_camera_movement": info.get("shotCameraMovement", ""),
                "expression_style": info.get("expressionStyle", ""),
                "expression_purpose": info.get("expressionPurpose", ""),
            })
        # technique_only：裁掉锁段/锁时长/锁内容的结构字段，只留拍摄技法参考。
        if technique_only:
            fields = {k: v for k, v in fields.items() if k not in _STRUCTURAL_FIELDS}
        if is_fission:
            # 裂变只补显式映射的技法空位，不覆盖新结构已有的人工设计。
            for key, value in fields.items():
                if value not in (None, "", []) and target.get(key) in (None, "", [], {}):
                    target[key] = value
        else:
            # 仿写的结构和技法字段以拆镜为准；资产、承接、blocking 等其他人工字段保留。
            target["id"] = _segment_id(seg, injected)
            for key, value in fields.items():
                target[key] = value
        injected += 1

    plan["contract"] = artifact_contracts.DIRECTOR_PLAN
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "plan_path": str(plan_path),
        "segments_injected": injected,
        "mode": breakdown.get("mode", ""),
        "technique_only": technique_only,
        "only_existing_ids": only_existing_ids,
        "skipped_new_segments": skipped_new,
    }


def _segment_id(seg: dict, idx: int) -> str:
    """拆镜的「分镜」列归一成 seg_01 风格的段 ID。"""
    raw = _field(seg, "shot")
    m = re.search(r"(\d+)", raw)
    n = int(m.group(1)) if m else idx + 1
    return f"seg_{n:02d}"


# ============== VLM：豆包主路径 + 阿里兜底 ==============
def _vlm_breakdown(video_path: str, project_id: str | None = None) -> tuple[str, str]:
    """完整原片多模态观察；采样不足以判断的动作应记为未知。"""
    prompt = _load_breakdown_prompt()
    prompt += (
        f"\n本片探测时长：{_probe_reference_duration(video_path):.3f} 秒。"
        "视频采样为每秒4帧，快速事件只能做采样支持的估计；"
        "不能精确识别的次数、间隔和文字列入 uncertainties。"
    )
    try:
        started = time.time()
        data = sdk.doubao_video_understand(video_path, prompt, fps=4, temperature=0.1)
        ws.log_model_call(
            project_id, data.get("body", {}).get("model", sdk.get_model("vlm", sdk.MODEL_VLM)),
            {"via_tool": "analyze_reference_video", "video": video_path,
             "prompt": prompt, "fps": 4, "analysis_method": "multimodal"},
            raw_request=data.get("body"), raw_response=data.get("raw"),
            duration_s=time.time() - started,
        )
        return _extract_content(data.get("raw", {})), "doubao"
    except (RuntimeError, TimeoutError) as e:
        if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
            raise
        doubao_err = e
    try:
        return _qwen_vl_fallback(video_path, prompt), _qwen_model()
    except Exception as fallback_err:
        raise RuntimeError(
            f"参考视频拆镜失败：豆包主路径异常（{doubao_err}）；"
            f"阿里 {_qwen_model()} 兜底也失败（{fallback_err}）"
        )


def _extract_content(raw: dict) -> str:
    """从 OpenAI 兼容响应里取正文；空/结构异常统一抛 RuntimeError 以触发兜底。"""
    try:
        content = (raw["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"VLM 返回结构异常：{json.dumps(raw, ensure_ascii=False, default=str)[:300]}")
    if not content:
        raise RuntimeError("VLM 返回 content 为空")
    return content


def _qwen_vl_fallback(video_path: str, prompt: str) -> str:
    """阿里 qwen-vl-max-latest 兜底：直接 HTTP 调 dashscope 的 OpenAI 兼容端点。"""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY 环境变量，阿里 VLM 兜底不可用")

    if video_path.startswith(("http://", "https://")):
        video_url = video_path
    else:
        p = Path(video_path)
        if not p.is_file():
            raise FileNotFoundError(f"参考视频不存在：{p}")
        size = p.stat().st_size
        if size > QWEN_INLINE_MAX_BYTES:
            raise RuntimeError(
                f"本地视频 {size / 1024 / 1024:.1f}MB 超过兜底内嵌上限 "
                f"{QWEN_INLINE_MAX_BYTES // 1024 // 1024}MB：请提供 URL 或压缩视频"
            )
        suffix = p.suffix.lower().lstrip(".") or "mp4"
        video_url = f"data:video/{suffix};base64,{base64.b64encode(p.read_bytes()).decode()}"

    model = _qwen_model()
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    resp = requests.post(
        _qwen_endpoint(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=600,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"阿里 {model} 返回 {resp.status_code}：{resp.text[:300]}")
    return _extract_content(resp.json())


# ============== 解析：JSON 注释块优先，回退 Markdown 表 ==============
_JSON_BLOCK_RE = re.compile(r"<!--\s*JSON:\s*(\[.*?\])\s*-->", re.DOTALL)
_TABLE_ROW_RE = re.compile(r"^\|.*\|$", re.MULTILINE)
_SEPARATOR_RE = re.compile(r"^\|[\s:\-|]+\|$")


def _parse_breakdown(raw_text: str, expected_duration: float | None = None) -> list[dict]:
    """解析新版 JSON；保留旧 JSON 注释块和 Markdown 产物读取能力。"""
    observed = _observation_json(raw_text)
    if observed:
        segments = observed.get("segments")
        if not isinstance(segments, list) or not all(isinstance(s, dict) for s in segments):
            raise BreakdownParseError("segments 必须是镜头对象数组")
        _validate_segments(segments, expected_duration=expected_duration)
        return segments
    m = _JSON_BLOCK_RE.search(raw_text or "")
    if m:
        try:
            segments = json.loads(m.group(1))
            if isinstance(segments, list) and segments:
                _validate_segments(
                    [s for s in segments if isinstance(s, dict)],
                    expected_duration=expected_duration,
                )
                return [s for s in segments if isinstance(s, dict)]
        except (json.JSONDecodeError, BreakdownParseError):
            pass  # JSON 辅助块不可用/不合格 → 回退表格解析

    rows = [r for r in _TABLE_ROW_RE.findall(raw_text or "") if not _SEPARATOR_RE.match(r.strip())]
    if len(rows) < 2:
        raise BreakdownParseError(f"未从 VLM 输出里解析到 Markdown 分镜表（有效行 {len(rows)} 行）")
    header = _split_row(rows[0])
    segments = []
    for row in rows[1:]:
        cells = _split_row(row)
        if len(cells) != len(header):
            raise BreakdownParseError(f"表格列数不一致：表头 {len(header)} 列，数据行 {len(cells)} 列 → {row[:80]}")
        segments.append(dict(zip(header, cells)))
    _validate_segments(segments, expected_duration=expected_duration)
    return segments


def _split_row(row: str) -> list[str]:
    """按 | 切分表格行并 strip 每格（首尾空串来自行首行尾的竖线）。"""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _validate_segments(segments: list[dict], expected_duration: float | None = None):
    """校验镜头数、时间连续性和末镜覆盖；失败抛 BreakdownParseError 供上层切兜底。"""
    if not segments:
        raise BreakdownParseError("拆镜结果没有有效镜头")
    first_start = _norm_time(_field(segments[0], "start"))
    if first_start != "00:00":
        raise BreakdownParseError(f"首镜开始时间应为 00:00，实际为 {first_start!r}")
    declared_total = 0.0
    for index, segment in enumerate(segments, start=1):
        start_s = _to_seconds(_field(segment, "start"))
        end_s = _to_seconds(_field(segment, "end"))
        duration_s = _to_seconds(_field(segment, "duration"))
        if start_s is None or end_s is None or duration_s is None or end_s <= start_s:
            raise BreakdownParseError(f"第 {index} 镜时间字段无效")
        timeline_duration = end_s - start_s
        if abs(float(duration_s) - float(timeline_duration)) > 0.051:
            raise BreakdownParseError(
                f"第 {index} 镜声明时长 {duration_s}s 与时间轴 "
                f"{start_s}-{end_s}s 不一致"
            )
        declared_total += float(duration_s)
    for i in range(1, len(segments)):
        prev_end = _norm_time(_field(segments[i - 1], "end"))
        cur_start = _norm_time(_field(segments[i], "start"))
        if prev_end != cur_start:
            raise BreakdownParseError(
                f"第 {i} 与第 {i + 1} 镜时间不连续：上一镜结束 {prev_end!r} != 本镜开始 {cur_start!r}"
            )
    last_end = _to_seconds(_field(segments[-1], "end"))
    if last_end is None or abs(declared_total - float(last_end)) > 0.051:
        raise BreakdownParseError(
            f"分段声明时长合计 {declared_total}s 与末镜结束 {last_end!r}s 不一致"
        )
    if expected_duration is not None:
        if last_end is None or abs(float(last_end) - float(expected_duration)) > 0.26:
            raise BreakdownParseError(
                f"末镜结束 {last_end!r}s 未覆盖参考视频真实时长 "
                f"{float(expected_duration):.2f}s"
            )


def _canonical_video_source(video_path: str) -> str:
    value = str(video_path or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    return str(Path(value).expanduser().resolve())


def _video_source_identity(video_path: str) -> dict:
    """Stable cache identity; local files include a content digest."""
    canonical = _canonical_video_source(video_path)
    if canonical.startswith(("http://", "https://")):
        return {"source": canonical, "kind": "url"}
    path = Path(canonical)
    if not path.is_file():
        raise FileNotFoundError(f"参考视频不存在：{path}")
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "source": canonical,
        "kind": "file",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _probe_reference_duration(video_path: str) -> float:
    """Probe the source video once so a truncated VLM timeline cannot pass validation."""
    try:
        duration = float(sdk.probe_duration(video_path))
    except Exception as exc:
        raise RuntimeError(f"无法探测参考视频真实时长：{video_path}（{exc}）") from exc
    if duration <= 0:
        raise RuntimeError(f"参考视频时长无效：{duration}")
    return duration


# ============== 生成拆镜脚本草稿 + 主角硬锁 ==============
def _write_script_from_breakdown(pid: str, segments: list[dict], mode: str = "",
                                 script_summary: Optional[dict] = None) -> Path:
    """把拆镜结果写成 materials/script_from_breakdown.md 草稿。

    正式 script.md 由剧本阶段按 mode（imitate=仿写 / fission=裂变）创建和维护；
    本函数永不覆盖正式稿。mode 留空时标 pending（写剧本前必须先问用户）。
    多模态分析的镜头信息与全局结构一并写入，
    供后续剧本与镜头规划阶段按中立产物合同取用。返回路径。
    """
    lines = [
        "---",
        "source: reference_video",
        f"mode: {mode or 'pending'}",
        "draft: true",
        f"generated_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"segments_count: {len(segments)}",
        "---",
        "",
        "# 分镜脚本（参考视频拆镜草稿）",
        "",
        "> 本文件是拆镜表的草稿版：正式剧本需按 front matter 里的 mode（imitate=仿写 / fission=裂变）改写后落盘。",
        "",
    ]
    for idx, seg in enumerate(segments):
        start = _norm_time(_field(seg, "start")) or "-"
        end = _norm_time(_field(seg, "end")) or "-"
        lines.append(f"## 场景 {idx + 1}（{start} - {end}）")
        lines.append("")
        for label, key in (("景别", "shot_size"), ("运镜", "camera"), ("画面内容", "content"),
                           ("拍摄指导", "shooting"), ("口播/台词", "dialogue"),
                           ("音效/BGM", "audio"), ("画面文案/花字", "onscreen_text")):
            value = _field(seg, key)
            if value:
                lines.append(f"- {label}：{value}")
        info = seg.get("shot_info") or {}
        if info:
            lines.append(f"- 镜头信息：角度 {info.get('shotAngle', '-')} / 景别 {info.get('shotSize', '-')}"
                         f" / 运镜 {info.get('shotCameraMovement', '-')}")
            if info.get("expressionStyle") or info.get("expressionPurpose"):
                lines.append(f"- 表达：{info.get('expressionStyle', '-')}"
                             f"（目的：{info.get('expressionPurpose', '-')}）")
        lines.append("")

    if script_summary:
        lines.append("## 全局脚本总结（多模态观察）")
        lines.append("")
        for label, key in (("结构公式", "scriptStructureFormula"), ("钩子形式", "scriptHookForm"),
                           ("主打场景", "scriptMainScenarios"), ("用户痛点", "scriptUserPainPoints"),
                           ("产品卖点", "scriptProductSellingPoints"), ("圈定人群", "scriptTargetAudience")):
            value = (script_summary or {}).get(key)
            if value:
                lines.append(f"- {label}：{value}")
        lines.append("")

    script_path = ws.project_dir(pid) / MATERIALS_DIR_NAME / "script_from_breakdown.md"
    script_path.write_text("\n".join(lines), encoding="utf-8")
    return script_path


def _lock_entities_no_new_rule(pid: str):
    """有上传硬锁实体时，在 manifest 顶层写入通用禁止新增标志。"""
    def _apply(m: dict):
        entities = m.get("entities") or {}
        locked = any(isinstance(v, dict) and v.get("locked_from_upload") for v in entities.values())
        if locked:
            m["_locked_no_new"] = True

    ws.update_manifest(pid, _apply)
