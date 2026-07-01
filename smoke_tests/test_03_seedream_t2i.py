"""Test 3: Seedream 文生图。

走 Ark 的 /images/generations 接口；模型名来自项目配置。
"""
import json
import urllib.request
from _common import banner, ok, fail, info, get_ark_key, get_ark_base, get_model, save_bytes


def try_model(model: str, key: str):
    url = f"{get_ark_base()}/images/generations"
    body = {
        "model": model,
        "prompt": "一只橘猫坐在窗台上，阳光，写实风格",
        "size": "2048x2048",
        "response_format": "url",
        "n": 1,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    banner("Test 3: Seedream 文生图")
    key = get_ark_key()
    if not key:
        fail("缺少 ark.api_key")
        return False

    model = get_model("image", "doubao-seedream-4-5-251128")
    info(f"model = {model}")
    try:
        data = try_model(model, key)
        url = data["data"][0].get("url") or data["data"][0].get("b64_json", "")[:30] + "..."
        ok(f"出图成功：{url}")
        # 把 URL 落到本地，给后续图生图测试用
        try:
            from _common import OUT_DIR
            (OUT_DIR / "last_seedream_url.txt").write_text(data["data"][0].get("url", ""), encoding="utf-8")
        except Exception:
            pass
        # 顺便下载保存
        try:
            if data["data"][0].get("url"):
                img = urllib.request.urlopen(data["data"][0]["url"], timeout=60).read()
                p = save_bytes(f"seedream_{model}.png", img)
                info(f"已保存 → {p}")
        except Exception as e:
            info(f"图片下载失败（不影响测试）：{e}")
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
