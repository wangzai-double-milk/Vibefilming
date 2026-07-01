"""Dry-run for project phase state updates."""
from __future__ import annotations

import sys

from _common import ROOT, banner, ok

sys.path.insert(0, str(ROOT))

from film import tools


class FakeHandler:
    _film_active_project = "pid-test"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    banner("Test 18: manifest phase 更新工具")
    captured = {}
    original_update_phase = tools.ws.update_phase
    try:
        def fake_update_phase(pid, phase, **payload):
            captured.update({"pid": pid, "phase": phase, "payload": payload})
            return {"phases": {phase: payload}}

        tools.ws.update_phase = fake_update_phase
        result = tools._project_update_phase(FakeHandler(), {
            "phase": "review",
            "status": "blocked",
            "note": "final review incomplete",
            "artifact": "reviews/final_review_v1.json",
            "shots_done": None,
            "shots_total": None,
        })
    finally:
        tools.ws.update_phase = original_update_phase

    _assert(captured["pid"] == "pid-test", "没有使用当前活跃项目")
    _assert(captured["phase"] == "review", "phase 不正确")
    _assert(captured["payload"]["status"] == "blocked", "status 不正确")
    _assert(result["state"]["artifact"] == "reviews/final_review_v1.json", "artifact 没写入")
    ok("agent 可以把阶段状态、产物和阻塞点写回 manifest.json")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
