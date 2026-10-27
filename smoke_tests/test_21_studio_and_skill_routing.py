import json
import os
import queue
import re
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import agentmain
from film import studio
from film import tools
from film import video_models
from film import workspace as ws
from film import workflow_canvas


class DummyAgent:
    def __init__(self):
        self.is_running = False
        self.aborted = False

    def put_task(self, message, source="user", images=None, project_id=None):
        self.project_id = project_id
        output = queue.Queue()
        first = (
            "\n\nTurn 1 ...\n\n"
            "<summary>先创建项目并建立工作区。</summary>\n"
            "🛠️ project_create({\"brief\":\"x\"})\n\n"
        )
        second = (
            "\n\nTurn 2 ...\n\n"
            "<summary>项目已创建，开始生成主角素材。</summary>\n"
            "🛠️ gen_image({\"prompt\":\"lamp\"})\n\n"
        )
        output.put({"turn": 1})
        output.put({"next": first, "turn": 1})
        output.put({"next": second, "turn": 2})
        output.put({"done": first + second + f"完成：{message}", "turn": 2})
        return output

    def abort(self):
        self.aborted = True


class StudioAndSkillRoutingTest(unittest.TestCase):
    def test_project_output_paths_cannot_escape_project(self):
        with TemporaryDirectory() as tmp:
            with patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
                manifest = ws.project_create("路径安全")
                handler = SimpleNamespace(_film_active_project=manifest["project_id"])
                safe = tools._project_path(handler, "reviews", "frames")
                self.assertEqual(
                    (Path(manifest["project_dir"]) / "reviews" / "frames").resolve(),
                    safe,
                )
                for bad in ("/tmp/outside", "../../outside", "../outside"):
                    with self.assertRaises(ValueError):
                        tools._project_path(handler, "reviews", bad)

    def test_debug_stages_use_manifest_names_only(self):
        canonical = (
            "breakdown", "story", "entity", "asset",
            "animate", "compose", "review",
        )
        self.assertEqual(canonical, tuple(key for key, _ in studio.DEBUG_STOP_POINTS))
        schema = next(
            item["function"] for item in tools.build_film_schema()
            if item["function"]["name"] == "set_debug_mode"
        )
        self.assertEqual(
            list(canonical),
            schema["parameters"]["properties"]["stop_after"]["enum"],
        )
        self.assertNotIn("plan", schema["parameters"]["properties"]["stop_after"]["enum"])
        self.assertNotIn("storyboard", schema["parameters"]["properties"]["stop_after"]["enum"])
        html = studio.render_studio_html()
        debug_options = re.findall(
            r'<option value="([^"]+)"(?: selected)?>[^<]+</option>',
            html,
        )
        self.assertEqual(list(canonical), debug_options)
        self.assertIn("entity: '编导方案'", html)
        self.assertNotIn("plan: '编导方案'", html)
        self.assertNotIn("storyboard: '故事板（分镜）'", html)
        with TemporaryDirectory() as tmp:
            with patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
                manifest = ws.project_create("调试阶段迁移")
                pid = manifest["project_id"]
                ws.update_manifest(
                    pid,
                    lambda data: data.__setitem__(
                        "debug", {"enabled": True, "stop_after": "storyboard"}
                    ),
                )
                self.assertEqual(
                    {"enabled": True, "stop_after": "asset"},
                    studio._debug_state(pid),
                )
                self.assertEqual("asset", ws.read_manifest(pid)["debug"]["stop_after"])

    def test_seedance_and_ecommerce_contracts_are_aligned(self):
        with patch.object(
            tools,
            "_video_profile",
            return_value=video_models.PROFILES["2.0"],
        ):
            with self.assertRaisesRegex(ValueError, "4-15"):
                tools._DECORATED_TOOLS["gen_video_t2v"](
                    SimpleNamespace(), {"prompt": "产品特写", "name": "too_short",
                                        "unit_id": "too_short",
                                        "duration": 3, "ratio": "9:16"}
                )
            with self.assertRaisesRegex(ValueError, "最多 9"):
                tools._DECORATED_TOOLS["gen_video_t2v"](
                    SimpleNamespace(), {
                        "prompt": "产品特写", "name": "too_many_refs",
                        "unit_id": "too_many_refs",
                        "duration": 4, "ratio": "9:16",
                        "reference_images": [
                            f"https://example.com/{i}.png" for i in range(10)
                        ],
                    }
                )

        skills_text = "\n".join(
            path.read_text(encoding="utf-8") for path in Path("skills").rglob("*.md")
        )
        review = Path("skills/skill_review/SKILL.md").read_text(encoding="utf-8")
        xhs = Path("skills/skill_xhs_food_video/SKILL.md").read_text(encoding="utf-8")
        tools_source = Path("film/tools.py").read_text(encoding="utf-8")
        from film.video_models import PROFILES
        self.assertEqual(9, PROFILES["2.0"].max_images)
        self.assertEqual(30, PROFILES["2.5"].max_images)
        for skill in (
            "skill_director", "skill_movie",
            "skill_prompt_engineering", "skill_review",
        ):
            text = Path(f"skills/{skill}/SKILL.md").read_text(
                encoding="utf-8")
            self.assertNotIn("Seedance 2.0 为 `4-15 秒`", text)
            self.assertIn("capability", text)
        self.assertIn("不做滑窗审", review)
        self.assertNotIn("时长覆盖", review)
        self.assertNotIn("xhs_research_brief.json", xhs)
        self.assertIn("画风", skills_text)
        self.assertNotIn("一次传两个 clip", tools_source)
        self.assertIn("同条件对比", skills_text)

    def test_visual_reference_policy_is_owned_by_skills(self):
        video_schema = next(
            item["function"] for item in tools.build_film_schema()
            if item["function"]["name"] == "gen_video_t2v"
        )
        image_schema = next(
            item["function"] for item in tools.build_film_schema()
            if item["function"]["name"] == "gen_image"
        )
        self.assertNotIn("reference_images", image_schema["parameters"]["required"])
        self.assertNotIn("reference_images", video_schema["parameters"]["required"])
        self.assertNotIn(
            "scene_reference_images",
            video_schema["parameters"]["properties"],
        )
        skills_text = "\n".join(
            path.read_text(encoding="utf-8") for path in Path("skills").rglob("*.md")
        )
        self.assertIn("含商品主体的项目禁止裸生成视觉素材", skills_text)
        self.assertIn("生图需要真实产品图", skills_text)
        self.assertIn("生视频还需要本段的场景或关键帧锚点", skills_text)

    def test_transitions_use_the_single_concat_entry(self):
        tool_names = {
            item["function"]["name"] for item in tools.build_film_schema()
        }
        self.assertNotIn("video_crossfade", tool_names)
        self.assertNotIn("video_crossfade", tools._DECORATED_TOOLS)
        self.assertFalse(hasattr(tools.sdk, "video_crossfade"))
        self.assertTrue(callable(tools.sdk.video_concat))
        self.assertNotIn(
            "`video_crossfade`",
            Path("skills/skill_movie/SKILL.md").read_text(encoding="utf-8"),
        )

    def test_audio_set_default_matches_schema(self):
        with TemporaryDirectory() as tmp:
            with patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
                manifest = ws.project_create("音频默认值")
                handler = SimpleNamespace(_film_active_project=manifest["project_id"])
                with patch.object(
                    tools.sdk,
                    "video_set_audio",
                    return_value={"path": "ok.mp4"},
                ) as mocked:
                    tools._DECORATED_TOOLS["audio_process"](
                        handler,
                        {
                            "action": "set_audio",
                            "name": "final",
                            "video": "video.mp4",
                            "audio": "audio.mp3",
                        },
                    )
                self.assertEqual("first", mocked.call_args.kwargs["duration"])

    def test_studio_html_has_chat_and_canvas(self):
        html = studio.render_studio_html()
        self.assertIn('id="messages"', html)
        self.assertIn('id="input"', html)
        self.assertIn('id="canvas"', html)
        self.assertIn("/api/chat", html)
        self.assertIn("clamp(280px, 24vw, 380px) minmax(0, 1fr)", html)
        self.assertNotIn("minmax(280px, 32vw)", html)
        self.assertIn("/api/state", html)
        self.assertIn(
            "grid-template-rows: auto minmax(0, 1fr) auto auto auto auto auto",
            html,
        )
        self.assertRegex(
            html,
            r"\.chat\s*\{[^}]*min-height:\s*0;[^}]*overflow-y:\s*auto;",
        )
        self.assertRegex(html, r"\.messages\s*\{[^}]*min-height:\s*0;")
        self.assertIn("height: 100dvh", html)
        self.assertIn(".empty-canvas[hidden], iframe[hidden]", html)
        self.assertNotIn("event.type === 'delta'", html)
        self.assertIn("event.type === 'trace'", html)
        self.assertIn("activity-steps", html)
        self.assertIn("step-detail", html)
        self.assertIn('id="project-modal"', html)
        self.assertIn("/api/projects/new", html)
        self.assertIn("/api/projects/open", html)
        self.assertIn("/api/video-model", html)
        self.assertIn('id="active-video-model-picker"', html)
        self.assertIn('id="new-video-model-picker"', html)
        self.assertIn('id="outputs-panel"', html)
        self.assertIn('id="outputs-list"', html)
        self.assertIn("renderOutputs(data.outputs, data.outputs_error)", html)
        self.assertIn("loadProjects", html)
        self.assertIn("configureMaterialTypes", html)
        self.assertIn("type.max_items", html)
        self.assertIn("const label = p.label || p.phase", html)
        self.assertNotIn('data-category="reference_video"', html)

    def test_manifest_drives_material_types_phases_and_canvas_stages(self):
        with TemporaryDirectory() as tmp:
            with patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
                manifest = ws.project_create("动态工作流")
                pid = manifest["project_id"]

                def customize(data):
                    data["material_types"] = [{
                        "id": "moodboard",
                        "label": "情绪板",
                        "media_kind": "image",
                        "accept": "image/*",
                        "extensions": [".png"],
                        "max_bytes": 1024,
                        "max_items": 3,
                        "entity_lock": False,
                    }]
                    data["workflow"] = {
                        "phases": [{"id": "ingest", "label": "素材理解"}],
                        "stages": [{"id": "ingest", "label": "输入"}],
                    }
                    data["phases"] = {"ingest": {"status": "in_progress"}}

                ws.update_manifest(pid, customize)
                current = ws.read_manifest(pid)
                self.assertEqual("moodboard", ws.material_type_definitions(current)[0]["id"])
                self.assertEqual("素材理解", ws.workflow_phase_definitions(current)[0]["label"])
                source = Path(tmp) / "mood.png"
                source.write_bytes(b"\x89PNG\r\n\x1a\n")
                uploaded = tools._DECORATED_TOOLS["upload_reference_material"](
                    SimpleNamespace(_film_active_project=pid),
                    {"file_path": str(source), "category": "moodboard"},
                )
                self.assertTrue(uploaded["ok"])
                self.assertIn(
                    uploaded["materials_path"],
                    ws.read_manifest(pid)["materials"]["moodboard"],
                )
                graph = workflow_canvas.build_graph(Path(manifest["project_dir"]))
                self.assertEqual(
                    [{"id": "ingest", "title": "输入"}],
                    graph["stages"],
                )
                self.assertEqual(
                    [{"id": "ingest", "label": "素材理解"}],
                    graph["phase_definitions"],
                )

        phase_schema = next(
            item["function"] for item in tools.build_film_schema()
            if item["function"]["name"] == "project_update_phase"
        )
        self.assertNotIn(
            "enum",
            phase_schema["parameters"]["properties"]["phase"],
        )

    def test_project_list_is_sorted_and_skips_broken_manifests(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (pid, brief) in enumerate((("p-old", "旧项目"), ("p-new", "新项目"))):
                pdir = root / pid
                pdir.mkdir()
                manifest = {
                    "project_id": pid,
                    "brief": brief,
                    "created_at": f"2026-07-0{index + 1} 10:00:00",
                    "phases": {
                        "story": {"status": "done"},
                        "compose": {"status": "pending"},
                    },
                }
                manifest_path = pdir / "manifest.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                stamp = 1000 + index
                os.utime(manifest_path, (stamp, stamp))
            broken = root / "p-broken"
            broken.mkdir()
            (broken / "manifest.json").write_text("{bad json", encoding="utf-8")
            oversized = root / "p-oversized"
            oversized.mkdir()
            (oversized / "manifest.json").write_text(json.dumps({
                "project_id": "p-oversized",
                "brief": "历史超长规范",
                "remake_spec": "x" * 12001,
                "phases": {},
            }), encoding="utf-8")
            with patch.object(ws, "PROJECTS_ROOT", root):
                projects = ws.list_projects()
            self.assertEqual(["p-new", "p-old"], [item["project_id"] for item in projects])
            self.assertEqual(1, projects[0]["done_phases"])
            self.assertEqual(2, projects[0]["total_phases"])

    def test_explicit_project_id_is_created_by_only_one_thread(self):
        with TemporaryDirectory() as tmp, patch.object(
            ws, "PROJECTS_ROOT", Path(tmp)
        ):
            barrier = threading.Barrier(8)
            results = []
            result_lock = threading.Lock()

            def create():
                barrier.wait()
                try:
                    ws.project_create(
                        "并发创建",
                        project_id="shared-project",
                    )
                    result = "created"
                except FileExistsError:
                    result = "exists"
                with result_lock:
                    results.append(result)

            workers = [threading.Thread(target=create) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(1, results.count("created"))
            self.assertEqual(7, results.count("exists"))
            manifest = ws.read_manifest("shared-project")
            self.assertEqual("并发创建", manifest["brief"])

    def test_project_switch_resets_agent_context(self):
        client = SimpleNamespace(
            backend=SimpleNamespace(history=[{"role": "user"}]),
            last_tools="cached",
        )
        agent = SimpleNamespace(
            is_running=False,
            history=["old"],
            handler=object(),
            llmclients=[client],
        )
        with patch.dict(studio._STATE, {"agent": agent, "jobs": {}}):
            studio._reset_agent_context(agent)
        self.assertEqual([], agent.history)
        self.assertIsNone(agent.handler)
        self.assertEqual([], client.backend.history)
        self.assertEqual("", client.last_tools)
        agent.is_running = True
        with patch.dict(studio._STATE, {"agent": agent, "jobs": {}}), self.assertRaises(RuntimeError):
            studio._reset_agent_context(agent)

    def test_studio_trace_preserves_summaries_and_repeated_tool_calls(self):
        text = (
            "<summary>seg01-seg04 故事板完成。继续 seg05-seg08</summary>\n"
            "🛠️ gen_image({\"prompt\":\"故事板 05\"})\n"
            "🛠️ gen_image({\"prompt\":\"故事板 06\"})\n"
            "<summary>全部8段视频生成成功！现在进入审片阶段</summary>\n"
            "🛠️ file_read({\"path\":\"skills/skill_review/SKILL.md\"})\n"
        )
        events = studio._extract_trace_events(text)
        self.assertEqual(
            ["summary", "tool", "tool", "summary", "tool"],
            [event["kind"] for event in events],
        )
        self.assertEqual("gen_image", events[1]["tool"])
        self.assertEqual("gen_image", events[2]["tool"])
        self.assertIn("故事板 05", events[1]["detail"])
        self.assertIn("故事板 06", events[2]["detail"])
        self.assertEqual("file_read", events[-1]["tool"])

    def test_studio_exposes_only_the_final_user_facing_reply(self):
        item = {
            "done": "完整内部日志",
            "outputs": [
                "<summary>读取内部 skill。</summary>\n🛠️ file_read({\"path\":\"skills/x/SKILL.md\"})",
                "<summary>创建项目。</summary>\n🛠️ project_create({\"brief\":\"测试\"})",
                (
                    "<summary>项目已创建，等待用户决策。</summary>\n\n"
                    "项目已经创建。请选择 A、B 或 C 方向。"
                ),
            ],
        }
        reply = studio._public_reply(item)
        self.assertEqual("项目已经创建。请选择 A、B 或 C 方向。", reply)
        self.assertNotIn("summary", reply)
        self.assertNotIn("🛠️", reply)

    def test_studio_job_streams_to_done(self):
        old_agent = studio._STATE.get("agent")
        old_jobs = studio._STATE.get("jobs")
        studio._STATE["agent"] = DummyAgent()
        studio._STATE["jobs"] = {}
        try:
            result = studio._create_job("测试任务")
            job_id = result["job_id"]
            for _ in range(100):
                job = studio._STATE["jobs"][job_id]
                if job["status"] == "done":
                    break
                threading.Event().wait(0.01)
            self.assertEqual("done", job["status"])
            types = [event["type"] for event in job["events"]]
            self.assertEqual(["trace", "trace", "trace", "trace", "done"], types)
            self.assertEqual("summary", job["events"][0]["kind"])
            self.assertEqual("先创建项目并建立工作区。", job["events"][0]["text"])
            self.assertEqual("project_create", job["events"][1]["tool"])
            self.assertEqual("gen_image", job["events"][3]["tool"])
            self.assertIn("lamp", job["events"][3]["detail"])
            self.assertEqual("完成：测试任务", job["events"][-1]["text"])
        finally:
            studio._STATE["agent"] = old_agent
            studio._STATE["jobs"] = old_jobs

    def test_new_session_ignores_last_active_project_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_project = root / "old-project"
            old_project.mkdir()
            (root / ".active_project").write_text("old-project", encoding="utf-8")
            old_session = ws._SESSION_ACTIVE_PROJECT
            old_canvas_project = studio._STATE.get("canvas_project")
            old_canvas_url = studio._STATE.get("canvas_url")
            try:
                with patch.object(ws, "PROJECTS_ROOT", root):
                    ws.clear_active_project()
                    studio._STATE["canvas_project"] = None
                    studio._STATE["canvas_url"] = None
                    self.assertIsNone(ws.get_active_project())
                    self.assertEqual(
                        {"project_id": None, "canvas_url": None},
                        studio._active_canvas(),
                    )

                    ws.set_active_project("old-project")
                    with patch.object(
                        studio.workflow_canvas,
                        "ensure_live_server",
                        return_value={"url": "http://127.0.0.1:9999/canvas.html"},
                    ):
                        state = studio._active_canvas()
                    self.assertEqual("old-project", state["project_id"])
                    self.assertEqual(
                        "http://127.0.0.1:9999/canvas.html", state["canvas_url"]
                    )
            finally:
                ws._SESSION_ACTIVE_PROJECT = old_session
                studio._STATE["canvas_project"] = old_canvas_project
                studio._STATE["canvas_url"] = old_canvas_url

    def test_workflow_canvas_supports_horizontal_pan(self):
        html = workflow_canvas.render_html({
            "project": {"brief": "测试画布"},
            "stats": {},
            "phases": {},
            "nodes": [],
            "edges": [],
        })
        self.assertIn("overflow: scroll", html)
        self.assertIn("cursor: grab", html)
        self.assertIn("viewportEl.addEventListener('pointermove'", html)
        self.assertIn("viewportEl.scrollLeft", html)
        self.assertIn("event.shiftKey", html)
        self.assertIn("data-artifact-content", html)
        self.assertIn("loadArtifactContent(node)", html)
        self.assertIn("JSON.stringify(JSON.parse(content), null, 2)", html)

    def test_workflow_canvas_renders_breakdown_markdown_safely(self):
        with TemporaryDirectory() as tmp:
            with patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
                manifest = ws.project_create("Markdown 预览")
                project_dir = Path(manifest["project_dir"])
                breakdown = project_dir / "materials" / "拆片表.md"
                breakdown.write_text(
                    "# 拆片表\n\n| 镜号 | 画面 |\n|---|---|\n| 1 | 产品特写<br>字幕 |\n\n"
                    "<script>alert('x')</script>",
                    encoding="utf-8",
                )
                graph = workflow_canvas.build_graph(project_dir)
                node = next(n for n in graph["nodes"] if n["kind"] == "breakdown_table")
                rendered = node["meta"]["artifact_html"]
                self.assertIn("<table>", rendered)
                self.assertIn("<br>", rendered)
                self.assertNotIn("<script", rendered)
                html = workflow_canvas.render_html(graph)
                self.assertIn("Markdown 预览", html)
                self.assertIn("isTextArtifact", html)

    def test_system_prompt_uses_runtime_model_contract(self):
        with patch.object(agentmain, "get_global_memory", return_value=""):
            prompt = agentmain.get_system_prompt()
        template = Path("assets/sys_prompt_film.txt").read_text(encoding="utf-8")
        self.assertIn(
            "实际模型 ID、API 地址和凭证只认用户明确选择、当前运行配置、项目清单与执行 schema",
            prompt,
        )
        self.assertIn("用户已选模型、项目配置和当前工具 schema", prompt)
        self.assertNotIn("Active Film Project", prompt)
        self.assertIn("每轮只维护一个", prompt)
        self.assertNotIn("deepseek-v4-pro-260425", prompt)
        self.assertIn("路由只认下方自动生成的 skill 索引", prompt)
        self.assertNotRegex(template, r"skill_[A-Za-z0-9_-]+")
        self.assertNotRegex(template, r"skills/[A-Za-z0-9_-]+/SKILL\.md")
        self.assertIn("{{SKILLS_INDEX}}", template)
        self.assertIn("skills/skill_movie/SKILL.md", prompt)

    def test_skills_keep_bounded_depth_and_orchestration_has_one_owner(self):
        skill_files = sorted(Path("skills").glob("*/SKILL.md"))
        texts = {
            path.parent.name: path.read_text(encoding="utf-8")
            for path in skill_files
        }
        for name, text in texts.items():
            limit = 360 if "entrypoint:" in text else 300
            self.assertLessEqual(
                len(text.splitlines()), limit,
                f"{name} should keep bounded depth",
            )

        movie = texts["skill_movie"]
        self.assertIn("manifest.json", movie)
        self.assertIn("human_gated", movie)
        for name, text in texts.items():
            if name == "skill_movie" or "entrypoint:" in text:
                continue
            self.assertNotRegex(
                text, r"(?<!delivery_)manifest\.",
                f"{name} duplicates workflow state",
            )
            self.assertNotIn("auto_approved", text, f"{name} duplicates approvals")
            self.assertNotIn("awaiting_human", text, f"{name} duplicates approvals")

        forbidden_shared_fields = (
            "production_task",
            "visual_edit_scope",
            "game_constraints",
            "business_acceptance",
        )
        for name, text in texts.items():
            for field in forbidden_shared_fields:
                self.assertNotIn(field, text, f"{name} exposes shared field {field}")

        self.assertTrue(
            Path("skills/skill_director/references/handbook.md").exists()
        )
        self.assertTrue(
            Path("skills/skill_prompt_engineering/references/handbook.md").exists()
        )

    def test_new_skill_is_discovered_without_system_prompt_change(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "skill_new_contributor"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: contributor-skill\n"
                "description: 当用户需要处理一种未来新增任务时使用。负责输出新增任务产物。\n"
                "---\n\n# Contributor Skill\n",
                encoding="utf-8",
            )
            with patch.object(agentmain, "script_dir", str(root)):
                index = agentmain.build_skills_index()
            self.assertIn("未来新增任务", index)
            self.assertIn(
                'file_read path="skills/skill_new_contributor/SKILL.md"', index
            )
            self.assertIn("匹配后读取", index)

    def test_skill_documents_do_not_reference_other_skills(self):
        skill_files = sorted(Path("skills").glob("*/SKILL.md"))
        names = {path.parent.name for path in skill_files}
        frontmatter_name = re.compile(r"(?m)^name:\s*([^\s]+)")
        violations = []
        for path in skill_files:
            text = path.read_text(encoding="utf-8")
            body = text.split("\n---", 1)[-1]
            own_folder = path.parent.name
            match = frontmatter_name.search(text)
            own_declared = match.group(1) if match else ""
            for other in names:
                if other == own_folder:
                    continue
                if other in body:
                    violations.append(f"{path}: references {other}")
            for token in re.findall(r"`?(skill[-_][A-Za-z0-9_-]+)`?", body):
                normalized = token.replace("-", "_")
                if normalized not in {
                    own_folder.replace("-", "_"),
                    own_declared.replace("-", "_"),
                }:
                    violations.append(f"{path}: references {token}")
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
