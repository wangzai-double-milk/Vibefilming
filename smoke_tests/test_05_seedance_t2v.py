"""Test 5: Seedance 文生视频（异步任务）。

策略：只验证「提交任务 → 拿到 task_id → 查询一次状态」，不阻塞等出片，
出片成本高且耗时长，本 smoke 仅证明链路通。
"""
import json
import time
import urllib.request
from _common import banner, ok, fail, info, get_ark_key, get_ark_base, get_model


def submit(model: str, key: str):
    url = f"{get_ark_base()}/contents/generations/tasks"
    body = {
        "model": model,
        "content": [
            {"type": "text", "text": "一只橘猫慢慢走过窗台 --resolution 480p --duration 5 --ratio 16:9"}
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def query(task_id: str, key: str):
    url = f"{get_ark_base()}/contents/generations/tasks/{task_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    banner("Test 5: Seedance 文生视频（仅验证任务提交 + 一次状态查询）")
    key = get_ark_key()
    if not key:
        fail("缺少 ark.api_key")
        return False

    model = get_model("video", "doubao-seedance-2-0-260128")
    info(f"model = {model}")
    try:
        sub = submit(model, key)
        task_id = sub.get("id") or sub.get("task_id")
        if not task_id:
            fail(f"返回里没有 task_id：{sub}")
            return False
        ok(f"任务已提交，task_id = {task_id}")
        time.sleep(2)
        st = query(task_id, key)
        ok(f"状态查询成功：status = {st.get('status')}")
        info("（出片需 1-3 分钟，本 smoke 不等待落地）")
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
