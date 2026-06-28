"""Context trimming should preserve workflow anchors better than generic logs."""
from __future__ import annotations

import sys

from _common import ROOT, banner, ok

sys.path.insert(0, str(ROOT))

import llmcore


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    banner("Test 17: 长任务上下文压缩保留关键锚点")
    old_cd = getattr(llmcore.compress_history_tags, "_cd", 0)
    try:
        llmcore.compress_history_tags._cd = 0
        key_info = "当前阶段：成片终审；不得交付，直到 reviews/final_review_v1.json 落盘。" + "K" * 5000
        skill_body = "审片规则：" + "R" * 5000 + "必须成片终审。" + "S" * 5000
        noisy_tool = "普通日志：" + "N" * 5000
        messages = [
            {
                "role": "user",
                "content": (
                    f"<key_info>{key_info}</key_info>\n"
                    "<history>old history should be folded</history>\n"
                    f"<tool_result>{noisy_tool}</tool_result>"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": (
                            '{"path":"skills/skill_review/SKILL.md","content":"'
                            + skill_body
                            + '"}'
                        ),
                    }
                ],
            },
            {"role": "assistant", "content": "recent message"},
        ]
        llmcore.compress_history_tags(messages, keep_recent=1, max_len=800, force=True, interval=1)
    finally:
        llmcore.compress_history_tags._cd = old_cd

    compressed_text = messages[0]["content"]
    compressed_skill = messages[1]["content"][0]["content"]
    _assert("<key_info>" in compressed_text and "当前阶段：成片终审" in compressed_text, "key_info 被整体压没了")
    _assert("<history>[...]</history>" in compressed_text, "history 没有折叠")
    _assert(len(compressed_skill) > 2000, "skill 锚点被按普通工具结果过度截断")
    _assert("skills/skill_review/SKILL.md" in compressed_skill, "skill 路径锚点丢失")
    ok("压缩旧历史时，key_info 和 skill/director_plan 这类锚点会优先保留")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
