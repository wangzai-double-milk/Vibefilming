"""Dry-run test: Seedance prompt special symbols are preserved.

This test does not submit a real Seedance task. It validates three local facts:
- skill_prompt_engineering keeps the Seedance special-symbol rules.
- gen_video_t2v schema exposes the audio prompt requirements and defaults audio on.
- a prompt containing （） <> {} 【】 reaches the SDK request unchanged.
"""
import sys

from _common import ROOT, banner, ok, fail, info

sys.path.insert(0, str(ROOT))
from film import tools


SPECIAL_SYMBOLS = {
    "music": ("（", "）", "音乐 / BGM"),
    "sfx": ("<", ">", "音效 / 环境声"),
    "speech": ("{", "}", "台词 / 实声对白"),
    "subtitle": ("【", "】", "字幕 / 标题 / 屏幕叠字"),
}


def _find_video_schema():
    for item in tools.build_film_schema():
        fn = item.get("function") or {}
        if fn.get("name") == "gen_video_t2v":
            return fn
    return None


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    banner("Test 11: Seedance 特殊符号 prompt 本地 dry-run")

    skill_path = ROOT / "skills" / "skill_prompt_engineering" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    _assert("Seedance 特殊符号" in skill_text, "SKILL.md 缺少 Seedance 特殊符号章节")
    for label, (left, right, title) in SPECIAL_SYMBOLS.items():
        _assert(left in skill_text and right in skill_text, f"SKILL.md 缺少 {label} 符号 {left}{right}")
        _assert(title in skill_text, f"SKILL.md 缺少 {title} 规则")
    _assert("{逐字台词}" in skill_text, "SKILL.md 缺少逐字台词硬规则")
    ok("SKILL.md 保留了 （） / <> / {} / 【】 的 Seedance prompt 语法规则")

    schema = _find_video_schema()
    _assert(schema is not None, "找不到 gen_video_t2v schema")
    props = schema["parameters"]["properties"]
    prompt_desc = props["prompt"]["description"]
    audio_spec = props["generate_audio"]
    _assert(audio_spec.get("default") is True, "generate_audio 默认值不是 true")
    _assert("{逐字台词}" in prompt_desc, "gen_video_t2v.prompt 描述缺少 {逐字台词}")
    _assert("<具体音效>" in prompt_desc, "gen_video_t2v.prompt 描述缺少 <具体音效>")
    _assert("背景音乐" in prompt_desc, "gen_video_t2v.prompt 描述缺少背景音乐说明")
    ok("gen_video_t2v schema 保留音频 prompt 规则，且 generate_audio 默认 true")

    sample_prompt = (
        "承接上段视频，夜晚实验室中，女主角看向失控的屏幕，opening frame: 她站在蓝色报警灯前；"
        "ending frame: 她转身跑向门口。"
        "女主角低声说 {别怕，我会把你带回家。}"
        "<警报器短促鸣响><金属门远处震动>"
        "（低频悬疑电子音乐，音量低，台词时自动避让）"
        "屏幕上出现可读文字【HOME SIGNAL FOUND】"
    )

    captured = {}

    def fake_submit_video_task(
        prompt,
        reference_images=None,
        reference_video_url=None,
        duration=None,
        generate_audio=True,
        resolution="720p",
        ratio="16:9",
        seed=None,
        camera_fixed=None,
    ):
        captured.update(
            {
                "prompt": prompt,
                "reference_images": reference_images,
                "reference_video_url": reference_video_url,
                "duration": duration,
                "generate_audio": generate_audio,
                "resolution": resolution,
                "ratio": ratio,
                "seed": seed,
                "camera_fixed": camera_fixed,
            }
        )
        return {
            "task_id": "dry_run_seedance_symbols",
            "model": "dry-run-model",
            "raw": {"status": "dry_run"},
            "body": {
                "model": "dry-run-model",
                "content": [{"type": "text", "text": prompt}],
                "duration": duration,
                "ratio": ratio,
                "resolution": resolution,
                "generate_audio": generate_audio,
            },
        }

    original_submit = tools.sdk.submit_video_task
    original_active_pid = tools._active_pid
    tools.sdk.submit_video_task = fake_submit_video_task
    tools._active_pid = lambda handler: None
    try:
        result = tools._gen_video_t2v(
            object(),
            {
                "prompt": sample_prompt,
                "name": "dry_run_seedance_symbols",
                "duration": 8,
                "ratio": "16:9",
            },
        )
        _assert(result["task_id"] == "dry_run_seedance_symbols", "dry-run task_id 不正确")
        _assert(captured["prompt"] == sample_prompt, "prompt 没有原样传入 SDK")
        _assert(captured["generate_audio"] is True, "未显式传 generate_audio 时没有默认开启")
        for label, (left, right, _) in SPECIAL_SYMBOLS.items():
            _assert(left in captured["prompt"] and right in captured["prompt"], f"SDK prompt 丢失 {label} 符号")
        ok("含特殊符号的 prompt 已原样进入 Seedance 请求参数，generate_audio 默认开启")

        try:
            tools._gen_video_t2v(
                object(),
                {
                    "prompt": "角色开口说 {测试对白}",
                    "name": "dry_run_bad_audio_flag",
                    "duration": 5,
                    "ratio": "16:9",
                    "generate_audio": False,
                },
            )
            raise AssertionError("generate_audio=false + 台词 prompt 未被拦截")
        except ValueError as e:
            _assert("generate_audio=false" in str(e), "拦截错误信息不符合预期")
            ok("generate_audio=false 与台词符号冲突会被拦截")
    finally:
        tools.sdk.submit_video_task = original_submit
        tools._active_pid = original_active_pid

    info("未提交真实 Seedance 任务；仅捕获本地请求体")
    return True


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
