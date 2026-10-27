from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from film import film_sdk, server_config, video_models


class ServerConfigTest(unittest.TestCase):
    def test_server_precedence_isolated_defaults_and_public_url(self):
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "server": {
                    "studio": {"host": "0.0.0.0", "port": 8764},
                    "canvas": {
                        "host": None, "port": 8765,
                        "public_url": "https://example.test/canvas/",
                    },
                },
            }))
            with patch.dict(os.environ, {
                "VIBEFILMING_STUDIO_HOST": "192.0.2.10",
                "VIBEFILMING_CANVAS_PORT": "9001",
            }, clear=False), patch.object(server_config, "_OVERRIDES", {}):
                canvas = server_config.resolve_server_settings("canvas", config_path=config)
                self.assertEqual("127.0.0.1", canvas.host)
                self.assertEqual(9001, canvas.port)
                self.assertEqual(
                    "https://example.test/canvas/canvas.html",
                    canvas.browser_url(9001, "canvas.html"),
                )
                explicit = server_config.resolve_server_settings(
                    "canvas", host="127.0.0.1", port=9002, config_path=config)
                self.assertEqual(("127.0.0.1", 9002), (explicit.host, explicit.port))

    def test_remote_unauthenticated_bind_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError,
                "VIBEFILMING_ALLOW_UNAUTHENTICATED_REMOTE_STUDIO",
            ):
                server_config.require_explicit_remote_bind(
                    "studio", "0.0.0.0")
            server_config.require_explicit_remote_bind(
                "studio", "127.0.0.1")
        with patch.dict(os.environ, {
            "VIBEFILMING_ALLOW_UNAUTHENTICATED_REMOTE_STUDIO": "1",
        }, clear=True):
            server_config.require_explicit_remote_bind(
                "studio", "0.0.0.0")

    def test_native_caption_prompt_and_audio_choice_reach_seedance(self):
        prompt = "原生字幕逐字显示：Build: 50% -> King's Tower 中文"
        profile = replace(
            video_models.PROFILES["2.0"], model_id="configured-model")
        with patch.object(film_sdk, "_http_post", return_value={"id": "offline"}), \
             patch.object(
                 video_models, "available_profiles", return_value=[profile]):
            result = film_sdk.submit_video_task(
                prompt, generate_audio=False, reference_video_url="asset://source")
        body = result["body"]
        self.assertEqual(prompt, body["content"][0]["text"])
        self.assertFalse(body["generate_audio"])
        self.assertEqual("configured-model", body["model"])
        self.assertEqual("asset://source", body["content"][1]["video_url"]["url"])


if __name__ == "__main__":
    unittest.main()
