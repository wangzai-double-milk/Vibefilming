"""Seedance 2.0/2.5 multimodal limits and request-shape regressions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from film import film_sdk as sdk
from film import tools
from film import video_models
from film import workspace as ws


class SeedanceMultimodalLimitsTest(unittest.TestCase):
    def setUp(self):
        # Request-shape tests use explicit model fixtures, never private user config.
        configuration = patch.object(
            video_models, "_configured_models",
            return_value=(
                {"2.0": video_models.SEEDANCE_2_0, "2.5": video_models.SEEDANCE_2_5},
                video_models.SEEDANCE_2_0,
            ),
        )
        configuration.start()
        self.addCleanup(configuration.stop)

    def test_sdk_builds_three_video_and_three_audio_content_items(self):
        videos = [f"asset://video-{index}" for index in range(3)]
        audios = [f"asset://audio-{index}" for index in range(3)]
        with patch.object(sdk, "_http_post", return_value={"id": "task-1"}):
            result = sdk.submit_video_task(
                "保持参考动作和音色。",
                reference_images=["data:image/png;base64,AA=="],
                reference_video_urls=videos,
                reference_audio_urls=audios,
                duration=4,
                model="2.0",
            )
        content = result["body"]["content"]
        self.assertEqual(
            ["reference_video"] * 3,
            [item["role"] for item in content if item["type"] == "video_url"],
        )
        self.assertEqual(
            ["reference_audio"] * 3,
            [item["role"] for item in content if item["type"] == "audio_url"],
        )
        self.assertNotIn("seed", result["body"])
        self.assertNotIn("output_format", result["body"])

    def test_sdk_rejects_over_limit_audio_only_and_seed(self):
        with self.assertRaisesRegex(ValueError, "最多 3"):
            sdk.submit_video_task(
                "test",
                reference_video_urls=[f"asset://v-{index}" for index in range(4)],
                model="2.0",
            )
        with self.assertRaisesRegex(ValueError, "仅输入参考音频"):
            sdk.submit_video_task(
                "test", reference_audio_urls=["asset://voice"], model="2.0")
        with self.assertRaisesRegex(ValueError, "不支持 seed"):
            sdk.submit_video_task("test", seed=1, model="2.0")

    def test_sdk_25_accepts_larger_audio_only_and_edit_requests(self):
        with patch.object(sdk, "_http_post", return_value={"id": "task-25"}):
            audio_only = sdk.submit_video_task(
                "保持音色生成画面。",
                reference_audio_urls=["asset://voice"],
                duration=30,
                output_format="mov",
                model="2.5",
            )
            edit = sdk.submit_video_task(
                "替换视频主体。",
                reference_video_urls=["asset://source"],
                duration=-1,
                ratio="adaptive",
                omni_reference_task_type="edit",
                model="2.5",
            )
            extend = sdk.submit_video_task(
                "向后延长视频1，从末态继续。",
                reference_video_urls=["asset://source"],
                duration=6,
                ratio="adaptive",
                omni_reference_task_type="extend",
                model="2.5",
            )
        self.assertEqual(video_models.SEEDANCE_2_5, audio_only["model"])
        self.assertEqual("mov", audio_only["body"]["output_format"])
        self.assertEqual(30, audio_only["body"]["duration"])
        self.assertEqual("edit", edit["body"]["omni_reference_task_type"])
        self.assertEqual(-1, edit["body"]["duration"])
        self.assertEqual("mov", edit["body"]["output_format"])
        self.assertEqual("mov", extend["body"]["output_format"])
        with self.assertRaisesRegex(ValueError, "resolution"):
            sdk.submit_video_task("test", resolution="4k", model="2.5")

    def test_strict_frame_mode_is_separate_from_reference_and_task_modes(self):
        frame = "data:image/png;base64,AA=="
        with patch.object(sdk, "_http_post", return_value={"id": "task-frame"}):
            result = sdk.submit_video_task(
                "从首帧运动到尾帧。",
                first_frame=frame,
                last_frame=frame,
                duration=6,
                ratio="adaptive",
                model="2.5",
            )
        self.assertEqual(
            ["first_frame", "last_frame"],
            [
                item["role"]
                for item in result["body"]["content"]
                if item["type"] == "image_url"
            ],
        )
        with self.assertRaisesRegex(ValueError, "不能与 reference"):
            sdk.submit_video_task(
                "invalid",
                first_frame=frame,
                reference_images=[frame],
                duration=6,
                ratio="adaptive",
                model="2.5",
            )
        with self.assertRaisesRegex(ValueError, "独立锁定任务"):
            sdk.submit_video_task(
                "invalid",
                first_frame=frame,
                duration=6,
                ratio="adaptive",
                omni_reference_task_type="reference",
                model="2.5",
            )

    def test_tool_defaults_25_extend_to_mov_and_reports_strategy(self):
        captured = {}

        def fake_submit(prompt, **kwargs):
            captured.update(kwargs)
            return {
                "task_id": "task-extend",
                "model": video_models.SEEDANCE_2_5,
                "body": kwargs,
                "raw": {},
            }

        with patch.object(
            tools, "_video_profile", return_value=video_models.PROFILES["2.5"]
        ), patch.object(
            tools.sdk, "submit_video_task", side_effect=fake_submit
        ), patch.object(tools, "_active_pid", return_value=None):
            result = tools._gen_video_t2v(SimpleNamespace(), {
                "prompt": "向后延长视频1，从视频末态继续。",
                "name": "extend-25",
                "unit_id": "extend-25",
                "duration": 6,
                "ratio": "adaptive",
                "reference_video_urls": ["asset://previous"],
                "omni_reference_task_type": "extend",
            })
        self.assertEqual("mov", captured["output_format"])
        self.assertEqual("video_extend", result["continuity_strategy"])
        self.assertEqual("mov", result["output_format"])

    def test_tool_blocks_ambiguous_extend_before_provider_call(self):
        with patch.object(
            tools, "_video_profile", return_value=video_models.PROFILES["2.5"]
        ), patch.object(tools.sdk, "submit_video_task") as submit:
            with self.assertRaisesRegex(ValueError, "不能只写“承接上段”"):
                tools._gen_video_t2v(SimpleNamespace(), {
                    "prompt": "承接上段，保持一致。",
                    "name": "bad-extend",
                    "unit_id": "bad-extend",
                    "duration": 6,
                    "ratio": "adaptive",
                    "reference_video_urls": ["asset://previous"],
                    "omni_reference_task_type": "extend",
                })
        submit.assert_not_called()

    def test_tool_rejects_bare_continuity_word_as_extend_intent(self):
        with patch.object(
            tools, "_video_profile", return_value=video_models.PROFILES["2.5"]
        ), patch.object(tools.sdk, "submit_video_task") as submit:
            with self.assertRaisesRegex(ValueError, "延长、延续或续写视频"):
                tools._gen_video_t2v(SimpleNamespace(), {
                    "prompt": "延续人物服装和光线，保持一致。",
                    "name": "bad-bare-continuity",
                    "unit_id": "bad-bare-continuity",
                    "duration": 6,
                    "ratio": "adaptive",
                    "reference_video_urls": ["asset://previous"],
                    "omni_reference_task_type": "extend",
                })
        submit.assert_not_called()

    def test_tool_schema_and_plural_arguments_match_official_limits(self):
        schema = next(
            item["function"] for item in tools.build_film_schema()
            if item["function"]["name"] == "gen_video_t2v"
        )
        props = schema["parameters"]["properties"]
        self.assertEqual(30, props["reference_images"]["maxItems"])
        self.assertEqual(10, props["reference_video_urls"]["maxItems"])
        self.assertEqual(10, props["reference_audio_urls"]["maxItems"])
        self.assertIn("adaptive", props["ratio"]["enum"])
        self.assertIn("omni_reference_task_type", props)
        self.assertNotIn("seed", props)
        self.assertIn("unit_id", schema["parameters"]["required"])

        captured = {}

        def fake_submit(prompt, **kwargs):
            captured.update(kwargs)
            return {
                "task_id": "dry-run",
                "model": "seedance-2.0",
                "body": {},
                "raw": {},
            }

        with patch.object(tools.sdk, "submit_video_task", side_effect=fake_submit), \
                patch.object(tools, "_active_pid", return_value=None):
            result = tools._gen_video_t2v(SimpleNamespace(), {
                "prompt": "保持三段参考中的主体、动作与声音。",
                "name": "multi-ref",
                "unit_id": "multi-ref",
                "duration": 4,
                "ratio": "16:9",
                "reference_images": ["data:image/png;base64,AA=="],
                "reference_video_urls": [
                    f"https://example.test/v{index}.mp4" for index in range(3)
                ],
                "reference_audio_urls": [
                    f"https://example.test/a{index}.mp3" for index in range(3)
                ],
            })
        self.assertEqual(3, len(captured["reference_video_urls"]))
        self.assertEqual(3, len(captured["reference_audio_urls"]))
        self.assertEqual(3, len(result["reference_video_urls"]))

    def test_local_reference_total_duration_is_blocked_before_submit(self):
        with TemporaryDirectory() as temp:
            paths = []
            for index in range(3):
                path = Path(temp) / f"ref-{index}.mp4"
                path.write_bytes(b"x")
                paths.append(str(path))
            with patch.object(
                tools, "_validate_local_reference",
                side_effect=lambda path, kind, **_kwargs: {
                    "path": str(path),
                    "kind": kind,
                    "duration": 6.0,
                    "size_bytes": 1,
                    "fps": 24.0,
                },
            ), patch.object(
                tools, "_publish_reference_path",
                side_effect=lambda _handler, path: f"https://example.test/{path.name}",
            ):
                with self.assertRaisesRegex(ValueError, "总时长不能超过 15"):
                    tools._resolve_reference_media(None, paths, "video")

    def test_sandbox_audio_uses_data_uri_without_tos(self):
        with TemporaryDirectory() as temp:
            audio = Path(temp) / "voice.wav"
            audio.write_bytes(b"RIFFtest")
            with patch.object(
                tools,
                "_validate_local_reference",
                return_value={
                    "path": str(audio),
                    "kind": "audio",
                    "duration": 2.0,
                    "size_bytes": audio.stat().st_size,
                },
            ), patch.object(tools, "_publish_reference_path") as publish:
                urls, sources = tools._resolve_reference_media(
                    None, [str(audio)], "audio",
                    profile=video_models.PROFILES["2.5"],
                )
        publish.assert_not_called()
        self.assertTrue(urls[0].startswith("data:audio/wav;base64,"))
        self.assertEqual("sandbox_data_uri", sources[0]["url_source"])

    def test_sandbox_video_transport_error_is_not_tos_requirement(self):
        with TemporaryDirectory() as temp:
            video = Path(temp) / "source.mp4"
            video.write_bytes(b"x")
            with self.assertRaisesRegex(
                RuntimeError, "REFERENCE_TRANSPORT_UNAVAILABLE"
            ) as raised:
                tools._publish_reference_path(None, video)
        self.assertIn("这不代表必须配置 TOS", str(raised.exception))

    def test_publish_reference_file_creates_private_sidecar_without_url_output(self):
        with TemporaryDirectory() as temp, patch.object(
            ws, "PROJECTS_ROOT", Path(temp)
        ):
            pid = ws.project_create("files transport")["project_id"]
            handler = SimpleNamespace(_film_active_project=pid)
            clip = ws.project_dir(pid) / "composed" / "clip.mp4"
            clip.parent.mkdir(exist_ok=True)
            clip.write_bytes(b"video")
            with patch.object(tools, "_publish_to_ark_files", return_value={
                "file_id": "file-test",
                "download_url": "https://files.example/clip.mp4?token=secret",
                "expire_at": 123,
                "bytes": 5,
                "mime_type": "video/mp4",
            }):
                result = tools._publish_reference_file(
                    handler, {"file_path": "project://composed/clip.mp4"})
            sidecar = clip.with_suffix(".url.txt")
            self.assertTrue(sidecar.is_file())
            self.assertEqual(
                "https://files.example/clip.mp4?token=secret",
                sidecar.read_text(),
            )
            self.assertEqual(0o600, sidecar.stat().st_mode & 0o777)
            self.assertNotIn("download_url", result)
            self.assertEqual("file-test", result["file_id"])
            self.assertFalse(result["custom_tos_required"])

    def test_old_project_material_settings_are_upgraded(self):
        old = {
            "material_types": [
                {
                    "id": "reference_video",
                    "media_kind": "video",
                    "extensions": [".mp4", ".mov", ".webm", ".mkv"],
                    "max_bytes": 500 * 1024 * 1024,
                    "max_items": 1,
                },
                {
                    "id": "reference_audio",
                    "media_kind": "audio",
                    "extensions": [".mp3", ".wav", ".m4a"],
                    "max_bytes": 100 * 1024 * 1024,
                    "max_items": 0,
                },
            ]
        }
        definitions = {
            item["id"]: item for item in ws.material_type_definitions(old)
        }
        self.assertEqual(3, definitions["reference_video"]["max_items"])
        self.assertEqual([".mp4", ".mov"], definitions["reference_video"]["extensions"])
        self.assertEqual(3, definitions["reference_audio"]["max_items"])
        self.assertEqual([".mp3", ".wav"], definitions["reference_audio"]["extensions"])

        selected_25 = {
            **old,
            "video_model": video_models.SEEDANCE_2_5,
        }
        definitions_25 = {
            item["id"]: item
            for item in ws.material_type_definitions(selected_25)
        }
        self.assertEqual(10, definitions_25["reference_video"]["max_items"])
        self.assertEqual(10, definitions_25["reference_audio"]["max_items"])


if __name__ == "__main__":
    unittest.main()
