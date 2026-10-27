"""Project-level Seedance selection and single-model fallback regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from film import tools
from film import video_models
from film import workspace as ws


class SeedanceModelRoutingTest(unittest.TestCase):
    def _config(self, root: str, models: dict) -> Path:
        path = Path(root) / "config.json"
        path.write_text(
            json.dumps({"ark": {"models": models}}),
            encoding="utf-8",
        )
        return path

    def test_only_25_configured_automatically_overrides_stale_20_selection(self):
        with TemporaryDirectory() as temp:
            config = self._config(temp, {
                "video_2_5": video_models.SEEDANCE_2_5,
            })
            with patch.object(video_models, "CONFIG_JSON", config), \
                    patch.dict(os.environ, {}, clear=True):
                available = video_models.available_profiles()
                self.assertEqual(["2.5"], [item.key for item in available])
                self.assertEqual(
                    "2.5", video_models.resolve("2.0").key)

    def test_both_models_use_legacy_default_and_allow_project_switch(self):
        with TemporaryDirectory() as temp:
            config = self._config(temp, {
                "video": video_models.SEEDANCE_2_0,
                "video_2_0": video_models.SEEDANCE_2_0,
                "video_2_5": video_models.SEEDANCE_2_5,
            })
            projects = Path(temp) / "projects"
            projects.mkdir()
            with patch.object(video_models, "CONFIG_JSON", config), \
                    patch.object(ws, "PROJECTS_ROOT", projects), \
                    patch.dict(os.environ, {}, clear=True):
                manifest = ws.project_create("routing")
                pid = manifest["project_id"]
                self.assertEqual(
                    video_models.SEEDANCE_2_0, manifest["video_model"])
                selected = ws.set_video_model(pid, "2.5")
                self.assertEqual("2.5", selected["key"])
                self.assertEqual(
                    10,
                    next(
                        item["max_items"]
                        for item in ws.material_type_definitions(
                            ws.read_manifest(pid))
                        if item["id"] == "reference_video"
                    ),
                )

                captured = {}

                def fake_submit(prompt, **kwargs):
                    captured.update(kwargs)
                    return {
                        "task_id": "task-25",
                        "model": kwargs["model"],
                        "body": kwargs,
                        "raw": {},
                    }

                handler = SimpleNamespace(_film_active_project=pid)
                with patch.object(
                    tools.sdk, "submit_video_task", side_effect=fake_submit
                ):
                    result = tools._gen_video_t2v(handler, {
                        "prompt": "30 秒连续镜头，无人物说话。",
                        "name": "route-25",
                        "unit_id": "route-25",
                        "duration": 30,
                        "ratio": "16:9",
                        "output_format": "mov",
                    })
                self.assertEqual(
                    video_models.SEEDANCE_2_5, captured["model"])
                self.assertEqual("2.5", result["model_version"])
                self.assertEqual("mov", captured["output_format"])

                ws.set_video_model(pid, "2.0")
                with self.assertRaisesRegex(ValueError, "4-15"):
                    tools._gen_video_t2v(handler, {
                        "prompt": "30 秒连续镜头。",
                        "name": "route-20",
                        "unit_id": "route-20",
                        "duration": 30,
                        "ratio": "16:9",
                    })

    def test_unknown_submission_blocks_same_unit_retry(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            pid = ws.project_create("unknown submission")["project_id"]
            ws.log_seedance_call(pid, {
                "unit_id": "unit-1",
                "status": "submission_failed_or_unknown",
                "counts_as_attempt": True,
            })
            with self.assertRaisesRegex(
                RuntimeError, "BLOCKED_SUBMISSION_UNKNOWN"
            ):
                tools._guard_seedance_submission(pid, "unit-1")

    def test_legacy_name_only_log_uses_plan_unit_alias(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            pid = ws.project_create("legacy unit log")["project_id"]
            (ws.project_dir(pid) / "director_plan.json").write_text(
                json.dumps({
                    "contract": "vibefilming.director-plan/v1",
                    "units": [{
                        "id": "U03",
                        "raw_output_path": "shots/U03_handle4s_v2.mp4",
                        "seedance_submissions": [{
                            "name": "U03_old_name",
                            "status": "accepted",
                        }],
                    }],
                }),
                encoding="utf-8",
            )
            ws.log_seedance_call(pid, {
                "name": "U03_old_name",
                "status": "submission_failed_or_unknown",
            })
            with self.assertRaisesRegex(
                RuntimeError, "BLOCKED_SUBMISSION_UNKNOWN"
            ):
                tools._guard_seedance_submission(pid, "U03")

    def test_gen_video_requires_unit_id_before_provider_call(self):
        with patch.object(
            tools, "_video_profile", return_value=video_models.PROFILES["2.0"]
        ), patch.object(tools.sdk, "submit_video_task") as submit:
            with self.assertRaisesRegex(ValueError, "稳定 unit_id"):
                tools._gen_video_t2v(SimpleNamespace(), {
                    "prompt": "固定机位空镜。",
                    "name": "missing-unit",
                    "duration": 4,
                    "ratio": "16:9",
                })
        submit.assert_not_called()

    def test_unknown_submission_cannot_retry_by_renaming_output(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            pid = ws.project_create("rename retry")["project_id"]
            handler = SimpleNamespace(_film_active_project=pid)
            common = {
                "prompt": "固定机位空镜，无人物说话。",
                "unit_id": "stable-unit",
                "duration": 4,
                "ratio": "16:9",
            }
            with patch.object(
                tools.sdk,
                "submit_video_task",
                side_effect=tools.sdk.VideoSubmissionError("mock timeout"),
            ):
                with self.assertRaises(tools.sdk.VideoSubmissionError):
                    tools._gen_video_t2v(
                        handler, {**common, "name": "first-name"})
            with patch.object(tools.sdk, "submit_video_task") as submit:
                with self.assertRaisesRegex(
                    RuntimeError, "BLOCKED_SUBMISSION_UNKNOWN"
                ):
                    tools._gen_video_t2v(
                        handler, {**common, "name": "renamed-output"})
            submit.assert_not_called()

    def test_query_restores_mov_format_from_persistent_submission_log(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            pid = ws.project_create("mov restore")["project_id"]
            ws.log_seedance_call(pid, {
                "task_id": "task-mov",
                "unit_id": "unit-mov",
                "status": "submitted",
                "counts_as_attempt": True,
                "output_format": "mov",
            })
            handler = SimpleNamespace(_film_active_project=pid)
            tools._VIDEO_TASK_FORMATS.clear()

            def download(_url, path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"mov")
                return path

            with patch.object(tools.sdk, "query_video_task", return_value={
                "status": "succeeded",
                "video_url": "https://example.test/video.mov",
                "raw": {},
            }), patch.object(tools.sdk, "_download", side_effect=download):
                result = tools._query_video_task(handler, {
                    "task_id": "task-mov",
                    "save_name": "restored",
                    "wait": False,
                })
            self.assertEqual(".mov", Path(result["path"]).suffix)
            self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
