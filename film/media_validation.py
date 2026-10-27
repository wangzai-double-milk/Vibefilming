"""Content validation for uploaded media and document files."""
from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path


_IMAGE_SIGNATURES = {
    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
    ".webp": lambda data: (
        len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP"
    ),
}


def _is_iso_bmff(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _validate_video_stream(path: Path) -> None:
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError(
            "视频上传校验需要安装 imageio-ffmpeg") from exc
    try:
        result = subprocess.run(
            [
                executable,
                "-nostdin",
                "-v", "error",
                "-i", str(path),
                "-map", "0:v:0",
                "-frames:v", "1",
                "-f", "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("视频容器解析超时") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(
            "视频容器损坏或不含可解码视频流"
            + (f"：{detail[:200]}" if detail else "")
        )


def _is_mp3(data: bytes) -> bool:
    return data.startswith(b"ID3") or (
        len(data) >= 2
        and data[0] == 0xFF
        and data[1] & 0xE0 == 0xE0
    )


def _validate_office(path: Path, suffix: str) -> str:
    expected = {
        ".docx": "word/",
        ".pptx": "ppt/",
        ".xlsx": "xl/",
    }[suffix]
    if not zipfile.is_zipfile(path):
        raise ValueError(f"{suffix} 文件不是有效的 Office ZIP 容器")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "[Content_Types].xml" not in names or not any(
                name.startswith(expected) for name in names
            ):
                raise ValueError(f"文件内容与 {suffix} 扩展名不一致")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"{suffix} 文件损坏") from exc
    return suffix.lstrip(".")


def validate_upload_content(
    path: Path, filename: str, media_kind: str,
) -> str:
    """Validate bytes against the declared extension and material kind."""
    source = Path(path)
    suffix = Path(filename).suffix.lower()
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("上传文件为空")
    with source.open("rb") as handle:
        header = handle.read(8192)

    if media_kind == "image":
        check = _IMAGE_SIGNATURES.get(suffix)
        if check is None or not check(header):
            raise ValueError("图片内容与扩展名不一致或格式损坏")
        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError as exc:
            raise RuntimeError("图片上传校验需要安装 Pillow") from exc
        expected = {
            ".png": "PNG",
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".gif": "GIF",
            ".webp": "WEBP",
        }[suffix]
        try:
            with Image.open(source) as image:
                detected = image.format
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError("图片内容损坏，无法完成解码校验") from exc
        if detected != expected:
            raise ValueError("图片内容与扩展名不一致")
        return suffix.lstrip(".")

    if media_kind == "video":
        if suffix not in {".mp4", ".mov"} or not _is_iso_bmff(header):
            raise ValueError("视频不是有效的 MP4/MOV 容器")
        _validate_video_stream(source)
        return "iso-bmff"

    if media_kind == "audio":
        valid = (
            suffix == ".wav"
            and len(header) >= 12
            and header.startswith(b"RIFF")
            and header[8:12] == b"WAVE"
        ) or (suffix == ".mp3" and _is_mp3(header))
        if not valid:
            raise ValueError("音频内容与扩展名不一致或格式损坏")
        return suffix.lstrip(".")

    if media_kind != "document":
        raise ValueError(f"未知素材类型：{media_kind}")
    if suffix == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise ValueError("PDF 内容与扩展名不一致或格式损坏")
        return "pdf"
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return _validate_office(source, suffix)
    if suffix not in {".txt", ".md", ".csv", ".json", ".srt"}:
        raise ValueError(f"不支持校验该文档格式：{suffix or '(无扩展名)'}")
    if b"\x00" in header:
        raise ValueError("文本文件包含二进制 NUL 字节")
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("文本文件必须使用 UTF-8 编码") from exc
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 文件内容无效") from exc
    return suffix.lstrip(".")
