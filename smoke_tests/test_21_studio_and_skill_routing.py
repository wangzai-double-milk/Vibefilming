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
from film import workspace as ws
from film import workflow_canvas


class DummyAgent:
    def __init__(self):
        self.is_running = False
        self.aborted = False

    def put_task(self, message, source="user", images=None):
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
    def test_studio_html_has_chat_and_canvas(self):
        html = studio.render_studio_html()
        self.assertIn('id="messages"', html)
        self.assertIn('id="input"', html)
        self.assertIn('id="canvas"', html)
        self.assertIn("/api/chat", html)
        self.assertIn("clamp(280px, 24vw, 380px) minmax(0, 1fr)", html)
        self.assertNotIn("minmax(280px, 32vw)", html)
        self.assertIn("/api/state", html)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr) auto", html)
        self.assertRegex(html, r"\.chat\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
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
        self.assertIn("loadProjects", html)

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
            with patch.object(ws, "PROJECTS_ROOT", root):
                projects = ws.list_projects()
            self.assertEqual(["p-new", "p-old"], [item["project_id"] for item in projects])
            self.assertEqual(1, projects[0]["done_phases"])
            self.assertEqual(2, projects[0]["total_phases"])

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
        studio._reset_agent_context(agent)
        self.assertEqual([], agent.history)
        self.assertIsNone(agent.handler)
        self.assertEqual([], client.backend.history)
        self.assertEqual("", client.last_tools)
        agent.is_running = True
        with self.assertRaises(RuntimeError):
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

    def test_system_prompt_uses_runtime_model_contract(self):
        with patch.object(agentmain, "get_global_memory", return_value=""):
            prompt = agentmain.get_system_prompt()
        template = Path("assets/sys_prompt_film.txt").read_text(encoding="utf-8")
        self.assertIn("实际模型 ID、API 地址和凭证只认当前运行配置", prompt)
        self.assertIn("每轮只维护一个", prompt)
        self.assertNotIn("deepseek-v4-pro-260425", prompt)
        self.assertIn("路由只认下方自动生成的 skill 索引", prompt)
        self.assertNotRegex(template, r"skill_[A-Za-z0-9_-]+")
        self.assertNotRegex(template, r"skills/[A-Za-z0-9_-]+/SKILL\.md")
        self.assertIn("{{SKILLS_INDEX}}", template)
        self.assertIn("skills/skill_movie/SKILL.md", prompt)

    def test_large_skills_are_compact_and_reference_handbooks(self):
        director = Path("skills/skill_director/SKILL.md").read_text(encoding="utf-8")
        prompt_engineering = Path(
            "skills/skill_prompt_engineering/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertLessEqual(len(director.splitlines()), 100)
        self.assertLessEqual(len(prompt_engineering.splitlines()), 100)
        self.assertIn("references/handbook.md", director)
        self.assertIn("references/handbook.md", prompt_engineering)

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
