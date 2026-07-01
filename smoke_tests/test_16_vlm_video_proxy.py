"""Dry-run for VLM video upload proxy compression."""
from __future__ import annotations

import sys
from pathlib import Path

from _common import ROOT, OUT_DIR, banner, ok

sys.path.insert(0, str(ROOT))

from film import film_sdk as sdk


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    banner("Test 16: VLM 大视频上传前自动生成 review proxy")
    work = OUT_DIR / "test_16_vlm_proxy"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "sample_source.mp4"

    sdk._run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "2",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "96k",
        str(src),
    ], timeout=60)
    _assert(src.exists() and src.stat().st_size > 1, "测试视频没有生成")

    info = sdk.prepare_video_for_vlm(str(src), force=True, files_max_bytes=1, target_bytes=20 * 1024 * 1024)
    proxy = Path(info["path"])
    _assert(info["prepared"], f"没有生成 proxy：{info}")
    _assert(proxy.exists() and proxy.stat().st_size > 0, "proxy 文件不存在或为空")
    _assert(proxy != src, "proxy 不应覆盖原片")
    _assert(info["audio"] == "copy", "proxy 应复制音频，避免依赖音频编码器重编码")

    captured = {}
    original_inline = sdk._VIDEO_INLINE_MAX_BYTES
    original_files_max = sdk._ARK_FILES_MAX_BYTES
    original_via_files = sdk._video_understand_via_files
    try:
        sdk._VIDEO_INLINE_MAX_BYTES = 0
        sdk._ARK_FILES_MAX_BYTES = 1

        def fake_via_files(video, text, fps, system, max_tokens, temperature):
            captured["video"] = video
            return {
                "raw": {"choices": [{"message": {"content": "ok"}}]},
                "body": {"video": video, "text": text},
            }

        sdk._video_understand_via_files = fake_via_files
        data = sdk.doubao_video_understand(str(src), "review this video")
    finally:
        sdk._VIDEO_INLINE_MAX_BYTES = original_inline
        sdk._ARK_FILES_MAX_BYTES = original_files_max
        sdk._video_understand_via_files = original_via_files

    _assert(captured.get("video", "").endswith("_vlm_review.mp4"), f"VLM 没有走 proxy：{captured}")
    _assert(data.get("prepared_video", {}).get("prepared") is True, "返回值没有记录 prepared_video")
    original_upload = sdk.upload_file_ark
    original_wait = sdk.wait_file_active
    original_delete = sdk.delete_file_ark
    original_post = sdk._http_post
    try:
        sdk.upload_file_ark = lambda path, fps=1.0: {"id": "file-test"}
        sdk.wait_file_active = lambda file_id: {"status": "active"}
        sdk.delete_file_ark = lambda file_id: None
        sdk._http_post = lambda url, body, timeout=180: {
            "id": "resp-test",
            "status": "incomplete",
            "incomplete_details": {"reason": "length"},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "partial review"}],
                }
            ],
        }
        partial = sdk._video_understand_via_files(str(src), "review", fps=1.0, system=None, max_tokens=8)
    finally:
        sdk.upload_file_ark = original_upload
        sdk.wait_file_active = original_wait
        sdk.delete_file_ark = original_delete
        sdk._http_post = original_post

    answer = partial["raw"]["choices"][0]["message"]["content"]
    _assert("未完成" in answer and "partial review" in answer, "VLM length incomplete 没有保留半截审查")
    _assert(partial["raw"].get("_incomplete", {}).get("reason") == "length", "incomplete 元数据缺失")
    ok("本地视频超过 VLM 上传上限时，会先生成压缩代理文件再上传")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
