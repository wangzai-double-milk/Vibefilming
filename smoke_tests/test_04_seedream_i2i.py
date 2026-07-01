"""Test 4: Seedream 图生图（依赖 Test 3 的产出图）。"""
import json
import urllib.request
from _common import banner, ok, fail, info, get_ark_key, get_ark_base, get_model, OUT_DIR


def main():
    banner("Test 4: Seedream 图生图")
    key = get_ark_key()
    if not key:
        fail("缺少 ark.api_key")
        return False

    # 优先使用 Test 3 缓存的 Seedream URL（在火山 TOS 上，最稳）
    ref_url = None
    cache = OUT_DIR / "last_seedream_url.txt"
    if cache.exists():
        u = cache.read_text(encoding="utf-8").strip()
        if u:
            ref_url = u
    if not ref_url:
        info("未找到 Test 3 出的图，先跑 test_03_seedream_t2i.py 再来。")
        fail("无可用参考图")
        return False
    info(f"参考图：{ref_url[:80]}...")

    url = f"{get_ark_base()}/images/generations"
    model = get_model("image", "doubao-seedream-4-5-251128")
    info(f"model = {model}")
    body = {
        "model": model,
        "prompt": "参考输入图片中的橘猫主体，保持主体一致，把背景改成蓝天白云",
        "image": ref_url,
        "size": "2048x2048",
        "response_format": "url",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data["data"][0].get("url")
        ok(f"图生图成功：{out}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        fail(f"HTTP {e.code}: {body[:300]}")
    except Exception as e:
        fail(str(e))
    return False


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
