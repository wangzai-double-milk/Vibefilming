"""VibeFilming SDK：所有外部 API + 本地 ffmpeg 封装。

设计原则：
  - 每个函数职责单一，输入输出清晰
  - 失败抛具体 RuntimeError（包含 error_code / status）
  - 长任务（Seedance）拆成 submit + query 两个函数，agent 自己轮询
  - 所有产物落到指定路径，返回 path（agent 可后续读取）
  - VLM / TTS / GenBGM 缺凭证时返回 stub 错误，不静默失败
"""
import base64
import json
import os
import shutil
import subprocess
import sys
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
        suffix = p.suffix.lower().lstrip(".") or "png"
        if suffix == "jpg":
            suffix = "jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"data:image/{suffix};base64,{b64}"
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
    """Doubao VLM。image_paths 是本地 jpg/png 列表，会 base64 内嵌。
    system: 可选系统提示（设定 VLM 的角色/输出格式）。"""
    image_msgs = []
    for p in image_paths:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        image_msgs.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
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
    
    raw = _http_post(f"{get_ark_base()}/chat/completions", body)
    return {"raw": raw, "body": body}


def upload_file_ark(file_path: Path, fps: float = 1.0, timeout: int = 600) -> dict:
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


def wait_file_active(file_id: str, timeout: int = 300, interval: int = 3) -> dict:
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


def _responses_extract_text(raw: dict) -> str:
    """从 Responses API 返回里抽出纯文本回答。
    Responses 的输出结构：output 是 item 列表，message item 的 content 里
    type=output_text 的项带 text。做防御式解析，兼容多种字段命名。
    """
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


def _video_understand_via_files(video: str, text: str, fps: float,
                                system: Optional[str], max_tokens: int) -> dict:
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
        }
        raw = _http_post(f"{get_ark_base()}/responses", body, timeout=600)
        answer = _responses_extract_text(raw)
        # 归一成 chat/completions 形状，保持调用方 raw["choices"][0]["message"]["content"] 不变
        normalized = {
            "choices": [{"message": {"content": answer}}],
            "_responses_raw": raw,
            "_via": "files_api",
        }
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
    if not video.startswith(("http://", "https://")):
        p = Path(video)
        if p.exists() and p.stat().st_size > _VIDEO_INLINE_MAX_BYTES:
            return _video_understand_via_files(video, text, fps, system, max_tokens)

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
    
    # 视频原生理解（推理模型）单次推理常 >180s，对齐大文件路径用 600s 超时。
    raw = _http_post(f"{get_ark_base()}/chat/completions", body, timeout=600)
    return {"raw": raw, "body": body}


# ============== Seedream（文生图 / 图编辑统一接口）==============
def gen_image(prompt: str, save_path: Path,
              ref_image_url=None,
              size: str = "1024x1024",
              watermark: bool = False) -> dict:
    """Seedream 4.0 — 文生图 / 图编辑 / 多图融合。返回 {url, path, raw}。

    ref_image_url: None=文生图；str=单图编辑；list[str]=多图融合（最多 14 张）。
        可传本地 path（自动 base64 内嵌）或 http(s) url；优先传本地 path，省去转抄长签名 url。
    watermark: 是否加「AI 生成」水印（官方默认 true，本 SDK 默认 false）。
    """
    body = {
        "model": MODEL_IMG,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": watermark,
        "n": 1,
    }
    if ref_image_url:
        if isinstance(ref_image_url, (list, tuple)):
            body["image"] = [_img_to_url(x) for x in ref_image_url]
        else:
            body["image"] = _img_to_url(ref_image_url)
    raw = _http_post(f"{get_ark_base()}/images/generations", body)
    url = raw["data"][0]["url"]
    p = _download(url, Path(save_path))
    return {"url": url, "path": str(p), "raw": raw, "body": body}


# ============== Seedance（异步） ==============
def submit_video_task(prompt: str,
                      reference_images: Optional[list] = None,
                      reference_video_url: Optional[str] = None,
                      duration: Optional[int] = None,
                      generate_audio: bool = False,
                      resolution: str = "720p",
                      ratio: str = "16:9",
                      seed: Optional[int] = None,
                      camera_fixed: Optional[bool] = None) -> dict:
    """提交 Seedance 任务（**仅多模态参考模式 / 路径 B**）。返回 {task_id, raw}。

    本 SDK 已经统一只走「t2v + 多模态参考」一条路径——这是官方做精良
    商业短剧（如水果茶广告 demo）的标准姿势：
      - prompt 里详细描述本镜场景/动作/构图，并用文字暗示首尾帧
      - reference_images：最多 9 张图（角色三视图 + 关键道具 + 概念图等）
      - reference_video_url：链式生成第 N 段（N≥2）时把上一段视频塞进来，
        让模型看到完整动作连续性，比仅"末帧静止参考"稳得多
      - generate_audio=True → Seedance 2.0 原生生成同步音频
      - duration 4-15 秒；不传由模型自定（一般 5s）

    注意：**已不再支持首尾帧（i2v）模式**，原因：
      - first_frame/last_frame 与 reference_image/video 互斥，鱼与熊掌不可兼得
      - 多模态参考能塞 9 图 + 3 视频 + 3 音频，控制力远胜首尾帧
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
    body = {"model": MODEL_VIDEO, "content": content,
            "resolution": resolution, "ratio": ratio}
    if duration:
        body["duration"] = int(duration)
    if generate_audio:
        body["generate_audio"] = True
    if seed is not None:
        body["seed"] = int(seed)
    if camera_fixed is not None:
        body["camera_fixed"] = bool(camera_fixed)
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


def video_concat(clips: list, save_path: Path) -> dict:
    """硬拼接（无重编码）。要求所有 clip 编码参数一致。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = save_path.parent / f".concat_{int(time.time()*1000)}.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(c).resolve()}'" for c in clips), encoding="utf-8"
    )
    try:
        _run_ffmpeg(["-f", "concat", "-safe", "0",
                     "-i", str(list_file), "-c", "copy", str(save_path)])
    finally:
        list_file.unlink(missing_ok=True)
    return {"path": str(save_path)}


def video_crossfade(clip_a: str, clip_b: str, save_path: Path,
                    duration: float = 1.0, offset: float = 4.0,
                    transition: str = "fade", preset: str = "ultrafast") -> dict:
    """A/B 两段做转场（视频 xfade + 音频 acrossfade）。
    transition: xfade 转场类型（fade/wipeleft/slideup/circleopen/dissolve 等，见 ffmpeg xfade 文档）。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(clip_a), "-i", str(clip_b),
        "-filter_complex",
        f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
        f"[0:a][1:a]acrossfade=d={duration}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", preset,
        str(save_path),
    ])
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
    _run_ffmpeg([
        "-i", str(clip),
        "-filter_complex",
        f"[0:v]setpts={pts}*PTS[v];[0:a]{','.join(tempos)}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", preset,
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
    _run_ffmpeg([
        "-i", str(clip),
        "-vf",
        f"fade=t=in:st=0:d={fade_in},fade=t=out:st={fade_out_start}:d={fade_out}",
        "-af",
        f"afade=t=in:st=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}",
        "-c:v", "libx264", "-preset", preset,
        str(save_path),
    ])
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


def audio_amix(base_video: str, bgm_audio: str, save_path: Path,
               bgm_volume: float = 0.2) -> dict:
    """把 bgm_audio 混进 base_video 的音轨。"""
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        "-i", str(base_video), "-i", str(bgm_audio),
        "-filter_complex",
        f"[1:a]volume={bgm_volume}[bgm];"
        "[0:a][bgm]amix=inputs=2:duration=longest:dropout_transition=0[a]",
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
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        candidate = FFMPEG.replace("ffmpeg", "ffprobe")
        if os.path.exists(candidate):
            ffprobe = candidate
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


# ============== TTS / GenBGM stub（待 key） ==============
def tts(text: str, save_path: Path, voice: str = "default") -> dict:
    """豆包大模型语音合成。需 TTS_APP_ID + TTS_TOKEN。当前返回 stub。"""
    if not (get_extra("TTS_APP_ID") and get_extra("TTS_TOKEN")):
        raise RuntimeError(
            "TTS 凭证缺失：请到 https://console.volcengine.com/speech 开通"
            "「大模型语音合成」，把 TTS_APP_ID / TTS_TOKEN 填到 "
            "vibefilming.config.json"
        )
    raise NotImplementedError("TTS 实现待补：参考 smoke_tests/test_06_tts.py")


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


def gen_bgm(prompt: str, save_path: Path,
            duration: int = 60,
            segments: Optional[list] = None,
            *,
            poll: bool = True,
            poll_interval: float = 5.0,
            poll_timeout: float = 300.0,
            enable_input_rewrite: bool = False) -> dict:
    """火山引擎 GenBGM（Action=GenBGM, Version=2024-08-12, v5.0）。

    Args:
        prompt: Text 字段，自然语言描述风格/情绪/乐器/场景
        save_path: 落盘路径（默认 wav，但火山可能返回 mp4 容器，按内容决定）
        duration: 30-120 秒
        segments: 可选，[{"Name":"intro","Duration":10}, ...]，
                  Name ∈ intro/verse/chorus/inst/bridge/outro，
                  总和需 [30,120]，传入则覆盖 duration
        poll: True=同步阻塞到拿到 audio_url；False=只回 task_id
        poll_interval / poll_timeout: 轮询参数
        enable_input_rewrite: 是否让模型自动改写 prompt

    Returns:
        poll=True: {"path": str, "audio_url": str, "task_id": str,
                    "duration": float, "style_info": dict, "raw": dict}
        poll=False: {"task_id": str}

    Raises:
        RuntimeError: 凭证缺失 / API 错误 / 50000001 版权校验失败 / 轮询超时
    """
    submitted = submit_bgm_task(
        prompt, duration=duration, segments=segments,
        enable_input_rewrite=enable_input_rewrite,
    )
    task_id = submitted["task_id"]
    if not poll:
        return {"task_id": task_id}

    deadline = time.time() + poll_timeout
    last = None
    while time.time() < deadline:
        last = query_bgm_task(task_id, save_path=save_path)
        st = last["status"]
        if st == "succeeded":
            return last
        if st == "failed":
            raise RuntimeError(f"GenBGM 任务失败 task_id={task_id}: {last.get('error')}")
        time.sleep(poll_interval)
    raise TimeoutError(f"GenBGM 轮询超时 task_id={task_id}, last={last}")


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
