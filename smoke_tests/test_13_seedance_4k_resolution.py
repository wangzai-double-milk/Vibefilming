"""Dry-run test: Seedance 4k resolution is accepted and passed through.

This test does not submit a real Seedance task. It validates:
- gen_video_t2v schema exposes resolution=4k while keeping 720p as default.
- the tool layer passes resolution=4k to the SDK.
- the SDK request body preserves resolution=4k.
"""
import sys

from _common import ROOT, banner, ok, info

sys.path.insert(0, str(ROOT))
from film import film_sdk as sdk
from film import tools


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _find_video_schema():
    for item in tools.build_film_schema():
        fn = item.get("function") or {}
        if fn.get("name") == "gen_video_t2v":
            return fn
    return None


def main():
    banner("Test 13: Seedance 4k resolution 本地 dry-run")

    schema = _find_video_schema()
    _assert(schema is not None, "找不到 gen_video_t2v schema")
    resolution_spec = schema["parameters"]["properties"]["resolution"]
    _assert("4k" in resolution_spec.get("enum", []), "gen_video_t2v resolution enum 缺少 4k")
    _assert(resolution_spec.get("default") == "720p", "gen_video_t2v resolution 默认值不应被改成 4k")
    ok("gen_video_t2v schema 已支持 resolution=4k，默认仍为 720p")

    captured_tool = {}

    def fake_submit_video_task(
        prompt,
        reference_images=None,
        reference_video_url=None,
        duration=None,
        generate_audio=True,
        resolution="720p",
        ratio="16:9",
        seed=None,
    ):
        captured_tool.update(
            {
                "prompt": prompt,
                "duration": duration,
                "generate_audio": generate_audio,
                "resolution": resolution,
                "ratio": ratio,
            }
        )
        return {
            "task_id": "dry_run_seedance_4k",
            "model": "dry-run-model",
            "raw": {"status": "dry_run"},
            "body": {
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                "generate_audio": generate_audio,
            },
        }

    original_submit = tools.sdk.submit_video_task
    original_active_pid = tools._active_pid
    tools.sdk.submit_video_task = fake_submit_video_task
    tools._active_pid = lambda handler: None
    try:
        result = tools._gen_video_t2v(
            object(),
            {
                "prompt": "镜头1：城市天台夜景，固定机位，角色望向远方。无人物说话，无背景音乐，仅保留轻微风声。",
                "name": "dry_run_seedance_4k",
                "duration": 5,
                "ratio": "16:9",
                "resolution": "4k",
            },
        )
        _assert(result["task_id"] == "dry_run_seedance_4k", "dry-run task_id 不正确")
        _assert(captured_tool["resolution"] == "4k", "工具层没有把 resolution=4k 传给 SDK")
        _assert(captured_tool["ratio"] == "16:9", "工具层 ratio 透传异常")
        ok("工具层已把 resolution=4k 原样传给 SDK")

        try:
            tools._gen_video_t2v(
                object(),
                {
                    "prompt": "固定机位空镜，只有环境声。",
                    "name": "dry_run_bad_resolution",
                    "duration": 5,
                    "ratio": "16:9",
                    "resolution": "2160p",
                },
            )
            raise AssertionError("工具层未拦截非法 resolution=2160p")
        except ValueError as e:
            _assert("resolution" in str(e), "工具层非法 resolution 错误信息不符合预期")
            ok("工具层会拒绝未声明的 resolution 值")
    finally:
        tools.sdk.submit_video_task = original_submit
        tools._active_pid = original_active_pid

    captured_sdk = {}

    def fake_http_post(url, body, timeout=180):
        captured_sdk["url"] = url
        captured_sdk["body"] = body
        captured_sdk["timeout"] = timeout
        return {"id": "dry_run_sdk_4k"}

    original_http_post = sdk._http_post
    sdk._http_post = fake_http_post
    try:
        response = sdk.submit_video_task(
            "镜头1：产品特写，缓慢推近，高清质感。",
            duration=5,
            resolution="4k",
            ratio="16:9",
            generate_audio=True,
        )
        _assert(response["task_id"] == "dry_run_sdk_4k", "SDK dry-run task_id 不正确")
        _assert(response["body"]["resolution"] == "4k", "SDK 返回 body 中 resolution 不是 4k")
        _assert(captured_sdk["body"]["resolution"] == "4k", "SDK 请求体没有保留 resolution=4k")
        _assert(captured_sdk["body"]["ratio"] == "16:9", "SDK 请求体 ratio 透传异常")
        ok("SDK 请求体已保留 resolution=4k")

        try:
            sdk.submit_video_task(
                "固定机位空镜。",
                duration=5,
                resolution="2160p",
                ratio="16:9",
            )
            raise AssertionError("SDK 未拦截非法 resolution=2160p")
        except ValueError as e:
            _assert("resolution" in str(e), "SDK 非法 resolution 错误信息不符合预期")
            ok("SDK 会拒绝未声明的 resolution 值")
    finally:
        sdk._http_post = original_http_post

    info("未提交真实 Seedance 任务；仅捕获本地请求体")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
