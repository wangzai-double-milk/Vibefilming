"""Dry-run for automatic continuation after max_tokens truncation."""
from __future__ import annotations

import sys
import types

from _common import ROOT, banner, ok

sys.path.insert(0, str(ROOT))

from agent_loop import BaseHandler, StepOutcome, agent_runner_loop
from llmcore import MockResponse, MockToolCall, TRUNCATION_MARKER


class FakeClient:
    def __init__(self):
        self.backend = types.SimpleNamespace(model="deepseek-v4-pro-260425", name="doubao", api_mode="chat_completions")
        self.last_tools = ""
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        if len(self.calls) == 1:
            yield "partial answer"
            return MockResponse("", f"partial answer\n\n{TRUNCATION_MARKER}", [], "raw", stop_reason="length")
        if False:
            yield ""
        return MockResponse("", "", [MockToolCall("finish", {"ok": True}, id="call_finish")], "raw")


class FakeHandler(BaseHandler):
    def __init__(self):
        self._done_hooks = []
        self.parent = types.SimpleNamespace(task_dir="")
        self.finished = False

    def do_finish(self, args, response):
        self.finished = bool(args.get("ok"))
        return StepOutcome({"finished": self.finished}, should_exit=True)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    banner("Test 15: LLM max_tokens 截断自动续跑")
    client = FakeClient()
    handler = FakeHandler()
    output = "".join(
        agent_runner_loop(
            client,
            "system prompt",
            "请完成一个长任务",
            handler,
            tools_schema=[
                {
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "description": "finish",
                        "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    },
                }
            ],
            max_turns=3,
            verbose=False,
        )
    )
    _assert(len(client.calls) == 2, f"截断后没有自动进入下一轮：calls={len(client.calls)} output={output}")
    _assert("max_tokens" in output, "输出中没有提示 max_tokens 截断续跑")
    _assert("从中断处继续" in str(client.calls[1]), "第二轮没有收到继续提示")
    _assert(handler.finished, "第二轮工具调用没有执行")
    ok("模型因 max_tokens 截断时，agent 会自动补一轮继续执行")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
