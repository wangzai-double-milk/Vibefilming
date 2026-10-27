"""可选 TOS 备份和模型参考传输；本地项目交付无需启用。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


_UNSET = object()
_SYNC_STATE = ".tos_sync.json"
_SYNC_LOCK = ".tos_sync.lock"
_SYNC_EXCLUDES = {
    _SYNC_STATE, _SYNC_LOCK, ".manifest.lock", ".active_project",
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def is_configured() -> bool:
    return bool(
        _env("TOS_ACCESS_KEY")
        and _env("TOS_SECRET_KEY")
        and _env("TOS_BUCKET")
    )


def is_required() -> bool:
    return _env("VIBEFILMING_REQUIRE_PUBLIC_OUTPUT", "0").lower() in {
        "1", "true", "yes",
    }


def _client(endpoint: str):
    try:
        import tos
    except ImportError as exc:
        raise RuntimeError(
            "TOS 已配置，但运行环境未安装 tos SDK；请安装 tos>=2.8"
        ) from exc
    options = {}
    token = _env("TOS_SECURITY_TOKEN")
    if token:
        options["security_token"] = token
    return tos.TosClientV2(
        _env("TOS_ACCESS_KEY"),
        _env("TOS_SECRET_KEY"),
        endpoint,
        _env("TOS_REGION", "cn-beijing"),
        **options,
    )


def _safe_relative(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/").lstrip("/"))
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"TOS 相对路径非法：{value!r}")
    return path.as_posix()


def build_object_key(relative: str, session_id: object = _UNSET) -> str:
    """生成带命名空间的对象键；session_id="" 表示部署级共享状态。"""
    prefix = _env("TOS_PREFIX", "vibefilming").strip("/")
    session = (
        ""
        if session_id is _UNSET
        else str(session_id or "").strip()
    )
    clean = _safe_relative(relative)
    return "/".join(part for part in (prefix, session.strip("/"), clean) if part)


def _clients():
    endpoint = _env("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
    public_endpoint = _env("TOS_PUBLIC_ENDPOINT", endpoint)
    client = _client(endpoint)
    signer = client if public_endpoint == endpoint else _client(public_endpoint)
    return client, signer


def _signed_url(signer, key: str) -> str:
    import tos

    signed = signer.pre_signed_url(
        tos.HttpMethodType.Http_Method_Get,
        _env("TOS_BUCKET"),
        key,
        expires=max(60, int(_env("TOS_URL_EXPIRE", "604800"))),
    )
    return (
        getattr(signed, "signed_url", None)
        or getattr(signed, "signedUrl", None)
        or str(signed)
    )


def url_for_key(relative: str, session_id: object = _UNSET) -> Optional[str]:
    if not is_configured():
        return None
    _, signer = _clients()
    return _signed_url(signer, build_object_key(relative, session_id=session_id))


def url_for_full_key(key: str) -> str:
    if not is_configured():
        raise RuntimeError("TOS 未配置")
    _, signer = _clients()
    return _signed_url(signer, str(key).lstrip("/"))


def publish(local_path, key: Optional[str] = None,
            session_id: object = _UNSET) -> Optional[str]:
    """上传本地文件并返回预签名 URL。"""
    path = Path(local_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"待发布产物不存在：{path}")
    if not is_configured():
        return None
    client, signer = _clients()
    object_key = build_object_key(key or path.name, session_id=session_id)
    extra = {}
    content_type, _ = mimetypes.guess_type(path.name)
    if content_type:
        extra["content_type"] = content_type
    try:
        client.put_object_from_file(
            _env("TOS_BUCKET"), object_key, str(path), **extra)
    except TypeError:
        client.put_object_from_file(_env("TOS_BUCKET"), object_key, str(path))
    return _signed_url(signer, object_key)


def put_bytes(data: bytes, key: str, *, content_type: str = "",
              session_id: object = _UNSET) -> str:
    if not is_configured():
        raise RuntimeError("TOS 未配置")
    client, signer = _clients()
    object_key = build_object_key(key, session_id=session_id)
    options = {"content_type": content_type} if content_type else {}
    client.put_object(
        _env("TOS_BUCKET"), object_key, content=data, **options)
    return _signed_url(signer, object_key)


def put_json(value: Any, key: str, session_id: object = _UNSET) -> str:
    return put_bytes(
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
        key,
        content_type="application/json; charset=utf-8",
        session_id=session_id,
    )


def _read_output(output) -> bytes:
    if hasattr(output, "read"):
        value = output.read()
    else:
        value = getattr(output, "content", b"")
        if hasattr(value, "read"):
            value = value.read()
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value or b"")


def get_bytes(key: str, session_id: object = _UNSET) -> bytes:
    if not is_configured():
        raise RuntimeError("TOS 未配置")
    client, _ = _clients()
    output = client.get_object(
        _env("TOS_BUCKET"),
        build_object_key(key, session_id=session_id),
    )
    return _read_output(output)


def get_json(key: str, session_id: object = _UNSET,
             default: Any = None, *, strict: bool = False) -> Any:
    try:
        return json.loads(get_bytes(key, session_id=session_id).decode("utf-8"))
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        error_code = str(getattr(exc, "code", "") or "")
        missing = status_code in {404, "404"} or error_code in {
            "NoSuchKey", "NoSuchObject",
        }
        if strict and not missing:
            raise
        return default


def list_objects(prefix: str, *, session_id: object = _UNSET,
                 limit: Optional[int] = None) -> list[dict]:
    if not is_configured():
        return []
    client, signer = _clients()
    object_prefix = build_object_key(prefix, session_id=session_id).rstrip("/") + "/"
    items = []
    token = ""
    seen_tokens = set()
    maximum = max(1, int(limit)) if limit is not None else None
    while maximum is None or len(items) < maximum:
        options = {"continuation_token": token} if token else {}
        result = client.list_objects_type2(
            _env("TOS_BUCKET"), prefix=object_prefix,
            max_keys=min(1000, maximum - len(items)) if maximum else 1000,
            **options,
        )
        for obj in getattr(result, "contents", None) or []:
            key = str(getattr(obj, "key", "") or "")
            if not key.startswith(object_prefix) or key.endswith("/"):
                continue
            items.append({
                "key": key,
                "filename": PurePosixPath(key).name,
                "url": _signed_url(signer, key),
                "size": int(getattr(obj, "size", 0) or 0),
                "etag": str(getattr(obj, "etag", "") or "").strip('"'),
                "last_modified": str(getattr(obj, "last_modified", "") or ""),
            })
        if not getattr(result, "is_truncated", False):
            break
        token = str(getattr(result, "next_continuation_token", "") or "")
        if not token or token in seen_tokens:
            raise RuntimeError("TOS 分页游标缺失或重复，无法读取完整项目")
        seen_tokens.add(token)
    return items


def delete_full_key(key: str, *, allowed_prefix: str = "") -> None:
    if not is_configured():
        raise RuntimeError("TOS 未配置")
    full_key = str(key or "").lstrip("/")
    configured_prefix = _env("TOS_PREFIX", "vibefilming").strip("/")
    required_prefix = "/".join(
        part for part in (configured_prefix, allowed_prefix.strip("/")) if part)
    if not full_key or (
        required_prefix and not full_key.startswith(required_prefix + "/")
        and full_key != required_prefix
    ):
        raise ValueError(f"TOS key 不属于允许前缀：{key!r}")
    client, _ = _clients()
    client.delete_object(_env("TOS_BUCKET"), full_key)


def _download_full_key(full_key: str, target: Path) -> None:
    client, _ = _clients()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.get_object_to_file(_env("TOS_BUCKET"), full_key, str(target))
    except AttributeError:
        target.write_bytes(_read_output(
            client.get_object(_env("TOS_BUCKET"), full_key)))


def restore_directory(remote_prefix: str, local_root: Path,
                      session_id: object = _UNSET) -> int:
    """把远端项目前缀恢复到本地可写目录。"""
    root = Path(local_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    snapshot = get_json(
        f"{remote_prefix.rstrip('/')}/{_SYNC_STATE}",
        session_id=session_id,
        default=None,
        strict=True,
    )
    if (
        isinstance(snapshot, dict)
        and snapshot.get("version") == 2
        and isinstance(snapshot.get("files"), dict)
    ):
        files = snapshot["files"]
        for relative, metadata in files.items():
            clean = _safe_relative(relative)
            if not isinstance(metadata, dict):
                raise ValueError("TOS 项目快照文件元数据格式错误")
            object_path = _safe_relative(metadata.get("object", ""))
            target = (root / clean).resolve()
            target.relative_to(root)
            _download_full_key(
                build_object_key(
                    f"{remote_prefix.rstrip('/')}/{object_path}",
                    session_id=session_id,
                ),
                target,
            )
            actual = _fingerprint(target)
            if actual.get("size") != metadata.get("size") or (
                metadata.get("sha256")
                and actual.get("sha256") != metadata.get("sha256")
            ):
                raise RuntimeError(f"TOS 项目快照校验失败：{clean}")
        (root / _SYNC_STATE).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return len(files) + 1

    # Backward-compatible restore for legacy snapshots with stable object keys.
    objects = list_objects(remote_prefix, session_id=session_id)
    full_prefix = build_object_key(
        remote_prefix, session_id=session_id).rstrip("/") + "/"
    count = 0
    snapshot = None
    index_key = full_prefix + _SYNC_STATE
    if any(item["key"] == index_key for item in objects):
        index_path = Path(local_root).resolve() / _SYNC_STATE
        _download_full_key(index_key, index_path)
        snapshot = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("TOS 项目快照索引格式错误")
        count += 1
        available = {item["key"][len(full_prefix):] for item in objects}
        if set(snapshot) - available:
            raise RuntimeError("TOS 项目快照缺少已登记文件，请重新同步完整项目")
    for item in objects:
        full_key = item["key"]
        if not full_key.startswith(full_prefix):
            continue
        relative = _safe_relative(full_key[len(full_prefix):])
        if relative == _SYNC_STATE and snapshot is not None:
            continue
        if snapshot is not None and relative not in snapshot:
            continue
        target = (Path(local_root).resolve() / relative).resolve()
        target.relative_to(Path(local_root).resolve())
        _download_full_key(full_key, target)
        count += 1
    return count


def _fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


@contextlib.contextmanager
def _sync_lock(root: Path):
    lock_path = root / _SYNC_LOCK
    with lock_path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _snapshot_fingerprint(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("size", "mtime_ns", "sha256")
        if key in value
    }


def _write_sync_state(path: Path, state: dict) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(json.dumps(state, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def sync_directory(local_root: Path, remote_prefix: str,
                   session_id: object = _UNSET) -> dict:
    """Publish immutable file objects, then commit one snapshot index."""
    root = Path(local_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"待同步目录不存在：{root}")
    if not is_configured():
        return {"configured": False, "uploaded": 0, "skipped": 0}
    with _sync_lock(root):
        state_path = root / _SYNC_STATE
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        previous_files = (
            previous.get("files", {})
            if previous.get("version") == 2
            and isinstance(previous.get("files"), dict)
            else previous if isinstance(previous, dict) else {}
        )
        paths = []
        fingerprints = {}
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file() or path.name in _SYNC_EXCLUDES
                or path.name.startswith((".artifact.", ".artifact_refs-"))
            ):
                continue
            path.resolve().relative_to(root)
            relative = path.relative_to(root).as_posix()
            paths.append((path, relative))
            fingerprints[relative] = _fingerprint(path)

        revision = f"{time.time_ns()}-{os.getpid()}"
        current = {}
        uploaded = 0
        skipped = 0
        urls = {}
        for path, relative in paths:
            fingerprint = fingerprints[relative]
            previous_entry = previous_files.get(relative)
            previous_object = (
                previous_entry.get("object")
                if isinstance(previous_entry, dict) else None
            )
            if (
                previous_object
                and _snapshot_fingerprint(previous_entry) == fingerprint
            ):
                current[relative] = {
                    **fingerprint,
                    "object": _safe_relative(previous_object),
                }
                skipped += 1
                urls[relative] = url_for_key(
                    f"{remote_prefix.rstrip('/')}/{relative}",
                    session_id=session_id,
                )
                continue

            object_path = (
                f".snapshots/{revision}/{relative}"
            )
            publish(
                path,
                key=f"{remote_prefix.rstrip('/')}/{object_path}",
                session_id=session_id,
            )
            if _fingerprint(path) != fingerprint:
                raise RuntimeError(
                    f"同步期间文件发生变化，快照未提交：{relative}")
            remote_key = f"{remote_prefix.rstrip('/')}/{relative}"
            url = publish(path, key=remote_key, session_id=session_id)
            if url:
                urls[relative] = url
            current[relative] = {
                **fingerprint,
                "object": object_path,
            }
            uploaded += 1

        snapshot = {
            "version": 2,
            "revision": revision,
            "created_at_ns": time.time_ns(),
            "files": current,
        }
        put_json(
            snapshot,
            f"{remote_prefix.rstrip('/')}/{_SYNC_STATE}",
            session_id=session_id,
        )
        _write_sync_state(state_path, snapshot)
        return {
            "configured": True,
            "uploaded": uploaded,
            "skipped": skipped,
            "urls": urls,
            "revision": revision,
        }


def status() -> dict:
    try:
        import tos  # noqa: F401
        sdk_ready = True
    except ImportError:
        sdk_ready = False
    return {
        "configured": is_configured(),
        "required": False,
        "output_required": is_required(),
        "sdk_ready": sdk_ready,
        "ready": bool(is_configured() and sdk_ready),
        "bucket": _env("TOS_BUCKET"),
        "region": _env("TOS_REGION", "cn-beijing"),
        "prefix": _env("TOS_PREFIX", "vibefilming"),
    }
