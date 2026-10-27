"""Versioned cross-stage artifact contracts.

Skills own domain judgment. Runtime producers and consumers communicate only
through these neutral contract identifiers, never through another Skill's
installation path or private implementation.
"""
from __future__ import annotations

from pathlib import Path


REFERENCE_BREAKDOWN = "vibefilming.reference-breakdown/v1"
DIRECTOR_PLAN = "vibefilming.director-plan/v1"
ACTION_STORYBOARD = "vibefilming.action-storyboard/v1"
TIMELINE = "vibefilming.timeline/v1"
SEAM_EVIDENCE = "vibefilming.seam-evidence/v1"
DELIVERY_MANIFEST = "vibefilming.delivery-manifest/v1"
GENERATION_UNIT_STATUS = "vibefilming.generation-unit-status/v1"
FULL_TIMELINE = "vibefilming.full-timeline/v1"
GENERATION_UNITS = "vibefilming.generation-units/v1"
REVIEW_FINAL = "vibefilming.review-final/v1"


def expected_contract(path: str | Path) -> str:
    candidate = Path(path)
    name = candidate.name
    if name == "reference_breakdown.json":
        return REFERENCE_BREAKDOWN
    if name == "director_plan.json":
        return DIRECTOR_PLAN
    if name == "delivery_manifest.json":
        return DELIVERY_MANIFEST
    if name == "full_timeline.json":
        return FULL_TIMELINE
    if name == "generation_units.json":
        return GENERATION_UNITS
    if name == "review_final.json":
        return REVIEW_FINAL
    if (
        name.startswith("storyboard_")
        and name.endswith(".json")
    ):
        return ACTION_STORYBOARD
    if name.endswith(".timeline.json"):
        return TIMELINE
    return ""


def validate(path: str | Path, value: object) -> None:
    expected = expected_contract(path)
    if not expected:
        return
    if not isinstance(value, dict):
        raise ValueError(
            f"{Path(path).name} 必须是 JSON object，contract={expected}")
    actual = str(value.get("contract") or "")
    if actual != expected:
        raise ValueError(
            f"{Path(path).name} contract 必须是 {expected}，实际为 "
            f"{actual or 'missing'}")
