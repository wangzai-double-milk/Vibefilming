"""Local project-level remake specification persistence and Studio regressions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from film import studio
from film import tools
from film import workspace as ws


SPEC = """世界观：冰雪末日，背景保持雪地。
核心资源：熔炉等级、煤炭、木材。
允许替换：主角外观。
禁止变化：不得改成金币体系或现代城市。"""


class RemakeSpecTest(unittest.TestCase):
    def test_single_variable_project_rejects_hidden_hud_or_audio_exceptions(self):
        brief = "完整复刻上传视频，只改玩法，其他保持不变"
        invalid_specs = (
            "允许改变：玩法对象及对应玩法 HUD/图标/数值的视觉表现。",
            "必须保持字幕/HUD；玩法专属 HUD 除外，按因果闭包最小适配。",
            "字幕/HUD 若与新玩法矛盾，按因果闭包最小改写。",
        )
        for spec in invalid_specs:
            with self.subTest(spec=spec), self.assertRaisesRegex(
                ValueError, "SINGLE_VARIABLE_LOCK_VIOLATION"
            ):
                tools._validate_single_variable_remake_spec(brief, spec)

        valid = (
            "允许改变：玩法对象与必要动作、接触和视觉反馈。"
            "必须保持：字幕/HUD/数字/CTA/图标逐字逐时保留，完整原音轨锁定。"
            "如有事实矛盾，复用源段或只调整玩法画面，不改 UI、文字或声音。"
        )
        tools._validate_single_variable_remake_spec(brief, valid)

    def test_single_variable_project_requires_spec_before_creation(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            handler = SimpleNamespace(_film_active_project=None)
            with self.assertRaisesRegex(
                ValueError, "SINGLE_VARIABLE_LOCK_VIOLATION"
            ):
                tools._project_create(handler, {
                    "brief": "完整复刻，只改玩法，其他保持不变",
                    "project_id": "missing-spec",
                    "execution_mode": "autonomous",
                    "remake_spec": "",
                })
            self.assertFalse((Path(temp) / "missing-spec").exists())

    def test_single_variable_project_accepts_user_authorized_hud_change(self):
        tools._validate_single_variable_remake_spec(
            "只改玩法，同时把 HUD 更新为净水数值，其他保持不变",
            "允许改变：玩法与 HUD 数值。必须保持：人物、背景、镜头和原音轨。",
        )

    def test_workspace_create_update_and_project_open_persist_spec(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            manifest = ws.project_create(
                "规范项目", execution_mode="autonomous", remake_spec=SPEC)
            project_id = manifest["project_id"]
            self.assertEqual(SPEC, manifest["remake_spec"])
            self.assertEqual(SPEC, ws.read_manifest(project_id)["remake_spec"])

            updated = SPEC + "\nUI术语：熔炉升级。"
            self.assertEqual(updated, ws.set_remake_spec(project_id, updated))
            handler = SimpleNamespace(_film_active_project=project_id)
            opened = tools._project_open(handler, {"project_id": project_id})
            self.assertEqual(updated, opened["remake_spec"])

    def test_project_create_schema_and_tool_forward_spec(self):
        schema = next(
            item["function"] for item in tools.build_film_schema()
            if item["function"]["name"] == "project_create"
        )
        spec_field = schema["parameters"]["properties"]["remake_spec"]
        self.assertEqual(ws.MAX_REMAKE_SPEC_CHARS, spec_field["maxLength"])

        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            handler = SimpleNamespace(_film_active_project=None)
            with patch(
                "film.workflow_canvas.canvas_launch_hint",
                return_value={"enabled": False},
            ):
                created = tools._project_create(handler, {
                    "brief": "工具规范项目",
                    "project_id": "remake-spec-tool",
                    "execution_mode": "autonomous",
                    "remake_spec": SPEC,
                })
            self.assertEqual(SPEC, created["remake_spec"])
            self.assertEqual(
                SPEC,
                ws.read_manifest("remake-spec-tool")["remake_spec"],
            )

    def test_local_studio_injects_spec_into_every_agent_turn(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            project_id = ws.project_create(
                "本地注入", remake_spec=SPEC)["project_id"]
            ws.set_active_project(project_id)
            prompt = studio._with_remake_spec("只替换人物")
            self.assertIn("[REMAKE_SPEC]", prompt)
            self.assertIn(SPEC, prompt)
            self.assertTrue(prompt.endswith("只替换人物"))

    def test_local_studio_auto_creates_named_project_with_pending_spec(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            ws.clear_active_project()
            project_id = studio._ensure_active_project(
                "只替换主角并直接开始生成",
                execution_mode="autonomous",
                remake_spec=SPEC,
            )
            manifest = ws.read_manifest(project_id)
            self.assertEqual("只替换主角并直接开始生成", manifest["brief"])
            self.assertEqual(SPEC, manifest["remake_spec"])
            self.assertEqual("autonomous", manifest["execution_mode"]["mode"])

    def test_frontend_contains_create_and_edit_controls(self):
        html = studio.render_studio_html()
        for marker in (
            'id="project-remake-spec"',
            'id="remake-spec"',
            'id="remake-spec-save"',
            "/api/remake-spec",
            "maxlength=\"12000\"",
            "const pendingRemakeSpec = hasProject ? undefined : remakeSpec.value",
            "remake_spec: pendingRemakeSpec",
            "随任务保存",
        ):
            self.assertIn(marker, html)

    def test_overlong_spec_is_rejected(self):
        invalid = "x" * (ws.MAX_REMAKE_SPEC_CHARS + 1)
        with self.assertRaisesRegex(ValueError, "最多 12000"):
            ws.normalize_remake_spec(invalid)


if __name__ == "__main__":
    unittest.main()
