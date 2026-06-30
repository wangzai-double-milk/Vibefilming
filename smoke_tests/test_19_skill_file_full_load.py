"""file_read 对 skill 文件必须整篇返回；其余文件顺序截断 + 续读提示，不掐中段。

回归背景：曾因 do_file_read 用 smart_format「掐头去尾」截断，且一轮批量读 6 个文件
时每个预算被压到 ~2500 字符，导致 skill 正中段（故事板红线、写实词红线）被静默 omit，
模型根本没读到规则。本测试锁死「skill 整篇 + 非 skill 顺序截断」不变量。
"""
from __future__ import annotations

import sys

from _common import ROOT, banner, ok

sys.path.insert(0, str(ROOT))

import ga


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _simulate_do_file_read(rel_path, tool_num=6, count=200):
    """复刻 do_file_read 的截断分支（不实例化 handler，避免副作用）。"""
    abspath = str(ROOT / rel_path)
    is_skill = ga._is_skill_doc(rel_path)
    c = max(count, 100000) if (is_skill and count) else count
    result = ga.file_read(abspath, start=1, keyword=None, count=c, show_linenos=True)
    result = "由于设置了show_linenos\n" + result
    if not (is_skill and not result.startswith("Error:")):
        result = ga.truncate_keep_head(result, 60000 // tool_num)
    return result


def main():
    banner("Test 19: skill 文件整篇加载 + 非 skill 顺序截断")

    # 1) skill 判定
    _assert(ga._is_skill_doc("skills/skill_director/SKILL.md"), "skill 路径未被识别")
    _assert(ga._is_skill_doc("/abs/repo/skills/skill_movie/SKILL.md"), "绝对 skill 路径未被识别")
    _assert(not ga._is_skill_doc("projects/p1/director_plan.json"), "非 skill 被误判")
    _assert(not ga._is_skill_doc("skills/x/notes.md"), "非 SKILL.md 被误判")
    ok("_is_skill_doc 判定正确")

    # 2) truncate_keep_head：顺序保留开头、给续读提示、不掐中段
    fake = "[FILE] 100 lines\n" + "\n".join(f"{i}|line-{i}-pad" for i in range(1, 101))
    out = ga.truncate_keep_head(fake, 300)
    _assert("1|line-1-" in out, "顺序截断丢了开头")
    _assert("100|line-100" not in out, "顺序截断没有真的截掉尾部")
    _assert("续读" in out and "未读" in out, "缺少续读提示")
    ok("truncate_keep_head 顺序保留开头 + 续读提示")

    # 3) skill_director 必须整篇返回，故事板红线全在，无 omit 中段
    r = _simulate_do_file_read("skills/skill_director/SKILL.md", tool_num=6)
    for section in [
        "## 一、先重排叙事", "## 五、导演镜头节拍", "## 六、段间因果与衔接设计",
        "## 七、故事板：编导的多宫格分镜图", "## 九、音频处理计划", "## 十、交付",
    ]:
        _assert(section in r, f"skill_director 缺章节 {section}（被截断了）")
    for word in ["多宫格", "手绘线稿", "photorealistic"]:
        _assert(word in r, f"故事板红线词 {word} 没进上下文")
    _assert("[omitted long content]" not in r, "skill 仍被掐头去尾 omit 中段")
    ok(f"skill_director 整篇加载（{len(r)} 字符），故事板红线全部在")

    # 4) skill_prompt_engineering 的写实词红线也整篇在
    r2 = _simulate_do_file_read("skills/skill_prompt_engineering/SKILL.md", tool_num=6)
    for word in ["photorealistic", "cinematic"]:
        _assert(word in r2, f"PE 红线词 {word} 没进上下文")
    _assert("[omitted long content]" not in r2, "PE 仍被 omit 中段")
    ok(f"skill_prompt_engineering 整篇加载（{len(r2)} 字符），写实词红线全在")

    # 5) 非 skill 大文件仍被截断保护（不会无限灌爆上下文）
    big = _simulate_do_file_read("ga.py", tool_num=6, count=5000)
    _assert("[内容过长·已顺序截断]" in big, "非 skill 大文件没有被截断保护")
    ok("非 skill 大文件仍走顺序截断 + 续读提示")

    ok("skill 整篇 / 非 skill 顺序截断 不变量已锁定")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
