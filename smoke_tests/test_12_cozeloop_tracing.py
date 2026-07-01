"""Dry-run test for the optional CozeLoop tracing plugin.

The test uses a fake ``cozeloop`` module, so it does not require a real
CozeLoop account, SDK installation, or network access. It runs the real
agent_runner_loop to exercise the same hook path used by local runtime.
"""
from __future__ import annotations

import importlib
import sys
import types

from _common import ROOT, banner, ok, info

sys.path.insert(0, str(ROOT))

from agent_loop import BaseHandler, StepOutcome, agent_runner_loop


class FakeSpan:
    def __init__(self, name, span_type, child_of=None, start_new_trace=False):
        self.name = name
        self.span_type = span_type
        self.child_of = child_of
        self.start_new_trace = start_new_trace
        self.input = None
        self.output = None
        self.tags = {}
        self.finished = False
        self.service_name = None
        self.deployment_env = None

    def set_input(self, value):
        self.input = value

    def set_output(self, value):
        self.output = value

    def set_tags(self, value):
        self.tags.update(value or {})

    def set_service_name(self, value):
        self.service_name = value

    def set_deployment_env(self, value):
        self.deployment_env = value

    def set_model_provider(self, value):
        self.tags["model_provider"] = value

    def set_model_name(self, value):
        self.tags["model_name"] = value

    def finish(self):
        self.finished = True


class FakeClient:
    def __init__(self):
        self.spans = []
        self.flush_count = 0

    def start_span(self, name, span_type, *, child_of=None, start_new_trace=False, **kwargs):
        span = FakeSpan(name, span_type, child_of=child_of, start_new_trace=start_new_trace)
        self.spans.append(span)
        return span

    def flush(self):
        self.flush_count += 1


class FakeBackend:
    name = "doubao"
    model = "deepseek-v4-pro-260425"
    api_mode = "chat_completions"


class FakeFunction:
    name = "file_read"
    arguments = '{"path": "skills/skill_review/SKILL.md"}'


class FakeToolCall:
    function = FakeFunction()
    id = "call_file_read"


class FakeResponse:
    content = "需要读取文件"
    tool_calls = [FakeToolCall()]
    stop_reason = "tool_use"


class FakeLLMRuntimeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""

    def chat(self, messages, tools=None):
        if False:
            yield ""
        return FakeResponse()


class FakeRuntimeHandler(BaseHandler):
    def __init__(self):
        self._done_hooks = []
        self.parent = types.SimpleNamespace(task_dir="")

    def do_file_read(self, args, response):
        yield "reading file\n"
        return StepOutcome(
            {"path": args["path"], "content": "review skill"},
            next_prompt=None,
        )


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    banner("Test 12: CozeLoop tracing 插件本地 dry-run")

    import llmcore
    import plugins.hooks as hooks

    fake_client = FakeClient()
    fake_cozeloop = types.ModuleType("cozeloop")
    fake_cozeloop.new_client = lambda **kwargs: fake_client

    original_cozeloop = sys.modules.get("cozeloop")
    original_reload = llmcore.reload_runtime_config
    original_registry = {k: list(v) for k, v in hooks._registry.items()}

    try:
        hooks.clear()
        sys.modules["cozeloop"] = fake_cozeloop
        sys.modules.pop("plugins.cozeloop_tracing", None)
        llmcore.reload_runtime_config = lambda: (
            {
                "cozeloop_config": {
                    "enabled": True,
                    "workspace_id": "dry-run-workspace",
                    "api_token": "dry-run-token",
                    "service_name": "vibefilming-test",
                    "deployment_env": "test",
                    "capture_inputs": True,
                    "capture_outputs": True,
                }
            },
            True,
        )

        plugin = importlib.import_module("plugins.cozeloop_tracing")
        _assert(plugin.is_enabled(), f"CozeLoop 插件没有启用：{plugin.init_error()}")
        ok("插件在配置存在时会启用并初始化 CozeLoop client")

        output = "".join(
            chunk
            for chunk in agent_runner_loop(
                FakeLLMRuntimeClient(),
                "system prompt",
                "读取审查 skill",
                FakeRuntimeHandler(),
                tools_schema=[
                    {
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "description": "read file",
                            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                        },
                    }
                ],
                max_turns=2,
                verbose=False,
            )
        )
        _assert("file_read" in output, "agent loop 没有执行 fake tool")

        names = [s.name for s in fake_client.spans]
        _assert(names == ["vibefilming.agent", "llm.chat", "tool.file_read"], f"span 顺序不对：{names}")
        agent_span, llm_span, tool_span = fake_client.spans
        _assert(agent_span.start_new_trace is True, "agent span 没有开启新 trace")
        _assert(llm_span.child_of is agent_span, "LLM span 没有挂到 agent span 下")
        _assert(tool_span.child_of is agent_span, "tool span 没有挂到 agent span 下")
        _assert(all(s.finished for s in fake_client.spans), "存在未 finish 的 span")
        _assert(fake_client.flush_count == 1, "agent 结束时没有 flush")
        _assert(llm_span.tags["model_name"] == "deepseek-v4-pro-260425", "LLM model tag 不正确")
        _assert(tool_span.input == {"path": "skills/skill_review/SKILL.md"}, "tool 输入没有去掉内部字段")
        ok("真实 agent_runner_loop 路径下，agent / llm / tool spans 都能正确创建、挂父子关系、写入输入输出并 finish")
    finally:
        hooks.clear()
        hooks._registry.update(original_registry)
        llmcore.reload_runtime_config = original_reload
        sys.modules.pop("plugins.cozeloop_tracing", None)
        if original_cozeloop is None:
            sys.modules.pop("cozeloop", None)
        else:
            sys.modules["cozeloop"] = original_cozeloop

    info("未连接真实 CozeLoop；这是无网络、无账号的本地 dry-run")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
