from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from film import execution_mode
from film import studio
from film import tools
from film import workspace as ws


class ExecutionModeTest(unittest.TestCase):
    def test_default_precedence_and_validation(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "workflow": {"default_execution_mode": "autonomous"},
            }))
            self.assertEqual("autonomous", execution_mode.default_mode(config))
            with patch.dict(os.environ, {
                "VIBEFILMING_EXECUTION_MODE": "human_gated",
            }):
                self.assertEqual("human_gated", execution_mode.default_mode(config))
            with self.assertRaises(ValueError):
                execution_mode.normalize_mode("skip_everything")

    def test_project_modes_and_legacy_migration(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            autonomous = ws.project_create("自动项目", execution_mode="autonomous")
            state = autonomous["execution_mode"]
            self.assertEqual("autonomous", state["mode"])
            self.assertEqual([], state["human_gates"])
            self.assertFalse(execution_mode.requires_human_approval(
                state, "breakdown_approval"))

            legacy = ws.project_create("旧项目", execution_mode="human_gated")
            pid = legacy["project_id"]
            ws.update_manifest(pid, lambda data: data.pop("execution_mode"))
            migrated = ws.normalize_execution_mode(pid)
            self.assertEqual("human_gated", migrated["mode"])
            self.assertEqual(
                ["breakdown_approval", "script_approval"], migrated["human_gates"])
            ws.update_manifest(pid, lambda data: data.__setitem__(
                "execution_mode", {"mode": "unknown"}))
            fallback = ws.normalize_execution_mode(pid)
            self.assertEqual("human_gated", fallback["mode"])
            self.assertEqual("invalid_fallback", fallback["source"])

    def test_switch_to_autonomous_clears_only_stale_wait_note(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            manifest = ws.project_create("切换模式", execution_mode="human_gated")
            pid = manifest["project_id"]
            ws.update_phase(
                pid, "story", status="in_progress",
                note="awaiting_human_approval: breakdown")
            selected = ws.set_execution_mode(pid, "autonomous", source="test")
            current = ws.read_manifest(pid)
            self.assertEqual("autonomous", selected["mode"])
            self.assertEqual("in_progress", current["phases"]["story"]["status"])
            self.assertIn(
                "continue after machine validation", current["phases"]["story"]["note"])
            self.assertNotIn("awaiting_human_approval", current["phases"]["story"]["note"])

    def test_switch_to_human_requires_human_confirmation_for_machine_breakdown(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            manifest = ws.project_create("自动转人工", execution_mode="autonomous")
            pid = manifest["project_id"]
            source = ws.project_dir(pid) / "materials" / "source.mp4"
            source.write_bytes(b"video")
            ws.update_manifest(
                pid, lambda data: data.update(materials={"reference_video": [str(source)]}))
            ws.update_phase(pid, "breakdown", status="done", source="reference_video")
            ws.set_execution_gate(
                pid, "breakdown_approval", "approved",
                actor="machine", evidence="autonomous validation")
            ws.set_execution_mode(pid, "human_gated", source="test")
            gate = ws.execution_gate_state(
                ws.read_manifest(pid), "breakdown_approval")
            self.assertEqual("pending", gate["status"])
            self.assertEqual("system", gate["actor"])

    def test_tools_and_studio_expose_both_modes(self):
        schema = {
            item["function"]["name"]: item["function"]
            for item in tools.build_film_schema()
        }
        self.assertIn("set_execution_mode", schema)
        self.assertEqual(
            ["autonomous", "human_gated"],
            schema["set_execution_mode"]["parameters"]["properties"]["mode"]["enum"],
        )
        html = studio.render_studio_html()
        self.assertIn('name="new-execution-mode" value="autonomous"', html)
        self.assertIn('name="new-execution-mode" value="human_gated"', html)
        self.assertIn('name="active-execution-mode" value="autonomous"', html)
        self.assertIn('class="execution-row composer-mode"', html)
        self.assertIn("execution_mode: mode", html)
        self.assertIn("form.append('execution_mode', selectedExecutionMode())", html)
        self.assertIn("/api/execution-mode", html)
        self.assertIn("let newModeDirty = false", html)
        self.assertIn("if (!newModeDirty)", html)

        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            manifest = ws.project_create("工具切换", execution_mode="human_gated")
            handler = SimpleNamespace(_film_active_project=manifest["project_id"])
            result = tools._DECORATED_TOOLS["set_execution_mode"](
                handler, {"mode": "autonomous"})
            self.assertTrue(result["ok"])
            self.assertEqual("autonomous", result["execution_mode"]["mode"])

    def test_external_manifest_material_does_not_break_state(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as external:
            with patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
                manifest = ws.project_create("外部素材状态")
                pid = manifest["project_id"]
                source = Path(external) / "reference.png"
                source.write_bytes(b"\x89PNG\r\n\x1a\n")
                ws.update_manifest(
                    pid, lambda data: data.update(
                        materials={"reference_image": [str(source)]}))
                materials = studio._project_materials_state(pid)
                item = materials["reference_image"][0]
                self.assertEqual("", item["url"])
                self.assertTrue(item["preview_unavailable"])

    def test_human_gates_block_script_write_and_story_done(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            manifest = ws.project_create("硬闸门", execution_mode="human_gated")
            pid = manifest["project_id"]
            pdir = Path(manifest["project_dir"])
            handler = SimpleNamespace(
                _film_active_project=pid,
                _get_abs_path=lambda path: str(pdir / path),
            )
            source = pdir / "materials" / "source.mp4"
            source.write_bytes(b"video")
            ws.update_manifest(
                pid, lambda data: data.update(materials={"reference_video": [str(source)]}))
            ws.set_execution_gate(
                pid, "breakdown_approval", "pending",
                actor="system", evidence="breakdown generated")
            reason = tools._formal_script_write_block_reason(
                handler, {"path": "script.md"})
            self.assertIn("禁止写正式 script.md", reason)

            ws.update_phase(pid, "breakdown", status="done")
            tools._DECORATED_TOOLS["approve_execution_gate"](
                handler, {"gate": "breakdown_approval", "evidence": "用户确认拆镜"})
            self.assertEqual(
                "", tools._formal_script_write_block_reason(
                    handler, {"path": "script.md"}))

            with self.assertRaises(RuntimeError):
                tools._DECORATED_TOOLS["project_update_phase"](
                    handler, {"phase": "story", "status": "done"})
            (pdir / "script.md").write_text("# script\n")
            tools._DECORATED_TOOLS["approve_execution_gate"](
                handler, {"gate": "script_approval", "evidence": "用户确认剧本"})
            result = tools._DECORATED_TOOLS["project_update_phase"](
                handler, {"phase": "story", "status": "done"})
            self.assertEqual("done", result["state"]["status"])
            (pdir / "script.md").write_text("# revised\n")
            tools._mark_formal_script_changed(handler, b"# script\n")
            current = ws.read_manifest(pid)
            self.assertEqual(
                "pending",
                ws.execution_gate_state(current, "script_approval")["status"])
            self.assertEqual("in_progress", current["phases"]["story"]["status"])

    def test_human_project_without_reference_can_write_script_before_script_review(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            manifest = ws.project_create("原创项目", execution_mode="human_gated")
            pid = manifest["project_id"]
            pdir = Path(manifest["project_dir"])
            handler = SimpleNamespace(
                _film_active_project=pid,
                _get_abs_path=lambda path: str(pdir / path),
            )
            self.assertEqual(
                "", tools._formal_script_write_block_reason(
                    handler, {"path": "script.md"}))


if __name__ == "__main__":
    unittest.main()
