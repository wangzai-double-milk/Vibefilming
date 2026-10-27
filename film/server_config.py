"""Resolve Studio/Canvas bind addresses separately from browser-facing URLs."""
from __future__ import annotations

from dataclasses import dataclass
from http.server import ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

CONFIG_JSON = Path(__file__).resolve().parent.parent / "vibefilming.config.json"
_OVERRIDES: dict[str, dict] = {}


def is_loopback_host(host: str) -> bool:
    candidate = str(host or "").strip().removeprefix("[").removesuffix("]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def require_explicit_remote_bind(service: str, host: str) -> None:
    """Unauthenticated local UIs need an explicit opt-in before remote bind."""
    if is_loopback_host(host):
        return
    variable = f"VIBEFILMING_ALLOW_UNAUTHENTICATED_REMOTE_{service.upper()}"
    if os.environ.get(variable, "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError(
            f"{service} 默认只允许回环监听；如已通过防火墙或认证代理隔离，"
            f"请显式设置 {variable}=1 后再绑定 {host}"
        )


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    public_url: str = ""

    def browser_url(self, bound_port: int, page: str = "") -> str:
        if self.public_url:
            base = self.public_url.rstrip("/")
            return base if page and base.endswith("/" + page) else base + "/" + page
        host = "127.0.0.1" if self.host in ("", "0.0.0.0") else self.host
        if host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{bound_port}/{page}"


def configure_server_overrides(**services: dict) -> None:
    """Install explicit CLI choices before any tool can start a Canvas server."""
    for service, values in services.items():
        if service not in ("studio", "canvas"):
            raise ValueError(f"Unknown server: {service}")
        _OVERRIDES[service] = {k: v for k, v in values.items() if v is not None}


def resolve_server_settings(
    service: str, *, host: str | None = None, port: int | None = None,
    public_url: str | None = None, config_path: Path | None = None,
) -> ServerSettings:
    """Explicit call/CLI > environment > JSON > loopback defaults."""
    if service not in ("studio", "canvas"):
        raise ValueError(f"Unknown server: {service}")
    path = config_path or CONFIG_JSON
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = config.get("server", {})
    if not isinstance(servers, dict):
        raise ValueError("server must be a JSON object")
    section = servers.get(service, {})
    if not isinstance(section, dict):
        raise ValueError(f"server.{service} must be a JSON object")
    explicit = {**_OVERRIDES.get(service, {}),
                **{k: v for k, v in dict(host=host, port=port, public_url=public_url).items()
                   if v is not None}}

    def value(key, default):
        env = os.environ.get(f"VIBEFILMING_{service.upper()}_{key.upper()}")
        if key in explicit:
            return explicit[key]
        if env is not None and (env != "" or key == "public_url"):
            return env
        result = section.get(key)
        return default if result is None else result

    default_host = "127.0.0.1"
    bind_host = str(value("host", default_host)).strip()
    if not bind_host or any(c in bind_host for c in "/ \t\r\n"):
        raise ValueError(f"server.{service}.host must be a hostname or IP, without scheme/port")
    bind_host = bind_host.removeprefix("[").removesuffix("]")
    raw_port = value("port", 8764 if service == "studio" else 8765)
    if isinstance(raw_port, bool) or not str(raw_port).isdigit():
        raise ValueError(f"server.{service}.port must be an integer from 0 to 65535")
    bind_port = int(raw_port)
    if not 0 <= bind_port <= 65535:
        raise ValueError(f"server.{service}.port must be from 0 to 65535")
    external = str(value("public_url", "")).strip()
    if external:
        parsed = urlsplit(external)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError(f"server.{service}.public_url must be an HTTP(S) base URL")
        _ = parsed.port  # Validate malformed port without exposing config credentials.
    return ServerSettings(bind_host, bind_port, external)


def bind_http_server(settings: ServerSettings, handler, *, fallback: bool = True):
    """Support IPv4/IPv6; public URLs keep the requested port stable."""
    server_type = ThreadingHTTPServer
    if ":" in settings.host:
        class IPv6HTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6HTTPServer
    candidates = ([settings.port] if settings.port == 0 or settings.public_url or not fallback
                  else range(settings.port, min(settings.port + 20, 65536)))
    last_error = None
    for candidate in candidates:
        try:
            return server_type((settings.host, candidate), handler)
        except OSError as exc:
            last_error = exc
    raise OSError(f"Could not bind {settings.host}:{settings.port}") from last_error
