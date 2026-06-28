"""VibeFilming SDK：所有外部 API + 本地 ffmpeg 封装。

设计原则：
  - 每个函数职责单一，输入输出清晰
  - 失败抛具体 RuntimeError（包含 error_code / status）
  - 长任务（Seedance）拆成 submit + query 两个函数，agent 自己轮询
  - 所有产物落到指定路径，返回 path（agent 可后续读取）
  - VLM / GenBGM 缺凭证时返回 stub 错误，不静默失败
"""
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

# ============== 配置读取 ==============
ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON = ROOT / "vibefilming.config.json"


def _load_json_config() -> dict:
    if not CONFIG_JSON.exists():
        return {}
    try:
        return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


_config = _load_json_config()


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


def get_extra(name: str, default=None):
    config_key = {"VOLC_AK": "ak", "VOLC_SK": "sk"}.get(name)
    if config_key:
        value = _config_get("volc", config_key, default=None)
        if value:
            return value
    return default


def get_model(name: str, default: str) -> str:
    return (
        _config_get("ark", "models", name, default="")
        or default
    )


# ============== 模型 ID（可统一改） ==============
MODEL_TEXT = get_model("text", "deepseek-v4-pro-260425")
MODEL_VLM = get_model("vlm", "doubao-seed-2-0-pro-260215")
MODEL_IMG = get_model("image", "doubao-seedream-4-5-251128")
MODEL_VIDEO = get_model("video", "doubao-seedance-2-0-260128")  # 标准版（已弃用 fast 档）


# ============== HTTP helper ==============
def _http_post(url: str, body: dict, timeout: int = 180) -> dict:
    key = get_ark_key()
    if not key:
        raise RuntimeError("ARK API key 未配置：请在 vibefilming.config.json 中填写 ark.api_key")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body_text[:300]}")


def _http_get(url: str, timeout: int = 60) -> dict:
    key = get_ark_key()
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _http_delete(url: str, timeout: int = 60) -> dict:
    key = get_ark_key()
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return {"ok": True, "status": r.status, "body": body}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": e.read().decode("utf-8", "ignore")}


def _http_post_multipart(url: str, fields: dict, file_field: str,
                         file_path: Path, timeout: int = 600) -> dict:
    """以 multipart/form-data 上传文件（ark Files API 用，纯标准库实现，不依赖 requests）。
    fields: 普通文本字段；file_field: 文件字段名；file_path: 本地文件。
    """
    key = get_ark_key()
    if not key:
        raise RuntimeError("ARK API key 未配置：请在 vibefilming.config.json 中填写 ark.api_key")
    boundary = f"----vibefilming{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []
    for k, v in fields.items():
        parts.append(b"--" + boundary.encode() + crlf)
        parts.append(f'Content-Disposition: form-data; name="{k}"'.encode() + crlf + crlf)
        parts.append(str(v).encode() + crlf)
    fp = Path(file_path)
    suffix = fp.suffix.lower().lstrip(".") or "mp4"
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{fp.name}"'.encode()
        + crlf
    )
    parts.append(f"Content-Type: video/{suffix}".encode() + crlf + crlf)
    parts.append(fp.read_bytes() + crlf)
    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    data = b"".join(parts)
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body_text[:300]}")


def _download(url: str, save_path: Path, timeout: int = 180) -> Path:
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(urllib.request.urlopen(url, timeout=timeout).read())
    return save_path


def _image_mime_from_bytes(data: bytes, fallback_suffix: str) -> str:
    """根据文件头判断真实图片 MIME，避免 .png 文件里实际是 JPEG 时被接口拒绝。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    suffix = (fallback_suffix or "png").lower().lstrip(".")
    if suffix == "jpg":
        suffix = "jpeg"
    return f"image/{suffix}"


def _img_to_url(ref: str) -> str:
    """把图片参考归一成模型能直接吃的形式。
    - http(s)/data: 原样返回；
    - 本地存在的文件 → base64 内嵌成 data URI。
      这样 agent 只需转抄很短的本地 path，不必转抄那条很长的带签名 url；
      实测 LLM 转抄长 url 时常把 `?X-Tos-Signature=...` query 段截掉，
      丢给 Seedance 一个无签名裸 url → 服务端报 resource download failed。
      base64 内嵌从根上绕开"转抄长 url"这一步。
    - 其余（找不到的字符串）原样返回，交给上游报错。
    """
    s = str(ref).strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    p = Path(s)
    if p.exists():
        data = p.read_bytes()
        mime = _image_mime_from_bytes(data, p.suffix)
        b64 = base64.b64encode(data).decode()
        return f"{mime};base64,{b64}".replace("image/", "data:image/", 1)
    return s


# ============== Doubao 文本 / VLM ==============
def doubao_chat(messages: list, max_tokens: int = 4096, temperature: float = 0.7,
                model: str = None) -> dict:
    """Doubao 文本对话。messages 遵循 OpenAI 格式。返回完整 API JSON。"""
    body = {
        "model": model or MODEL_TEXT,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    return _http_post(f"{get_ark_base()}/chat/completions", body)


def doubao_vlm(image_paths: list, text: str, max_tokens: int = 4096,
               temperature: float = 0.1, system: Optional[str] = None) -> dict:
    """Doubao VLM。image_paths 是本地图片路径或 URL，会归一成模型可读 image_url。
    system: 可选系统提示（设定 VLM 的角色/输出格式）。"""
    image_msgs = []
    for p in image_paths:
        image_msgs.append({
            "type": "image_url",
            "image_url": {"url": _img_to_url(str(p))},
        })
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": image_msgs + [{"type": "text", "text": text}],
    })
    
    body = {
        "model": MODEL_VLM,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    
    # Seed 2.1 Pro 图像理解在复杂审查题上可能超过默认 180s；
    # 对齐视频理解路径，给 VLM 审图保留更长推理时间。
    raw = _http_post(f"{get_ark_base()}/chat/completions", body, timeout=600)
    try:
        choice0 = raw["choices"][0]
        msg = choice0.get("message", {}) or {}
        content = msg.get("content")
        finish = choice0.get("finish_reason", "")
        if content is None or not isinstance(content, str) or not content.strip():
            raise RuntimeError(
                f"chat/completions VLM(图片) 返回空/非文本 content，finish_reason={finish}, "
                f"msg_keys={list(msg.keys())}, raw_keys={list(raw.keys())}"
            )
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"chat/completions VLM(图片) 返回结构异常：{json.dumps(raw, ensure_ascii=False)[:500]}")
    return {"raw": raw, "body": body}


def upload_file_ark(file_path: Path, fps: float = 1.0, timeout: int = 1800) -> dict:
    """上传本地文件到 ark Files API，返回文件元信息（含 id）。
    用于 >50MB 大文件：base64 内嵌进 chat/completions 会被网关拒收（Broken pipe / 413），
    官方要求改走 Files API + Responses API。
    fps: 视频抽帧速率（预处理用），长视频画面变化小可调低省时。
    """
    r = _http_post_multipart(
        f"{get_ark_base()}/files",
        fields={"purpose": "user_data", "preprocess_configs[video][fps]": fps},
        file_field="file",
        file_path=Path(file_path),
        timeout=timeout,
    )
    return r


def wait_file_active(file_id: str, timeout: int = 1200, interval: int = 3) -> dict:
    """轮询文件预处理状态，直到 active 才能在 Responses API 中使用。
    抛 TimeoutError / RuntimeError（失败状态）。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _http_get(f"{get_ark_base()}/files/{file_id}")
        status = last.get("status")
        if status == "active":
            return last
        if status in ("failed", "error", "expired"):
            raise RuntimeError(f"文件 {file_id} 预处理失败：status={status}, detail={last}")
        time.sleep(interval)
    raise TimeoutError(f"文件 {file_id} 预处理超时 {timeout}s，last={last}")


def delete_file_ark(file_id: str) -> dict:
    """删除 ark 上传的文件（用完清理，避免占满 20GB 配额）。"""
    return _http_delete(f"{get_ark_base()}/files/{file_id}")


def _responses_collect_text(raw: dict) -> str:
    # 优先 SDK 风格的 output_text 便捷字段
    if isinstance(raw.get("output_text"), str) and raw["output_text"].strip():
        return raw["output_text"].strip()
    texts = []
    for item in raw.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                texts.append(c["text"])
    return "\n".join(texts).strip()


def _responses_extract_text(raw: dict) -> str:
    """从 Responses API 返回里抽出纯文本回答。
    Responses 的输出结构：output 是 item 列表，message item 的 content 里
    type=output_text 的项带 text。做防御式解析，兼容多种字段命名。

    检查 Responses API 非成功状态并抛出有意义的错误，避免静默返回空字符串。
    """
    # 检查顶层状态：completed / failed / incomplete 等
    status = raw.get("status")
    if status and status not in ("completed",):
        inc = raw.get("incomplete_details") or {}
        err = raw.get("error") or {}
        reason = inc.get("reason") or err.get("message") or str(inc) or str(err) or status
        raise RuntimeError(
            f"Responses API 返回非完成状态 status={status}, reason={reason}, "
            f"raw_id={raw.get('id', '?')}"
        )

    # 收集 refusal 信息（内容安全/审核拦截）
    refusals = []
    for item in raw.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") in ("refusal", "content_filter"):
                refusals.append(c.get("refusal") or c.get("text") or c.get("reason", str(c)))
    if refusals:
        raise RuntimeError(f"Responses API 内容被拦截/拒绝：{'; '.join(refusals[:3])}")

    answer = _responses_collect_text(raw)
    if not answer:
        # 解析完毕但没有拿到任何文本，说明返回结构有意外，抛出原始信息供排查
        raise RuntimeError(
            "Responses API 返回中未提取到任何文本回答，可能是响应格式变更或模型未输出。"
            f" top_keys={list(raw.keys())}, output_sample={json.dumps(raw.get('output', [])[:2], ensure_ascii=False)[:500]}"
        )
    return answer


def _video_understand_via_files(video: str, text: str, fps: float,
                                system: Optional[str], max_tokens: int,
                                temperature: float = 0.1) -> dict:
    """大文件路径：Files API 上传 → 等 active → Responses API 引用 file_id。
    返回归一成 chat/completions 形状的 {raw, body}，让上层调用方无需区分大小路径。
    用完删除上传文件。
    """
    file_id = None
    try:
        meta = upload_file_ark(Path(video), fps=fps)
        file_id = meta.get("id")
        if not file_id:
            raise RuntimeError(f"Files API 上传未返回 id：{meta}")
        wait_file_active(file_id)
        user_content = [
            {"type": "input_video", "file_id": file_id},
            {"type": "input_text", "text": text},
        ]
        input_msgs = []
        if system:
            input_msgs.append({"role": "system",
                               "content": [{"type": "input_text", "text": system}]})
        input_msgs.append({"role": "user", "content": user_content})
        body = {
            "model": MODEL_VLM,
            "input": input_msgs,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        raw = _http_post(f"{get_ark_base()}/responses", body, timeout=1800)
        incomplete = None
        try:
            answer = _responses_extract_text(raw)
        except RuntimeError as e:
            inc = raw.get("incomplete_details") or {}
            reason = inc.get("reason", "")
            partial = _responses_collect_text(raw)
            if raw.get("status") == "incomplete" and reason == "length" and partial:
                incomplete = {
                    "status": raw.get("status"),
                    "reason": reason,
                    "raw_id": raw.get("id"),
                    "message": str(e),
                }
                answer = "【未完成，不可作为通过结论：VLM 输出达到长度上限，请拆分审查问题后继续。】\n" + partial
            else:
                raise
        # 归一成 chat/completions 形状，保持调用方 raw["choices"][0]["message"]["content"] 不变
        normalized = {
            "choices": [{"message": {"content": answer}}],
            "_responses_raw": raw,
            "_via": "files_api",
        }
        if incomplete:
            normalized["_incomplete"] = incomplete
        return {"raw": normalized, "body": body}
    finally:
        if file_id:
            try:
                delete_file_ark(file_id)
            except Exception:
                pass  # 清理失败不影响主流程，文件会按 expire_at 自动过期


# base64 后会膨胀 4/3，chat/completions 网关上限约 50MB，
# 原文件 > 35MB 时 base64 后逼近上限，直接走 Files API 更稳。
_VIDEO_INLINE_MAX_BYTES = 35 * 1024 * 1024
_ARK_FILES_MAX_BYTES = 512 * 1024 * 1024
_VLM_PROXY_TARGET_BYTES = 500 * 1024 * 1024


def _mb(n: int) -> float:
    return n / (1024 * 1024)


def prepare_video_for_vlm(video: str, *, force: bool = False,
                          files_max_bytes: int = _ARK_FILES_MAX_BYTES,
                          target_bytes: int = _VLM_PROXY_TARGET_BYTES,
                          preset: str = "ultrafast") -> dict:
    """给 VLM 上传准备本地视频代理文件。

    Ark Files API 对单文件有 512MB 上限。4K 成片很容易贴近或超过该上限，
    这里做的是传输/审核代理压缩，不重新生成创作内容。音频默认 copy，避免依赖
    shell PATH 里的 ffmpeg 音频编码器；视频用项目内 imageio-ffmpeg 统一转码。
    """
    if video.startswith(("http://", "https://")):
        return {"path": video, "prepared": False, "reason": "remote_url"}
    src = Path(video)
    if not src.exists():
        return {"path": video, "prepared": False, "reason": "missing_local_file"}

    source_size = src.stat().st_size
    if not force and source_size <= files_max_bytes:
        return {
            "path": str(src),
            "prepared": False,
            "reason": "within_files_api_limit",
            "source_size_bytes": source_size,
            "source_size_mb": round(_mb(source_size), 2),
            "files_api_limit_mb": round(_mb(files_max_bytes), 2),
        }

    out = src.with_name(f"{src.stem}_vlm_review.mp4")
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime and out.stat().st_size <= target_bytes:
        proxy_size = out.stat().st_size
        return {
            "path": str(out),
            "prepared": True,
            "cached": True,
            "source_path": str(src),
            "source_size_bytes": source_size,
            "source_size_mb": round(_mb(source_size), 2),
            "proxy_size_bytes": proxy_size,
            "proxy_size_mb": round(_mb(proxy_size), 2),
            "files_api_limit_mb": round(_mb(files_max_bytes), 2),
            "ffmpeg": FFMPEG,
        }

    duration = None
    try:
        duration = probe_duration(str(src))
    except Exception:
        pass
    timeout = max(600, int((duration or 120) * 20))
    attempts = [
        {"height": 1080, "crf": 30},
        {"height": 720, "crf": 28},
        {"height": 540, "crf": 30},
    ]
    last_error = None
    for attempt in attempts:
        try:
            _run_ffmpeg([
                "-i", str(src),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", f"scale=-2:{attempt['height']},setsar=1",
                "-c:v", "libx264", "-preset", preset, "-crf", str(attempt["crf"]),
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(out),
            ], timeout=timeout)
            proxy_size = out.stat().st_size
            if proxy_size <= target_bytes:
                return {
                    "path": str(out),
                    "prepared": True,
                    "cached": False,
                    "source_path": str(src),
                    "source_size_bytes": source_size,
                    "source_size_mb": round(_mb(source_size), 2),
                    "proxy_size_bytes": proxy_size,
                    "proxy_size_mb": round(_mb(proxy_size), 2),
                    "files_api_limit_mb": round(_mb(files_max_bytes), 2),
                    "target_size_mb": round(_mb(target_bytes), 2),
                    "height": attempt["height"],
                    "crf": attempt["crf"],
                    "audio": "copy",
                    "ffmpeg": FFMPEG,
                }
            last_error = RuntimeError(
                f"VLM proxy 仍过大：{_mb(proxy_size):.1f}MB > {_mb(target_bytes):.1f}MB"
            )
        except Exception as e:
            last_error = e
    raise RuntimeError(
        f"prepare_video_for_vlm 压缩失败，source={src}, size={_mb(source_size):.1f}MB, "
        f"ffmpeg={FFMPEG}, last_error={last_error}"
    )


def doubao_video_understand(video: str, text: str, max_tokens: int = 4096,
                            temperature: float = 0.1, fps: float = 1.0,
                            system: Optional[str] = None) -> dict:
    """Doubao Seed 2.0 pro 直接理解视频（不抽帧）。
    video 可以是本地路径或 https URL。
    本地文件 ≤35MB：base64 内嵌走 chat/completions（轻量）。
    本地文件 >35MB：自动走 Files API + Responses API（官方大文件方案，上限 512MB），
        否则 base64 内嵌会被网关拒收（Broken pipe / 413）。
    system: 可选系统提示（设定 VLM 的角色/输出格式）。
    """
    prepared_video = None
    if not video.startswith(("http://", "https://")):
        p = Path(video)
        if p.exists() and p.stat().st_size > _ARK_FILES_MAX_BYTES:
            prepared_video = prepare_video_for_vlm(
                video,
                files_max_bytes=_ARK_FILES_MAX_BYTES,
                target_bytes=_VLM_PROXY_TARGET_BYTES,
            )
            video = prepared_video["path"]
            p = Path(video)
        if p.exists() and p.stat().st_size > _VIDEO_INLINE_MAX_BYTES:
            data = _video_understand_via_files(video, text, fps, system, max_tokens, temperature)
            if prepared_video:
                data["prepared_video"] = prepared_video
                data["video_uploaded"] = video
            return data

    if video.startswith(("http://", "https://")):
        video_url = video
    else:
        b64 = base64.b64encode(Path(video).read_bytes()).decode()
        suffix = Path(video).suffix.lower().lstrip(".") or "mp4"
        video_url = f"data:video/{suffix};base64,{b64}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": [
            {"type": "video_url", "video_url": {"url": video_url, "fps": fps}},
            {"type": "text", "text": text},
        ],
    })
    
    body = {
        "model": MODEL_VLM,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    
    # 视频原生理解（推理模型）单次推理常 >180s，长片/4K 整片更久，给足 1800s 超时。
    raw = _http_post(f"{get_ark_base()}/chat/completions", body, timeout=1800)
    # chat/completions 路径同样做防御：检查 finish_reason 与 content
    try:
        choice0 = raw["choices"][0]
        msg = choice0.get("message", {}) or {}
        content = msg.get("content")
        finish = choice0.get("finish_reason", "")
        if content is None or not isinstance(content, str) or not content.strip():
            raise RuntimeError(
                f"chat/completions VLM 返回空/非文本 content，finish_reason={finish}, "
                f"msg_keys={list(msg.keys())}, raw_keys={list(raw.keys())}"
            )
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"chat/completions VLM 返回结构异常：{json.dumps(raw, ensure_ascii=False)[:500]}")
    data = {"raw": raw, "body": body}
    if prepared_video:
        data["prepared_video"] = prepared_video
        data["video_uploaded"] = video
    return data


# ============== Seedream（文生图 / 图编辑统一接口）==============
def gen_image(prompt: str, save_path: Path,
              reference_images=None,
              size: str = "2048x2048",
              watermark: bool = True) -> dict:
    """Seedream 4.0 — 文生图 / 图编辑 / 多图融合。返回 {url, path, raw}。

    reference_images: None=文生图；str=单图编辑；list[str]=多图融合（最多 14 张）。
        可传本地 path（自动 base64 内嵌）或 http(s) url；优先传本地 path，省去转抄长签名 url。
    watermark: 是否加「AI 生成」水印/标识（官方默认 true，本 SDK 默认 true）。
    """
    body = {
        "model": MODEL_IMG,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": watermark,
        "n": 1,
    }
    if reference_images:
        if isinstance(reference_images, (list, tuple)):
            body["image"] = [_img_to_url(x) for x in reference_images]
        else:
            body["image"] = _img_to_url(reference_images)
    raw = _http_post(f"{get_ark_base()}/images/generations", body)
    url = raw["data"][0]["url"]
    p = _download(url, Path(save_path))
    return {"url": url, "path": str(p), "raw": raw, "body": body}


# ============== Seedance（异步） ==============
def submit_video_task(prompt: str,
                      reference_images: Optional[list] = None,
                      reference_video_url: Optional[str] = None,
                      duration: Optional[int] = None,
                      generate_audio: bool = True,
                      resolution: str = "720p",
                      ratio: str = "16:9",
                      seed: Optional[int] = None) -> dict:
    """提交 Seedance 任务（**多模态参考模式 / 路径 B**）。返回 {task_id, raw}。

    本 SDK 统一走「t2v + 多模态参考」一条路径——商业短剧/广告类项目的标准姿势：
      - prompt 里详细描述本镜场景/动作/构图，并用文字暗示首尾帧
      - reference_images：最多 9 张图（角色三视图 + 关键道具 + 概念图等）
      - reference_video_url：链式生成第 N 段（N≥2）时把上一段视频塞进来，
        让模型看到完整动作连续性
      - generate_audio=True → Seedance 2.0 原生生成同步音频（本 SDK 默认开启）
      - duration 4-15 秒；不传由模型自定（一般 5s）
      - resolution 支持 480p / 720p / 1080p / 4k；默认 720p，调用方按交付规格选择
    """
    content = [{"type": "text", "text": prompt}]
    if reference_images:
        for ref in reference_images:
            if isinstance(ref, str):
                url = ref
            elif isinstance(ref, dict) and ref.get("url"):
                url = ref["url"]
            else:
                continue
            content.append({
                "type": "image_url",
                "image_url": {"url": _img_to_url(url)},
                "role": "reference_image",
            })
    if reference_video_url:
        content.append({
            "type": "video_url",
            "video_url": {"url": reference_video_url},
            "role": "reference_video",
        })
    valid_resolutions = {"480p", "720p", "1080p", "4k"}
    if resolution not in valid_resolutions:
        raise ValueError(f"Seedance resolution 只支持 480p/720p/1080p/4k，当前为 {resolution!r}")
    body = {"model": MODEL_VIDEO, "content": content,
            "resolution": resolution, "ratio": ratio}
    if duration:
        body["duration"] = int(duration)
    if generate_audio:
        body["generate_audio"] = True
    if seed is not None:
        body["seed"] = int(seed)
    raw = _http_post(f"{get_ark_base()}/contents/generations/tasks", body)
    return {"task_id": raw.get("id"), "model": MODEL_VIDEO, "raw": raw, "body": body}


def query_video_task(task_id: str) -> dict:
    """查询 Seedance 任务状态。返回 {status, video_url?, raw}"""
    raw = _http_get(f"{get_ark_base()}/contents/generations/tasks/{task_id}")
    status = raw.get("status")
    content = raw.get("content", {}) or {}
    video_url = content.get("video_url") or raw.get("video_url")
    return {"status": status, "video_url": video_url, "raw": raw}


def cancel_video_task(task_id: str) -> dict:
    """主动取消 Seedance 云端任务（防止本地中断后云端继续跑烧预算）。
    返回 {ok, status, body}。404 / 已 succeeded 也会原样返回。"""
    return _http_delete(f"{get_ark_base()}/contents/generations/tasks/{task_id}")


def wait_video_task(task_id: str, save_path: Path, timeout: int = 600,
                    poll_interval: int = 10) -> dict:
    """阻塞等待 + 下载。agent 一般不直接用，更多用 submit + query。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        st = query_video_task(task_id)
        if st["status"] == "succeeded":
            p = _download(st["video_url"], Path(save_path))
            return {"path": str(p), "raw": st["raw"]}
        if st["status"] == "failed":
            raise RuntimeError(f"Seedance 任务失败：{st['raw']}")
    raise TimeoutError(f"Seedance 任务 {task_id} 轮询超时 {timeout}s")


# ============== ffmpeg 工具 ==============
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"


def _run_ffmpeg(args: list, timeout: int = 300) -> dict:
    cmd = [FFMPEG, "-y"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{r.stderr[-500:]}")
    return {"ok": True}


def _find_ffprobe() -> Optional[str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        candidate = FFMPEG.replace("ffmpeg", "ffprobe")
        if os.path.exists(candidate):
            ffprobe = candidate
    return ffprobe


def has_audio_stream(media: str) -> bool:
    """判断媒体是否包含音轨。优先 ffprobe；失败时回退解析 ffmpeg stderr。"""
    ffprobe = _find_ffprobe()
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=index", "-of", "csv=p=0", str(media)],
                capture_output=True, text=True, timeout=15,
            )
            return bool(r.stdout.strip())
        except Exception:
            pass
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(media)],
            capture_output=True, text=True, timeout=30,
        )
        return "Audio:" in r.stderr
    except Exception:
        return False


def _audio_codec_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "libmp3lame"
    if suffix == ".wav":
        return "pcm_s16le"
    return "aac"


def _audio_fade_filter(fade_in: float = 0.0, fade_out: float = 0.0,
                       duration: Optional[float] = None) -> str:
    filters = []
    if fade_in and fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out and fade_out > 0:
        if duration is None:
            raise ValueError("fade_out > 0 时必须提供 duration")
        start = max(0.0, float(duration) - float(fade_out))
        filters.append(f"afade=t=out:st={start}:d={fade_out}")
    return ",".join(filters) if filters else "anull"


def probe_video_size(clip: str) -> tuple[int, int]:
    """探测视频宽高。失败时抛错，避免 concat filter 靠猜导致隐性失败。"""
    ffprobe = _find_ffprobe()
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(clip)],
                capture_output=True, text=True, timeout=15,
            )
            s = r.stdout.strip()
            if "x" in s:
                w, h = s.split("x", 1)
                return int(w), int(h)
        except Exception:
            pass
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(clip)],
            capture_output=True, text=True, timeout=30,
        )
        import re
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", r.stderr)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    raise RuntimeError(f"probe_video_size 失败，无法探测视频宽高：{clip}")


def video_concat(clips: list, save_path: Path, crossfade: float = 0.3,
                 fade_in: float = 0.0, fade_out: float = 0.0,
                 preset: str = "ultrafast") -> dict:
    """一段/多段拼接。视频硬切，音频在切点做短淡化，统一重编码。

    这里的 crossfade 是切点音频平滑时长：上一段尾部淡出、下一段头部淡入。
    不做画面重叠，也不改变段落总时长，避免对白/口型相对画面漂移。
    """
    if not clips:
        raise ValueError("video_concat 至少需要 1 个 clip")
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    durations = [probe_duration(c) for c in clips]
    target_w, target_h = probe_video_size(clips[0])
    args = []
    filters = []
    concat_inputs = []
    next_input = 0
    smooth = max(0.0, float(crossfade or 0.0))

    for i, (clip, duration) in enumerate(zip(clips, durations)):
        clip_input = next_input
        args.extend(["-i", str(clip)])
        next_input += 1

        audio_src = f"[{clip_input}:a]"
        if not has_audio_stream(clip):
            args.extend([
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            ])
            audio_src = f"[{next_input}:a]"
            next_input += 1

        v_label = f"v{i}"
        a_label = f"a{i}"
        filters.append(
            f"[{clip_input}:v]"
            f"setpts=PTS-STARTPTS,"
            f"fps=30,"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,format=yuv420p"
            f"[{v_label}]"
        )

        fades = []
        start_fade = float(fade_in or 0.0) if i == 0 else smooth
        end_fade = float(fade_out or 0.0) if i == len(clips) - 1 else smooth
        start_fade = min(max(0.0, start_fade), max(0.0, duration / 3.0))
        end_fade = min(max(0.0, end_fade), max(0.0, duration / 3.0))
        if start_fade > 0:
            fades.append(f"afade=t=in:st=0:d={start_fade:.3f}")
        if end_fade > 0:
            fades.append(f"afade=t=out:st={max(0.0, duration - end_fade):.3f}:d={end_fade:.3f}")
        fade_chain = "," + ",".join(fades) if fades else ""
        filters.append(
            f"{audio_src}"
            f"aresample=44100,"
            f"aformat=sample_rates=44100:channel_layouts=stereo,"
            f"apad=whole_dur={duration:.3f},"
            f"atrim=0:{duration:.3f},"
            f"asetpts=PTS-STARTPTS"
            f"{fade_chain}"
            f"[{a_label}]"
        )
        concat_inputs.append(f"[{v_label}][{a_label}]")

    filters.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
    timeout = max(300, int(sum(durations) * 20))
    _run_ffmpeg([
        *args,
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", preset,
        "-c:a", "aac", "-movflags", "+faststart",
        str(save_path),
    ], timeout=timeout)
    return {
        "path": str(save_path),
        "clips": len(clips),
        "duration": sum(durations),
        "audio_smoothing": smooth,
    }


def video_crossfade(clip_a: str, clip_b: str, save_path: Path,
                    duration: float = 1.0, offset: float = 4.0,
                    transition: str = "fade", preset: str = "ultrafast") -> dict:
    """A/B 两段做转场（视频 xfade + 音频 acrossfade）。
    transition: xfade 转场类型（fade/wipeleft/slideup/circleopen/dissolve 等，见 ffmpeg xfade 文档）。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(clip_a), "-i", str(clip_b)]
    audio_a = "[0:a]"
    audio_b = "[1:a]"
    next_input = 2
    if not has_audio_stream(clip_a):
        args.extend(["-f", "lavfi", "-t", str(probe_duration(clip_a)),
                     "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
        audio_a = f"[{next_input}:a]"
        next_input += 1
    if not has_audio_stream(clip_b):
        args.extend(["-f", "lavfi", "-t", str(probe_duration(clip_b)),
                     "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
        audio_b = f"[{next_input}:a]"
    args.extend([
        "-filter_complex",
        f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
        f"{audio_a}{audio_b}acrossfade=d={duration}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", preset,
        str(save_path),
    ])
    _run_ffmpeg(args)
    return {"path": str(save_path)}


def video_trim(clip: str, save_path: Path, start: float, end: float,
               preset: str = "ultrafast") -> dict:
    """截取 [start, end] 秒。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-ss", str(start), "-to", str(end), "-i", str(clip),
        "-c:v", "libx264", "-preset", preset, "-c:a", "aac",
        str(save_path),
    ])
    return {"path": str(save_path)}


def video_speed(clip: str, save_path: Path, factor: float,
                preset: str = "ultrafast") -> dict:
    """变速。factor=2 即 2 倍速；0.5 即 0.5 倍速。视频音频同步。"""
    if factor <= 0:
        raise ValueError("factor 必须 > 0")
    pts = 1.0 / factor
    # atempo 范围 [0.5, 2]，超出要级联
    tempos = []
    rem = factor
    while rem > 2.0:
        tempos.append("atempo=2.0"); rem /= 2.0
    while rem < 0.5:
        tempos.append("atempo=0.5"); rem /= 0.5
    tempos.append(f"atempo={rem}")
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    if has_audio_stream(clip):
        _run_ffmpeg([
            "-i", str(clip),
            "-filter_complex",
            f"[0:v]setpts={pts}*PTS[v];[0:a]{','.join(tempos)}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", preset,
            str(save_path),
        ])
    else:
        _run_ffmpeg([
            "-i", str(clip),
            "-vf", f"setpts={pts}*PTS",
            "-c:v", "libx264", "-preset", preset,
            "-an",
            str(save_path),
        ])
    return {"path": str(save_path)}


def video_overlay(base: str, pip: str, save_path: Path,
                  pip_scale: float = 0.33, position: str = "br",
                  margin: int = 20, preset: str = "ultrafast") -> dict:
    """画中画。position ∈ tl/tr/bl/br。"""
    pos_map = {
        "tl": f"{margin}:{margin}",
        "tr": f"W-w-{margin}:{margin}",
        "bl": f"{margin}:H-h-{margin}",
        "br": f"W-w-{margin}:H-h-{margin}",
    }
    pos = pos_map.get(position, pos_map["br"])
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(base), "-i", str(pip),
        "-filter_complex",
        f"[1:v]scale=iw*{pip_scale}:ih*{pip_scale}[p];[0:v][p]overlay={pos}[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-c:a", "copy",
        str(save_path),
    ])
    return {"path": str(save_path)}


def video_fade(clip: str, save_path: Path, fade_in: float = 0.5,
               fade_out: float = 0.5, total_duration: Optional[float] = None,
               preset: str = "ultrafast") -> dict:
    """头尾黑场。total_duration 不传时尝试用 ffprobe 探测。"""
    if total_duration is None:
        total_duration = probe_duration(clip)
    fade_out_start = max(0, total_duration - fade_out)
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-i", str(clip),
        "-vf",
        f"fade=t=in:st=0:d={fade_in},fade=t=out:st={fade_out_start}:d={fade_out}",
        "-c:v", "libx264", "-preset", preset,
    ]
    if has_audio_stream(clip):
        args.extend([
            "-af",
            f"afade=t=in:st=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}",
        ])
    else:
        args.append("-an")
    args.append(str(save_path))
    _run_ffmpeg(args)
    return {"path": str(save_path)}


def video_portrait(clip: str, save_path: Path,
                   width: int = 720, height: int = 1280,
                   preset: str = "ultrafast") -> dict:
    """横转竖：居中裁剪 + 缩放。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(clip),
        "-vf", f"crop=ih*{width}/{height}:ih,scale={width}:{height},setsar=1",
        "-c:v", "libx264", "-preset", preset, "-c:a", "copy",
        str(save_path),
    ])
    return {"path": str(save_path)}


def audio_strip(video: str, save_path: Path) -> dict:
    """移除视频所有音轨，只保留画面。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(video),
        "-map", "0:v:0",
        "-c:v", "copy",
        "-an",
        str(save_path),
    ])
    return {"path": str(save_path)}


def video_add_silence(video: str, save_path: Path,
                      sample_rate: int = 44100) -> dict:
    """给视频添加一条静音 AAC 音轨。若原视频已有音轨，会被静音轨替换。"""
    duration = probe_duration(video)
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(video),
        "-f", "lavfi", "-t", str(duration),
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(save_path),
    ])
    return {"path": str(save_path), "duration": duration}


def audio_extract(video: str, save_path: Path) -> dict:
    """从视频中抽取音轨。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    codec = _audio_codec_for_path(save_path)
    _run_ffmpeg([
        "-i", str(video),
        "-vn",
        "-map", "0:a:0",
        "-c:a", codec,
        str(save_path),
    ])
    return {"path": str(save_path)}


def audio_normalize(input_media: str, save_path: Path, target_i: float = -16.0,
                    target_tp: float = -1.5, target_lra: float = 11.0) -> dict:
    """响度标准化。支持音频文件或带音轨视频；视频会保留原视频流。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    codec = _audio_codec_for_path(save_path)
    _run_ffmpeg([
        "-i", str(input_media),
        "-filter_complex",
        f"[0:a]loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}[a]",
        "-map", "0:v?",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", codec,
        str(save_path),
    ])
    return {"path": str(save_path)}


def audio_fade(audio: str, save_path: Path, fade_in: float = 0.5,
               fade_out: float = 0.5,
               total_duration: Optional[float] = None) -> dict:
    """给音频做淡入淡出。total_duration 不传时自动探测。"""
    if total_duration is None:
        total_duration = probe_duration(audio)
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    codec = _audio_codec_for_path(save_path)
    _run_ffmpeg([
        "-i", str(audio),
        "-vn",
        "-af", _audio_fade_filter(fade_in, fade_out, total_duration),
        "-c:a", codec,
        str(save_path),
    ])
    return {"path": str(save_path), "duration": total_duration}


def audio_fit_duration(audio: str, save_path: Path, duration: float,
                       mode: str = "loop", fade_in: float = 0.0,
                       fade_out: float = 0.0) -> dict:
    """把音频适配到指定时长。mode=loop 循环裁切；mode=pad 不循环、不足补静音。"""
    if duration <= 0:
        raise ValueError("duration 必须 > 0")
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    codec = _audio_codec_for_path(save_path)
    af = f"atrim=0:{duration},asetpts=PTS-STARTPTS"
    fade = _audio_fade_filter(fade_in, fade_out, duration)
    if fade != "anull":
        af = f"{af},{fade}"
    args = []
    if mode == "loop":
        args.extend(["-stream_loop", "-1"])
    elif mode != "pad":
        raise ValueError("mode 只支持 loop 或 pad")
    args.extend([
        "-i", str(audio),
        "-t", str(duration),
        "-vn",
        "-af", f"apad,{af}" if mode == "pad" else af,
        "-c:a", codec,
        str(save_path),
    ])
    _run_ffmpeg(args)
    return {"path": str(save_path), "duration": duration, "mode": mode}


def video_set_audio(video: str, audio: str, save_path: Path,
                    audio_volume: float = 1.0,
                    duration: str = "shortest") -> dict:
    """用指定音频替换视频音轨。duration=shortest/first/longest。"""
    if duration not in {"shortest", "first", "longest"}:
        raise ValueError("duration 只支持 shortest / first / longest")
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-i", str(video),
        "-i", str(audio),
        "-filter_complex", f"[1:a]volume={audio_volume}[a]",
        "-map", "0:v:0",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac",
    ]
    if duration == "first":
        args.extend(["-t", str(probe_duration(video))])
    if duration == "shortest":
        args.append("-shortest")
    args.append(str(save_path))
    _run_ffmpeg(args)
    return {"path": str(save_path)}


def audio_amix(base_video: str, bgm_audio: str, save_path: Path,
               bgm_volume: float = 0.2, base_volume: float = 1.0,
               duration: str = "first") -> dict:
    """把 bgm_audio 混进 base_video 的音轨。"""
    if duration not in {"first", "shortest", "longest"}:
        raise ValueError("duration 只支持 first / shortest / longest")
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(base_video), "-i", str(bgm_audio),
        "-filter_complex",
        f"[0:a]volume={base_volume}[base];"
        f"[1:a]volume={bgm_volume}[bgm];"
        f"[base][bgm]amix=inputs=2:duration={duration}:dropout_transition=0[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac",
        str(save_path),
    ])
    return {"path": str(save_path)}


def burn_subtitle(clip: str, save_path: Path, text: str,
                  start: float = 0, end: float = 3,
                  fontsize: int = 32, y_pos: str = "h-100") -> dict:
    """在指定时间段烧录文字字幕。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    safe_text = text.replace("'", "").replace(":", " ")
    _run_ffmpeg([
        "-i", str(clip),
        "-vf",
        f"drawtext=text='{safe_text}':fontsize={fontsize}:fontcolor=white:"
        f"x=(w-text_w)/2:y={y_pos}:box=1:boxcolor=black@0.5:boxborderw=8:"
        f"enable='between(t,{start},{end})'",
        "-c:a", "copy", "-c:v", "libx264", "-preset", "ultrafast",
        str(save_path),
    ])
    return {"path": str(save_path)}


def extract_frames(clip: str, save_dir: Path, fps: float = 1.0) -> dict:
    """抽帧。返回 {paths: [...]}。"""
    save_dir = Path(save_dir)
    if save_dir.exists():
        shutil.rmtree(save_dir)
    save_dir.mkdir(parents=True)
    _run_ffmpeg([
        "-i", str(clip), "-vf", f"fps={fps}",
        str(save_dir / "f_%03d.jpg"),
    ])
    paths = sorted(str(p) for p in save_dir.glob("*.jpg"))
    return {"paths": paths, "count": len(paths)}


def probe_duration(clip: str) -> float:
    """探测视频时长（秒）。优先 ffprobe；没有 ffprobe 时回退到 ffmpeg stderr 解析。

    注意：imageio_ffmpeg 只附带 ffmpeg，不带 ffprobe；所以在很多机器上
    `shutil.which("ffprobe")` 是 None，必须有 ffmpeg 兜底，否则会返回错误的
    默认值导致 video_fade 等下游计算出畸形 fade_out_start，整段视频黑屏。
    """
    # 1) 先尝试 ffprobe
    ffprobe = _find_ffprobe()
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(clip)],
                capture_output=True, text=True, timeout=15,
            )
            s = r.stdout.strip()
            if s:
                return float(s)
        except Exception:
            pass
    # 2) 回退：用 ffmpeg 自己跑一次空解码，从 stderr 抓 "Duration: HH:MM:SS.xx"
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(clip)],
            capture_output=True, text=True, timeout=30,
        )
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mi, sec = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(sec)
    except Exception:
        pass
    # 3) 实在拿不到：抛异常而不是返回错误的默认值，让上层立刻可见
    raise RuntimeError(f"probe_duration 失败，无法探测视频时长：{clip}")


# ---- 火山引擎 SigV4 签名（纯标准库实现，避免引入 volcengine SDK） ----
def _volc_sign_v4(method: str, query: dict, body_bytes: bytes,
                  ak: str, sk: str,
                  service: str = "imagination",
                  region: str = "cn-beijing",
                  host: str = "open.volcengineapi.com") -> dict:
    """对火山 OpenAPI 请求签 V4，返回需要附加的 headers。"""
    import hashlib
    import hmac
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body_bytes).hexdigest()

    # CanonicalQuery：按 key 排序 + URL-encode
    from urllib.parse import quote
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}"
        for k, v in sorted(query.items())
    )

    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        method.upper(), "/", canonical_query,
        canonical_headers, signed_headers, payload_hash,
    ])
    cred_scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, cred_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(k, m):
        return hmac.new(k, m.encode() if isinstance(m, str) else m, hashlib.sha256).digest()

    k_date = _hmac(sk.encode(), short_date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    auth = (
        f"HMAC-SHA256 Credential={ak}/{cred_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Content-Type": "application/json",
        "Authorization": auth,
    }


def _volc_call(action: str, body: Optional[dict],
               version: str = "2024-08-12",
               method: str = "POST",
               timeout: int = 60) -> dict:
    """对 https://open.volcengineapi.com 发签名请求，返回响应 JSON。"""
    ak = get_extra("VOLC_AK")
    sk = get_extra("VOLC_SK")
    if not (ak and sk):
        raise RuntimeError(
            "GenBGM 凭证缺失：请到 https://console.volcengine.com/iam/keymanage "
            "新建 AK/SK，把 volc.ak / volc.sk 填到 vibefilming.config.json"
        )
    host = "open.volcengineapi.com"
    query = {"Action": action, "Version": version}
    body_bytes = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _volc_sign_v4(method, query, body_bytes, ak, sk, host=host)

    from urllib.parse import urlencode
    url = f"https://{host}/?{urlencode(query)}"
    req = urllib.request.Request(url, data=body_bytes if body_bytes else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"VOLC HTTP {e.code} on Action={action}: {body_text[:500]}")


def submit_bgm_task(prompt: str,
                    duration: int = 60,
                    segments: Optional[list] = None,
                    *,
                    enable_input_rewrite: bool = False) -> dict:
    """提交 BigMusic GenBGM 任务，返回 {task_id}。

    跟 seedance submit_video_task 对称：只提交，不轮询。agent 自己用 query_bgm_task 轮询。
    """
    body: dict = {"Text": prompt, "Version": "v5.0",
                  "EnableInputRewrite": enable_input_rewrite}
    if segments:
        body["Segments"] = segments
    else:
        body["Duration"] = int(duration)

    submit = _volc_call("GenBGM", body)
    if submit.get("ResponseMetadata", {}).get("Error"):
        raise RuntimeError(f"GenBGM 提交失败: {submit['ResponseMetadata']['Error']}")
    task_id = submit.get("Result", {}).get("TaskID")
    if not task_id:
        raise RuntimeError(f"GenBGM 提交未返回 TaskID: {submit}")
    return {"task_id": task_id, "raw": submit, "body": body}


def query_bgm_task(task_id: str, save_path: Optional[Path] = None) -> dict:
    """查询 BGM 任务状态，跟 seedance query_video_task 对称。

    Status 映射：
      - 1 / processing → status="processing"
      - 2 / succeeded  → status="succeeded"，可选下载 audio_url 到 save_path
      - 3 / failed     → status="failed"，error 含 FailureReason

    Returns:
        {"status": "processing"|"succeeded"|"failed",
         "task_id": str, "audio_url"?: str, "path"?: str,
         "duration"?: float, "style_info"?: dict, "error"?: dict, "raw": dict}
    """
    raw = _volc_call("QuerySong", {"TaskID": task_id})
    result = raw.get("Result", {}) or {}
    status_code = result.get("Status")
    out: dict = {"task_id": task_id, "raw": raw}
    if status_code == 2:
        detail = result.get("SongDetail") or {}
        audio_url = detail.get("AudioUrl")
        out["status"] = "succeeded"
        out["audio_url"] = audio_url
        out["duration"] = detail.get("Duration")
        try:
            out["style_info"] = json.loads(detail.get("StyleInfo") or "{}")
        except Exception:
            out["style_info"] = {}
        if save_path and audio_url:
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            _download(audio_url, save_path)
            out["path"] = str(save_path)
    elif status_code == 3:
        reason = result.get("FailureReason") or {}
        code = reason.get("Code") if isinstance(reason, dict) else None
        out["status"] = "failed"
        out["error"] = reason
        if str(code) == "50000001":
            out["hint"] = ("版权校验失败：尝试丰富 prompt（≥50 字 + 风格/情绪/乐器三要素）"
                          " / 加 Segments / 拉长时长到 ≥45s")
    else:
        out["status"] = "processing"
        out["status_code"] = status_code
    return out
