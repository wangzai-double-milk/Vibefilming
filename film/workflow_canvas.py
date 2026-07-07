"""Generate a read-only workflow canvas for a VibeFilming project.

The canvas is an audit view, not an editor: it reconstructs the production
graph from project files and tool logs, then writes workflow_graph.json and
canvas.html into the project directory.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import workspace as ws


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STAGES = [
    ("story", "剧本"),
    ("plan", "编导方案"),
    ("assets", "参考资产"),
    ("storyboard", "故事板"),
    ("video", "视频段"),
    ("review", "审查"),
    ("compose", "合成成片"),
]


@dataclass
class Node:
    id: str
    kind: str
    stage: str
    title: str
    path: str | None = None
    status: str = "unknown"
    subtitle: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    label: str = ""
    kind: str = "depends_on"


class GraphBuilder:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.path_to_id: dict[str, str] = {}
        self.warnings: list[str] = []

    def rel(self, path: Path | str | None) -> str | None:
        if not path:
            return None
        p = Path(path)
        if not p.is_absolute():
            p = (self.project_dir / p).resolve()
        try:
            return p.relative_to(self.project_dir).as_posix()
        except ValueError:
            return str(p)

    def node_id(self, kind: str, key: str) -> str:
        raw = f"{kind}:{key}"
        safe = re.sub(r"[^0-9a-zA-Z_\-\u4e00-\u9fff:.]+", "_", raw).strip("_")
        return safe[:160] or f"{kind}:{len(self.nodes) + 1}"

    def add_node(
        self,
        kind: str,
        stage: str,
        title: str,
        *,
        path: Path | str | None = None,
        status: str = "unknown",
        subtitle: str = "",
        meta: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> str:
        rel_path = self.rel(path)
        node_key = key or rel_path or title
        nid = self.node_id(kind, node_key)
        if nid not in self.nodes:
            self.nodes[nid] = Node(
                id=nid,
                kind=kind,
                stage=stage,
                title=title,
                path=rel_path,
                status=status,
                subtitle=subtitle,
                meta=meta or {},
            )
        else:
            node = self.nodes[nid]
            if status != "unknown":
                node.status = status
            if subtitle and not node.subtitle:
                node.subtitle = subtitle
            if meta:
                node.meta.update(meta)
            if rel_path and not node.path:
                node.path = rel_path
        if rel_path:
            self.path_to_id[self._path_key(rel_path)] = nid
        return nid

    def add_edge(self, source: str | None, target: str | None, label: str = "", kind: str = "depends_on"):
        if not source or not target or source == target:
            return
        edge = Edge(source=source, target=target, label=label, kind=kind)
        if edge not in self.edges:
            self.edges.append(edge)

    def id_for_path(self, path: Path | str | None) -> str | None:
        rel_path = self.rel(path)
        if not rel_path:
            return None
        return self.path_to_id.get(self._path_key(rel_path))

    @staticmethod
    def _path_key(path: str) -> str:
        return path.replace("\\", "/").lower()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def resolve_project(value: str) -> Path:
    raw = Path(value).expanduser()
    if raw.exists():
        return raw.resolve()
    candidate = ws.PROJECTS_ROOT / value
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(f"找不到项目目录：{value}")


def status_from_review(path: Path) -> str:
    if not path.exists():
        return "missing"
    data = read_json(path)
    explicit = review_explicit_pass(data)
    if explicit is not None:
        return "done" if explicit else "failed"

    text = review_result_text(data)
    text = re.sub(r"(无|没有|未见|不存在)\s*不通过", "", text)
    if re.search(r"(不通过|失败|FAIL|FAILED)", text):
        return "failed"
    if re.search(r"(通过|PASS|PASSED)", text):
        return "done"
    return "done"


def review_explicit_pass(value: Any) -> bool | None:
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"pass", "passed", "is_pass", "approved"} and isinstance(item, bool):
                return item
            if lower in {"status", "result", "conclusion", "verdict"} and isinstance(item, str):
                normalized = item.strip().lower()
                if normalized in {"pass", "passed", "done", "success", "approved", "通过"}:
                    return True
                if normalized in {"fail", "failed", "error", "rejected", "blocked", "不通过", "失败"}:
                    return False
        for key, item in value.items():
            if str(key).lower() in {"question", "prompt", "criteria", "instruction"}:
                continue
            found = review_explicit_pass(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        results = [review_explicit_pass(item) for item in value]
        results = [item for item in results if item is not None]
        if results:
            return all(results)
    return None


def review_result_text(data: dict[str, Any]) -> str:
    fields = ("answer", "result", "conclusion", "verdict", "summary", "status")
    parts: list[str] = []
    for field in fields:
        if field in data and data[field] is not None:
            parts.append(json.dumps(data[field], ensure_ascii=False) if not isinstance(data[field], str) else data[field])
    if not parts:
        for key, value in data.items():
            if str(key).lower() in {"question", "prompt", "criteria", "instruction", "images", "video"}:
                continue
            parts.append(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
    return "\n".join(parts)


def title_from_file(path: Path) -> str:
    return path.stem.replace("_", " ")


def short_text(value: Any, limit: int = 150) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[: limit - 1] + "…" if len(text) > limit else text


def full_text(value: Any) -> str:
    return str(value or "").strip()


def build_graph(project_dir: Path) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    builder = GraphBuilder(project_dir)
    manifest = read_json(project_dir / "manifest.json")
    plan = read_json(project_dir / "director_plan.json")
    tool_calls = read_jsonl(project_dir / "logs" / "tool_calls.jsonl")

    script_id = None
    if (project_dir / "script.md").exists():
        phase = manifest.get("phases", {}).get("story", {})
        script_id = builder.add_node(
            "script",
            "story",
            "script.md",
            path=project_dir / "script.md",
            status=phase.get("status", "done"),
            subtitle=short_text(manifest.get("brief", "")),
        )

    plan_id = None
    if (project_dir / "director_plan.json").exists():
        phase = manifest.get("phases", {}).get("entity", {})
        plan_title = plan.get("project") or "director_plan.json"
        plan_id = builder.add_node(
            "director_plan",
            "plan",
            str(plan_title),
            path=project_dir / "director_plan.json",
            status=phase.get("status", "done" if plan else "unknown"),
            subtitle="全片唯一真相源",
            meta={"segments": len(plan.get("segments", []))},
        )
        builder.add_edge(script_id, plan_id, "编导转译")

    segment_ids: dict[str, str] = {}
    for seg in plan.get("segments", []) if isinstance(plan.get("segments"), list) else []:
        seg_key = str(seg.get("id") or seg.get("title") or len(segment_ids) + 1)
        title = f"{seg_key} · {seg.get('title') or seg.get('summary') or '未命名片段'}"
        sid = builder.add_node(
            "segment",
            "plan",
            title,
            status=str(seg.get("status") or "planned"),
            subtitle=f"{seg.get('duration', '?')}s · {seg.get('scene') or ''}",
            meta={
                "start_time": seg.get("start_time"),
                "end_time": seg.get("end_time"),
                "assets_needed": seg.get("assets_needed") or seg.get("asset_plan") or [],
                "onscreen_text": seg.get("onscreen_text"),
            },
            key=seg_key,
        )
        segment_ids[seg_key.lower()] = sid
        builder.add_edge(plan_id, sid, "segment")

    for subdir, stage, kind in [
        ("entities", "assets", "asset"),
        ("shots", "storyboard", "storyboard"),
        ("shots", "video", "video"),
        ("reviews", "review", "review"),
        ("composed", "compose", "composed"),
    ]:
        folder = project_dir / subdir
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if kind == "asset" and suffix not in IMAGE_EXTS:
                continue
            if kind == "storyboard" and not (suffix in IMAGE_EXTS and path.name.startswith("storyboard")):
                continue
            if kind == "video" and suffix not in VIDEO_EXTS:
                continue
            if kind == "video" and path.name.startswith("storyboard"):
                continue
            if kind == "review" and suffix != ".json":
                continue
            if kind == "composed" and suffix not in VIDEO_EXTS:
                continue
            status = "done"
            node_meta: dict[str, Any] = {"size_bytes": path.stat().st_size}
            if kind == "review":
                status = status_from_review(path)
                verdict = review_result_text(read_json(path))
                if verdict:
                    node_meta["conclusion"] = full_text(verdict)
            if kind == "composed" and "final" in path.stem:
                status = manifest.get("phases", {}).get("compose", {}).get("status", "done")
            builder.add_node(
                kind,
                stage,
                title_from_file(path),
                path=path,
                status=status,
                subtitle=subdir,
                meta=node_meta,
            )

    apply_tool_edges(builder, tool_calls, plan_id)
    add_missing_segment_placeholders(builder, project_dir, segment_ids)

    phases = manifest.get("phases", {})
    stats = {
        "nodes": len(builder.nodes),
        "edges": len(builder.edges),
        "assets": sum(1 for n in builder.nodes.values() if n.kind == "asset"),
        "storyboards": sum(1 for n in builder.nodes.values() if n.kind == "storyboard"),
        "videos": sum(1 for n in builder.nodes.values() if n.kind == "video"),
        "reviews": sum(1 for n in builder.nodes.values() if n.kind == "review"),
        "composed": sum(1 for n in builder.nodes.values() if n.kind == "composed"),
    }
    return {
        "project": {
            "id": manifest.get("project_id") or project_dir.name,
            "brief": manifest.get("brief") or plan.get("project") or project_dir.name,
            "dir": str(project_dir),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "stages": [{"id": sid, "title": title} for sid, title in STAGES],
        "phases": phases,
        "budget": manifest.get("budget", {}),
        "stats": stats,
        "warnings": builder.warnings,
        "nodes": [node.__dict__ for node in builder.nodes.values()],
        "edges": [edge.__dict__ for edge in builder.edges],
    }


def apply_tool_edges(builder: GraphBuilder, calls: list[dict[str, Any]], plan_id: str | None):
    for call in calls:
        tool = call.get("tool")
        args = call.get("args") or {}
        if tool == "gen_image":
            category = args.get("category") or "shot"
            name = args.get("name") or "image"
            path = builder.project_dir / ("entities" if category == "entity" else "shots") / f"{name}.png"
            out_id = builder.add_node(
                "asset" if category == "entity" else "storyboard",
                "assets" if category == "entity" else "storyboard",
                title_from_file(path),
                path=path,
                status="done" if path.exists() else "planned",
                meta={
                    "prompt": short_text(args.get("prompt", "")),
                    "prompt_full": full_text(args.get("prompt", "")),
                    "reference_images": [builder.rel(r) or str(r) for r in (args.get("reference_images") or [])],
                },
            )
            refs = args.get("reference_images") or []
            if refs:
                for ref in refs:
                    ref_id = builder.id_for_path(ref) or builder.add_node(
                        "reference",
                        "assets",
                        title_from_file(Path(str(ref))),
                        path=ref,
                        status="done" if Path(str(ref)).exists() else "unknown",
                    )
                    builder.add_edge(ref_id, out_id, "参考")
            else:
                builder.add_edge(plan_id, out_id, "生成")

        elif tool == "gen_video_t2v":
            name = args.get("name") or "video"
            path = builder.project_dir / "shots" / f"{name}.mp4"
            out_id = builder.add_node(
                "video",
                "video",
                title_from_file(path),
                path=path,
                status="done" if path.exists() else "running",
                subtitle=f"{args.get('duration', '?')}s · {args.get('ratio', '')}",
                meta={
                    "prompt": short_text(args.get("prompt", ""), 260),
                    "prompt_full": full_text(args.get("prompt", "")),
                    "duration": args.get("duration"),
                    "ratio": args.get("ratio"),
                    "reference_images": [builder.rel(r) or str(r) for r in (args.get("reference_images") or [])],
                },
            )
            refs = args.get("reference_images") or []
            if not refs:
                builder.add_edge(plan_id, out_id, "生成")
            for ref in refs:
                ref_id = builder.id_for_path(ref)
                if not ref_id:
                    ref_id = builder.add_node(
                        "reference",
                        "assets",
                        title_from_file(Path(str(ref))),
                        path=ref,
                        status="done" if Path(str(ref)).exists() else "unknown",
                    )
                builder.add_edge(ref_id, out_id, "Seedance 参考")

        elif tool == "vlm_understand":
            name = args.get("name") or "review"
            review_path = builder.project_dir / "reviews" / f"{name}.json"
            targets_rel = []
            if args.get("video"):
                targets_rel.append(builder.rel(args["video"]) or str(args["video"]))
            targets_rel.extend(builder.rel(t) or str(t) for t in (args.get("images") or []))
            review_meta = {
                "question": short_text(args.get("question", ""), 260),
                "question_full": full_text(args.get("question", "")),
                "targets": targets_rel,
            }
            if review_path.exists():
                verdict = review_result_text(read_json(review_path))
                if verdict:
                    review_meta["conclusion"] = full_text(verdict)
            review_id = builder.add_node(
                "review",
                "review",
                title_from_file(review_path),
                path=review_path,
                status=status_from_review(review_path) if review_path.exists() else "planned",
                meta=review_meta,
            )
            targets = []
            if args.get("video"):
                targets.append(args["video"])
            targets.extend(args.get("images") or [])
            for target in targets:
                target_id = builder.id_for_path(target)
                if target_id:
                    builder.add_edge(target_id, review_id, "审查")
                else:
                    builder.add_edge(plan_id, review_id, "审查")

        elif tool == "video_crossfade":
            name = args.get("name") or "crossfade"
            path = builder.project_dir / "composed" / f"{name}.mp4"
            out_id = builder.add_node(
                "composed",
                "compose",
                title_from_file(path),
                path=path,
                status="done" if path.exists() else "planned",
                subtitle=f"{args.get('transition', 'fade')} · {args.get('duration', '?')}s",
            )
            for key in ("clip_a", "clip_b"):
                src_id = builder.id_for_path(args.get(key))
                builder.add_edge(src_id, out_id, "转场")

        elif tool == "video_concat":
            name = args.get("name") or "final"
            path = builder.project_dir / "composed" / f"{name}.mp4"
            out_id = builder.add_node(
                "composed",
                "compose",
                title_from_file(path),
                path=path,
                status="done" if path.exists() else "planned",
                subtitle="最终拼接",
            )
            for clip in args.get("clips") or []:
                src_id = builder.id_for_path(clip)
                builder.add_edge(src_id, out_id, "拼接")


def add_missing_segment_placeholders(builder: GraphBuilder, project_dir: Path, segment_ids: dict[str, str]):
    if not segment_ids:
        return
    storyboard_files = [p.name.lower() for p in (project_dir / "shots").glob("storyboard*") if p.is_file()]
    video_files = [p.name.lower() for p in (project_dir / "shots").glob("*.mp4") if p.is_file()]
    running_video_nodes = [
        node
        for node in builder.nodes.values()
        if node.kind == "video" and node.status in {"done", "running", "in_progress"}
    ]
    for seg_key, seg_id in segment_ids.items():
        variants = segment_key_variants(seg_key)
        has_storyboard = any(any(v in name for v in variants) for name in storyboard_files)
        matched_video_ids = [
            node.id
            for node in running_video_nodes
            if any(v in " ".join([node.title, node.path or ""]).lower() for v in variants)
        ]
        has_video = any(any(v in name for v in variants) for name in video_files) or bool(matched_video_ids)
        if not has_storyboard:
            missing_id = builder.add_node(
                "missing_storyboard",
                "storyboard",
                f"{seg_key} 故事板待生成",
                status="missing",
                subtitle="planned from director_plan",
                key=f"{seg_key}:missing_storyboard",
            )
            builder.add_edge(seg_id, missing_id, "待生成")
        for video_id in matched_video_ids:
            status = builder.nodes[video_id].status
            builder.add_edge(seg_id, video_id, "生成中" if status in {"running", "in_progress"} else "已生成")
        if not has_video:
            missing_id = builder.add_node(
                "missing_video",
                "video",
                f"{seg_key} 视频待生成",
                status="missing",
                subtitle="planned from director_plan",
                key=f"{seg_key}:missing_video",
            )
            builder.add_edge(seg_id, missing_id, "待生成")


def segment_key_variants(seg_key: str) -> set[str]:
    key = seg_key.lower()
    variants = {key, key.replace("_", ""), key.replace("seg_", "s")}
    match = re.search(r"(\d+)", key)
    if match:
        raw_num = match.group(1)
        int_num = str(int(raw_num))
        variants.update(
            {
                f"seg_{raw_num}",
                f"seg{raw_num}",
                f"s{raw_num}",
                f"segment_{raw_num}",
                f"segment{raw_num}",
                f"seg_{int_num}",
                f"seg{int_num}",
                f"s{int_num}",
                f"segment_{int_num}",
                f"segment{int_num}",
            }
        )
    return {item for item in variants if item}


def render_html(graph: dict[str, Any]) -> str:
    graph_json = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(graph["project"]["brief"])} · Workflow Canvas</title>
  <style>
    :root {{
      --bg: #0b0d10;
      --panel: rgba(22, 26, 31, .84);
      --panel-2: rgba(255, 255, 255, .055);
      --line: rgba(190, 150, 78, .52);
      --text: #f3efe7;
      --muted: #9b9285;
      --gold: #d6a64f;
      --green: #67c587;
      --red: #e36b6b;
      --blue: #72a7ff;
      --grain: radial-gradient(circle at 22% 10%, rgba(214,166,79,.15), transparent 26%),
               radial-gradient(circle at 76% 22%, rgba(83,116,173,.18), transparent 32%),
               linear-gradient(135deg, rgba(255,255,255,.04), transparent 40%);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Avenir Next", "PingFang SC", "Hiragino Sans GB", sans-serif;
      overflow: hidden;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      background: var(--grain);
      pointer-events: none;
    }}
    header {{
      height: 112px;
      padding: 18px 24px 14px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      background: rgba(8, 10, 12, .88);
      position: relative;
      z-index: 5;
    }}
    .eyebrow {{
      color: var(--gold);
      letter-spacing: .18em;
      font-size: 11px;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 7px 0 10px;
      font-size: 25px;
      font-weight: 700;
      letter-spacing: -.02em;
    }}
    .topline {{
      display: flex;
      gap: 16px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .phase {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 9px;
      border: 1px solid rgba(255,255,255,.09);
      border-radius: 999px;
      background: rgba(255,255,255,.04);
    }}
    .dot {{
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--muted);
    }}
    .phase.done .dot, .status-done {{ background: var(--green); }}
    .phase.in_progress .dot, .status-in_progress, .status-running {{ background: var(--blue); }}
    .phase.pending .dot, .status-pending, .status-planned, .status-missing {{ background: var(--gold); }}
    .phase.blocked .dot, .phase.failed .dot, .status-failed {{ background: var(--red); }}
    .toolbar {{
      position: absolute;
      right: 24px;
      bottom: 16px;
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .refresh-status {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      padding: 7px 10px;
      border: 1px solid rgba(255,255,255,.10);
      border-radius: 999px;
      background: rgba(255,255,255,.045);
      white-space: nowrap;
    }}
    .refresh-status.live::before {{
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 10px rgba(103,197,135,.8);
    }}
    .refresh-status.warn::before {{
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--gold);
    }}
    input {{
      width: 260px;
      background: rgba(255,255,255,.07);
      border: 1px solid rgba(255,255,255,.12);
      color: var(--text);
      border-radius: 10px;
      padding: 8px 11px;
      outline: none;
    }}
    main {{
      height: calc(100vh - 112px);
      overflow: auto;
      position: relative;
      padding: 28px 28px 80px;
    }}
    #canvas {{
      position: relative;
      min-width: 1680px;
      min-height: 760px;
      padding: 0 0 64px;
    }}
    #edges {{
      position: absolute;
      inset: 0;
      overflow: visible;
      pointer-events: none;
      z-index: 1;
    }}
    .columns {{
      display: grid;
      grid-template-columns: repeat(7, 220px);
      gap: 22px;
      align-items: start;
      position: relative;
      z-index: 2;
    }}
    .col {{
      min-height: 640px;
      padding: 12px;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 22px;
      background: rgba(255,255,255,.026);
    }}
    .col h2 {{
      margin: 0 0 12px;
      color: #cab487;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: .12em;
    }}
    .card {{
      position: relative;
      margin: 0 0 12px;
      padding: 11px;
      border-radius: 16px;
      background: var(--panel);
      border: 1px solid rgba(255,255,255,.10);
      box-shadow: 0 12px 32px rgba(0,0,0,.26);
      transition: transform .15s ease, border-color .15s ease, opacity .15s ease;
      overflow: hidden;
    }}
    .card:hover {{
      transform: translateY(-2px);
      border-color: rgba(214,166,79,.75);
    }}
    .card.dim {{ opacity: .22; }}
    .card.active {{ border-color: var(--gold); box-shadow: 0 0 0 1px rgba(214,166,79,.4), 0 16px 40px rgba(0,0,0,.34); }}
    .thumb {{
      width: 100%;
      height: 106px;
      object-fit: cover;
      display: block;
      border-radius: 11px;
      margin-bottom: 9px;
      background: #151515;
    }}
    video.thumb {{ object-fit: cover; }}
    .kind {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 10px;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    .status-dot {{
      display: inline-block;
      width: 7px;
      height: 7px;
      border-radius: 999px;
    }}
    .title {{
      margin: 7px 0 4px;
      font-size: 13px;
      line-height: 1.35;
      font-weight: 700;
    }}
    .subtitle {{
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
      word-break: break-word;
    }}
    .meta {{
      margin-top: 8px;
      color: #b6ad9d;
      font-size: 10px;
      line-height: 1.45;
      max-height: 46px;
      overflow: hidden;
    }}
    .missing {{
      border-style: dashed;
      background: rgba(214,166,79,.08);
    }}
    .edge {{
      fill: none;
      stroke: var(--line);
      stroke-width: 1.6;
    }}
    .edge.fade {{ opacity: .08; }}
    .empty {{
      color: rgba(255,255,255,.22);
      font-size: 12px;
      padding: 20px 8px;
      border: 1px dashed rgba(255,255,255,.08);
      border-radius: 14px;
      text-align: center;
    }}
    .card {{ cursor: pointer; }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: rgba(4, 6, 8, .55);
      backdrop-filter: blur(2px);
      opacity: 0;
      pointer-events: none;
      transition: opacity .2s ease;
      z-index: 40;
    }}
    .overlay.open {{ opacity: 1; pointer-events: auto; }}
    .drawer {{
      position: fixed;
      top: 0;
      right: 0;
      height: 100vh;
      width: 460px;
      max-width: 92vw;
      background: rgba(14, 17, 21, .98);
      border-left: 1px solid rgba(214,166,79,.28);
      box-shadow: -24px 0 60px rgba(0,0,0,.5);
      transform: translateX(104%);
      transition: transform .24s cubic-bezier(.22,.61,.36,1);
      z-index: 41;
      display: flex;
      flex-direction: column;
    }}
    .drawer.open {{ transform: translateX(0); }}
    .drawer-head {{
      padding: 20px 22px 14px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      position: relative;
    }}
    .drawer-head .kind {{ margin-bottom: 8px; }}
    .drawer-head h3 {{
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      line-height: 1.3;
      padding-right: 30px;
    }}
    .drawer-head .path {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
      word-break: break-all;
    }}
    .drawer-close {{
      position: absolute;
      top: 16px;
      right: 16px;
      width: 30px;
      height: 30px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.05);
      color: var(--text);
      font-size: 16px;
      line-height: 1;
      cursor: pointer;
    }}
    .drawer-close:hover {{ border-color: var(--gold); color: var(--gold); }}
    .drawer-body {{
      padding: 18px 22px 40px;
      overflow-y: auto;
      flex: 1;
    }}
    .section {{ margin-bottom: 22px; }}
    .section-title {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--gold);
      font-size: 11px;
      letter-spacing: .14em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}
    .section-title .count {{
      color: var(--muted);
      letter-spacing: 0;
      text-transform: none;
    }}
    .field {{ margin-bottom: 12px; }}
    .field-label {{
      color: var(--muted);
      font-size: 10px;
      letter-spacing: .1em;
      text-transform: uppercase;
      margin-bottom: 4px;
    }}
    .field-value {{
      font-size: 12.5px;
      line-height: 1.6;
      color: var(--text);
      word-break: break-word;
      white-space: pre-wrap;
    }}
    .field-value.mono {{
      font-family: "SFMono-Regular", ui-monospace, Menlo, monospace;
      font-size: 11.5px;
      background: rgba(255,255,255,.04);
      border: 1px solid rgba(255,255,255,.07);
      border-radius: 10px;
      padding: 10px 12px;
      max-height: 240px;
      overflow-y: auto;
    }}
    .ref-item {{
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 8px;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 12px;
      margin-bottom: 8px;
      cursor: pointer;
      transition: border-color .15s ease, background .15s ease;
    }}
    .ref-item:hover {{ border-color: rgba(214,166,79,.6); background: rgba(214,166,79,.06); }}
    .ref-item.plain {{ cursor: default; }}
    .ref-item.plain:hover {{ border-color: rgba(255,255,255,.08); background: none; }}
    .ref-thumb {{
      width: 54px;
      height: 54px;
      border-radius: 9px;
      object-fit: cover;
      background: #151515;
      flex: 0 0 auto;
    }}
    .ref-thumb.placeholder {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 9px;
      letter-spacing: .1em;
    }}
    .ref-info {{ min-width: 0; flex: 1; }}
    .ref-kind {{
      color: var(--muted);
      font-size: 9px;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    .ref-title {{
      font-size: 12px;
      font-weight: 600;
      margin: 2px 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .ref-edge {{ color: var(--gold); font-size: 10px; }}
    .out-media {{
      width: 100%;
      border-radius: 12px;
      max-height: 260px;
      object-fit: contain;
      background: #0e0e0e;
      display: block;
      margin-bottom: 10px;
    }}
    .out-media.placeholder {{
      height: 140px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: .06em;
      border: 1px dashed rgba(255,255,255,.12);
    }}
    .drawer-empty {{
      color: rgba(255,255,255,.3);
      font-size: 12px;
      font-style: italic;
      padding: 6px 0;
    }}
  </style>
</head>
<body>
  <div id="overlay" class="overlay"></div>
  <aside id="drawer" class="drawer">
    <div class="drawer-head">
      <button id="drawer-close" class="drawer-close" title="关闭">×</button>
      <div id="drawer-kind" class="kind"></div>
      <h3 id="drawer-title"></h3>
      <div id="drawer-path" class="path"></div>
    </div>
    <div id="drawer-body" class="drawer-body"></div>
  </aside>
  <header>
    <div class="eyebrow">VibeFilming Workflow Canvas</div>
    <h1>{escape(graph["project"]["brief"])}</h1>
    <div id="phases" class="topline"></div>
    <div class="toolbar">
      <span id="refresh-status" class="refresh-status warn">静态快照</span>
      <input id="search" placeholder="搜索节点、文件、prompt 摘要…" />
    </div>
  </header>
  <main>
    <section id="canvas">
      <svg id="edges"></svg>
      <div id="columns" class="columns"></div>
    </section>
  </main>
  <script id="graph-data" type="application/json">{graph_json}</script>
  <script>
    let graph = JSON.parse(document.getElementById('graph-data').textContent);
    let connected = new Map();
    let graphSignature = "";
    const POLL_MS = 2000;
    const columnsEl = document.getElementById('columns');
    const edgesEl = document.getElementById('edges');
    const canvasEl = document.getElementById('canvas');
    const searchEl = document.getElementById('search');
    const refreshEl = document.getElementById('refresh-status');

    function graphDigest(data) {{
      return JSON.stringify({{
        stats: data.stats || {{}},
        phases: data.phases || {{}},
        nodes: (data.nodes || []).map(n => [n.id, n.kind, n.stage, n.status, n.path, n.title]),
        edges: (data.edges || []).map(e => [e.source, e.target, e.label, e.kind]),
      }});
    }}

    function rebuildConnected() {{
      connected = new Map();
      for (const edge of graph.edges || []) {{
        if (!connected.has(edge.source)) connected.set(edge.source, new Set());
        if (!connected.has(edge.target)) connected.set(edge.target, new Set());
        connected.get(edge.source).add(edge.target);
        connected.get(edge.target).add(edge.source);
      }}
    }}

    function setRefreshStatus(text, mode = 'live') {{
      refreshEl.textContent = text;
      refreshEl.classList.toggle('live', mode === 'live');
      refreshEl.classList.toggle('warn', mode !== 'live');
    }}

    function statusClass(status) {{
      return 'status-' + String(status || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '_');
    }}

    function kindLabel(kind) {{
      return {{
        script: 'SCRIPT', director_plan: 'PLAN', segment: 'SEGMENT',
        asset: 'ASSET', storyboard: 'BOARD', video: 'VIDEO',
        review: 'REVIEW', composed: 'COMPOSED',
        missing_storyboard: 'TODO', missing_video: 'TODO', reference: 'REF'
      }}[kind] || kind.toUpperCase();
    }}

    function isImage(path) {{ return /\\.(png|jpg|jpeg|webp|gif)$/i.test(path || ''); }}
    function isVideo(path) {{ return /\\.(mp4|mov|webm|mkv)$/i.test(path || ''); }}

    function renderPhases() {{
      const phases = graph.phases || {{}};
      const order = ['story','entity','asset','animate','compose','review'];
      const stats = graph.stats || {{nodes: 0, edges: 0}};
      const generatedAt = graph.project && graph.project.generated_at ? ` · updated ${{graph.project.generated_at}}` : '';
      document.getElementById('phases').innerHTML = order.map(key => {{
        const phase = phases[key] || {{}};
        const status = phase.status || 'pending';
        return `<span class="phase ${{status}}"><span class="dot"></span>${{key}} · ${{status}}</span>`;
      }}).join('') + `<span>${{stats.nodes}} nodes / ${{stats.edges}} edges${{generatedAt}}</span>`;
    }}

    function renderColumns() {{
      columnsEl.innerHTML = '';
      for (const stage of graph.stages || []) {{
        const col = document.createElement('section');
        col.className = 'col';
        col.dataset.stage = stage.id;
        col.innerHTML = `<h2>${{stage.title}}</h2>`;
        const nodes = (graph.nodes || []).filter(n => n.stage === stage.id);
        if (!nodes.length) {{
          const empty = document.createElement('div');
          empty.className = 'empty';
          empty.textContent = '暂无产物';
          col.appendChild(empty);
        }}
        for (const node of nodes) col.appendChild(renderCard(node));
        columnsEl.appendChild(col);
      }}
    }}

    function renderCard(node) {{
      const card = document.createElement('article');
      card.className = 'card ' + (node.status === 'missing' ? 'missing' : '');
      card.dataset.id = node.id;
      card.dataset.text = JSON.stringify(node).toLowerCase();
      if (node.path && isImage(node.path)) {{
        const img = document.createElement('img');
        img.className = 'thumb';
        img.src = node.path;
        img.loading = 'lazy';
        card.appendChild(img);
      }} else if (node.path && isVideo(node.path) && node.status === 'done') {{
        const video = document.createElement('video');
        video.className = 'thumb';
        video.src = node.path;
        video.controls = true;
        video.muted = true;
        video.preload = 'metadata';
        card.appendChild(video);
      }}
      const metaText = node.meta && (node.meta.prompt || node.meta.question)
        ? `<div class="meta">${{escapeHtml(node.meta.prompt || node.meta.question)}}</div>` : '';
      card.insertAdjacentHTML('beforeend', `
        <div class="kind"><span class="status-dot ${{statusClass(node.status)}}"></span>${{kindLabel(node.kind)}} · ${{node.status || 'unknown'}}</div>
        <div class="title">${{escapeHtml(node.title)}}</div>
        <div class="subtitle">${{escapeHtml(node.subtitle || node.path || '')}}</div>
        ${{metaText}}
      `);
      card.addEventListener('mouseenter', () => focusNode(node.id));
      card.addEventListener('mouseleave', clearFocus);
      card.addEventListener('click', ev => {{
        if (ev.target.closest('video')) return;
        openDrawer(node.id);
      }});
      return card;
    }}

    function escapeHtml(text) {{
      return String(text || '').replace(/[&<>"']/g, ch => ({{
        '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
      }}[ch]));
    }}

    function drawEdges() {{
      const canvasBox = canvasEl.getBoundingClientRect();
      edgesEl.setAttribute('width', canvasEl.scrollWidth);
      edgesEl.setAttribute('height', canvasEl.scrollHeight);
      edgesEl.innerHTML = '';
      for (const edge of graph.edges || []) {{
        const source = document.querySelector(`[data-id="${{CSS.escape(edge.source)}}"]`);
        const target = document.querySelector(`[data-id="${{CSS.escape(edge.target)}}"]`);
        if (!source || !target || source.classList.contains('hidden') || target.classList.contains('hidden')) continue;
        const a = source.getBoundingClientRect();
        const b = target.getBoundingClientRect();
        const x1 = a.right - canvasBox.left + canvasEl.scrollLeft;
        const y1 = a.top + a.height / 2 - canvasBox.top + canvasEl.scrollTop;
        const x2 = b.left - canvasBox.left + canvasEl.scrollLeft;
        const y2 = b.top + b.height / 2 - canvasBox.top + canvasEl.scrollTop;
        const dx = Math.max(50, (x2 - x1) * .45);
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', `M ${{x1}} ${{y1}} C ${{x1 + dx}} ${{y1}}, ${{x2 - dx}} ${{y2}}, ${{x2}} ${{y2}}`);
        path.setAttribute('class', 'edge');
        path.dataset.source = edge.source;
        path.dataset.target = edge.target;
        edgesEl.appendChild(path);
      }}
    }}

    function focusNode(id) {{
      document.querySelectorAll('.card').forEach(card => {{
        const related = card.dataset.id === id || (connected.get(id) && connected.get(id).has(card.dataset.id));
        card.classList.toggle('active', card.dataset.id === id);
        card.classList.toggle('dim', !related);
      }});
      document.querySelectorAll('.edge').forEach(edge => {{
        edge.classList.toggle('fade', edge.dataset.source !== id && edge.dataset.target !== id);
      }});
    }}

    function clearFocus() {{
      document.querySelectorAll('.card').forEach(card => card.classList.remove('active', 'dim'));
      document.querySelectorAll('.edge').forEach(edge => edge.classList.remove('fade'));
    }}

    let activeDrawerId = null;
    const overlayEl = document.getElementById('overlay');
    const drawerEl = document.getElementById('drawer');
    const drawerBody = document.getElementById('drawer-body');

    function nodeById(id) {{
      return (graph.nodes || []).find(n => n.id === id) || null;
    }}

    function incomingEdges(id) {{ return (graph.edges || []).filter(e => e.target === id); }}
    function outgoingEdges(id) {{ return (graph.edges || []).filter(e => e.source === id); }}

    function refItemHtml(node, edgeLabel) {{
      if (!node) return '';
      let thumb;
      if (node.path && isImage(node.path)) {{
        thumb = `<img class="ref-thumb" src="${{escapeHtml(node.path)}}" data-fallback="${{escapeHtml(kindLabel(node.kind))}}" />`;
      }} else if (node.path && isVideo(node.path)) {{
        thumb = `<video class="ref-thumb" src="${{escapeHtml(node.path)}}" muted preload="metadata" data-fallback="${{escapeHtml(kindLabel(node.kind))}}"></video>`;
      }} else {{
        thumb = `<div class="ref-thumb placeholder">${{escapeHtml(kindLabel(node.kind))}}</div>`;
      }}
      const edgeTag = edgeLabel ? `<span class="ref-edge">${{escapeHtml(edgeLabel)}}</span>` : '';
      return `<div class="ref-item" data-goto="${{escapeHtml(node.id)}}">
        ${{thumb}}
        <div class="ref-info">
          <div class="ref-kind">${{kindLabel(node.kind)}} ${{edgeTag}}</div>
          <div class="ref-title">${{escapeHtml(node.title)}}</div>
        </div>
      </div>`;
    }}

    function outputMediaHtml(node) {{
      if (node.path && isImage(node.path)) {{
        return `<img class="out-media" src="${{escapeHtml(node.path)}}" data-fallback="产物暂不可预览" />`;
      }}
      if (node.path && isVideo(node.path) && node.status === 'done') {{
        return `<video class="out-media" src="${{escapeHtml(node.path)}}" controls muted preload="metadata" data-fallback="产物暂不可预览"></video>`;
      }}
      return '';
    }}

    function wireMediaFallback(container) {{
      container.querySelectorAll('img[data-fallback], video[data-fallback]').forEach(el => {{
        const swap = () => {{
          if (el.dataset.swapped) return;
          el.dataset.swapped = '1';
          const box = document.createElement('div');
          box.className = el.classList.contains('out-media') ? 'out-media placeholder' : 'ref-thumb placeholder';
          box.textContent = el.dataset.fallback || '暂无预览';
          el.replaceWith(box);
        }};
        if (el.tagName === 'IMG') {{
          if (el.complete && el.naturalWidth === 0) swap();
          el.addEventListener('error', swap);
        }} else {{
          el.addEventListener('error', swap);
        }}
      }});
    }}

    function buildDrawer(node) {{
      const meta = node.meta || {{}};
      const inEdges = incomingEdges(node.id);
      const outEdges = outgoingEdges(node.id);
      let html = '';

      // ---- 输入 ----
      html += `<div class="section"><div class="section-title">输入 <span class="count">Inputs</span></div>`;
      let inputInner = '';
      const promptText = meta.prompt_full || meta.prompt;
      if (promptText) {{
        inputInner += `<div class="field"><div class="field-label">生成 Prompt</div><div class="field-value mono">${{escapeHtml(promptText)}}</div></div>`;
      }}
      const questionText = meta.question_full || meta.question;
      if (questionText) {{
        inputInner += `<div class="field"><div class="field-label">审查问题</div><div class="field-value mono">${{escapeHtml(questionText)}}</div></div>`;
      }}
      const upstream = inEdges.map(e => refItemHtml(nodeById(e.source), e.label)).filter(Boolean).join('');
      if (upstream) {{
        inputInner += `<div class="field"><div class="field-label">上游来源 / 参考</div>${{upstream}}</div>`;
      }}
      if (!inputInner) inputInner = `<div class="drawer-empty">这是一个源节点，没有上游输入。</div>`;
      html += inputInner + `</div>`;

      // ---- 输出 ----
      html += `<div class="section"><div class="section-title">输出 <span class="count">Outputs</span></div>`;
      let outputInner = outputMediaHtml(node);
      if (node.kind === 'review' && meta.conclusion) {{
        outputInner += `<div class="field"><div class="field-label">审查结论</div><div class="field-value mono">${{escapeHtml(meta.conclusion)}}</div></div>`;
      }}
      if (node.path) {{
        outputInner += `<div class="field"><div class="field-label">产物文件</div><div class="field-value">${{escapeHtml(node.path)}}</div></div>`;
      }}
      const downstream = outEdges.map(e => refItemHtml(nodeById(e.target), e.label)).filter(Boolean).join('');
      if (downstream) {{
        outputInner += `<div class="field"><div class="field-label">下游去向</div>${{downstream}}</div>`;
      }}
      if (!outputInner) outputInner = `<div class="drawer-empty">${{node.status === 'missing' ? '尚未生成，暂无产物。' : '暂无可展示的产物。'}}</div>`;
      html += outputInner + `</div>`;

      // ---- 其他信息 ----
      const extras = [];
      if (meta.duration != null) extras.push(['时长', meta.duration + 's']);
      if (meta.ratio) extras.push(['画幅', meta.ratio]);
      if (meta.segments != null) extras.push(['片段数', meta.segments]);
      if (meta.start_time != null || meta.end_time != null) extras.push(['时间轴', `${{meta.start_time ?? '?'}} → ${{meta.end_time ?? '?'}}`]);
      if (meta.onscreen_text) extras.push(['画面文字', meta.onscreen_text]);
      if (Array.isArray(meta.assets_needed) && meta.assets_needed.length) {{
        extras.push(['所需资产', meta.assets_needed.map(a => typeof a === 'string' ? a : JSON.stringify(a)).join('、')]);
      }}
      if (meta.size_bytes != null) extras.push(['文件大小', (meta.size_bytes / 1024).toFixed(1) + ' KB']);
      if (extras.length) {{
        html += `<div class="section"><div class="section-title">其他信息 <span class="count">Details</span></div>`;
        html += extras.map(([k, v]) => `<div class="field"><div class="field-label">${{escapeHtml(k)}}</div><div class="field-value">${{escapeHtml(v)}}</div></div>`).join('');
        html += `</div>`;
      }}
      return html;
    }}

    function renderDrawer() {{
      const node = activeDrawerId ? nodeById(activeDrawerId) : null;
      if (!node) {{ closeDrawer(); return; }}
      document.getElementById('drawer-kind').innerHTML =
        `<span class="status-dot ${{statusClass(node.status)}}"></span>${{kindLabel(node.kind)}} · ${{node.status || 'unknown'}}`;
      document.getElementById('drawer-title').textContent = node.title;
      document.getElementById('drawer-path').textContent = node.path || '';
      drawerBody.innerHTML = buildDrawer(node);
      wireMediaFallback(drawerBody);
      drawerBody.querySelectorAll('[data-goto]').forEach(el => {{
        el.addEventListener('click', () => openDrawer(el.dataset.goto));
      }});
    }}

    function openDrawer(id) {{
      activeDrawerId = id;
      overlayEl.classList.add('open');
      drawerEl.classList.add('open');
      drawerBody.scrollTop = 0;
      renderDrawer();
      focusNode(id);
    }}

    function closeDrawer() {{
      activeDrawerId = null;
      overlayEl.classList.remove('open');
      drawerEl.classList.remove('open');
      clearFocus();
    }}

    overlayEl.addEventListener('click', closeDrawer);
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', ev => {{ if (ev.key === 'Escape') closeDrawer(); }});

    function applySearch() {{
      const q = searchEl.value.trim().toLowerCase();
      document.querySelectorAll('.card').forEach(card => {{
        const hide = q && !card.dataset.text.includes(q);
        card.style.display = hide ? 'none' : '';
        card.classList.toggle('hidden', hide);
      }});
      requestAnimationFrame(drawEdges);
    }}

    function renderAll() {{
      rebuildConnected();
      renderPhases();
      renderColumns();
      applySearch();
      if (activeDrawerId) renderDrawer();
      requestAnimationFrame(drawEdges);
    }}

    async function pollGraph() {{
      if (location.protocol === 'file:') {{
        setRefreshStatus('静态快照：用 --serve 开启实时刷新', 'warn');
        return;
      }}
      try {{
        const response = await fetch('workflow_graph.json?ts=' + Date.now(), {{cache: 'no-store'}});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const next = await response.json();
        const signature = graphDigest(next);
        if (signature !== graphSignature) {{
          graph = next;
          graphSignature = signature;
          renderAll();
          const updated = next.project && next.project.generated_at ? next.project.generated_at : new Date().toLocaleTimeString();
          setRefreshStatus('已更新 ' + updated, 'live');
        }} else {{
          setRefreshStatus('实时刷新中 ' + new Date().toLocaleTimeString(), 'live');
        }}
      }} catch (err) {{
        setRefreshStatus('轮询失败：' + err.message, 'warn');
      }}
    }}

    function startPolling() {{
      graphSignature = graphDigest(graph);
      if (location.protocol === 'file:') {{
        setRefreshStatus('静态快照：用 --serve 开启实时刷新', 'warn');
        return;
      }}
      setRefreshStatus('实时刷新中', 'live');
      window.setInterval(pollGraph, POLL_MS);
      pollGraph();
    }}

    searchEl.addEventListener('input', applySearch);

    renderAll();
    startPolling();
    window.addEventListener('resize', () => requestAnimationFrame(drawEdges));
  </script>
</body>
</html>
"""


def write_canvas(project_dir: Path, graph: dict[str, Any]) -> tuple[Path, Path]:
    graph_path = project_dir / "workflow_graph.json"
    html_path = project_dir / "canvas.html"
    graph_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(graph), encoding="utf-8")
    return graph_path, html_path


def canvas_launch_hint(project_dir: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, str]:
    # 优先尝试自动起一个常驻实时看板：成功就直接给可点开的链接，失败再回退到手动命令。
    live = ensure_live_server(project_dir, host=host, port=port)
    if live:
        return {
            "local_canvas_url": live["url"],
            "status": "实时看板已自动启动，直接在浏览器打开上面的链接即可（页面每 2 秒自动刷新进度）。",
        }
    actual_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    return {
        "local_canvas_url": f"http://{actual_host}:{port}/canvas.html",
        "serve_command": f"python3 -m film.workflow_canvas {project_dir} --serve --port {port}",
        "note": "自动启动实时看板失败，请手动运行 serve_command 再打开上面的链接。",
    }


# ------- 常驻实时看板（单例后台服务，项目间自动切换根目录）-------
_LIVE_LOCK = threading.Lock()
_LIVE_STATE: dict[str, Any] = {"server": None, "thread": None, "project_dir": None, "url": None, "port": None}


class _ResilientHandlerMixin:
    """吞掉客户端断连异常（浏览器关页/刷新，或 2 秒自动轮询取消了还没写完的请求）。

    这类断连会让 sendall/flush 抛 BrokenPipeError/ConnectionResetError，本身无害，
    但默认会冒泡到 socketserver 的错误处理里刷一大段 traceback，污染 CLI 输出。
    这里在写响应体、收尾和单次请求三个写入点分别兜住，静默丢弃即可。
    """

    _CLIENT_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except self._CLIENT_DISCONNECT:
            pass

    def finish(self):
        try:
            super().finish()
        except self._CLIENT_DISCONNECT:
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except self._CLIENT_DISCONNECT:
            self.close_connection = True


class _LiveCanvasHandler(_ResilientHandlerMixin, SimpleHTTPRequestHandler):
    """始终服务 _LIVE_STATE 当前指向的项目目录；每次请求前刷新画布。"""

    def translate_path(self, path: str) -> str:
        project_dir = _LIVE_STATE.get("project_dir")
        if project_dir:
            self.directory = str(project_dir)
        route = urlsplit(path).path
        if route in ("", "/"):
            path = "/canvas.html"
        return super().translate_path(path)

    def do_GET(self):
        route = urlsplit(self.path).path
        if route in ("", "/"):
            self.path = "/canvas.html"
            route = "/canvas.html"
        if route in ("/workflow_graph.json", "/canvas.html"):
            project_dir = _LIVE_STATE.get("project_dir")
            if project_dir:
                try:
                    refresh_canvas(Path(project_dir))
                except Exception as exc:
                    print(f"[WARN] 画布刷新失败: {exc}")
        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        return super().end_headers()

    def log_message(self, *args, **kwargs):
        pass  # 静音 HTTP 访问日志，别污染 CLI 输出


def ensure_live_server(project_dir: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, str] | None:
    """幂等地保证有一个后台常驻画布服务，并把它指向 project_dir。

    - 首次调用：起一个守护线程 HTTP 服务（端口被占用则自动顺延最多 20 个端口）。
    - 后续调用：只把服务根目录切到新的 project_dir，不重复起服务。
    - 起服务失败返回 None，交由调用方回退到手动命令提示。
    """
    project_dir = Path(project_dir)
    with _LIVE_LOCK:
        try:
            refresh_canvas(project_dir)
        except Exception as exc:
            print(f"[WARN] 画布生成失败: {exc}")
        _LIVE_STATE["project_dir"] = str(project_dir)
        if _LIVE_STATE.get("server") is not None:
            return {"url": _LIVE_STATE["url"], "port": str(_LIVE_STATE["port"])}
        actual_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
        server = None
        for candidate in range(port, port + 20):
            try:
                server = ThreadingHTTPServer((host, candidate), _LiveCanvasHandler)
                break
            except OSError:
                continue
        if server is None:
            return None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        bound_port = server.server_port
        url = f"http://{actual_host}:{bound_port}/canvas.html"
        _LIVE_STATE.update({"server": server, "thread": thread, "url": url, "port": bound_port})
        print(f"\n\x1b[92m● 实时工作流看板\x1b[0m {url}  \x1b[90m(浏览器打开，随进度自动刷新)\x1b[0m\n", flush=True)
        return {"url": url, "port": str(bound_port)}


def refresh_canvas(project_dir: Path) -> tuple[dict[str, Any], Path, Path]:
    graph = build_graph(project_dir)
    graph_path, html_path = write_canvas(project_dir, graph)
    return graph, graph_path, html_path


def serve_canvas(project_dir: Path, host: str, port: int, open_browser: bool = False) -> int:
    graph, graph_path, html_path = refresh_canvas(project_dir)

    class CanvasHandler(_ResilientHandlerMixin, SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(project_dir), **kwargs)

        def do_GET(self):
            route = urlsplit(self.path).path
            if route in ("", "/"):
                self.path = "/canvas.html"
                route = "/canvas.html"
            if route in ("/workflow_graph.json", "/canvas.html"):
                try:
                    refresh_canvas(project_dir)
                except Exception as exc:
                    print(f"[WARN] 画布刷新失败: {exc}")
            return super().do_GET()

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            return super().end_headers()

    server = ThreadingHTTPServer((host, port), CanvasHandler)
    actual_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    url = f"http://{actual_host}:{server.server_port}/canvas.html"
    print(f"\n实时工作流画布已启动：{url}", flush=True)
    print("保持这个命令运行，浏览器会自动轮询 workflow_graph.json 更新进度。\n", flush=True)
    print(json.dumps({
        "project": str(project_dir),
        "graph": str(graph_path),
        "html": str(html_path),
        "url": url,
        "polling": "workflow_graph.json every 2s",
        "nodes": graph["stats"]["nodes"],
        "edges": graph["stats"]["edges"],
    }, ensure_ascii=False, indent=2))
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止工作流画布服务")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或启动 VibeFilming 项目的只读工作流画布")
    parser.add_argument("project", help="项目 id 或项目目录路径")
    parser.add_argument("--serve", action="store_true", help="启动本地网站，页面自动轮询刷新")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"本地服务监听地址，默认 {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"本地服务端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开")
    args = parser.parse_args(argv)
    project_dir = resolve_project(args.project)
    if args.serve:
        return serve_canvas(project_dir, args.host, args.port, open_browser=args.open)
    graph, graph_path, html_path = refresh_canvas(project_dir)
    print(json.dumps({
        "project": str(project_dir),
        "graph": str(graph_path),
        "html": str(html_path),
        **canvas_launch_hint(project_dir),
        "nodes": graph["stats"]["nodes"],
        "edges": graph["stats"]["edges"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
