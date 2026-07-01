"""共享配置与工具函数。

凭证统一读取仓库根目录下的 vibefilming.config.json。
"""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON = ROOT / "vibefilming.config.json"
OUT_DIR = Path(__file__).resolve().parent / "outputs"
OUT_DIR.mkdir(exist_ok=True)


def _load_config() -> dict:
    if not CONFIG_JSON.exists():
        return {}
    return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))


_config = _load_config()


def _config_get(*path, default=None):
    cur = _config
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def get_ark_key() -> str:
    return (
        _config_get("ark", "api_key", default="")
        or ""
    )


def get_ark_base() -> str:
    return (
        _config_get("ark", "api_base", default="")
        or "https://ark.cn-beijing.volces.com/api/v3"
    )


def get_model(name: str, default: str) -> str:
    return (
        _config_get("ark", "models", name, default="")
        or default
    )


def get_extra(name: str, default=None):
    """读取额外凭证：统一来自 vibefilming.config.json。"""
    mapping = {
        "VOLC_AK": ("volc", "ak"),
        "VOLC_SK": ("volc", "sk"),
        "VOD_AK": ("volc", "ak"),
        "VOD_SK": ("volc", "sk"),
    }
    if name in mapping:
        return _config_get(*mapping[name], default=default)
    return default


def banner(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def ok(msg: str):
    print(f"  ✅ {msg}")


def fail(msg: str):
    print(f"  ❌ {msg}")


def info(msg: str):
    print(f"  ℹ️  {msg}")


def save_json(name: str, data):
    p = OUT_DIR / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def save_bytes(name: str, data: bytes):
    p = OUT_DIR / name
    p.write_bytes(data)
    return p
