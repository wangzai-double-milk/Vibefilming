"""CozeLoop tracing via the existing hook system.

This plugin is intentionally optional. It activates only when CozeLoop is
configured, and it degrades to a no-op if the SDK or credentials are missing.

Configuration sources:
- vibefilming.config.json: {"cozeloop": {...}}
- environment variables supported by the CozeLoop SDK:
  COZELOOP_WORKSPACE_ID / COZELOOP_API_TOKEN, or JWT OAuth variables.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any


def _load_runtime_cfg() -> dict:
    try:
        from llmcore import reload_runtime_config

        return reload_runtime_config()[0] or {}
    except Exception:
        return {}


def _cozeloop_env_ready() -> bool:
    has_workspace = bool(os.environ.get("COZELOOP_WORKSPACE_ID"))
    has_token = bool(os.environ.get("COZELOOP_API_TOKEN"))
    has_jwt = all(
        os.environ.get(k)
        for k in (
            "COZELOOP_JWT_OAUTH_CLIENT_ID",
            "COZELOOP_JWT_OAUTH_PRIVATE_KEY",
            "COZELOOP_JWT_OAUTH_PUBLIC_KEY_ID",
        )
    )
    return has_workspace and (has_token or has_jwt)


_cfg = _load_runtime_cfg().get("cozeloop_config") or {}
_explicit_enabled = _cfg.get("enabled")
_enabled = bool(_explicit_enabled) if _explicit_enabled is not None else bool(_cfg or _cozeloop_env_ready())
_client = None
_cozeloop = None
_init_error = None

if _enabled:
    try:
        import cozeloop as _cozeloop

        client_kwargs = {
            "api_base_url": _cfg.get("api_base_url", ""),
            "workspace_id": _cfg.get("workspace_id", ""),
            "api_token": _cfg.get("api_token", ""),
            "jwt_oauth_client_id": _cfg.get("jwt_oauth_client_id", ""),
            "jwt_oauth_private_key": _cfg.get("jwt_oauth_private_key", ""),
            "jwt_oauth_public_key_id": _cfg.get("jwt_oauth_public_key_id", ""),
        }
        client_kwargs = {k: v for k, v in client_kwargs.items() if v}
        _client = _cozeloop.new_client(**client_kwargs)
    except Exception as e:
        _init_error = e
        _client = None
        _cozeloop = None


_capture_inputs = bool(_cfg.get("capture_inputs", True))
_capture_outputs = bool(_cfg.get("capture_outputs", True))
try:
    _max_payload_chars = max(1000, int(_cfg.get("max_payload_chars", 20000)))
except Exception:
    _max_payload_chars = 20000
_service_name = str(_cfg.get("service_name") or "vibefilming")
_deployment_env = str(_cfg.get("deployment_env") or os.environ.get("VIBEFILMING_ENV") or "local")
_tls = threading.local()


def is_enabled() -> bool:
    return _client is not None


def init_error() -> str:
    return "" if _init_error is None else str(_init_error)


def _safe_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > _max_payload_chars:
        return text[: _max_payload_chars // 2] + "\n...[truncated]...\n" + text[-_max_payload_chars // 2 :]
    return text


def _payload(value: Any) -> Any:
    text = _safe_json(value)
    try:
        return json.loads(text)
    except Exception:
        return text


def _safe_call(obj: Any, method: str, *args, **kwargs) -> None:
    try:
        fn = getattr(obj, method, None)
        if fn:
            fn(*args, **kwargs)
    except Exception:
        pass


def _current_parent():
    stack = getattr(_tls, "stack", [])
    return stack[-1] if stack else None


def _push(span) -> None:
    if not hasattr(_tls, "stack"):
        _tls.stack = []
    _tls.stack.append(span)


def _pop(span=None):
    stack = getattr(_tls, "stack", [])
    if not stack:
        return None
    if span is None or stack[-1] is span:
        return stack.pop()
    try:
        stack.remove(span)
        return span
    except ValueError:
        return None


def _start_span(name: str, span_type: str, *, new_trace: bool = False):
    if not _client:
        return None
    try:
        parent = None if new_trace else _current_parent()
        span = _client.start_span(
            name,
            span_type,
            child_of=parent,
            start_new_trace=new_trace,
        )
        _safe_call(span, "set_service_name", _service_name)
        _safe_call(span, "set_deployment_env", _deployment_env)
        return span
    except Exception:
        return None


if _client:
    import plugins.hooks as hooks

    @hooks.register("agent_before")
    def _on_agent_before(ctx):
        span = _start_span("vibefilming.agent", "custom", new_trace=True)
        if not span:
            return
        if _capture_inputs:
            _safe_call(span, "set_input", _payload({"user_input": ctx.get("user_input", "")}))
        _safe_call(
            span,
            "set_tags",
            {
                "component": "agent",
                "max_turns": getattr(ctx.get("handler"), "max_turns", None),
            },
        )
        _tls.agent_span = span
        _push(span)

    @hooks.register("agent_after")
    def _on_agent_after(ctx):
        span = getattr(_tls, "agent_span", None)
        if not span:
            return
        if _capture_outputs:
            _safe_call(span, "set_output", _payload(ctx.get("exit_reason") or {"result": "MAX_TURNS_EXCEEDED"}))
        _safe_call(span, "finish")
        _pop(span)
        _tls.agent_span = None
        try:
            _client.flush()
        except Exception:
            pass

    @hooks.register("llm_before")
    def _on_llm_before(ctx):
        span = _start_span("llm.chat", "model")
        if not span:
            return
        client = ctx.get("client")
        backend = getattr(client, "backend", None)
        model = getattr(backend, "model", None)
        if model:
            _safe_call(span, "set_model_name", model)
        _safe_call(span, "set_model_provider", getattr(backend, "name", None) or "ark")
        if _capture_inputs:
            _safe_call(span, "set_input", _payload({"turn": ctx.get("turn"), "messages": ctx.get("messages")}))
        _safe_call(span, "set_tags", {"component": "llm", "api_mode": getattr(backend, "api_mode", None)})
        _tls.llm_span = span
        _push(span)

    @hooks.register("llm_after")
    def _on_llm_after(ctx):
        span = getattr(_tls, "llm_span", None)
        if not span:
            return
        response = ctx.get("response")
        if _capture_outputs:
            tool_names = []
            for tc in getattr(response, "tool_calls", []) or []:
                fn = getattr(tc, "function", None)
                tool_names.append(getattr(fn, "name", ""))
            _safe_call(
                span,
                "set_output",
                _payload({
                    "content": getattr(response, "content", ""),
                    "tool_calls": tool_names,
                    "stop_reason": getattr(response, "stop_reason", None),
                }),
            )
        _safe_call(span, "finish")
        _pop(span)
        _tls.llm_span = None

    @hooks.register("tool_before")
    def _on_tool_before(ctx):
        tool_name = ctx.get("tool_name") or "unknown_tool"
        span = _start_span(f"tool.{tool_name}", "tool")
        if not span:
            return
        args = {k: v for k, v in (ctx.get("args") or {}).items() if not str(k).startswith("_")}
        if _capture_inputs:
            _safe_call(span, "set_input", _payload(args))
        _safe_call(
            span,
            "set_tags",
            {
                "component": "tool",
                "tool_name": tool_name,
                "tool_index": ctx.get("index"),
                "tool_num": ctx.get("tool_num"),
            },
        )
        if not hasattr(_tls, "tool_spans"):
            _tls.tool_spans = []
        _tls.tool_spans.append(span)
        _push(span)

    @hooks.register("tool_after")
    def _on_tool_after(ctx):
        stack = getattr(_tls, "tool_spans", [])
        span = stack.pop() if stack else None
        if not span:
            return
        ret = ctx.get("ret")
        if _capture_outputs:
            _safe_call(
                span,
                "set_output",
                _payload({
                    "data": getattr(ret, "data", None),
                    "next_prompt": getattr(ret, "next_prompt", None),
                    "should_exit": getattr(ret, "should_exit", None),
                }),
            )
        _safe_call(span, "finish")
        _pop(span)
