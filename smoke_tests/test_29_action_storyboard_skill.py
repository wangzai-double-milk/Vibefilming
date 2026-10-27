from __future__ import annotations

import re
import sys
from pathlib import Path

from _common import ROOT, banner, ok


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    _assert(match is not None, "skill-storyboard 缺 YAML frontmatter")
    values = {}
    for line in match.group(1).splitlines():
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def main():
    banner("Test 29: 动作故事板 Skill 合同")

    storyboard_path = ROOT / "skills" / "skill_storyboard" / "SKILL.md"
    _assert(storyboard_path.is_file(), "skill_storyboard/SKILL.md 不存在")
    text = storyboard_path.read_text(encoding="utf-8")
    metadata = _frontmatter(text)
    _assert(metadata.get("name") == "skill-storyboard", "frontmatter name 错误")
    description = metadata.get("description", "")
    _assert("故事板" in description and "视频" in description, "description 缺触发语义")
    _assert(": " not in description and "<" not in description and ">" not in description,
            "description 含非法 frontmatter 字符")

    for required in (
        "`2–4 秒`：3–4 格",
        "`4–8 秒`：4–6 格",
        "`8–15 秒`：6–9 格",
        "红色实线箭头",
        "橙色弧形箭头",
        "蓝色箭头",
        "黄色星形或十字标记",
        "初始姿态 → 触发/起势 → 主运动路径 → 接触/命中 → 反馈/反作用 → 落点与稳定状态",
        "shots/storyboard_<unit_id>_v1.png",
        "shots/storyboard_<unit_id>_v1.json",
        "reference_images",
    ):
        _assert(required in text, f"故事板合同缺少：{required}")
    _assert("一张图内固定做 **3×3 九宫格**" not in text, "仍机械固定九宫格")
    ok("独立动作故事板 Skill 合同完整")

    director = (ROOT / "skills" / "skill_director" / "SKILL.md").read_text(
        encoding="utf-8")
    _assert("storyboard_plan" in director and "3–6 格" in director,
            "编导 Skill 未登记动作故事板计划")

    prompt = (
        ROOT / "skills" / "skill_prompt_engineering" / "SKILL.md"
    ).read_text(encoding="utf-8")
    _assert("BLOCKED_STORYBOARD_MISSING" in prompt,
            "提示词 Skill 缺故事板输入门禁")
    _assert("同名 JSON" in prompt and "真实参考图传入" in prompt,
            "提示词 Skill 未要求真实消费故事板")

    review = (ROOT / "skills" / "skill_review" / "SKILL.md").read_text(
        encoding="utf-8")
    _assert("## 动作故事板审查" in review and "接触/命中" in review,
            "审查 Skill 缺动作故事板验收")

    movie = (ROOT / "skills" / "skill_movie" / "SKILL.md").read_text(
        encoding="utf-8")
    _assert("动作故事板图片与同名 JSON" in movie,
            "总控 Skill 缺故事板资产门禁")
    ok("编导 / 提示词 / 审查 / 总控边界已同步")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
