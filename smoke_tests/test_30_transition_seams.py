from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from film import film_sdk as sdk
from film import tools
from film import workspace as ws


class TransitionSeamTest(unittest.TestCase):
    def test_skills_require_cut_class_and_seam_review(self):
        director = (ROOT / "skills/skill_director/SKILL.md").read_text(
            encoding="utf-8")
        director_handbook = (
            ROOT / "skills/skill_director/references/handbook.md"
        ).read_text(encoding="utf-8")
        prompt = (
            ROOT / "skills/skill_prompt_engineering/SKILL.md"
        ).read_text(encoding="utf-8")
        prompt_handbook = (
            ROOT / "skills/skill_prompt_engineering/references/handbook.md"
        ).read_text(encoding="utf-8")
        review = (ROOT / "skills/skill_review/SKILL.md").read_text(
            encoding="utf-8")
        storyboard = (ROOT / "skills/skill_storyboard/SKILL.md").read_text(
            encoding="utf-8")
        movie = (ROOT / "skills/skill_movie/SKILL.md").read_text(
            encoding="utf-8")
        system = (ROOT / "assets/sys_prompt_film.txt").read_text(
            encoding="utf-8")
        for marker in (
            "same_scene_continuous",
            "same_scene_elliptical",
            "scene_change",
            "continuity_anchors",
            "visual_bridge",
            "seam_review",
        ):
            self.assertIn(marker, director)
        self.assertIn("同场景连续接缝", prompt)
        self.assertIn("跨场景", prompt)
        self.assertNotIn("extract_seam_frames", review)
        self.assertIn("continuity_strategy", review)
        self.assertIn("video_extend", movie)
        self.assertIn("capability 允许的长单元", storyboard)
        self.assertIn("不得为使用严格首帧而删掉动作故事板", storyboard)
        self.assertIn("15–30 秒", director_handbook)
        self.assertIn("15–30 秒", prompt_handbook)
        self.assertNotIn("extract_seam_frames", movie)
        self.assertIn("extract_seam_frames", {
            item["function"]["name"] for item in tools.build_film_schema()
        })
        self.assertIn("extract_seam_frames", system)

    def test_extract_seam_frames_writes_six_ordered_images(self):
        with TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.mp4"
            right = Path(tmp) / "right.mp4"
            out = Path(tmp) / "seam"
            for path, color in ((left, "red"), (right, "blue")):
                sdk._run_ffmpeg([
                    "-f", "lavfi", "-i",
                    f"color=c={color}:s=96x96:d=1:r=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
                ])
            result = sdk.extract_seam_frames(
                str(left), str(right), out, window=.3)
            self.assertEqual(
                "vibefilming.seam-evidence/v1",
                result["contract"],
            )
            self.assertEqual(6, result["count"])
            self.assertEqual(
                ["left_1", "left_2", "left_3",
                 "right_1", "right_2", "right_3"],
                [item["label"] for item in result["frames"]],
            )
            self.assertTrue(all(Path(path).is_file() for path in result["paths"]))
            self.assertGreater(result["frames"][2]["time_s"], .9)
            self.assertEqual(0.0, result["frames"][3]["time_s"])

    def test_extract_boundary_frame_returns_real_last_frame_anchor(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "source.mp4"
            first_output = Path(tmp) / "first.png"
            last_output = Path(tmp) / "last.png"
            sdk._run_ffmpeg([
                "-f", "lavfi", "-i", "color=c=red:s=96x96:d=0.5:r=30",
                "-f", "lavfi", "-i", "color=c=blue:s=96x96:d=0.5:r=30",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
            ])
            first = sdk.extract_boundary_frame(
                str(clip), first_output, edge="first")
            last = sdk.extract_boundary_frame(
                str(clip), last_output, edge="last")
            with Image.open(first_output) as image:
                first_pixel = image.convert("RGB").getpixel((48, 48))
            with Image.open(last_output) as image:
                last_pixel = image.convert("RGB").getpixel((48, 48))
            self.assertGreater(first_pixel[0], 200)
            self.assertLess(first_pixel[2], 60)
            self.assertGreater(last_pixel[2], 200)
            self.assertLess(last_pixel[0], 60)
            self.assertEqual(0.0, first["time_s"])
            self.assertGreater(last["time_s"], .9)
            self.assertLess(last["time_s"], last["duration_s"])

    def test_boundary_tool_resolves_project_references(self):
        with TemporaryDirectory() as tmp, patch.object(
            ws, "PROJECTS_ROOT", Path(tmp)
        ):
            pid = ws.project_create("boundary refs")["project_id"]
            clip = ws.project_dir(pid) / "shots" / "source.mp4"
            sdk._run_ffmpeg([
                "-f", "lavfi", "-i",
                "color=c=yellow:s=96x96:d=0.5:r=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
            ])
            cwd = Path(tmp) / "cwd"
            cwd_clip = cwd / "shots" / "source.mp4"
            cwd_clip.parent.mkdir(parents=True)
            sdk._run_ffmpeg([
                "-f", "lavfi", "-i",
                "color=c=red:s=96x96:d=0.5:r=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cwd_clip),
            ])
            handler = SimpleNamespace(_film_active_project=pid)
            original_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                self.assertEqual(
                    str(clip.resolve()),
                    tools._resolve_project_media_reference(
                        handler, "shots/source.mp4"),
                )
                for index, reference in enumerate((
                    "project://shots/source.mp4",
                    "shots/source.mp4",
                    str(clip),
                )):
                    result = tools._extract_boundary_frame(handler, {
                        "clip": reference,
                        "name": f"anchor-{index}",
                        "edge": "last",
                    })
                    self.assertTrue(Path(result["path"]).is_file())
            finally:
                os.chdir(original_cwd)

    def test_schema_and_timeline_keep_transition_contract(self):
        schemas = {
            item["function"]["name"]: item["function"]
            for item in tools.build_film_schema()
        }
        self.assertIn("extract_seam_frames", schemas)
        self.assertIn("extract_boundary_frame", schemas)
        props = schemas["video_concat"]["parameters"]["properties"]
        transition_props = props["transitions"]["items"]["properties"]
        for marker in (
            "cut_class", "continuity_anchors", "visual_bridge",
            "effect_source", "effect_peak", "effect_clear", "seam_review",
        ):
            self.assertIn(marker, transition_props)

        contract = {
            "type": "match_cut",
            "cut_class": "same_scene_continuous",
            "at": "U03->U04",
            "continuity_anchors": {
                "subject": "player",
                "screen_position": "42%,58%",
                "motion": "left_to_right",
            },
            "visual_bridge": None,
            "effect_source": "",
            "effect_peak": "",
            "effect_clear": "",
            "seam_review": {
                "frames": "reviews/seam_U03__U04",
                "status": "PASS",
            },
        }
        with TemporaryDirectory() as tmp, \
                patch.object(sdk, "probe_video_duration", return_value=2.0), \
                patch.object(sdk, "probe_video_size", return_value=(96, 96)), \
                patch.object(sdk, "probe_video_fps", return_value=30.0), \
                patch.object(sdk, "has_audio_stream", return_value=True), \
                patch.object(sdk, "_run_ffmpeg"):
            output = Path(tmp) / "joined.mp4"
            result = sdk.video_concat(
                ["left.mp4", "right.mp4"],
                output,
                transitions=[contract],
            )
            self.assertEqual(
                "vibefilming.timeline/v1", result["contract"])
            transition = result["transitions"][0]
            self.assertEqual("cut", transition["type"])
            self.assertEqual("match_cut", transition["strategy"])
            self.assertEqual(
                "same_scene_continuous", transition["cut_class"])
            self.assertEqual("PASS", transition["seam_review"]["status"])
            timeline = json.loads(
                output.with_suffix(".timeline.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "vibefilming.timeline/v1", timeline["contract"])
            self.assertEqual(transition, timeline["transitions"][0])


if __name__ == "__main__":
    unittest.main()
