"""Test 24: Studio 自动建项目 + 素材入库离线冒烟。

真起一个 studio HTTP server 打真实请求，但 agent 传 None、不调任何模型。锁死的不变量：
  T1 没有活跃项目时首次上传自动建项目
  T2 项目名称为空时 /api/projects/new 拒绝并提示补名称
  T3 新建只落工作区，绝不创建 Agent 任务
  T4 建完可连续上传参考视频与多张人物图，实体名只在单张时硬锁
  T5 素材落盘 + manifest.materials / entities 登记正确
  T6 上传提示进入待注入队列，此刻依然零任务
  T7 首条消息才把素材前缀拼进用户消息，且队列随之清空
  T8 新建与切换执行模式写回 manifest
  T9 复刻规范在新建与后续编辑时写回 manifest
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # 直接 python smoke_tests/test_24_xxx.py
sys.path.insert(0, str(_HERE.parent))     # python -m smoke_tests.test_24_xxx

from _common import banner, fail, info, ok  # noqa: E402

from film import studio                     # noqa: E402
from film import workspace as ws            # noqa: E402


def _valid_mp4() -> bytes:
    import imageio_ffmpeg

    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "fixture.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-nostdin",
                "-v", "error",
                "-f", "lavfi",
                "-i", "color=c=black:s=16x16:d=0.2",
                "-an",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-y",
                str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        return path.read_bytes()


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
MP4 = _valid_mp4()
REMAKE_SPEC = "世界观：雪地生存。核心资源：熔炉等级。禁止改成金币体系。"

_STATE: dict = {}
_CREATED_PIDS: list = []


def _base() -> str:
    return _STATE["base"]


def _post_json(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(_base() + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post_upload(category: str, entity: str, filename: str, blob: bytes,
                 execution_mode: str = "",
                 remake_spec: str = "") -> tuple[int, dict]:
    """手工拼 multipart：字段先于文件，和前端 FormData 的顺序保持一致。"""
    boundary = "----vfSmoke" + uuid.uuid4().hex

    def field(name: str, value: str) -> bytes:
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n").encode("utf-8")

    body = (field("category", category) + field("entity_name", entity)
            + field("execution_mode", execution_mode)
            + field("remake_spec", remake_spec))
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
             f'filename="{filename}"\r\nContent-Type: application/octet-stream'
             "\r\n\r\n").encode("utf-8")
    body += blob + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        _base() + "/api/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def t1_upload_without_project():
    ws.clear_active_project()
    status, data = _post_upload(
        "character_image", "", "girl.png", PNG,
        execution_mode="autonomous", remake_spec=REMAKE_SPEC)
    assert status == 200 and data.get("ok"), f"首次上传应自动建项目：{status} {data}"
    pid = data.get("project_id")
    assert pid and ws.get_active_project() == pid, f"自动项目未激活：{data}"
    _CREATED_PIDS.append(pid)
    manifest = ws.read_manifest(pid)
    assert len((manifest.get("materials") or {}).get("character_image") or []) == 1, manifest
    assert manifest["execution_mode"]["mode"] == "autonomous", manifest
    assert manifest["remake_spec"] == REMAKE_SPEC, manifest


def t2_blank_brief_rejected():
    status, data = _post_json("/api/projects/new", {"brief": "   "})
    assert status == 400, f"空名称应被拒，实际 HTTP {status}：{data}"
    assert "项目名称" in data.get("error", ""), f"提示语未引导补项目名称：{data}"


def t3_new_project_creates_no_job():
    status, created = _post_json("/api/projects/new", {
        "brief": "T24 只建工作区", "execution_mode": "autonomous",
        "remake_spec": REMAKE_SPEC,
    })
    assert status == 201, f"建项目应返回 201，实际 {status}：{created}"
    pid = created.get("project_id")
    assert pid, f"响应缺 project_id：{created}"
    _CREATED_PIDS.append(pid)
    _STATE["pid"] = pid
    assert not studio._STATE["jobs"], f"新建阶段不该有 Agent 任务：{studio._STATE['jobs']}"
    assert ws.get_active_project() == pid, "新建后活跃项目未切过去"
    assert created.get("execution_mode", {}).get("mode") == "autonomous", created
    assert ws.read_manifest(pid)["remake_spec"] == REMAKE_SPEC


def t4_pre_shoot_uploads():
    status, video = _post_upload("reference_video", "", "ref.mp4", MP4)
    assert status == 200 and video.get("ok"), f"参考视频入库失败：{status} {video}"
    status, locked = _post_upload("character_image", "小雨", "girl.png", PNG)
    assert status == 200 and locked.get("ok"), f"人物图入库失败：{status} {locked}"
    assert locked.get("entity_locked") and locked.get("entity_name") == "小雨", \
        f"给了实体名却没硬锁：{locked}"
    status, plain = _post_upload("character_image", "", "extra.png", PNG)
    assert status == 200 and plain.get("ok"), f"第二张人物图入库失败：{status} {plain}"
    assert not plain.get("entity_locked"), f"没给实体名却硬锁了：{plain}"


def t5_manifest_and_disk():
    manifest = ws.read_manifest(_STATE["pid"])
    mats = manifest.get("materials") or {}
    assert len(mats.get("reference_video") or []) == 1, f"参考视频数不对：{mats}"
    assert len(mats.get("character_image") or []) == 2, f"人物图数不对：{mats}"
    assert "小雨" in (manifest.get("entities") or {}), f"entities 未登记小雨：{manifest.get('entities')}"
    missing = [p for group in mats.values() for p in group if not Path(p).is_file()]
    assert not missing, f"manifest 登记了但磁盘上不存在：{missing}"


def t6_pending_notes_queued():
    notes = studio._STATE["pending_notes"].get(_STATE["pid"]) or []
    assert len(notes) == 3, f"待注入素材提示应为 3 条，实际 {len(notes)}：{notes}"
    assert any("多模态模型拆镜" in n and "execution_mode" in n for n in notes), f"参考视频未带拆镜引导：{notes}"
    assert any("entity_locked=true" in n for n in notes), f"硬锁信息未进提示：{notes}"
    assert not studio._STATE["jobs"], f"挂素材阶段不该有 Agent 任务：{studio._STATE['jobs']}"


def t7_notes_injected_on_first_message():
    merged = studio._with_pending_notes("拍一条 30 秒宠物公益短片")
    assert merged.startswith("[System] 用户刚通过 Studio 上传"), f"素材前缀缺失：{merged[:80]}"
    assert merged.endswith("拍一条 30 秒宠物公益短片"), f"用户原文被改写：{merged[-60:]}"
    assert "girl.png" in merged or "materials" in merged, f"提示未带素材路径：{merged[:200]}"
    assert not studio._STATE["pending_notes"].get(_STATE["pid"]), "注入后队列未清空"


def t8_execution_mode_switch():
    status, data = _post_json("/api/execution-mode", {"mode": "human_gated"})
    assert status == 200 and data.get("ok"), f"模式切换失败：{status} {data}"
    assert data["execution_mode"]["mode"] == "human_gated", data
    manifest = ws.read_manifest(_STATE["pid"])
    assert manifest["execution_mode"]["mode"] == "human_gated", manifest["execution_mode"]


def t9_first_message_mode_preselected():
    ws.clear_active_project()
    pid = studio._ensure_active_project(
        "T24 首条消息自动建项目", execution_mode="autonomous")
    _CREATED_PIDS.append(pid)
    manifest = ws.read_manifest(pid)
    assert manifest["execution_mode"]["mode"] == "autonomous", manifest


def t10_remake_spec_update():
    ws.set_active_project(_STATE["pid"])
    updated = REMAKE_SPEC + "\nUI术语：熔炉升级。"
    status, data = _post_json("/api/remake-spec", {
        "project_id": _STATE["pid"],
        "remake_spec": updated,
    })
    assert status == 200 and data.get("ok"), f"复刻规范更新失败：{status} {data}"
    assert data["remake_spec"] == updated, data
    assert ws.read_manifest(_STATE["pid"])["remake_spec"] == updated


CASES = [
    ("T1 首次上传自动建项目", t1_upload_without_project),
    ("T2 空项目名被拒", t2_blank_brief_rejected),
    ("T3 新建不触发任务", t3_new_project_creates_no_job),
    ("T4 建完连续上传素材", t4_pre_shoot_uploads),
    ("T5 落盘与 manifest 登记", t5_manifest_and_disk),
    ("T6 素材提示进待注入队列", t6_pending_notes_queued),
    ("T7 首条消息才注入素材", t7_notes_injected_on_first_message),
    ("T8 执行模式切换", t8_execution_mode_switch),
    ("T9 首条消息继承预选模式", t9_first_message_mode_preselected),
    ("T10 复刻规范新建与编辑持久化", t10_remake_spec_update),
]


def main():
    banner("Test 24: Studio 自动建项目 + 素材入库")
    started = studio.start_studio_server(None, port=8899)
    if not started:
        fail("studio server 起不来（8899~8918 端口都被占）")
        return False
    _STATE["base"] = started["url"].rstrip("/")
    info(f"studio: {_STATE['base']}")

    results = []
    t_all = time.time()
    try:
        for label, case in CASES:
            t0 = time.time()
            try:
                case()
                results.append((label, True))
                ok(f"{label}  ({time.time() - t0:.2f}s)")
            except Exception as e:
                results.append((label, False))
                fail(f"{label}  ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
    finally:
        ws.clear_active_project()
        for pid in _CREATED_PIDS:
            shutil.rmtree(ws.PROJECTS_ROOT / pid, ignore_errors=True)
        leftovers = [pid for pid in _CREATED_PIDS if (ws.PROJECTS_ROOT / pid).exists()]
        info(f"临时项目已清理：{len(_CREATED_PIDS) - len(leftovers)}/{len(_CREATED_PIDS)}")

    n_ok = sum(1 for _, passed in results if passed)
    info(f"{n_ok} / {len(CASES)} 通过，总耗时 {time.time() - t_all:.2f}s")
    return n_ok == len(CASES) and not leftovers


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
