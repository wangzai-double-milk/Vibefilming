"""裂变流程回归：实际 FFmpeg 切点、计划传递、任务绑定和紧凑日志。"""
import array
import json
import queue
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import agentmain
from film import film_sdk as sdk, studio, tools, workspace as ws


class FissionRegressionTest(unittest.TestCase):
    def test_fission_contract_covers_scope_logic_and_independent_variants(self):
        root = Path(__file__).resolve().parents[1]
        system_prompt = (root / "assets/sys_prompt_film.txt").read_text(
            encoding="utf-8")
        self.assertIn("复刻与裂变的变更合同", system_prompt)
        self.assertIn("不同裂变版本必须从同一母片独立派生", system_prompt)
        self.assertIn("跨阶段编排类 description 直接命中", system_prompt)
        self.assertIn("不能跳过跨阶段产物", system_prompt)
        self.assertIn("媒介与质感默认锁定", system_prompt)
        self.assertIn("写实人物不得变成塑料 3D", system_prompt)
        self.assertIn("本地运行", system_prompt)
        self.assertIn("本地交付", system_prompt)
        self.assertIn(
            "VIBEFILMING_PROJECTS_ROOT",
            system_prompt,
        )
        self.assertIn("不是本地制作、查看或交付的前提", system_prompt)

        for relative in (
            "skills/skill_movie/SKILL.md",
            "skills/skill_script/SKILL.md",
            "skills/skill_director/SKILL.md",
            "skills/skill_prompt_engineering/SKILL.md",
            "skills/skill_review/SKILL.md",
        ):
            with self.subTest(skill=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn("因果适配闭包", text)
                self.assertNotRegex(text, r"skills/[^`\s]+/SKILL\.md")
        movie = (root / "skills/skill_movie/SKILL.md").read_text(
            encoding="utf-8")
        self.assertIn("两者不存在时不得进入首次生成", movie)

        breakdown = (
            root / "skills/skill_breakdown/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("因果适配候选", breakdown)
        self.assertIn("写实不得推断成 3D/动画", breakdown)
        replacement = (
            root / "skills/skill_prompt_engineering/references/replacement_edit.md"
        ).read_text(encoding="utf-8")
        for marker in ("纯视觉换景", "环境条件变化", "## 玩法替换",
                       "玩法版本直接从原母片派生",
                       "来源媒介与写实程度已锁定"):
            self.assertIn(marker, replacement)
        review = (root / "skills/skill_review/SKILL.md").read_text(
            encoding="utf-8")
        self.assertIn("写实变 3D/动画", review)

    def test_skill_description_and_task_binding(self):
        for marker in (">", "|", ">-", "|-"):
            with self.subTest(marker=marker):
                description = agentmain._parse_frontmatter_description(
                    f"---\nname: sample\ndescription: {marker}\n  用户要求拆镜时使用。\n"
                    "  输出观察结果。\n---\n正文")
                self.assertIn("用户要求拆镜", description)
                self.assertNotIn(marker, description)
        agent = agentmain.GenericAgent.__new__(agentmain.GenericAgent)
        agent.task_queue = queue.Queue()
        agent.put_task("继续", project_id="selected-project")
        self.assertEqual("selected-project", agent.task_queue.get_nowait()["project_id"])

    def test_queued_jobs_block_switches_before_agent_starts(self):
        agent = SimpleNamespace(is_running=False, task_queue=queue.Queue())
        for status in ("queued", "running"):
            with self.subTest(status=status), patch.dict(
                studio._STATE, {"agent": agent, "jobs": {"j": {"status": status}}}
            ):
                with self.assertRaises(RuntimeError):
                    studio._assert_idle()
        with patch.dict(studio._STATE, {"agent": agent, "jobs": {}}):
            agent.task_queue.put("pending")
            with self.assertRaises(RuntimeError):
                studio._assert_idle()
            agent.task_queue.get_nowait()
            agent.task_queue.task_done()
            studio._assert_idle()

    def test_concat_uses_matching_plan_or_explicit_transitions(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            manifest = ws.project_create("合成计划")
            pid = manifest["project_id"]
            handler = SimpleNamespace(_film_active_project=pid)
            clips = ["first.mp4", "second.mp4"]
            transitions = [{"type": "dissolve", "duration": .4}]
            plan_path = ws.project_dir(pid) / "director_plan.json"
            with patch.object(sdk, "video_concat", return_value={"path": "out.mp4"}) as concat:
                with self.assertRaisesRegex(ValueError, "transitions"):
                    tools._video_concat(handler, {"clips": clips, "name": "out"})
                concat.assert_not_called()
                plan_path.write_text(json.dumps({
                    "contract": "vibefilming.director-plan/v1",
                    "composition": {
                        "clips": clips, "transitions": transitions,
                    },
                }))
                tools._video_concat(handler, {"clips": clips, "name": "out"})
                self.assertEqual(transitions, concat.call_args.kwargs["transitions"])
                with self.assertRaises(ValueError):
                    tools._video_concat(handler, {"clips": list(reversed(clips)), "name": "out"})
                cuts = [{"type": "cut"}]
                tools._video_concat(handler, {"clips": clips, "name": "out", "transitions": cuts})
                self.assertEqual(cuts, concat.call_args.kwargs["transitions"])
            for ref in ("asset://source", "https://example.test/source.mp4"):
                self.assertEqual(ref, tools._resolve_reference_video(ref))
            with self.assertRaisesRegex(RuntimeError, "原片复刻"):
                tools._resolve_reference_video(str(Path(tmp) / "original.mp4"))
            schema = {s["function"]["name"] for s in tools.build_film_schema()}
            self.assertFalse({"video_crossfade", "burn_subtitle", "video_text_overlay"} & schema)

    def test_source_and_match_cut_metadata_survives_timeline(self):
        transitions = [{
            "type": "match_cut",
            "at": "S3->S4",
            "source_time_s": 28.0,
            "generation_overlap_s": .25,
            "reason": "模型时长上限导致的同镜头拆分",
            "audio_handling": "回铺完整原片音轨",
            "fallback": {"type": "dissolve", "duration": .15},
        }]
        with TemporaryDirectory() as tmp, \
                patch.object(sdk, "probe_video_duration", return_value=2.0), \
                patch.object(sdk, "probe_video_size", return_value=(96, 96)), \
                patch.object(sdk, "probe_video_fps", return_value=24.0), \
                patch.object(sdk, "has_audio_stream", return_value=True), \
                patch.object(sdk, "_run_ffmpeg"):
            output = Path(tmp) / "out.mp4"
            result = sdk.video_concat(["first.mp4", "second.mp4"], output,
                                      transitions=transitions)
            actual = result["transitions"][0]
            self.assertEqual("cut", actual["type"])
            self.assertEqual("match_cut", actual["strategy"])
            self.assertEqual(28.0, actual["source_time_s"])
            self.assertEqual(.25, actual["generation_overlap_s"])
            self.assertEqual("回铺完整原片音轨", actual["audio_handling"])
            timeline = json.loads(output.with_suffix(".timeline.json").read_text())
            self.assertEqual(actual, timeline["transitions"][0])

    def test_probe_fade_and_concat_preserve_source_fps(self):
        schemas = {
            item["function"]["name"]: item["function"]
            for item in tools.build_film_schema()
        }
        self.assertIn("video_probe", schemas)
        self.assertIn("video_fade", schemas)
        self.assertIn("fps", schemas["video_concat"]["parameters"]["properties"])

        with TemporaryDirectory() as tmp:
            clips = []
            for i, color in enumerate(("red", "blue")):
                clip = Path(tmp) / f"{i}.mp4"
                sdk._run_ffmpeg([
                    "-f", "lavfi", "-i", f"color=c={color}:s=96x160:d=1:r=60",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(clip),
                ])
                clips.append(str(clip))

            source_probe = sdk.probe_media(clips[0])
            self.assertEqual((96, 160), (
                source_probe["width"], source_probe["height"]))
            self.assertAlmostEqual(60, source_probe["fps"], delta=.01)
            self.assertTrue(source_probe["has_audio"])

            output = Path(tmp) / "concat.mp4"
            result = sdk.video_concat(
                clips,
                output,
                crossfade=0,
                transitions=[{"type": "cut"}],
            )
            concat_probe = sdk.probe_media(str(output))
            self.assertAlmostEqual(60, result["fps"], delta=.01)
            self.assertAlmostEqual(60, concat_probe["fps"], delta=.01)
            self.assertEqual(0, result["fade_in"])
            self.assertEqual(0, result["fade_out"])

            faded = Path(tmp) / "faded.mp4"
            fade_result = sdk.video_fade(
                clips[0], faded, fade_in=.15, fade_out=.2)
            fade_timeline = json.loads(
                faded.with_suffix(".timeline.json").read_text())
            self.assertEqual(.15, fade_result["fade_in"])
            self.assertEqual(.2, fade_result["fade_out"])
            self.assertTrue(fade_result["has_audio"])
            self.assertEqual(fade_result, fade_timeline)
            self.assertAlmostEqual(
                60, sdk.probe_media(str(faded))["fps"], delta=.01)

    def test_many_hard_cuts_do_not_drop_one_frame_per_clip(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "one_second.mp4"
            output = Path(tmp) / "fifteen_seconds.mp4"
            sdk._run_ffmpeg([
                "-f", "lavfi", "-i", "color=c=black:s=96x160:d=1:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(clip),
            ])
            result = sdk.video_concat(
                [str(clip)] * 15,
                output,
                crossfade=0,
                transitions=[{"type": "cut"}] * 14,
                fps=30,
            )
            probe = sdk.probe_media(str(output), accurate_duration=True)
            self.assertAlmostEqual(15.0, result["duration"], delta=.02)
            self.assertAlmostEqual(15.0, probe["duration"], delta=.04)
            self.assertEqual(450, probe["estimated_frames"])
            self.assertAlmostEqual(30, probe["fps"], delta=.01)

    def test_model_log_compacts_media_and_preserves_prompt(self):
        with TemporaryDirectory() as tmp, patch.object(ws, "PROJECTS_ROOT", Path(tmp)):
            pid = ws.project_create("日志")["project_id"]
            body = {"messages": [{"content": [
                {"type": "text", "text": "保留原生字幕及节奏"},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}},
            ]}]}
            ws.log_model_call(pid, "vlm", {}, raw_request=body)
            entry = json.loads((ws.project_dir(pid) / "logs/model_calls.jsonl").read_text())
            content = entry["raw_request"]["messages"][0]["content"]
            self.assertEqual("保留原生字幕及节奏", content[0]["text"])
            self.assertEqual(4, content[1]["video_url"]["url"]["base64_length"])
            self.assertEqual("data:video/mp4;base64,AAAA", body["messages"][0]["content"][1]["video_url"]["url"])

    def test_mixed_transitions_have_expected_frames_duration_and_audio(self):
        def rgb(path, timestamp):
            result = subprocess.run([
                sdk.FFMPEG, "-v", "error", "-ss", str(timestamp), "-i", str(path),
                "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24",
                "-f", "rawvideo", "-",
            ], capture_output=True, check=True, timeout=30)
            return list(result.stdout[:3])

        def level(path, timestamp):
            result = subprocess.run([
                sdk.FFMPEG, "-v", "error", "-ss", str(timestamp), "-i", str(path),
                "-t", "0.1", "-vn", "-ac", "1", "-ar", "8000",
                "-f", "f32le", "-",
            ], capture_output=True, check=True, timeout=30)
            samples = array.array("f", result.stdout)
            return (sum(x * x for x in samples) / len(samples)) ** .5

        with TemporaryDirectory() as tmp:
            clips = []
            for i, (color, fps) in enumerate((("red", 24), ("lime", 30), ("blue", 25))):
                clip = Path(tmp) / f"{i}.mp4"
                args = ["-f", "lavfi", "-i", f"color=c={color}:s=96x96:d=2:r={fps}"]
                if i != 1:
                    args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
                sdk._run_ffmpeg([*args, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", str(clip)])
                clips.append(str(clip))
            cases = [
                (["cut", "fade"], 5.6, 3.8, (1, 2)),
                (["fade", "cut"], 5.6, 1.8, (0, 1)),
                (["fade", "fade"], 5.2, 3.4, (1, 2)),
            ]
            for types, expected_duration, midpoint, mixed_channels in cases:
                with self.subTest(transitions=types):
                    output = Path(tmp) / ("_".join(types) + ".mp4")
                    result = sdk.video_concat(
                        clips, output,
                        transitions=[{"type": t, "duration": .4} for t in types])
                    self.assertAlmostEqual(expected_duration, sdk.probe_duration(str(output)), delta=.07)
                    self.assertTrue(sdk.has_audio_stream(str(output)))
                    self.assertGreater(rgb(output, .5)[0], 220)
                    self.assertGreater(rgb(output, expected_duration - .5)[2], 220)
                    pixel = rgb(output, midpoint)
                    for channel in mixed_channels:
                        self.assertGreater(pixel[channel], 65, pixel)
                        self.assertLess(pixel[channel], 190, pixel)
                    self.assertGreater(level(output, .5), .02)
                    self.assertLess(level(output, 2.5), .001)
                    self.assertGreater(level(output, expected_duration - .5), .02)
                    timeline = json.loads(output.with_suffix(".timeline.json").read_text())
                    self.assertEqual(result["timeline"], timeline["timeline"])
                    self.assertAlmostEqual(expected_duration, timeline["timeline"][-1]["end"])
            for transitions in ([], [{"type": "fade", "duration": 1.1}] * 2):
                with self.subTest(invalid=transitions), self.assertRaises(ValueError):
                    sdk.video_concat(clips, Path(tmp) / "invalid.mp4", transitions=transitions)

    def test_concat_uses_video_stream_duration_when_audio_is_longer(self):
        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.mp4"
            second = Path(tmp) / "second.mp4"
            output = Path(tmp) / "out.mp4"
            sdk._run_ffmpeg([
                "-f", "lavfi", "-i", "color=c=red:s=96x96:d=2:r=24",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac",
                str(first),
            ])
            sdk._run_ffmpeg([
                "-f", "lavfi", "-i", "color=c=blue:s=96x96:d=2:r=24",
                "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac",
                str(second),
            ])
            self.assertAlmostEqual(3, sdk.probe_duration(str(first)), delta=.07)
            self.assertAlmostEqual(2, sdk.probe_video_duration(str(first)), delta=.07)
            result = sdk.video_concat(
                [str(first), str(second)], output,
                transitions=[{"type": "fade", "duration": .4}],
            )
            self.assertAlmostEqual(3.6, sdk.probe_duration(str(output)), delta=.08)
            self.assertAlmostEqual(3.6, result["timeline"][-1]["end"], delta=.02)
            probe = subprocess.run([
                sdk.FFMPEG, "-v", "error", "-ss", "3.2", "-i", str(output),
                "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24",
                "-f", "rawvideo", "-",
            ], capture_output=True, check=True, timeout=30)
            self.assertGreater(probe.stdout[2], 220, list(probe.stdout[:3]))


if __name__ == "__main__":
    unittest.main()
