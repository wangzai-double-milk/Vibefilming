"""Project execution modes and their human-approval gates."""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_JSON = Path(__file__).resolve().parent.parent / "vibefilming.config.json"

AUTONOMOUS = "autonomous"
HUMAN_GATED = "human_gated"
VALID_MODES = (AUTONOMOUS, HUMAN_GATED)
MODE_LABELS = {
    AUTONOMOUS: "端到端无人干预",
    HUMAN_GATED: "关键步骤人工介入",
}
HUMAN_GATES = ("breakdown_approval", "script_approval")
ALIASES = {
    "auto": AUTONOMOUS,
    "automatic": AUTONOMOUS,
    "autonomous": AUTONOMOUS,
    "无人干预": AUTONOMOUS,
    "全自动": AUTONOMOUS,
    "human": HUMAN_GATED,
    "manual": HUMAN_GATED,
    "human_gated": HUMAN_GATED,
    "人工介入": HUMAN_GATED,
    "人工确认": HUMAN_GATED,
}


def normalize_mode(value: object) -> str:
    mode = ALIASES.get(str(value or "").strip().lower())
    if mode not in VALID_MODES:
        raise ValueError(
            f"execution_mode 只支持 {AUTONOMOUS} 或 {HUMAN_GATED}，当前为 {value!r}")
    return mode


def default_mode(config_path: Path | None = None) -> str:
    """Environment > JSON > human-gated compatibility default."""
    env = os.environ.get("VIBEFILMING_EXECUTION_MODE")
    if env:
        return normalize_mode(env)
    path = config_path or CONFIG_JSON
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        workflow = config.get("workflow", {})
        if workflow is not None and not isinstance(workflow, dict):
            raise ValueError("workflow must be a JSON object")
        configured = (workflow or {}).get("default_execution_mode")
        if configured:
            return normalize_mode(configured)
    return HUMAN_GATED


def state(mode: object | None = None, *, source: str = "project") -> dict:
    selected = default_mode() if mode is None else normalize_mode(mode)
    return {
        "mode": selected,
        "label": MODE_LABELS[selected],
        "human_gates": list(HUMAN_GATES if selected == HUMAN_GATED else ()),
        "source": source,
    }


def requires_human_approval(mode_or_state: object, gate: str) -> bool:
    if isinstance(mode_or_state, dict):
        mode = mode_or_state.get("mode", HUMAN_GATED)
    else:
        mode = mode_or_state
    return normalize_mode(mode) == HUMAN_GATED and gate in HUMAN_GATES
