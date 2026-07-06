"""Test 21: PE 自检 TIME-1 / EMO-1 结构化门禁 mock。

离线测试，不提交 Seedance 任务。
目标：
1. 验证包含段内时间轴、表情变化和副主体随动的 PE 自检可以 PASS 放行。
2. 验证缺 TIME-1 / EMO-1 细节时会 BLOCKED。
3. 验证 BLOCKED 后系统下一步是明确回退，而不是继续调用模型。
"""
from pathlib import Path

from _common import ROOT, banner, fail, info, ok, save_json


REQUIRED_SELF_CHECK_TERMS = (
    "pe_self_check / model_call_preflight: PASS | BLOCKED",
    "time_axis_motion",
    "expression_and_secondary_motion",
    "prompt_specificity",
    "blocking_findings",
)

REQUIRED_PROMPT_TERMS = (
    "参考绑定表",
    "只用于理解剧情梗概",
    "不代表分镜",
    "七层画面描述",
    "段内时间轴",
    "0-3秒",
    "3-6秒",
    "6-10秒",
    "10-14秒",
    "眉毛",
    "眼睛",
    "嘴角",
    "马尾",
    "衣袖",
    "蒸汽",
    "焦点",
)

PASSABLE_ITEM_KEYS = (
    "plan_fidelity",
    "seven_layer_detail",
    "time_axis_motion",
    "reference_binding",
    "storyboard_boundary",
    "route_or_blocking",
    "continuity_and_props",
    "expression_and_secondary_motion",
    "generation_settings_match",
    "prompt_specificity",
)


def mock_director_plan_segment():
    return {
        "id": "seg04_beef_ball_bite_time_axis",
        "duration": 14,
        "camera": "50mm、f/2.8，中近景，轻微手持，焦点从牛肉丸转到鲣鱼眼神。",
        "scene": "潮汕夜市牛肉丸粿条摊前，暖色灯牌、蒸汽、拥挤人流。",
        "storyboard_role": "九宫格故事板只用于剧情梗概、事件顺序、情绪推进，不代表分镜或人物材质。",
        "time_axis": [
            "0-3秒：鲣鱼从摊位右前方靠近，视线先扫锅再落到碗里。",
            "3-6秒：她夹起牛肉丸，热气扑到脸前，眉毛微抬，鼻翼轻动。",
            "6-10秒：牛肉丸入口，她先被烫到，嘴唇微张哈气，随后眼睛亮起来。",
            "10-14秒：她笑着点头，右手稳住碗，左手筷子轻晃，回味后看向镜头。",
        ],
        "dialogue": "{鲣鱼}哇，这个真的会弹，烫归烫，但停不下来。",
        "references": {
            "A": "鲣鱼角色卡三视图",
            "B": "牛肉丸粿条摊主视角",
            "C": "seg04 牛肉丸品尝九宫格故事板",
        },
    }


def good_prompt():
    return (
        "参考绑定表：参考图A = [鲣鱼]角色卡，只锁身份、脸、体型、马尾、浅色衬衫、帆布包和配饰；"
        "参考图B = 牛肉丸粿条摊，只锁摊位空间、锅灶位置、灯牌和蒸汽环境；"
        "参考图C = 九宫格故事板，只用于理解剧情梗概、事件顺序、情绪 / 信息推进，"
        "不代表分镜、不决定切几刀、不锁人物细节、材质、场景结构或镜头视角，箭头和格号不进成片。\n"
        "七层画面描述：1. 镜头层：50mm、f/2.8，中近景，轻微手持，浅景深，焦点从筷子夹起的牛肉丸转到[鲣鱼]眼神。"
        "2. 主体层：[鲣鱼]站在摊位前，右肩略向前，右手托碗，左手用筷子夹牛肉丸；皮肤有真实毛孔，"
        "马尾扎起，浅色衬衫袖口有细褶。3. 地理位置层：潮汕夜市牛肉丸粿条摊前。"
        "4. 空间层次层：前景是筷子和牛肉丸，中景是[鲣鱼]脸和碗，后景是锅灶、灯牌和模糊人流。"
        "5. 天气空气层：夜市空气潮湿，锅边蒸汽上升，局部轻微水雾。"
        "6. 光层：摊位暖色灯从画面左上方打来，锅灶火光从下方补亮脸颊，眼镜边缘有暖光反射。"
        "7. 色彩层：暖黄灯、红褐汤色和深蓝夜色形成对比，整体写实短视频质感。\n"
        "段内时间轴：0-3秒：[鲣鱼]从摊位右前方靠近，视线先扫锅面再落到碗里，"
        "眉心从放松变成期待，马尾和帆布包带随步伐轻晃，蒸汽从锅边向上漂，焦点落在汤面油花，"
        "声音是锅汤翻滚和夜市人声。"
        "3-6秒：她左手筷子夹起一颗牛肉丸，右手稳住碗，热气扑到脸前，眉毛微抬，鼻翼轻动，"
        "嘴角先压住笑意，衣袖随抬手动作产生细小褶皱，筷子尖轻微颤动，焦点从牛肉丸移到她的眼睛，"
        "听到筷子碰碗的轻响。"
        "6-10秒：牛肉丸入口，她先被烫到，嘴唇微张哈气，眼睛短暂睁大，左肩下意识后缩半寸，"
        "随后咀嚼放慢，眼神从惊讶转为发亮，嘴角开始上扬，碗中汤面因右手微动泛起小波纹，"
        "蒸汽掠过脸侧，声音加入轻微哈气和咀嚼声。"
        "10-14秒：她笑着点头，眼睛看向镜头确认好吃，眉毛放松，嘴角完全扬起，"
        "左手筷子在碗边轻晃后停住，马尾末端回摆，衬衫衣摆因身体前倾轻动，"
        "背景灯牌虚化成暖色光斑，焦点稳定在她满足表情上。{鲣鱼}哇，这个真的会弹，烫归烫，但停不下来。"
    )


def bad_prompt_missing_time_axis():
    return (
        "参考图A是鲣鱼，参考图B是牛肉丸摊，参考图C是故事板。"
        "[鲣鱼]在牛肉丸摊前夹起一颗牛肉丸吃下，先被烫到，然后眼睛一亮，笑着点头。"
        "{鲣鱼}哇，这个真的会弹，烫归烫，但停不下来。"
    )


def good_self_check(prompt):
    return {
        "status": "PASS",
        "generation_target": "video",
        "segment_or_asset": "seg04_beef_ball_bite_time_axis",
        "prompt_ready_for_generation": "yes",
        "checked_against": {
            "director_plan": "mock_director_plan.seg04_beef_ball_bite_time_axis",
            "script": "mock script dialogue",
            "references": "3 张：角色卡、场景、九宫格故事板，职责均已写入 prompt",
            "generation_settings": "duration=14, ratio=16:9, resolution=720p, generate_audio=true",
        },
        "items": {
            "plan_fidelity": "PASS: 四个时间阶段、50mm、f/2.8、台词均进入 prompt",
            "seven_layer_detail": "PASS: 七层画面描述逐项存在",
            "time_axis_motion": "PASS: 0-3/3-6/6-10/10-14 秒覆盖完整段落",
            "reference_binding": "PASS: 参考图A/B/C职责明确",
            "storyboard_boundary": "PASS: 已声明故事板不代表分镜、人物细节、材质、场景结构或镜头视角",
            "route_or_blocking": "N/A: 本段为摊位前固定品尝，不穿越场景",
            "continuity_and_props": "PASS: 右手托碗、左手筷子、牛肉丸入口状态连续",
            "expression_and_secondary_motion": "PASS: 眉毛、鼻翼、嘴唇、眼睛、嘴角、马尾、衣袖、蒸汽均按时间变化",
            "generation_settings_match": "PASS: 自检设置与 plan 的 14 秒段一致",
            "prompt_specificity": "PASS: prompt 不是一句动作概述，包含可见细节和声音节点",
        },
        "blocking_findings": [],
        "decision": "PASS",
        "prompt_text": prompt,
    }


def blocked_self_check(prompt):
    return {
        "status": "BLOCKED",
        "generation_target": "video",
        "segment_or_asset": "seg04_beef_ball_bite_time_axis",
        "prompt_ready_for_generation": "no",
        "checked_against": {
            "director_plan": "mock_director_plan.seg04_beef_ball_bite_time_axis",
            "script": "mock script dialogue",
            "references": "3 张：角色卡、场景、九宫格故事板",
            "generation_settings": "duration=14, ratio=16:9, resolution=720p, generate_audio=true",
        },
        "items": {
            "plan_fidelity": "BLOCKED: plan 的 0-3/3-6/6-10/10-14 秒阶段没有进入 prompt",
            "seven_layer_detail": "BLOCKED: 缺镜头层、空间层次、天气空气、光和色彩",
            "time_axis_motion": "BLOCKED: 只写夹起→入口→点头，没有段内时间轴",
            "reference_binding": "BLOCKED: 只写参考图A/B/C，没有说明各自职责",
            "storyboard_boundary": "BLOCKED: 没声明故事板不代表分镜/人物细节/材质/场景结构/镜头视角",
            "route_or_blocking": "N/A: 本段为摊位前固定品尝，不穿越场景",
            "continuity_and_props": "PASS: 碗、筷子、牛肉丸基本连续",
            "expression_and_secondary_motion": "BLOCKED: 只有眼睛一亮和笑着点头，缺眉毛、嘴唇、鼻翼、马尾、衣袖、蒸汽随动",
            "generation_settings_match": "PASS: 设置与 14 秒计划一致",
            "prompt_specificity": "BLOCKED: prompt 明显过短，是一句动作概述",
        },
        "blocking_findings": [
            {
                "category": "pe_rule_missing",
                "rule_id": "TIME-1",
                "evidence": "prompt 缺 0-3 / 3-6 / 6-10 / 10-14 秒段内时间轴。",
                "required_fix": "回 PE 重写 prompt，按 plan 时间轴扩写每阶段主体、表情、道具、副主体、环境、镜头和声音。",
            },
            {
                "category": "pe_rule_missing",
                "rule_id": "EMO-1",
                "evidence": "表情只写眼睛一亮和笑着点头，缺触发、压住/失守、外部微表情流露。",
                "required_fix": "回 PE 重写表情推进，补眉毛、眼睛、嘴唇、鼻翼和咀嚼节奏变化。",
            },
        ],
        "decision": "BLOCKED",
        "prompt_text": prompt,
    }


def inconsistent_pass_self_check(prompt):
    check = good_self_check(prompt)
    check["items"]["time_axis_motion"] = "BLOCKED: 人为构造顶层 PASS 与子项 BLOCKED 冲突"
    return check


def assert_skill_rules_present():
    pe_text = (ROOT / "skills/skill_prompt_engineering/SKILL.md").read_text(encoding="utf-8")
    missing = [term for term in REQUIRED_SELF_CHECK_TERMS if term not in pe_text]
    return missing


def validate_prompt_content(prompt):
    return [term for term in REQUIRED_PROMPT_TERMS if term not in prompt]


def item_is_passable(value):
    text = str(value).strip()
    return text.startswith("PASS") or text.startswith("N/A:")


def validate_pe_self_check(check):
    errors = []
    status = check.get("status")
    decision = check.get("decision")
    items = check.get("items") or {}
    blocking_findings = check.get("blocking_findings") or []
    prompt_ready = check.get("prompt_ready_for_generation")

    if status not in {"PASS", "BLOCKED"}:
        errors.append("自检顶层 status 必须是 PASS 或 BLOCKED。")
    if decision not in {"PASS", "BLOCKED"}:
        errors.append("decision 必须是 PASS 或 BLOCKED。")
    if status != decision:
        errors.append("status 和 decision 必须一致。")

    missing_item_keys = [key for key in PASSABLE_ITEM_KEYS if key not in items]
    if missing_item_keys:
        errors.append("自检缺少必需 items: " + "、".join(missing_item_keys))

    blocked_items = [
        key for key, value in items.items()
        if str(value).strip().startswith("BLOCKED")
    ]

    if status == "PASS":
        if prompt_ready != "yes":
            errors.append("PASS 时 prompt_ready_for_generation 必须是 yes。")
        if blocking_findings:
            errors.append("PASS 时 blocking_findings 必须为空。")
        if blocked_items:
            errors.append("PASS 时不能存在 BLOCKED 子项: " + "、".join(blocked_items))
        non_passable = [
            key for key in PASSABLE_ITEM_KEYS
            if key in items and not item_is_passable(items[key])
        ]
        if non_passable:
            errors.append("PASS 时每个必需 item 必须 PASS 或 N/A: " + "、".join(non_passable))
        missing_prompt_terms = validate_prompt_content(check.get("prompt_text", ""))
        if missing_prompt_terms:
            errors.append("PASS prompt 缺少关键 PE 证据: " + "、".join(missing_prompt_terms))
    else:
        if prompt_ready != "no":
            errors.append("BLOCKED 时 prompt_ready_for_generation 必须是 no。")
        if not blocking_findings:
            errors.append("BLOCKED 时必须列出 blocking_findings。")
        if not blocked_items:
            errors.append("BLOCKED 时至少要有一个 BLOCKED 子项。")

    return errors


def decide_blocked_next_step(check):
    findings = check.get("blocking_findings") or []
    categories = {finding.get("category") for finding in findings}
    rule_ids = {finding.get("rule_id") for finding in findings}

    if not findings:
        return {
            "allow_model_call": False,
            "next_action": "halt_and_request_structured_findings",
            "message": "PE 自检为 BLOCKED，但没有阻塞证据；先补 blocking_findings，不允许调用模型。",
        }

    if "tool_param_conflict" in categories:
        next_action = "fix_generation_settings_then_rerun_pe_self_check"
    elif "route_map_missing" in categories or "asset_or_storyboard_issue" in categories:
        next_action = "return_to_director_or_assets_then_recompile_prompt"
    elif "chain_reference_missing" in categories:
        next_action = "wait_for_previous_final_reference_then_recompile_prompt"
    elif "plan_mismatch" in categories:
        next_action = "return_to_pe_or_director_to_restore_plan_fidelity"
    else:
        next_action = "return_to_prompt_engineering_rewrite_prompt"

    required_fixes = [finding["required_fix"] for finding in findings if finding.get("required_fix")]
    return {
        "allow_model_call": False,
        "next_action": next_action,
        "rule_ids": sorted(rule_ids),
        "required_fixes": required_fixes,
        "message": "禁止调用模型；按 blocking_findings 修复 prompt / plan / 素材 / 设置后，重新输出 PE 自检。",
    }


def main():
    banner("Test 21: PE 自检 TIME-1 / EMO-1 结构化门禁 mock")

    missing_rules = assert_skill_rules_present()
    if missing_rules:
        for term in missing_rules:
            fail(f"skill_prompt_engineering 缺少规则文本: {term}")
        return False
    ok("skill_prompt_engineering 已包含结构化 PE 自检关键字段")

    plan = mock_director_plan_segment()
    good = good_self_check(good_prompt())
    blocked = blocked_self_check(bad_prompt_missing_time_axis())
    inconsistent = inconsistent_pass_self_check(good_prompt())

    cases = [
        ("good_time_axis_expression_prompt", good, True, True),
        ("bad_missing_time_axis_expression", blocked, False, True),
        ("bad_inconsistent_pass_with_blocked_item", inconsistent, False, False),
    ]

    all_ok = True
    results = []
    for name, check, expect_allow, expect_schema_ok in cases:
        errors = validate_pe_self_check(check)
        schema_ok = not errors
        allow_generation = check.get("status") == "PASS" and schema_ok
        results.append({
            "name": name,
            "expected_schema_ok": expect_schema_ok,
            "actual_schema_ok": schema_ok,
            "expected_allow_generation": expect_allow,
            "actual_allow_generation": allow_generation,
            "errors": errors,
            "blocked_next_step": None if allow_generation else decide_blocked_next_step(check),
        })
        if allow_generation == expect_allow and schema_ok == expect_schema_ok:
            ok(f"{name}：结果符合预期")
        else:
            all_ok = False
            fail(
                f"{name}：预期 allow={expect_allow}/schema={expect_schema_ok}，"
                f"实际 allow={allow_generation}/schema={schema_ok}，errors={errors}"
            )

    blocked_step = decide_blocked_next_step(blocked)
    expected_action = "return_to_prompt_engineering_rewrite_prompt"
    if blocked_step["allow_model_call"] is False and blocked_step["next_action"] == expected_action:
        ok("BLOCKED 后回退逻辑正确：禁止调用模型，回 PE 重写并重审")
    else:
        all_ok = False
        fail(f"BLOCKED 回退逻辑不符合预期: {blocked_step}")

    out = save_json("test_21_pe_self_check_time_axis_mock.json", {
        "director_plan_segment": plan,
        "results": results,
        "blocked_next_step": blocked_step,
    })
    info(f"mock 结果已写入 {Path(out).relative_to(ROOT)}")
    return all_ok


if __name__ == "__main__":
    import sys

    sys.exit(0 if main() else 1)
