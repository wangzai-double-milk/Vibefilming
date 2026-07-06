"""Test 20: 长镜头链式生成的 CHAIN-1 prompt 静态门禁 mock。

离线测试，不提交 Seedance 任务。
目标是验证新规则能拦住两类常见错误：
1. plan 已标链式延展，但调用参数里没有 reference_video_url。
2. 传了 reference_video_url，但 prompt 只写"承接上段"，没有写"基于参考视频继续往后扩写"。
"""
import json
from pathlib import Path

from _common import ROOT, banner, fail, info, ok, save_json


REQUIRED_CHAIN_PHRASE = "基于参考视频继续往后扩写"
TAIL_STATE_PHRASES = ("参考视频最后", "参考视频末尾", "最后一帧")
NO_RESET_TERMS = ("不重置", "人物", "服装", "道具", "光线", "机位", "运动方向")


def mock_director_plan():
    """构造一个被 15 秒上限拆开的同一长镜头。

    seg07a 是链式起点，seg07b 是必须基于 seg07a 终版视频继续延展的下一段。
    """
    return {
        "project": "mock_chain_long_take",
        "segments": [
            {
                "id": "seg07a_corridor_long_take_part1",
                "chain_mode": "chain_start",
                "duration": 12,
                "camera": "35mm、f/2.8，稳定器后跟长镜头",
                "end_state": "侦探走到走廊尽头，右手握住门把，左肩略低，手电光扫到门缝。",
            },
            {
                "id": "seg07b_corridor_long_take_part2",
                "chain_mode": "chain_extension",
                "chain_from": "seg07a_corridor_long_take_part1",
                "duration": 10,
                "camera": "继续同一机位和同一稳定器运动，不切镜",
                "start_state": "必须承接 seg07a 终版视频最后状态：右手仍握门把，手电光仍落在门缝。",
                "end_state": "门被推开，镜头跟随侦探进入暗房。",
            },
        ],
    }


def find_segment(plan, segment_id):
    for segment in plan.get("segments", []):
        if segment.get("id") == segment_id:
            return segment
    raise KeyError(f"找不到 segment: {segment_id}")


def is_chain_extension(segment):
    return (
        segment.get("chain_mode") == "chain_extension"
        or bool(segment.get("chain_from"))
        or segment.get("transition") == "chain_extension"
    )


def validate_chain_gate(plan, segment_id, call_args):
    """最小化模拟 PE 调用前静态审查里的 CHAIN-1 门禁。"""
    segment = find_segment(plan, segment_id)
    if not is_chain_extension(segment):
        return []

    errors = []
    prompt = call_args.get("prompt", "")
    reference_video_url = call_args.get("reference_video_url")

    if not reference_video_url:
        errors.append("CHAIN-1: 链式延展段缺少 reference_video_url，不能只靠首尾帧文字。")

    if REQUIRED_CHAIN_PHRASE not in prompt:
        errors.append(f"CHAIN-1: prompt 缺少'{REQUIRED_CHAIN_PHRASE}'。")

    if not any(phrase in prompt for phrase in TAIL_STATE_PHRASES):
        errors.append("CHAIN-1: prompt 没写清参考视频最后状态 / 末尾状态。")

    missing_no_reset_terms = [term for term in NO_RESET_TERMS if term not in prompt]
    if missing_no_reset_terms:
        errors.append(
            "CHAIN-1: prompt 没写清不允许重置的连续项："
            + "、".join(missing_no_reset_terms)
        )

    if ".mp4" in prompt or "/Users/" in prompt:
        errors.append("CHAIN-1: prompt 正文泄漏了视频路径；参考视频应作为调用输入传入。")

    return errors


def build_cases():
    bad_missing_video = {
        "name": "bad_missing_reference_video",
        "segment_id": "seg07b_corridor_long_take_part2",
        "expect_pass": False,
        "args": {
            "name": "seg07b_bad_missing_reference_video",
            "duration": 10,
            "ratio": "21:9",
            "prompt": (
                "承接上一段视频，opening frame 与上一段 ending frame 一致。"
                "侦探继续推门进入暗房，保持悬疑气氛。"
            ),
        },
    }

    bad_missing_phrase = {
        "name": "bad_missing_continue_phrase",
        "segment_id": "seg07b_corridor_long_take_part2",
        "expect_pass": False,
        "args": {
            "name": "seg07b_bad_missing_continue_phrase",
            "duration": 10,
            "ratio": "21:9",
            "reference_video_url": "https://example.invalid/mock/seg07a.mp4",
            "prompt": (
                "承接上一段视频。侦探推开门进入暗房，走廊灯光逐渐熄灭。"
                "opening frame: 侦探右手握住门把。ending frame: 镜头进入暗房。"
            ),
        },
    }

    good_chain_extension = {
        "name": "good_chain_extension",
        "segment_id": "seg07b_corridor_long_take_part2",
        "expect_pass": True,
        "args": {
            "name": "seg07b_good_chain_extension",
            "duration": 10,
            "ratio": "21:9",
            "reference_video_url": "https://example.invalid/mock/seg07a.mp4",
            "prompt": (
                "基于参考视频继续往后扩写；参考视频最后一帧中，[侦探] 站在走廊尽头，"
                "右手仍握住门把，左肩略低，手电光仍落在门缝上。本段从这个状态无缝继续，"
                "不重置人物、服装、道具、光线、机位和运动方向。"
                "35mm、f/2.8 稳定器后跟长镜头继续向前推进，门被缓慢推开，"
                "冷白手电光先扫进暗房，空气尘粒在光束里漂浮，衣摆和肩带随推门动作轻微滞后摆动。"
            ),
        },
    }

    normal_new_shot = {
        "name": "normal_new_shot_not_chain",
        "segment_id": "seg07a_corridor_long_take_part1",
        "expect_pass": True,
        "args": {
            "name": "seg07a_normal_new_shot",
            "duration": 12,
            "ratio": "21:9",
            "prompt": "新生成长镜头起点：[侦探] 从走廊入口向深处走去。",
        },
    }

    return [bad_missing_video, bad_missing_phrase, good_chain_extension, normal_new_shot]


def assert_skill_and_tool_rules_present():
    pe_text = (ROOT / "skills/skill_prompt_engineering/SKILL.md").read_text(encoding="utf-8")
    tools_text = (ROOT / "film/tools.py").read_text(encoding="utf-8")

    checks = [
        ("PE 包含 CHAIN-1", "CHAIN-1" in pe_text),
        ("PE 要求基于参考视频继续往后扩写", REQUIRED_CHAIN_PHRASE in pe_text),
        ("工具层支持 reference_video_url", "reference_video_url" in tools_text),
        ("工具层参数说明包含链式续写口径", REQUIRED_CHAIN_PHRASE in tools_text),
    ]
    return [label for label, passed in checks if not passed]


def main():
    banner("Test 20: CHAIN-1 长镜头链式生成 prompt 静态门禁 mock")

    missing_rules = assert_skill_and_tool_rules_present()
    if missing_rules:
        for item in missing_rules:
            fail(item)
        return False
    ok("skill 与工具层关键规则存在")

    plan = mock_director_plan()
    cases = build_cases()
    results = []

    all_ok = True
    for case in cases:
        errors = validate_chain_gate(plan, case["segment_id"], case["args"])
        passed = not errors
        expected = bool(case["expect_pass"])
        results.append({
            "name": case["name"],
            "expected_pass": expected,
            "actual_pass": passed,
            "errors": errors,
        })

        if passed == expected:
            ok(f"{case['name']}：结果符合预期")
        else:
            all_ok = False
            fail(f"{case['name']}：预期 {expected}，实际 {passed}，errors={errors}")

    out = save_json("test_20_chain_prompt_gate_mock.json", {
        "director_plan": plan,
        "results": results,
    })
    info(f"mock 结果已写入 {Path(out).relative_to(ROOT)}")
    return all_ok


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
