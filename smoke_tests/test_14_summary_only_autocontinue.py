"""Dry-run test: summary-only progress replies should not end long tasks."""
import sys
from types import SimpleNamespace

from _common import ROOT, banner, ok, info

sys.path.insert(0, str(ROOT))
from ga import GenericAgentHandler, _summary_only_pending_action


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _run(gen):
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as e:
        return chunks, e.value


def _handler():
    parent = SimpleNamespace(task_dir="", verbose=False)
    return GenericAgentHandler(parent=parent, last_history=[], cwd=str(ROOT / "temp"))


def main():
    banner("Test 14: summary-only pending action auto-continue")

    pending = "<summary>S01-S04已提交，继续并行提交S05-S08让队列满负荷</summary>"
    _assert(_summary_only_pending_action(pending), "未识别 summary-only pending action")

    chunks, outcome = _run(_handler().do_no_tool({}, SimpleNamespace(content=pending, thinking="")))
    _assert(outcome.next_prompt, "summary-only pending action 被错误当作最终回复")
    _assert("必须在本轮直接调用下一步所需工具" in outcome.next_prompt, "续跑提示不明确")
    _assert(any("Continuing autonomously" in c for c in chunks), "没有输出自动续跑警告")
    ok("只写进度 summary 且承诺继续时，会自动要求下一轮直接调用工具")

    final = "<summary>已完成全部视频生成和审查，最终交付路径已给出</summary>"
    _assert(not _summary_only_pending_action(final), "最终完成 summary 不应触发续跑")
    _, outcome = _run(_handler().do_no_tool({}, SimpleNamespace(content=final, thinking="")))
    _assert(outcome.next_prompt is None, "最终完成回复不应自动续跑")
    ok("明确完成的 summary 不会被强行续跑")

    normal = "这个问题的原因是上一轮没有工具调用，所以 CLI 回到输入提示符。"
    _assert(not _summary_only_pending_action(normal), "普通解释不应触发续跑")
    _, outcome = _run(_handler().do_no_tool({}, SimpleNamespace(content=normal, thinking="")))
    _assert(outcome.next_prompt is None, "普通解释不应自动续跑")
    ok("普通最终回答不受影响")

    info("未调用真实 LLM 或工具；仅验证 no_tool 分支")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
