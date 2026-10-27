"""VibeFilming SDK：所有外部 API + 本地 ffmpeg 封装。

设计原则：
  - 每个函数职责单一，输入输出清晰
  - 失败抛具体 RuntimeError（包含 error_code / status）
  - 长任务（Seedance）拆成 submit + query 两个函数，agent 自己轮询
  - 所有产物落到指定路径，返回 path（agent 可后续读取）
  - VLM / GenBGM 缺凭证时返回 stub 错误，不静默失败
"""
import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from . import artifact_contracts, video_models

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


def _config_get(*path, default=None):
    # 与 Agent 一致：配置修改后下一次调用生效，无需重启进程。
    cur = _load_json_config()
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def get_ark_key() -> str:
    return (
        _env_first(
            "ARK_API_KEY",
            "VIBEFILMING_ARK_API_KEY",
            "ARK_KEY",
            "ARK_APIKEY",
            "VOLC_ARK_API_KEY",
        )
        or
        _config_get("ark", "api_key", default="")
        or ""
    )


def get_ark_base() -> str:
    value = (
        _env_first("ARK_API_BASE", "VIBEFILMING_ARK_API_BASE")
        or
        _config_get("ark", "api_base", default="")
        or "https://ark.cn-beijing.volces.com/api/v3"
    )
    return str(value).strip().strip("`'\"").rstrip("/")


def get_sts_creds() -> tuple[str, str, str]:
    """Read short-lived Ark credentials on every call so rotation is immediate."""
    return (
        _env_first("ARK_STS_AK", "VOLC_STS_AK", "ARK_ACCESS_KEY_ID"),
        _env_first("ARK_STS_SK", "VOLC_STS_SK", "ARK_SECRET_ACCESS_KEY"),
        _env_first("ARK_STS_TOKEN", "VOLC_STS_TOKEN", "X_SECURITY_TOKEN"),
    )


def _ark_sign_v4(method: str, url: str, body_bytes: bytes,
                 ak: str, sk: str, token: str,
                 content_type: str = "application/json") -> dict:
    """Sign an Ark REST request with Volcengine SigV4 and an STS token."""
    import hmac
    from datetime import datetime, timezone
    from urllib.parse import quote, urlsplit

    parts = urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    service = _env_first("ARK_SIGN_SERVICE") or "ark"
    region = _env_first("ARK_SIGN_REGION") or "cn-beijing"
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body_bytes).hexdigest()
    query_pairs = []
    for item in filter(None, parts.query.split("&")):
        key, _, value = item.partition("=")
        query_pairs.append((key, value))
    canonical_query = "&".join(
        f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
        for key, value in sorted(query_pairs)
    )
    signed_headers = "content-type;host;x-content-sha256;x-date;x-security-token"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
        f"x-security-token:{token}\n"
    )
    canonical_request = "\n".join([
        method.upper(), path, canonical_query,
        canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    signing_key = sign(sign(sign(sk.encode(), short_date), region), service)
    signing_key = sign(signing_key, "request")
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "X-Security-Token": token,
        "Content-Type": content_type,
        "Authorization": (
            f"HMAC-SHA256 Credential={ak}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def _ark_auth_headers(method: str, url: str, body_bytes: bytes,
                      content_type: str = "application/json") -> dict:
    key = get_ark_key()
    if key:
        return {"Authorization": f"Bearer {key}", "Content-Type": content_type}
    ak, sk, token = get_sts_creds()
    if ak and sk and token:
        return _ark_sign_v4(
            method, url, body_bytes, ak, sk, token, content_type)
    raise RuntimeError(
        "ARK 鉴权缺失：请配置 ARK_API_KEY，或同时配置 "
        "ARK_STS_AK / ARK_STS_SK / ARK_STS_TOKEN"
    )


def get_extra(name: str, default=None):
    env_value = _env_first(name)
    if env_value:
        return env_value
    config_key = {"VOLC_AK": "ak", "VOLC_SK": "sk"}.get(name)
    if config_key:
        value = _config_get("volc", config_key, default=None)
        if value:
            return value
    return default


def get_model(name: str, default: str) -> str:
    env_name = {
        "text": "ARK_MODEL_TEXT",
        "vlm": "ARK_MODEL_VLM",
        "image": "ARK_MODEL_IMAGE",
        "video": "ARK_MODEL_VIDEO",
    }.get(name)
    return (
        (_env_first(env_name) if env_name else "")
        or
        _config_get("ark", "models", name, default="")
        or default
    )


# ============== 模型 ID（可统一改） ==============
MODEL_TEXT = get_model("text", "doubao-seed-evolving")
MODEL_VLM = get_model("vlm", "doubao-seed-evolving")
MODEL_IMG = get_model("image", "doubao-seedream-5-0-260128")
MODEL_VIDEO = video_models.resolve().model_id


# ============== HTTP helper ==============
def _http_post(url: str, body: dict, timeout: int = 180) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers=_ark_auth_headers("POST", url, data),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body_text[:300]}")


def _http_get(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url, headers=_ark_auth_headers("GET", url, b""))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _http_delete(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url, headers=_ark_auth_headers("DELETE", url, b""), method="DELETE")
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
    content_type = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(
        url, data=data,
        headers=_ark_auth_headers("POST", url, data, content_type),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body_text[:300]}")


def _download(url: str, save_path: Path, timeout: int = 45) -> Path:
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    temp = save_path.with_name(f".{save_path.name}.{uuid.uuid4().hex}.part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, temp.open("wb") as handle:
            expected = int(response.headers.get("Content-Length") or 0)
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
        if not temp.stat().st_size or (expected and temp.stat().st_size != expected):
            raise IOError("模型文件下载不完整；保留原 task_id 重试下载")
        temp.replace(save_path)
    finally:
        temp.unlink(missing_ok=True)
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
    - data: 原样返回；
    - http(s): 由工具进程下载后转成 data URI，避免让模型服务二次拉取
      一次性预签名 URL；
    - 本地存在的文件 → base64 内嵌成 data URI。
      这样 agent 只需转抄很短的本地 path，不必转抄那条很长的带签名 url；
      实测 LLM 转抄长 url 时常把 `?X-Tos-Signature=...` query 段截掉，
      丢给 Seedance 一个无签名裸 url → 服务端报 resource download failed。
      base64 内嵌从根上绕开"转抄长 url"这一步。
    - 其余（找不到的字符串）原样返回，交给上游报错。
    """
    s = str(ref).strip()
    if s.startswith("data:"):
        return s
    if s.startswith(("http://", "https://")):
        query = {
            key.lower(): urllib.parse.unquote(value)
            for key, value in urllib.parse.parse_qsl(
                urllib.parse.urlsplit(s).query, keep_blank_values=True)
        }
        signature = (
            query.get("x-tos-signature")
            or query.get("x-amz-signature")
            or ""
        )
        if signature and not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
            raise ValueError(
                "图片预签名 URL 已损坏：signature 必须是 64 位十六进制。"
                "请改用项目内 path 或 project:// artifact_ref。"
            )
        request = urllib.request.Request(
            s, headers={"User-Agent": "VibeFilming/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = response.read(20 * 1024 * 1024 + 1)
                content_type = (
                    response.headers.get_content_type()
                    if response.headers else ""
                )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"图片 URL 下载失败 HTTP {exc.code}。不要重试该预签名 URL；"
                "请改用 gen_image 返回的项目内 path 或 artifact_ref。"
            ) from exc
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("远程参考图超过 20MB，请先登记为项目素材")
        mime = (
            content_type
            if content_type.startswith("image/")
            else _image_mime_from_bytes(
                data, Path(urllib.parse.urlsplit(s).path).suffix)
        )
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    p = Path(s)
    if p.exists():
        data = p.read_bytes()
        mime = _image_mime_from_bytes(data, p.suffix)
        b64 = base64.b64encode(data).decode()
        return f"{mime};base64,{b64}".replace("image/", "data:image/", 1)
    return s


# ============== Doubao 文本 / VLM ==============
def doubao_chat(messages: list, max_tokens: int = 16384, temperature: float = 0.7,
                model: str = None) -> dict:
    """Doubao 文本对话。messages 遵循 OpenAI 格式。返回完整 API JSON。"""
    body = {
        "model": model or get_model("text", MODEL_TEXT),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    return _http_post(f"{get_ark_base()}/chat/completions", body)


def doubao_vlm(image_paths: list, text: str, max_tokens: int = 16384,
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
        "model": get_model("vlm", MODEL_VLM),
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
            "model": get_model("vlm", MODEL_VLM),
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


def doubao_video_understand(video: str, text: str, max_tokens: int = 16384,
                            temperature: float = 0.1, fps: float = 1.0,
                            system: Optional[str] = None) -> dict:
    """使用当前配置的 Seed VLM 直接理解单个视频（当前为 Seed 2.1 Pro，不抽帧）。
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
        "model": get_model("vlm", MODEL_VLM),
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
IMAGE_MIN_PIXELS = 2560 * 1440
IMAGE_SIZE_ALIGNMENT = 8


def normalize_image_size(size: str) -> str:
    """Raise undersized WxH requests to Seedream's minimum pixel count."""
    value = str(size or "").strip()
    match = re.fullmatch(r"(\d{2,5})\s*[xX×]\s*(\d{2,5})", value)
    if not match:
        return value
    width, height = map(int, match.groups())
    if width <= 0 or height <= 0:
        raise ValueError(f"图片尺寸必须为正整数：{size!r}")
    if width * height >= IMAGE_MIN_PIXELS:
        return f"{width}x{height}"
    scale = math.sqrt(IMAGE_MIN_PIXELS / (width * height))
    align = IMAGE_SIZE_ALIGNMENT
    width = math.ceil(width * scale / align) * align
    height = math.ceil(height * scale / align) * align
    return f"{width}x{height}"


def gen_image(prompt: str, save_path: Path,
              reference_images=None,
              size: str = "2048x2048",
              watermark: bool = True) -> dict:
    """Seedream 4.0 — 文生图 / 图编辑 / 多图融合。返回 {url, path, raw}。

    reference_images: None=文生图；str=单图编辑；list[str]=多图融合（最多 14 张）。
        可传本地 path（自动 base64 内嵌）或 http(s) url；优先传本地 path，省去转抄长签名 url。
    watermark: 是否加「AI 生成」水印/标识（官方默认 true，本 SDK 默认 true）。
    """
    size = normalize_image_size(size)
    body = {
        "model": get_model("image", MODEL_IMG),
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
class VideoSubmissionError(RuntimeError):
    """A request reached the submission boundary; acceptance may be unknown."""


def submit_video_task(prompt: str,
                      reference_images: Optional[list] = None,
                      reference_video_url: Optional[str] = None,
                      reference_video_urls: Optional[list] = None,
                      reference_audio_urls: Optional[list] = None,
                      first_frame: Optional[str] = None,
                      last_frame: Optional[str] = None,
                      duration: Optional[int] = None,
                      generate_audio: bool = True,
                      resolution: str = "720p",
                      ratio: str = "16:9",
                      seed: Optional[int] = None,
                      model: Optional[str] = None,
                      omni_reference_task_type: Optional[str] = None,
                      output_format: Optional[str] = None) -> dict:
    """提交 Seedance 2.0/2.5 异步视频任务，按模型能力严格校验。"""
    def normalize_urls(values, legacy=None) -> list[str]:
        raw = []
        if legacy:
            raw.append(legacy)
        if values:
            if isinstance(values, str):
                raw.append(values)
            else:
                raw.extend(values)
        result = []
        for item in raw:
            value = (
                item.strip()
                if isinstance(item, str)
                else str((item or {}).get("url") or "").strip()
                if isinstance(item, dict)
                else ""
            )
            if value and value not in result:
                result.append(value)
        return result

    video_urls = normalize_urls(reference_video_urls, reference_video_url)
    audio_urls = normalize_urls(reference_audio_urls)
    profile = video_models.resolve(model, strict=bool(model))
    model_name = profile.model_id
    images = list(reference_images or [])
    if len(images) > profile.max_images:
        raise ValueError(
            f"{profile.label} reference_images 最多 {profile.max_images} 张，"
            f"当前为 {len(images)}")
    if len(video_urls) > profile.max_videos:
        raise ValueError(
            f"{profile.label} reference_video_urls 最多 {profile.max_videos} 段，"
            f"当前为 {len(video_urls)}")
    if len(audio_urls) > profile.max_audios:
        raise ValueError(
            f"{profile.label} reference_audio_urls 最多 {profile.max_audios} 段，"
            f"当前为 {len(audio_urls)}")
    if audio_urls and not images and not video_urls and not profile.audio_only:
        raise ValueError(
            f"{profile.label} 不支持仅输入参考音频；"
            "至少同时提供 1 张参考图或 1 段参考视频")
    if seed is not None:
        raise ValueError(f"{profile.label} 暂不支持 seed 参数，请删除 seed 后重试")
    if last_frame and not first_frame:
        raise ValueError("last_frame 不能单独输入，必须同时提供 first_frame")
    if (first_frame or last_frame) and (images or video_urls or audio_urls):
        raise ValueError("首尾帧模式不能与 reference_images/video/audio 同时使用")
    if (first_frame or last_frame) and ratio != "adaptive":
        raise ValueError("首帧/首尾帧模式必须使用 ratio=adaptive")

    task_type = str(omni_reference_task_type or "").strip()
    if task_type and (first_frame or last_frame):
        raise ValueError(
            "首帧/首尾帧是独立锁定任务，不能同时设置 "
            "omni_reference_task_type")
    if task_type:
        if task_type not in profile.task_types:
            supported = "/".join(profile.task_types) or "无"
            raise ValueError(
                f"{profile.label} 不支持 omni_reference_task_type={task_type!r}，"
                f"可选：{supported}")
        if task_type in {"edit", "extend"} and not video_urls:
            raise ValueError(f"{task_type} 任务必须提供 reference_video_urls")
        if task_type in {"edit", "extend"} and ratio != "adaptive":
            raise ValueError(f"{task_type} 任务必须使用 ratio=adaptive")
        if task_type == "edit" and duration != -1:
            raise ValueError("edit 任务必须使用 duration=-1")

    if duration is not None:
        parsed_duration = int(duration)
        if (
            parsed_duration != -1
            and not profile.min_output_duration
            <= parsed_duration
            <= profile.max_output_duration
        ):
            raise ValueError(
                f"{profile.label} duration 只支持 -1 或 "
                f"{profile.min_output_duration}-{profile.max_output_duration} 秒，"
                f"当前为 {parsed_duration}")
    if ratio not in profile.ratios:
        raise ValueError(
            f"{profile.label} ratio 不支持 {ratio!r}，可选：{list(profile.ratios)}")
    if resolution not in profile.resolutions:
        raise ValueError(
            f"{profile.label} resolution 只支持 {'/'.join(profile.resolutions)}，"
            f"当前为 {resolution!r}")
    output_format = str(
        output_format
        or (
            "mov"
            if profile.key == "2.5" and task_type in {"edit", "extend"}
            else "mp4"
        )
    ).strip().lower()
    if output_format not in profile.output_formats:
        raise ValueError(
            f"{profile.label} output_format 只支持 "
            f"{'/'.join(profile.output_formats)}，当前为 {output_format!r}")

    content = [{"type": "text", "text": prompt}]
    for ref, role in ((first_frame, "first_frame"), (last_frame, "last_frame")):
        if ref:
            content.append({
                "type": "image_url",
                "image_url": {"url": _img_to_url(ref)},
                "role": role,
            })
    if images:
        for ref in images:
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
    for url in video_urls:
        content.append({
            "type": "video_url",
            "video_url": {"url": url},
            "role": "reference_video",
        })
    for url in audio_urls:
        content.append({
            "type": "audio_url",
            "audio_url": {"url": url},
            "role": "reference_audio",
        })
    body = {"model": model_name, "content": content,
            "resolution": resolution, "ratio": ratio,
            "generate_audio": bool(generate_audio)}
    if profile.key == "2.5":
        body["output_format"] = output_format
    if duration is not None:
        body["duration"] = int(duration)
    if task_type:
        body["omni_reference_task_type"] = task_type
    try:
        raw = _http_post(f"{get_ark_base()}/contents/generations/tasks", body)
        if not isinstance(raw, dict) or not raw.get("id"):
            raise RuntimeError("Seedance 提交响应未返回 task_id，不能视为成功")
    except Exception as exc:
        raise VideoSubmissionError(str(exc)) from exc
    return {
        "task_id": raw.get("id"),
        "model": body["model"],
        "model_version": profile.key,
        "raw": raw,
        "body": body,
    }


def query_video_task(task_id: str) -> dict:
    """查询 Seedance 任务状态。返回 {status, video_url?, raw}"""
    raw = _http_get(f"{get_ark_base()}/contents/generations/tasks/{task_id}", timeout=20)
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


def has_audio_stream(media: str) -> bool:
    """判断媒体是否包含音轨。本机只有 imageio 附带的 ffmpeg、没有 ffprobe，
    统一用 ffmpeg 空解码后解析 stderr 里的 "Audio:"。"""
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
    """探测视频宽高。失败时抛错，避免 concat filter 靠猜导致隐性失败。
    本机没有 ffprobe，直接用 ffmpeg 空解码解析 stderr 里的分辨率。"""
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


def probe_video_duration(clip: str) -> float:
    """探测视频流自身时长，不把更长的音轨误算成画面时长。

    imageio-ffmpeg 不附带 ffprobe，因此用 showinfo 解码视频流并取最后一帧的
    pts_time + duration_time。探测失败时再回退容器时长。
    """
    try:
        r = subprocess.run(
            [
                FFMPEG, "-hide_banner", "-i", str(clip),
                "-map", "0:v:0", "-an", "-vf", "showinfo", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=max(60, int(probe_duration(clip) * 4)),
        )
        matches = re.findall(
            r"pts_time:([0-9.eE+-]+).*?duration_time:([0-9.eE+-]+)",
            r.stderr,
        )
        if matches:
            pts, frame_duration = matches[-1]
            duration = float(pts) + float(frame_duration)
            if duration > 0:
                return duration
    except Exception:
        pass
    return probe_duration(clip)


def probe_media(clip: str, accurate_duration: bool = False) -> dict:
    """返回 Agent 可消费的音视频元数据，不依赖系统 ffprobe。

    默认读取容器时长，避免每次探测都完整解码长视频；需要校验精确画面时长时，
    accurate_duration=True 会复用 probe_video_duration 做完整视频流探测。
    """
    path = Path(clip)
    if not path.is_file():
        raise FileNotFoundError(f"媒体文件不存在：{clip}")
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    stderr = r.stderr or ""
    video_line = next(
        (line.strip() for line in stderr.splitlines() if "Video:" in line), "")
    if not video_line:
        raise RuntimeError(f"probe_media 未找到视频流：{clip}")

    size_match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video_line)
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s+fps\b", video_line)
    codec_match = re.search(r"Video:\s*([a-zA-Z0-9_]+)", video_line)
    audio_line = next(
        (line.strip() for line in stderr.splitlines() if "Audio:" in line), "")
    audio_codec_match = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", audio_line)
    sample_rate_match = re.search(r"(\d+)\s+Hz\b", audio_line)
    channel_match = re.search(
        r"\b(mono|stereo|2\.1|3\.0|4\.0|5\.1|7\.1)\b", audio_line)

    duration = (
        probe_video_duration(str(path))
        if accurate_duration else probe_duration(str(path))
    )
    fps = float(fps_match.group(1)) if fps_match else None
    width = int(size_match.group(1)) if size_match else None
    height = int(size_match.group(2)) if size_match else None
    return {
        "path": str(path),
        "duration": duration,
        "duration_mode": "decoded_video" if accurate_duration else "container",
        "width": width,
        "height": height,
        "fps": fps,
        "estimated_frames": round(duration * fps) if fps else None,
        "video_codec": codec_match.group(1) if codec_match else None,
        "has_audio": bool(audio_line),
        "audio_codec": audio_codec_match.group(1) if audio_codec_match else None,
        "sample_rate": (
            int(sample_rate_match.group(1)) if sample_rate_match else None
        ),
        "channels": channel_match.group(1) if channel_match else None,
        "file_size": path.stat().st_size,
    }


def probe_video_fps(clip: str) -> float:
    fps = probe_media(clip).get("fps")
    if not fps or fps <= 0:
        raise RuntimeError(f"probe_video_fps 失败，无法探测视频帧率：{clip}")
    return float(fps)


def video_concat(clips: list, save_path: Path, crossfade: float = 0.3,
                 fade_in: float = 0.0, fade_out: float = 0.0,
                 preset: str = "ultrafast",
                 transitions: Optional[list] = None,
                 fps: Optional[float] = None) -> dict:
    """一段/多段拼接，支持逐切点画面转场并统一重编码。

    这里的 crossfade 是切点音频平滑时长：上一段尾部淡出、下一段头部淡入。
    transitions 不传时画面硬切，不改变段落总时长。传入时必须与切点数量一致，
    每项格式为 {"type": "cut|fade|dissolve|...", "duration": 0.5}。
    非 cut 类型使用 xfade + acrossfade，成片时长会减去画面重叠时长。
    """
    if not clips:
        raise ValueError("video_concat 至少需要 1 个 clip")
    if transitions is not None:
        if not isinstance(transitions, list):
            raise ValueError("transitions 必须是数组")
        if len(transitions) != len(clips) - 1:
            raise ValueError(
                f"transitions 数量必须等于切点数 {len(clips) - 1}，"
                f"实际为 {len(transitions)}"
            )
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    # 拼接是以画面为主的操作。容器可能因 duration_mode=longest 带有更长音轨，
    # 若用容器时长计算 xfade offset，画面已经结束后才开始转场，会吞掉下一段。
    durations = [probe_video_duration(c) for c in clips]
    normalized_transitions = []
    for i in range(len(clips) - 1):
        raw = transitions[i] if transitions is not None else {"type": "cut"}
        if isinstance(raw, str):
            raw = {"type": raw}
        if not isinstance(raw, dict):
            raise ValueError(f"transitions[{i}] 必须是对象或字符串")
        raw_transition = str(raw.get("type", "cut")).strip().lower()
        transition = raw_transition
        if transition in {
            "hard_cut", "hardcut", "source_cut", "match_cut", "matchcut",
            "generation_match_cut",
        }:
            transition = "cut"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", transition):
            raise ValueError(f"transitions[{i}].type 非法：{transition}")
        duration = 0.0 if transition == "cut" else float(raw.get("duration", 0.5))
        if transition != "cut":
            max_duration = min(durations[i], durations[i + 1])
            if duration <= 0 or duration >= max_duration:
                raise ValueError(
                    f"transitions[{i}].duration 必须 > 0 且小于相邻片段时长 "
                    f"{max_duration:.3f}s"
                )
        normalized = {
            "type": transition,
            "duration": duration,
        }
        if raw_transition != transition:
            normalized["strategy"] = raw_transition
        for key in (
            "at", "reason", "audio_handling", "source_time_s",
            "cut_class", "left_end_state", "right_start_state",
            "continuity_anchors", "generation_overlap_s", "visual_bridge",
            "effect_source", "effect_peak", "effect_clear", "seam_review",
            "fallback",
        ):
            if raw.get(key) is not None:
                normalized[key] = raw[key]
        normalized_transitions.append(normalized)
    for i in range(1, len(clips) - 1):
        incoming = normalized_transitions[i - 1]["duration"]
        outgoing = normalized_transitions[i]["duration"]
        if incoming + outgoing >= durations[i]:
            raise ValueError(
                f"片段 {i} 两侧转场共 {incoming + outgoing:.3f}s，"
                f"必须小于该片段时长 {durations[i]:.3f}s"
            )

    target_w, target_h = probe_video_size(clips[0])
    target_fps = float(fps) if fps is not None else probe_video_fps(clips[0])
    if target_fps < 1 or target_fps > 120:
        raise ValueError(f"video_concat fps 必须在 1-120，当前为 {target_fps}")
    fps_text = f"{target_fps:.6f}".rstrip("0").rstrip(".")
    args = []
    filters = []
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
            f"fps={fps_text},"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,format=yuv420p,setpts=PTS-STARTPTS,settb=AVTB"
            f"[{v_label}]"
        )

        fades = []
        previous_is_cut = i > 0 and normalized_transitions[i - 1]["type"] == "cut"
        next_is_cut = (
            i < len(clips) - 1 and normalized_transitions[i]["type"] == "cut"
        )
        start_fade = float(fade_in or 0.0) if i == 0 else (
            smooth if previous_is_cut else 0.0
        )
        end_fade = float(fade_out or 0.0) if i == len(clips) - 1 else (
            smooth if next_is_cut else 0.0
        )
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

    current_v = "v0"
    current_a = "a0"
    output_duration = durations[0]
    timeline = [{"clip": str(clips[0]), "start": 0.0, "end": durations[0]}]
    for i, transition in enumerate(normalized_transitions, start=1):
        next_v = f"vx{i}"
        next_a = f"ax{i}"
        clip_start = output_duration - transition["duration"]
        if transition["type"] == "cut":
            filters.append(
                f"[{current_v}][v{i}]concat=n=2:v=1:a=0[{next_v}]"
            )
            filters.append(
                f"[{current_a}][a{i}]concat=n=2:v=0:a=1[{next_a}]"
            )
            output_duration += durations[i]
        else:
            duration = transition["duration"]
            offset = output_duration - duration
            # concat 后的帧率元数据可能不再满足 xfade；转场输入需再次声明
            # 相同帧率和 timebase。纯硬切路径不会经过这里，不会按段丢帧。
            filters.append(
                f"[{current_v}]setpts=PTS-STARTPTS,"
                f"fps={fps_text},settb=AVTB[xfleft{i}]"
            )
            filters.append(
                f"[v{i}]setpts=PTS-STARTPTS,"
                f"fps={fps_text},settb=AVTB[xfright{i}]"
            )
            filters.append(
                f"[xfleft{i}][xfright{i}]"
                f"xfade=transition={transition['type']}:"
                f"duration={duration:.3f}:offset={offset:.3f}[{next_v}]"
            )
            filters.append(
                f"[{current_a}][a{i}]acrossfade=d={duration:.3f}[{next_a}]"
            )
            output_duration += durations[i] - duration
        timeline.append({
            "clip": str(clips[i]), "start": clip_start, "end": output_duration,
            "transition_in": dict(transition),
        })
        current_v = next_v
        current_a = next_a

    visual_fades = []
    visual_fade_in = min(
        max(0.0, float(fade_in or 0.0)),
        max(0.0, output_duration / 3.0),
    )
    visual_fade_out = min(
        max(0.0, float(fade_out or 0.0)),
        max(0.0, output_duration / 3.0),
    )
    if visual_fade_in > 0:
        visual_fades.append(f"fade=t=in:st=0:d={visual_fade_in:.3f}")
    if visual_fade_out > 0:
        start = max(0.0, output_duration - visual_fade_out)
        visual_fades.append(
            f"fade=t=out:st={start:.3f}:d={visual_fade_out:.3f}"
        )
    if visual_fades:
        filters.append(f"[{current_v}]{','.join(visual_fades)}[vout]")
        current_v = "vout"
    filters.append(f"[{current_v}]format=yuv420p[vfinal]")
    current_v = "vfinal"

    timeout = max(300, int(output_duration * 20))
    _run_ffmpeg([
        *args,
        "-filter_complex", ";".join(filters),
        "-map", f"[{current_v}]", "-map", f"[{current_a}]",
        "-c:v", "libx264", "-preset", preset,
        "-r", fps_text, "-c:a", "aac", "-movflags", "+faststart",
        str(save_path),
    ], timeout=timeout)
    result = {
        "contract": artifact_contracts.TIMELINE,
        "path": str(save_path),
        "clips": len(clips),
        "duration": output_duration,
        "fps": target_fps,
        "audio_smoothing": smooth,
        "fade_in": visual_fade_in,
        "fade_out": visual_fade_out,
        "transitions": normalized_transitions,
        "timeline": timeline,
    }
    save_path.with_suffix(".timeline.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


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
    result = probe_media(str(save_path))
    result.update({
        "source": str(clip),
        "start": float(start),
        "end": float(end),
    })
    return result


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
    """头尾黑场。total_duration 不传时用 ffmpeg 探测。"""
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
    result = {
        "contract": artifact_contracts.TIMELINE,
        "path": str(save_path),
        "duration": total_duration,
        "fade_in": fade_in,
        "fade_out": fade_out,
        "has_audio": has_audio_stream(str(save_path)),
    }
    save_path.with_suffix(".timeline.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


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


def extract_boundary_frame(
    clip: str,
    save_path: Path,
    *,
    edge: str = "last",
    offset_s: float = 0.0,
) -> dict:
    """Extract one exact boundary frame for a later first/last-frame task."""
    if edge not in {"first", "last"}:
        raise ValueError("extract_boundary_frame edge 只支持 first/last")
    offset = float(offset_s)
    if offset < 0:
        raise ValueError("extract_boundary_frame offset_s 必须 >= 0")
    duration = probe_video_duration(clip)
    fps = probe_video_fps(clip)
    last_timestamp = max(0.0, duration - max(1.0 / fps, 0.001))
    timestamp = (
        min(last_timestamp, offset)
        if edge == "first"
        else max(0.0, last_timestamp - offset)
    )
    output = Path(save_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(clip),
        "-ss", f"{timestamp:.6f}",
        "-frames:v", "1",
        "-compression_level", "3",
        str(output),
    ])
    return {
        "clip": str(clip),
        "edge": edge,
        "offset_s": offset,
        "time_s": round(timestamp, 6),
        "duration_s": duration,
        "fps": fps,
        "path": str(output),
    }


def extract_seam_frames(left_clip: str, right_clip: str, save_dir: Path,
                        window: float = 0.3) -> dict:
    """抽取左段末三帧和右段初三帧，供相邻片段接缝审查。"""
    if window <= 0:
        raise ValueError("extract_seam_frames window 必须 > 0")
    save_dir = Path(save_dir)
    if save_dir.exists():
        shutil.rmtree(save_dir)
    save_dir.mkdir(parents=True)

    left_duration = probe_video_duration(left_clip)
    right_duration = probe_video_duration(right_clip)
    left_fps = probe_video_fps(left_clip)
    right_fps = probe_video_fps(right_clip)
    left_last = max(0.0, left_duration - max(1.0 / left_fps, 0.001))
    right_last = max(0.0, right_duration - max(1.0 / right_fps, 0.001))
    samples = (
        ("left_1", left_clip, max(0.0, left_duration - window)),
        ("left_2", left_clip, max(0.0, left_duration - window / 2.0)),
        ("left_3", left_clip, left_last),
        ("right_1", right_clip, 0.0),
        ("right_2", right_clip, min(right_last, window / 2.0)),
        ("right_3", right_clip, min(right_last, window)),
    )
    frames = []
    for label, clip, timestamp in samples:
        path = save_dir / f"{label}.jpg"
        _run_ffmpeg([
            "-i", str(clip),
            "-ss", f"{timestamp:.6f}",
            "-frames:v", "1",
            "-q:v", "2",
            str(path),
        ])
        frames.append({
            "label": label,
            "clip": str(clip),
            "time_s": round(timestamp, 6),
            "path": str(path),
        })
    return {
        "contract": artifact_contracts.SEAM_EVIDENCE,
        "left_clip": str(left_clip),
        "right_clip": str(right_clip),
        "window_s": float(window),
        "frames": frames,
        "paths": [item["path"] for item in frames],
        "count": len(frames),
    }


def probe_duration(clip: str) -> float:
    """探测视频时长（秒）。本机没有 ffprobe（imageio_ffmpeg 只附带 ffmpeg），
    直接用 ffmpeg 空解码从 stderr 抓 "Duration: HH:MM:SS.xx"。
    拿不到就抛异常，而不是返回错误默认值——否则 video_fade 会算出畸形
    fade_out_start，整段视频黑屏。"""
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
