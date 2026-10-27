"""Test 23: 参考视频拆镜（素材收录 + VLM 逆向拆镜 + 双产物反写）离线冒烟。

全程 mock，不真调 VLM、不发网络请求。锁死的不变量：
  T0 主 Skill 与格式规则动态加载，缺文件明确报错
  T1 解析器 JSON 注释块优先于 Markdown 表
  T2 无 JSON 块时回退 Markdown 表解析，列名归一正确
  T3 时间不连续必须抛 BreakdownParseError（供上层切兜底）
  T4 豆包主路径抛错时切阿里 qwen-vl-max-latest 兜底
  T5 analyze_reference_video 落拆镜产物并生成 materials/script_from_breakdown.md 草稿
  T6 upload_reference_material 给了 entity_name 时把图硬锁成唯一参考形象
  T7 拆镜完成后 story 阶段自动 in_progress；未传 mode 时不得默认 imitate（须先问用户）
  T8 豆包返回内容但解析失败时，analyze_reference_video 同样切阿里兜底
  T9 两线程并发 update_manifest 时改动互不覆盖（Studio 上传线程 vs Agent 线程）
  T10 mode 参数：imitate/fission 落进 breakdown 与 manifest；非法 mode 报错
   T11 单镜头与小数时间轴
   T12 一次多模态调用产出全部观察，不额外调用文本模型
   T13 多模态观察落 reference_breakdown.json / script_from_breakdown.md，并注入 director_plan
   T14 缺失观察字段保留未知，不补造视觉信息
   T15 强制重拆不得覆盖已定稿 script.md
   T16 缓存 mode 与请求一致时仍修复 manifest 中的陈旧 mode
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # 直接 python smoke_tests/test_23_xxx.py
sys.path.insert(0, str(_HERE.parent))     # python -m smoke_tests.test_23_xxx

from _common import banner, fail, info, ok  # noqa: E402

from film import film_sdk as sdk           # noqa: E402
from film import reference_breakdown as rb  # noqa: E402
from film import tools                     # noqa: E402
from film import workspace as ws           # noqa: E402

_HEADER = ("| 分镜 | 景别 | 运镜 | 画面内容 | 拍摄指导 | 画面文案/花字 "
           "| 口播/台词 | 音效/BGM | 开始时间 | 结束时间 | 时长 |")
_SEP = "|---|---|---|---|---|---|---|---|---|---|---|"

MD_OK = "\n".join([
    _HEADER, _SEP,
    "| 1 | 中景 | 固定 | 雨夜巷口纸箱里的橘猫 | 85mm f/2.0 与猫同高 | - | - | 雨声渐入 | 00:00 | 00:03 | 3s |",
    "| 2 | 特写 | 手持 | 女孩蹲下拉开罐头拉环 | 50mm f/1.8 侧逆光 | 「它等了很久」 | 你饿了吧 | 拉环脆响 | 00:03 | 00:07 | 4s |",
    "| 3 | 全景 | 拉 | 女孩抱猫走进楼道暖光 | 35mm 慢速后拉 | - | - | 弦乐渐强 | 00:07 | 00:10 | 3s |",
])

# 表里 3 行、JSON 块里 2 段且内容带标记：解析结果来自哪边一目了然
MD_WITH_JSON = MD_OK + "\n\n" + (
    '<!-- JSON:[{"分镜": "1", "景别": "中景", "运镜": "固定", "画面内容": "JSON块首镜", '
    '"拍摄指导": "85mm", "画面文案/花字": "-", "口播/台词": "-", "音效/BGM": "雨声", '
    '"开始时间": "00:00", "结束时间": "00:04", "时长": "4s"}, '
    '{"分镜": "2", "景别": "特写", "运镜": "手持", "画面内容": "JSON块次镜", '
    '"拍摄指导": "50mm", "画面文案/花字": "-", "口播/台词": "你饿了吧", "音效/BGM": "钢琴", '
    '"开始时间": "00:04", "结束时间": "00:09", "时长": "5s"}] -->'
)

MD_TIME_GAP = "\n".join([
    _HEADER, _SEP,
    "| 1 | 中景 | 固定 | 雨夜巷口 | 85mm | - | - | 雨声 | 00:00 | 00:03 | 3s |",
    "| 2 | 特写 | 手持 | 罐头拉环 | 50mm | - | - | 脆响 | 00:05 | 00:08 | 3s |",
])

# 两级 LLM 分析的确定性假数据（med-ai-factory 两级分析口径，离线冒烟专用）
FAKE_SHOT_INFO = {"shotAngle": "平视", "shotSize": "特写", "shotCameraMovement": "固定",
                  "expressionStyle": "痛点引入", "expressionPurpose": "痛托气氛,产品功效"}
FAKE_SCRIPT_SUMMARY = {"scriptStructureFormula": "痛托气氛/产品功效", "scriptHookForm": "情绪调动",
                       "scriptMainScenarios": "巷口", "scriptUserPainPoints": "孤独感",
                       "scriptProductSellingPoints": "陪伴", "scriptTargetAudience": "爱宠人群"}


def _stub_analysis(shot_info=FAKE_SHOT_INFO, script_summary=FAKE_SCRIPT_SUMMARY):
    """仅替换媒体探测，观察由 VLM 响应 fixture 提供。"""
    original = rb._probe_reference_duration
    rb._probe_reference_duration = lambda video_path: 10.0
    return original


def _observed(shot_info=FAKE_SHOT_INFO, script_summary=FAKE_SCRIPT_SUMMARY):
    segments = rb._parse_breakdown(MD_OK)
    for seg in segments:
        seg["shot_info"] = dict(shot_info or {})
    return json.dumps({
        "segments": segments, "script_summary": script_summary or {},
        "uncertainties": ["未能辨认背景文字"],
    }, ensure_ascii=False)

_STATE: dict = {}
_CREATED_PIDS: list = []


def _new_project(brief: str) -> str:
    pid = ws.project_create(brief, execution_mode="human_gated")["project_id"]
    _CREATED_PIDS.append(pid)
    return pid


def _handler(pid: str):
    return type("H", (), {"_film_active_project": pid})()


def _tmp_file(suffix: str, data: bytes = b"\x00") -> Path:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with open(fd, "wb") as f:
        f.write(data)
    return Path(path)


def t0_skill_prompt_loading():
    prompt = rb._load_breakdown_prompt()
    assert "shotCameraMovement" in prompt and "uncertainties" in prompt
    original_path = rb._BREAKDOWN_PROMPT_PATH
    with tempfile.TemporaryDirectory() as tmp:
        rb._BREAKDOWN_PROMPT_PATH = Path(tmp) / "reference_breakdown_prompt.md"
        try:
            path = rb._BREAKDOWN_PROMPT_PATH
            path.write_text(
                "第一版观察规则 shotCameraMovement uncertainties")
            before = rb._analysis_signature()
            assert "第一版观察规则" in rb._load_breakdown_prompt()
            path.write_text(
                "第二版观察规则 shotCameraMovement uncertainties")
            assert rb._analysis_signature() != before
            assert "第二版观察规则" in rb._load_breakdown_prompt()
            path.unlink()
            try:
                rb._load_breakdown_prompt()
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("缺执行器 prompt 应显式失败")
        finally:
            rb._BREAKDOWN_PROMPT_PATH = original_path


def t1_json_first():
    segments = rb._parse_breakdown(MD_WITH_JSON)
    assert isinstance(segments, list), f"应返回 list，实际 {type(segments)}"
    assert len(segments) >= 2, f"JSON 块有 2 段，实际解析出 {len(segments)} 段"
    assert len(segments) == 2, f"JSON 块未优先（拿到 {len(segments)} 段，像是表格的 3 行）"
    assert rb._field(segments[0], "start") == "00:00", \
        f"首镜开始时间应为 00:00，实际 {rb._field(segments[0], 'start')!r}"
    assert rb._field(segments[0], "content") == "JSON块首镜", \
        f"解析结果不是来自 JSON 块：{rb._field(segments[0], 'content')!r}"


def t2_markdown_fallback():
    segments = rb._parse_breakdown(MD_OK)
    assert len(segments) == 3, f"表格 3 行数据，实际解析出 {len(segments)} 段"
    first = segments[0]
    assert rb._field(first, "shot_size") == "中景", f"景别列映射错误：{first}"
    assert rb._field(first, "camera") == "固定", f"运镜列映射错误：{first}"
    assert rb._field(first, "start") == "00:00", f"开始时间列映射错误：{first}"
    assert rb._field(first, "onscreen_text") == "", "占位符「-」应归一成空串"
    assert rb._field(segments[2], "end") == "00:10", f"末镜结束时间错误：{segments[2]}"


def t3_time_gap_rejected():
    try:
        rb._parse_breakdown(MD_TIME_GAP)
    except rb.BreakdownParseError as e:
        assert "不连续" in str(e), f"报错信息应指出时间不连续：{e}"
    else:
        raise AssertionError("时间断点未被拦截，_parse_breakdown 没抛 BreakdownParseError")
    try:
        rb._parse_breakdown(MD_OK, expected_duration=15)
    except rb.BreakdownParseError as e:
        assert "真实时长" in str(e), f"报错信息应指出末镜未覆盖真实时长：{e}"
    else:
        raise AssertionError("末镜未覆盖真实时长却未被拦截")


def t4_qwen_fallback():
    def boom(*a, **kw):
        raise RuntimeError("mock doubao fail")

    original_doubao, original_qwen = sdk.doubao_video_understand, rb._qwen_vl_fallback
    try:
        sdk.doubao_video_understand = boom
        rb._qwen_vl_fallback = lambda video_path, prompt: MD_OK
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "offline-test",
            "QWEN_VL_MODEL": rb.QWEN_VL_MODEL,
        }), \
             patch.object(rb, "_probe_reference_duration", return_value=10):
            raw_text, provider_used = rb._vlm_breakdown("fake.mp4")
    finally:
        sdk.doubao_video_understand = original_doubao
        rb._qwen_vl_fallback = original_qwen

    assert provider_used == rb.QWEN_VL_MODEL == "qwen-vl-max-latest", \
        f"豆包失败后应切阿里兜底，实际 provider_used={provider_used!r}"
    assert len(rb._parse_breakdown(raw_text)) == 3, "兜底返回的表格应可正常解析"


def t5_dual_artifacts_and_script():
    pid = _new_project("测试23参考视频拆镜")
    _STATE["pid"] = pid
    video = _tmp_file(".mp4")
    try:
        m = ws.read_manifest(pid)
        m.setdefault("materials", {})["reference_video"] = [str(video)]
        ws.write_manifest(pid, m)

        original = rb._vlm_breakdown
        restore_analysis = _stub_analysis()
        try:
            rb._vlm_breakdown = lambda video_path, **kw: (_observed(), "doubao")
            result = tools._DECORATED_TOOLS["analyze_reference_video"](_handler(pid), {})
        finally:
            rb._vlm_breakdown = original
            rb._probe_reference_duration = restore_analysis
    finally:
        video.unlink(missing_ok=True)

    pdir = ws.project_dir(pid)
    for rel in (f"materials/{rb.BREAKDOWN_JSON_FILENAME}",
                f"materials/{rb.BREAKDOWN_MD_FILENAME}",
                "materials/script_from_breakdown.md"):
        assert (pdir / rel).is_file(), f"产物缺失：{rel}"
    script = (pdir / "materials" / "script_from_breakdown.md").read_text(encoding="utf-8")
    assert "source: reference_video" in script, "拆镜草稿 front matter 缺 source: reference_video"
    assert "mode: pending" in script, "未指定模式时拆镜草稿应标 mode: pending（待问用户）"
    assert "draft: true" in script, "拆镜产物应标注 draft: true（正式剧本待按模式改写）"
    assert result["segments_count"] == 3, f"segments_count 应为 3，实际 {result['segments_count']}"
    assert result["provider_used"] == "doubao", f"provider_used 错误：{result['provider_used']!r}"
    assert result["total_duration"] == 10, f"总时长应为 10s，实际 {result['total_duration']}"
    assert result["mode"] == "pending", f"未指定 mode 应返回 pending，实际 {result['mode']!r}"

    # 两级分析产物（med-ai-factory 技术方案移植）：narration / shot_info / script_summary 全部落盘
    breakdown = json.loads((pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).read_text(encoding="utf-8"))
    assert breakdown.get("narration") == "[00:03] 你饿了吧", \
        f"narration 应按 [MM:SS] 拼台词，实际 {breakdown.get('narration')!r}"
    assert (breakdown.get("script_summary") or {}).get("scriptStructureFormula") == "痛托气氛/产品功效", \
        f"script_summary 缺失：{breakdown.get('script_summary')}"
    assert breakdown["segments"][0].get("shot_info", {}).get("shotSize") == "特写", \
        f"每段应带 shot_info：{breakdown['segments'][0].get('shot_info')}"
    assert "registries" in breakdown, "应预留 registries 注册表骨架"
    assert "镜头信息" in script and "全局脚本总结" in script, "拆镜草稿应写入两级分析结果"


def t6_image_hard_lock():
    pid = _new_project("测试23素材硬锁")
    image = _tmp_file(".png", b"\x89PNG\r\n\x1a\n")
    image_2 = _tmp_file(".png", b"\x89PNG\r\n\x1a\nsecond")
    try:
        result = tools._DECORATED_TOOLS["upload_reference_material"](_handler(pid), {
            "file_path": str(image),
            "category": "character_image",
            "entity_name": "女主角",
        })
        tools._DECORATED_TOOLS["upload_reference_material"](_handler(pid), {
            "file_path": str(image_2),
            "category": "character_image",
            "entity_name": "女主角",
        })
    finally:
        image.unlink(missing_ok=True)
        image_2.unlink(missing_ok=True)

    pdir = ws.project_dir(pid)
    entity_files = list((pdir / "entities").glob("女主角_*.png"))
    assert entity_files, f"entities/ 下没有女主角参考图：{list((pdir / 'entities').iterdir())}"
    m = ws.read_manifest(pid)
    entity = (m.get("entities") or {}).get("女主角")
    assert entity, f"manifest.entities 缺 女主角：{m.get('entities')}"
    assert entity.get("locked_from_upload") is True, f"未硬锁：{entity}"
    assert len(entity.get("ref_pack") or []) == 2, f"同一实体的多张参考图被覆盖：{entity}"
    assert len(entity.get("source_materials") or []) == 2, f"源素材列表未完整保留：{entity}"
    assert m.get("_locked_no_new") is True, "有硬锁实体时应打 _locked_no_new 标志"
    assert result["entity_locked"] is True, f"返回值未标记硬锁：{result}"
    assert Path(result["materials_path"]).is_file(), f"materials/ 下没有素材副本：{result}"
    escaped = pdir.parent / "escaped.png"
    copied = rb._copy_no_overwrite(
        entity_files[0], pdir / "entities", "../../escaped.png")
    assert copied.parent == pdir / "entities", f"文件名越过项目目录：{copied}"
    assert not escaped.exists(), f"项目外产生了实体文件：{escaped}"


def t8_parse_fail_triggers_qwen_fallback():
    pid = _new_project("测试23解析失败切兜底")
    video = _tmp_file(".mp4")
    try:
        m = ws.read_manifest(pid)
        m.setdefault("materials", {})["reference_video"] = [str(video)]
        ws.write_manifest(pid, m)

        original_vlm, original_qwen = rb._vlm_breakdown, rb._qwen_vl_fallback
        restore_analysis = _stub_analysis(shot_info={}, script_summary={})
        try:
            # 豆包有内容但时间不连续 → _parse_breakdown 抛 BreakdownParseError
            rb._vlm_breakdown = lambda video_path, **kw: (MD_TIME_GAP, "doubao")
            rb._qwen_vl_fallback = lambda video_path, prompt: MD_OK
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "offline-test"}):
                result = tools._DECORATED_TOOLS["analyze_reference_video"](_handler(pid), {})
        finally:
            rb._vlm_breakdown = original_vlm
            rb._qwen_vl_fallback = original_qwen
            rb._probe_reference_duration = restore_analysis
    finally:
        video.unlink(missing_ok=True)

    assert result["ok"] is True, f"解析失败切兜底后应成功返回：{result}"
    assert result["provider_used"] == "qwen-vl-max-latest", \
        f"parse 失败应切阿里兜底，实际 provider_used={result['provider_used']!r}"
    assert result["segments_count"] >= 2, f"兜底表格应解析出 >=2 段，实际 {result['segments_count']}"
    breakdown = ws.project_dir(pid) / "materials" / rb.BREAKDOWN_JSON_FILENAME
    assert breakdown.is_file(), f"拆镜产物缺失：{breakdown}"


def t7_story_phase_done():
    pid = _STATE.get("pid")
    assert pid, "T5 未成功建项目，T7 无法验证"
    story = (ws.read_manifest(pid).get("phases") or {}).get("story") or {}
    # 拆镜只是"写剧本之前"的分析步骤：story 标 in_progress，等按模式写剧本后才 done
    assert story.get("status") == "in_progress", f"story 阶段应自动 in_progress（待写剧本），实际 {story}"
    assert story.get("source") == "reference_video", f"story.source 应为 reference_video，实际 {story}"
    # 未传 mode 时显式标 pending，避免强制重拆后遗留旧确认。
    assert story.get("mode") == "pending", \
        f"未传 mode 时 story.mode 应为 pending，实际 {story.get('mode')!r}"


def t9_concurrent_update_manifest():
    """Studio HTTP 线程写 materials、Agent 线程写 phases，两边改动都必须留在最终 manifest。"""
    pid = _new_project("测试23并发写manifest")
    rounds = 40
    errors: list = []
    barrier = threading.Barrier(2)

    def writer(bucket: str, tag: str):
        try:
            barrier.wait(timeout=5)
            for i in range(rounds):
                ws.update_manifest(pid, lambda m, i=i: m.setdefault(bucket, {}).__setitem__(
                    f"{tag}_{i}", i))
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=("materials", "upload")),
        threading.Thread(target=writer, args=("phases", "agent")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads), "并发写 manifest 线程未在 20s 内结束（疑似死锁）"
    assert not errors, f"并发写 manifest 抛错：{errors[0]!r}"

    m = ws.read_manifest(pid)
    missing_up = [i for i in range(rounds) if f"upload_{i}" not in (m.get("materials") or {})]
    missing_ag = [i for i in range(rounds) if f"agent_{i}" not in (m.get("phases") or {})]
    assert not missing_up, f"上传线程的 {len(missing_up)} 条改动被覆盖：{missing_up[:5]}"
    assert not missing_ag, f"Agent 线程的 {len(missing_ag)} 条改动被覆盖：{missing_ag[:5]}"
    assert m.get("project_id") == pid, f"并发写坏了 manifest 基础字段：{m.get('project_id')!r}"


def t10_mode_fission_and_invalid():
    pid = _new_project("测试23模式参数")
    video = _tmp_file(".mp4")
    try:
        m = ws.read_manifest(pid)
        m.setdefault("materials", {})["reference_video"] = [str(video)]
        ws.write_manifest(pid, m)

        original = rb._vlm_breakdown
        restore_analysis = _stub_analysis(shot_info={}, script_summary={})
        try:
            rb._vlm_breakdown = lambda video_path, **kw: (_observed(), "doubao")
            result = tools._DECORATED_TOOLS["analyze_reference_video"](_handler(pid), {"mode": "fission"})
        finally:
            rb._vlm_breakdown = original
            rb._probe_reference_duration = restore_analysis
    finally:
        video.unlink(missing_ok=True)

    assert result["mode"] == "fission", f"mode 应透传为 fission，实际 {result['mode']!r}"
    pdir = ws.project_dir(pid)
    breakdown = json.loads((pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).read_text(encoding="utf-8"))
    assert breakdown.get("mode") == "fission", f"breakdown.json 应记录 fission，实际 {breakdown.get('mode')!r}"
    script = (pdir / "materials" / "script_from_breakdown.md").read_text(encoding="utf-8")
    assert "mode: fission" in script, "拆镜草稿 front matter 缺 mode: fission"
    story = (ws.read_manifest(pid).get("phases") or {}).get("story") or {}
    assert story.get("mode") == "fission", f"story.mode 应为 fission，实际 {story.get('mode')!r}"

    # 非法 mode 必须报错
    try:
        tools._DECORATED_TOOLS["analyze_reference_video"](_handler(pid), {"mode": "abc"})
    except ValueError as e:
        assert "mode 非法" in str(e), f"报错信息应指出 mode 非法：{e}"
        return
    raise AssertionError("非法 mode 未被拦截")


def t11_shot_info_unit():
    """原片可以是一个连续镜头，小数时间不能四舍五入到整秒。"""
    seg = rb._parse_breakdown(MD_OK)[0]
    seg.update({"开始时间": "00:00", "结束时间": "00:04.25", "时长": "4.25s"})
    parsed = rb._parse_breakdown(json.dumps({"segments": [seg]}), expected_duration=4.25)
    assert len(parsed) == 1
    assert rb._norm_time(rb._field(parsed[0], "end")) == "00:04.25"
    seg["时长"] = "4s"
    try:
        rb._parse_breakdown(json.dumps({"segments": [seg]}), expected_duration=4.25)
    except rb.BreakdownParseError:
        pass
    else:
        raise AssertionError("时长与起止相差 0.25s 应被拦截")


def t12_script_summary_unit():
    pid = _new_project("测试23一次多模态")
    video = _tmp_file(".mp4")
    response = {"raw": {"choices": [{"message": {"content": _observed()}}]}}
    try:
        with patch.object(rb, "_probe_reference_duration", return_value=10), \
             patch.object(sdk, "doubao_video_understand", return_value=response) as vlm, \
             patch.object(sdk, "doubao_chat", side_effect=AssertionError("禁止文字补视觉")):
            rb._analyze_reference_video(_handler(pid), {"video_path": str(video)})
        assert vlm.call_count == 1
        assert vlm.call_args.kwargs["fps"] == 4
        saved = json.loads((ws.project_dir(pid) / "materials" / rb.BREAKDOWN_JSON_FILENAME).read_text())
        assert saved["script_summary"] == FAKE_SCRIPT_SUMMARY
        assert saved["uncertainties"] == ["未能辨认背景文字"]
        assert saved["segments"][0]["shot_info"] == FAKE_SHOT_INFO
    finally:
        video.unlink(missing_ok=True)


def t13_full_pipeline_enriched():
    """两级分析产物落 breakdown.json / 拆镜草稿，且 inject 把镜头信息带进 director_plan。"""
    pid = _new_project("测试23两级分析")
    video = _tmp_file(".mp4")
    try:
        m = ws.read_manifest(pid)
        m.setdefault("materials", {})["reference_video"] = [str(video)]
        ws.write_manifest(pid, m)

        original = rb._vlm_breakdown
        restore_analysis = _stub_analysis()
        try:
            rb._vlm_breakdown = lambda video_path, **kw: (_observed(), "doubao")
            tools._DECORATED_TOOLS["analyze_reference_video"](
                _handler(pid), {"mode": "imitate"}
            )
        finally:
            rb._vlm_breakdown = original
            rb._probe_reference_duration = restore_analysis
    finally:
        video.unlink(missing_ok=True)

    pdir = ws.project_dir(pid)
    breakdown = json.loads((pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).read_text(encoding="utf-8"))
    assert breakdown["narration"] == "[00:03] 你饿了吧", f"narration 错误：{breakdown['narration']!r}"
    assert breakdown["script_summary"] == FAKE_SCRIPT_SUMMARY, \
        f"script_summary 错误：{breakdown['script_summary']}"
    assert all(seg.get("shot_info") == FAKE_SHOT_INFO for seg in breakdown["segments"]), \
        "每段都应带 shot_info"

    try:
        tools._DECORATED_TOOLS["inject_breakdown_to_plan"](_handler(pid), {})
    except RuntimeError as exc:
        assert "人工确认" in str(exc), f"应被剧本确认闸门拦截：{exc}"
    else:
        raise AssertionError("story 未完成时不应允许注入编导方案")
    (pdir / "script.md").write_text(
        "---\nsource: reference_video\nmode: imitate\n"
        "source_breakdown: materials/拆片表.md\n---\n\n# 正式剧本\n",
        encoding="utf-8",
    )
    ws.update_phase(pid, "story", status="done", mode="imitate")
    injected = tools._DECORATED_TOOLS["inject_breakdown_to_plan"](_handler(pid), {})
    assert injected["segments_injected"] == 3, f"应注入 3 段，实际 {injected}"
    plan = json.loads((pdir / "director_plan.json").read_text(encoding="utf-8"))
    source_breakdown = plan.get("source_breakdown") or {}
    assert source_breakdown.get("json") == str(pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME), \
        f"plan 未登记结构化拆镜来源：{source_breakdown}"
    assert source_breakdown.get("markdown") == str(pdir / "materials" / rb.BREAKDOWN_TABLE_FILENAME), \
        f"plan 未登记 Markdown 拆片表来源：{source_breakdown}"
    seg0 = plan["segments"][0]
    assert seg0.get("shot_angle") == "平视", f"plan 应带 shot_angle：{seg0}"
    assert seg0.get("shot_camera_movement") == "固定", f"plan 应带 shot_camera_movement：{seg0}"
    assert seg0.get("expression_style") == "痛点引入", f"plan 应带 expression_style：{seg0}"
    assert seg0.get("expression_purpose") == "痛托气氛,产品功效", f"plan 应带 expression_purpose：{seg0}"
    assert seg0.get("shooting") == "85mm f/2.0 与猫同高", f"plan 应带拍摄指导：{seg0}"
    assert seg0.get("audio") == "雨声渐入", f"plan 应带音效设计：{seg0}"
    plan["segments"][0]["duration"] = 999
    plan["segments"][0]["shooting"] = "stale shooting"
    plan["segments"][0]["audio"] = "stale audio"
    plan["total_duration"] = 999
    plan["segments"].append({"id": "extra", "content": "不应保留"})
    (pdir / "director_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    tools._DECORATED_TOOLS["inject_breakdown_to_plan"](_handler(pid), {})
    strict_plan = json.loads((pdir / "director_plan.json").read_text(encoding="utf-8"))
    assert len(strict_plan["segments"]) == 3, f"仿写不得保留额外段：{strict_plan['segments']}"
    assert strict_plan["segments"][0]["duration"] == 3, \
        f"仿写锁定字段必须覆盖冲突值：{strict_plan['segments'][0]}"
    assert strict_plan["segments"][0]["shooting"] == "85mm f/2.0 与猫同高", strict_plan
    assert strict_plan["segments"][0]["audio"] == "雨声渐入", strict_plan
    assert strict_plan["total_duration"] == 10, strict_plan
    try:
        tools._DECORATED_TOOLS["inject_breakdown_to_plan"](
            _handler(pid), {"technique_only": True}
        )
    except ValueError as exc:
        assert "仿写模式强制整表注入" in str(exc), f"仿写保护报错不清晰：{exc}"
    else:
        raise AssertionError("仿写模式不应允许收窄为 technique_only")


def t14_analysis_failure_tolerated():
    """旧 Markdown 没有观察字段时不补造；已有景别与运镜仍可归一。"""
    pid = _new_project("测试23分析失败容错")
    video = _tmp_file(".mp4")
    try:
        m = ws.read_manifest(pid)
        m.setdefault("materials", {})["reference_video"] = [str(video)]
        ws.write_manifest(pid, m)

        original = rb._vlm_breakdown
        restore_analysis = _stub_analysis()

        try:
            rb._vlm_breakdown = lambda video_path, **kw: (MD_OK, "doubao")
            result = tools._DECORATED_TOOLS["analyze_reference_video"](_handler(pid), {})
        finally:
            rb._vlm_breakdown = original
            rb._probe_reference_duration = restore_analysis
    finally:
        video.unlink(missing_ok=True)

    assert result["ok"] is True and result["segments_count"] == 3, f"分析失败不应阻断拆镜：{result}"
    breakdown = json.loads((ws.project_dir(pid) / "materials" / rb.BREAKDOWN_JSON_FILENAME)
                           .read_text(encoding="utf-8"))
    assert breakdown["script_summary"] == {}, f"总结失败应写空：{breakdown['script_summary']}"
    assert all("shotAngle" not in seg["shot_info"] for seg in breakdown["segments"]), "未知角度不补造"
    assert breakdown["narration"] == "[00:03] 你饿了吧", "narration 不依赖 LLM，失败时也应产出"


def t15_final_script_is_preserved():
    """已有正式剧本时，重建拆镜草稿必须写到 materials/，不能覆盖 script.md。"""
    pid = _new_project("测试23保护正式剧本")
    pdir = ws.project_dir(pid)
    final_script = "---\nsource: reference_video\nmode: fission\n---\n\n# 正式剧本\n"
    (pdir / "script.md").write_text(final_script, encoding="utf-8")

    output = rb._write_script_from_breakdown(
        pid,
        rb._parse_breakdown(MD_OK),
        mode="fission",
        script_summary=FAKE_SCRIPT_SUMMARY,
    )

    assert (pdir / "script.md").read_text(encoding="utf-8") == final_script, \
        "重拆不应覆盖已定稿 script.md"
    assert output == pdir / "materials" / "script_from_breakdown.md", \
        f"新拆镜草稿应写入 materials/script_from_breakdown.md，实际 {output}"
    assert "draft: true" in output.read_text(encoding="utf-8"), "替代草稿应保留 draft: true"


def t16_cached_mode_repairs_manifest():
    """缓存已是目标 mode 时，也必须同步 manifest 这一唯一状态源。"""
    pid = _new_project("测试23缓存模式同步")
    pdir = ws.project_dir(pid)
    video = _tmp_file(".mp4")
    manifest = ws.read_manifest(pid)
    manifest.setdefault("materials", {})["reference_video"] = [str(video)]
    ws.write_manifest(pid, manifest)
    segments = rb._parse_breakdown(MD_OK)
    breakdown = {
        "contract": "vibefilming.reference-breakdown/v1",
        "source": "reference_video",
        "source_video": str(video),
        "source_identity": rb._video_source_identity(str(video)),
        "analysis_signature": rb._analysis_signature(),
        "provider_used": "doubao",
        "mode": "fission",
        "segments_count": len(segments),
        "total_duration": 10,
        "source_duration": 10,
        "script_summary": {},
        "segments": segments,
    }
    (pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).write_text(
        json.dumps(breakdown, ensure_ascii=False), encoding="utf-8"
    )
    ws.update_phase(pid, "story", status="in_progress", mode="imitate")

    try:
        result = rb._analyze_reference_video(_handler(pid), {"mode": "fission"})

        assert result.get("cached") is True, f"应复用缓存：{result}"
        story = ws.read_manifest(pid)["phases"]["story"]
        assert story.get("mode") == "fission", f"manifest mode 未被缓存调用修复：{story}"

        stale = json.loads((pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).read_text(encoding="utf-8"))
        stale["mode"] = "imitate"
        (pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).write_text(
            json.dumps(stale, ensure_ascii=False), encoding="utf-8"
        )
        result = rb._analyze_reference_video(_handler(pid), {})
        assert result["mode"] == "fission", f"空参数复用不得让旧 JSON 覆盖 manifest：{result}"
        repaired = json.loads(
            (pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME).read_text(encoding="utf-8")
        )
        assert repaired["mode"] == "fission", f"缓存快照未向 manifest 权威值对齐：{repaired}"
        other_video = _tmp_file(".mp4")
        try:
            ws.update_manifest(
                pid,
                lambda data: data.setdefault("materials", {}).__setitem__(
                    "reference_video", [str(other_video)]
                ),
            )
            with patch.object(rb, "_probe_reference_duration", return_value=10), \
                 patch.object(rb, "_vlm_breakdown", return_value=(_observed(), "doubao")) as vlm:
                changed = rb._analyze_reference_video(_handler(pid), {})
            assert not changed.get("cached") and vlm.call_count == 1
            with patch.object(rb, "_analysis_signature", return_value="updated-rules"), \
                 patch.object(rb, "_probe_reference_duration", return_value=10), \
                 patch.object(rb, "_vlm_breakdown", return_value=(_observed(), "doubao")) as vlm:
                changed = rb._analyze_reference_video(_handler(pid), {})
            assert not changed.get("cached") and vlm.call_count == 1
        finally:
            other_video.unlink(missing_ok=True)
    finally:
        video.unlink(missing_ok=True)


def t17_force_without_mode_resets_confirmation():
    """强制重拆继承裂变选择，但旧拆镜与剧本的确认必须失效。"""
    pid = _new_project("测试23强制重拆模式复位")
    video = _tmp_file(".mp4")
    try:
        manifest = ws.read_manifest(pid)
        manifest.setdefault("materials", {})["reference_video"] = [str(video)]
        ws.write_manifest(pid, manifest)
        ws.update_phase(pid, "story", status="in_progress", mode="fission")
        ws.set_execution_gate(pid, "breakdown_approval", "approved", actor="user")
        ws.set_execution_gate(pid, "script_approval", "approved", actor="user")
        original_vlm = rb._vlm_breakdown
        restore_analysis = _stub_analysis(shot_info={}, script_summary={})
        try:
            rb._vlm_breakdown = lambda video_path, **kw: (_observed(), "doubao")
            rb._analyze_reference_video(_handler(pid), {"force": True})
        finally:
            rb._vlm_breakdown = original_vlm
            rb._probe_reference_duration = restore_analysis
    finally:
        video.unlink(missing_ok=True)
    story = ws.read_manifest(pid)["phases"]["story"]
    assert story.get("mode") == "fission", f"强制重拆应继承用户选择：{story}"
    assert not ws.execution_gate_approved(pid, "breakdown_approval")
    ws.set_execution_gate(pid, "breakdown_approval", "approved", actor="user")
    assert not ws.execution_gate_approved(pid, "script_approval"), "重新拆镜后旧剧本确认残留"


def t18_fission_requires_explicit_segment_mapping():
    """裂变只按显式 target -> source 映射搬技法，不按同名段号猜。"""
    pid = _new_project("测试23裂变显式映射")
    pdir = ws.project_dir(pid)
    segments = rb._parse_breakdown(MD_OK)
    for seg in segments:
        seg["shot_info"] = dict(FAKE_SHOT_INFO)
    breakdown = {
        "contract": "vibefilming.reference-breakdown/v1",
        "source": "reference_video",
        "source_video": "source.mp4",
        "provider_used": "doubao",
        "mode": "fission",
        "segments_count": len(segments),
        "total_duration": 10,
        "source_duration": 10,
        "script_summary": {},
        "segments": segments,
    }
    breakdown_path = pdir / "materials" / rb.BREAKDOWN_JSON_FILENAME
    breakdown_path.write_text(json.dumps(breakdown, ensure_ascii=False), encoding="utf-8")
    table_path = pdir / "materials" / rb.BREAKDOWN_TABLE_FILENAME
    table_path.write_text(
        rb._build_breakdown_table(
            segments,
            source_video="source.mp4",
            mode="fission",
            total_duration=10,
            script_summary={},
        ),
        encoding="utf-8",
    )
    (pdir / "script.md").write_text(
        "---\nsource: reference_video\nmode: fission\n"
        "source_breakdown: materials/拆片表.md\n---\n\n# 裂变正式剧本\n",
        encoding="utf-8",
    )
    (pdir / "director_plan.json").write_text(
        json.dumps({
            "contract": "vibefilming.director-plan/v1",
            "segments": [{"id": "new_01"}, {"id": "new_02"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    ws.update_phase(pid, "story", status="done", mode="fission")
    try:
        tools._DECORATED_TOOLS["inject_breakdown_to_plan"](_handler(pid), {})
    except ValueError as exc:
        assert "segment_mapping" in str(exc), f"裂变缺映射报错不清晰：{exc}"
    else:
        raise AssertionError("裂变模式缺 segment_mapping 不应继续")
    try:
        tools._DECORATED_TOOLS["inject_breakdown_to_plan"](
            _handler(pid), {"segment_mapping": {"new_01": "seg_02"}}
        )
    except ValueError as exc:
        assert "覆盖全部目标段" in str(exc), f"裂变映射不完整报错不清晰：{exc}"
    else:
        raise AssertionError("裂变模式不应接受未覆盖全部目标段的映射")
    result = tools._DECORATED_TOOLS["inject_breakdown_to_plan"](
        _handler(pid), {
            "segment_mapping": {"new_01": "seg_02", "new_02": "original"}
        }
    )
    assert result["segments_injected"] == 1, result
    plan = json.loads((pdir / "director_plan.json").read_text(encoding="utf-8"))
    assert plan["segments"][0]["shot_angle"] == "平视", plan
    assert plan["source_breakdown"]["adopted"] == [
        {"target_segment": "new_01", "source_segment": "seg_02"}
    ], plan["source_breakdown"]
    assert plan["source_breakdown"]["original"] == ["new_02"], plan["source_breakdown"]


def t19_breakdown_failure_marks_phase_failed():
    pid = _new_project("拆镜失败状态")
    ws.update_phase(pid, "breakdown", status="in_progress")
    with patch.object(
        rb,
        "_analyze_reference_video_impl",
        side_effect=RuntimeError("mock breakdown failure"),
    ):
        try:
            rb._analyze_reference_video(_handler(pid), {})
        except RuntimeError as exc:
            assert "mock breakdown failure" in str(exc), exc
        else:
            raise AssertionError("拆镜失败应向调用方抛出原始异常")
    phase = ws.read_manifest(pid)["phases"]["breakdown"]
    assert phase["status"] == "failed", phase
    assert "mock breakdown failure" in phase["note"], phase


CASES = [
    ("T0 Skill prompt 动态加载与明确失败", t0_skill_prompt_loading),
    ("T1 解析器 JSON 优先", t1_json_first),
    ("T2 解析器 Markdown 回退", t2_markdown_fallback),
    ("T3 时间断点失败", t3_time_gap_rejected),
    ("T4 豆包失败切阿里兜底", t4_qwen_fallback),
    ("T5 拆镜产物 + 独立脚本草稿", t5_dual_artifacts_and_script),
    ("T6 上传图片硬锁实体", t6_image_hard_lock),
    ("T7 story 阶段 in_progress 待写剧本", t7_story_phase_done),
    ("T8 解析失败切阿里兜底", t8_parse_fail_triggers_qwen_fallback),
    ("T9 并发写 manifest 不互相覆盖", t9_concurrent_update_manifest),
    ("T10 mode 仿写/裂变与非法值", t10_mode_fission_and_invalid),
    ("T11 单镜头与小数时间轴", t11_shot_info_unit),
    ("T12 一次多模态与不确定观察", t12_script_summary_unit),
    ("T13 多模态全链路 + plan 注入", t13_full_pipeline_enriched),
    ("T14 缺失观察不补造", t14_analysis_failure_tolerated),
    ("T15 强制重拆保护正式剧本", t15_final_script_is_preserved),
    ("T16 缓存 mode 修复 manifest", t16_cached_mode_repairs_manifest),
    ("T17 强制重拆复位 mode", t17_force_without_mode_resets_confirmation),
    ("T18 裂变显式段映射", t18_fission_requires_explicit_segment_mapping),
    ("T19 拆镜失败回写 failed", t19_breakdown_failure_marks_phase_failed),
]


def main():
    banner("Test 23: 参考视频拆镜（全程 mock）")
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
        for pid in _CREATED_PIDS:
            shutil.rmtree(ws.PROJECTS_ROOT / pid, ignore_errors=True)
        leftovers = [pid for pid in _CREATED_PIDS if (ws.PROJECTS_ROOT / pid).exists()]
        info(f"临时项目已清理：{len(_CREATED_PIDS) - len(leftovers)}/{len(_CREATED_PIDS)}")

    n_ok = sum(1 for _, passed in results if passed)
    info(f"{n_ok} / {len(CASES)} 通过，总耗时 {time.time() - t_all:.2f}s")
    return n_ok == len(CASES) and not leftovers


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
