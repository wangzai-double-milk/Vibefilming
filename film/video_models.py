"""Seedance model discovery, project selection, and capability profiles."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON = ROOT / "vibefilming.config.json"

SEEDANCE_2_0 = "doubao-seedance-2-0-260128"
SEEDANCE_2_5 = "doubao-seedance-2-5-260628"
MODEL_KEYS = ("2.0", "2.5")


@dataclass(frozen=True)
class VideoModelProfile:
    key: str
    label: str
    model_id: str
    max_images: int
    max_videos: int
    max_audios: int
    max_reference_duration: int
    max_output_duration: int
    audio_only: bool
    resolutions: tuple[str, ...]
    output_formats: tuple[str, ...]
    task_types: tuple[str, ...]

    @property
    def min_output_duration(self) -> int:
        return 4

    @property
    def ratios(self) -> tuple[str, ...]:
        return ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.key,
            "label": self.label,
            "model_id": self.model_id,
            "capabilities": {
                "max_images": self.max_images,
                "max_videos": self.max_videos,
                "max_audios": self.max_audios,
                "max_reference_duration": self.max_reference_duration,
                "min_output_duration": self.min_output_duration,
                "max_output_duration": self.max_output_duration,
                "audio_only": self.audio_only,
                "resolutions": list(self.resolutions),
                "ratios": list(self.ratios),
                "output_formats": list(self.output_formats),
                "task_types": list(self.task_types),
            },
        }


PROFILES = {
    "2.0": VideoModelProfile(
        key="2.0",
        label="Seedance 2.0",
        model_id=SEEDANCE_2_0,
        max_images=9,
        max_videos=3,
        max_audios=3,
        max_reference_duration=15,
        max_output_duration=15,
        audio_only=False,
        resolutions=("480p", "720p", "1080p", "4k"),
        output_formats=("mp4",),
        task_types=(),
    ),
    "2.5": VideoModelProfile(
        key="2.5",
        label="Seedance 2.5",
        model_id=SEEDANCE_2_5,
        max_images=30,
        max_videos=10,
        max_audios=10,
        max_reference_duration=30,
        max_output_duration=30,
        audio_only=True,
        resolutions=("480p", "720p", "1080p"),
        output_formats=("mp4", "mov"),
        task_types=("auto", "reference", "edit", "extend"),
    ),
}


def _load_config() -> dict:
    try:
        value = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _env_first(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _configured_models() -> tuple[dict[str, str], str]:
    config_models = ((_load_config().get("ark") or {}).get("models") or {})
    explicit = {
        "2.0": (
            _env_first("ARK_MODEL_VIDEO_2_0", "VIBEFILMING_ARK_MODEL_VIDEO_2_0")
            or str(config_models.get("video_2_0") or "").strip()
        ),
        "2.5": (
            _env_first("ARK_MODEL_VIDEO_2_5", "VIBEFILMING_ARK_MODEL_VIDEO_2_5")
            or str(config_models.get("video_2_5") or "").strip()
        ),
    }
    legacy = (
        _env_first("ARK_MODEL_VIDEO", "VIBEFILMING_ARK_MODEL_VIDEO")
        or str(config_models.get("video") or "").strip()
    )
    return {key: value for key, value in explicit.items() if value}, legacy


def model_key(value: str | None) -> str | None:
    candidate = str(value or "").strip().lower().replace("_", "-")
    if not candidate:
        return None
    aliases = {
        "2": "2.0",
        "2.0": "2.0",
        "seedance-2.0": "2.0",
        "seedance-2-0": "2.0",
        "2.5": "2.5",
        "seedance-2.5": "2.5",
        "seedance-2-5": "2.5",
    }
    if candidate in aliases:
        return aliases[candidate]
    if "seedance-2-5" in candidate or "seedance-2.5" in candidate:
        return "2.5"
    if "seedance-2-0" in candidate or "seedance-2.0" in candidate:
        return "2.0"
    return None


def available_profiles() -> list[VideoModelProfile]:
    explicit, legacy = _configured_models()
    legacy_key = model_key(legacy)
    if legacy:
        # An unknown legacy endpoint remains a 2.0 endpoint for compatibility.
        key = legacy_key or "2.0"
        explicit.setdefault(key, legacy)
    if not explicit:
        explicit["2.0"] = SEEDANCE_2_0
    return [
        replace(PROFILES[key], model_id=explicit[key])
        for key in MODEL_KEYS
        if key in explicit
    ]


def resolve(selection: str | None = None, *, strict: bool = False) -> VideoModelProfile:
    profiles = available_profiles()
    requested = str(selection or "").strip()
    if requested:
        requested_key = model_key(requested)
        for profile in profiles:
            if requested == profile.model_id or requested_key == profile.key:
                return profile
        if strict and len(profiles) > 1:
            choices = ", ".join(profile.label for profile in profiles)
            raise ValueError(f"视频模型 {requested!r} 未配置，可选：{choices}")
    if len(profiles) == 1:
        return profiles[0]

    _, legacy = _configured_models()
    legacy_key = model_key(legacy)
    for profile in profiles:
        if legacy and (legacy == profile.model_id or legacy_key == profile.key):
            return profile

    preferred = model_key(_env_first("VIBEFILMING_VIDEO_MODEL"))
    for profile in profiles:
        if preferred == profile.key:
            return profile
    return next(
        (profile for profile in profiles if profile.key == "2.5"),
        profiles[0],
    )


def resolve_manifest(manifest: dict | None) -> VideoModelProfile:
    return resolve((manifest or {}).get("video_model"))


def state(manifest: dict | None = None) -> dict[str, Any]:
    selected = resolve_manifest(manifest)
    options = [profile.public() for profile in available_profiles()]
    return {
        "selected": selected.model_id,
        "key": selected.key,
        "version": selected.key,
        "label": selected.label,
        "selectable": len(options) > 1,
        "options": options,
        "capabilities": selected.public()["capabilities"],
    }


def prompt_summary(profile: VideoModelProfile) -> str:
    audio_rule = "允许仅音频参考" if profile.audio_only else "音频不能单独输入"
    task_rule = (
        f"；任务类型 {','.join(profile.task_types)}"
        if profile.task_types else ""
    )
    return (
        f"{profile.label} ({profile.model_id})：最多 "
        f"{profile.max_images} 图/{profile.max_videos} 视频/"
        f"{profile.max_audios} 音频，参考视频和音频各自总时长不超过 "
        f"{profile.max_reference_duration}s，输出 4-{profile.max_output_duration}s"
        f"（或 -1 自适应），{audio_rule}，分辨率 "
        f"{'/'.join(profile.resolutions)}，格式 {'/'.join(profile.output_formats)}"
        f"{task_rule}。"
    )
