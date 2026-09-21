"""Web-layer security helpers: IP-aware rate limiting, safe host/URL resolution,
and media content-type sniffing for the aiohttp channel preview / ActivityPub server.
"""
import functools
import mimetypes
import os
import re
import threading
import time

try:
    from aiohttp import web
except ImportError:
    web = None

import database

_rate_limit_lock = threading.Lock()
_rate_limits: dict[str, list[float]] = {}  # key -> [timestamps]
_TRUSTED_PROXIES = {"127.0.0.1", "::1", "localhost", "testclient"}


def _get_client_ip(request) -> str:
    """Extract client IP, trusting X-Forwarded-For only from trusted local reverse proxies."""
    peer_ip = getattr(request, "remote", None)
    if isinstance(peer_ip, str):
        peer_ip = peer_ip.strip()
    else:
        peer_ip = "unknown"

    # Only trust X-Forwarded-For if incoming peer is a trusted local reverse proxy (Caddy / Nginx)
    # or if peer_ip is unknown (e.g. in test mock environments)
    if peer_ip in _TRUSTED_PROXIES or peer_ip.startswith("127.") or peer_ip == "unknown":
        forwarded = request.headers.get("X-Forwarded-For") if hasattr(request, "headers") else None
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
            if client_ip:
                return client_ip
    return peer_ip


def check_rate_limit(request, bucket: str, max_requests: int = 60, window_seconds: int = 60) -> bool:
    """In-memory sliding window rate limiter per client IP and bucket.
    Returns True if allowed, False if limit exceeded.
    """
    client_ip = _get_client_ip(request)
    key = f"{bucket}:{client_ip}"
    now = time.time()
    cutoff = now - window_seconds

    with _rate_limit_lock:
        timestamps = _rate_limits.get(key, [])
        timestamps = [ts for ts in timestamps if ts > cutoff]
        if len(timestamps) >= max_requests:
            _rate_limits[key] = timestamps
            return False
        timestamps.append(now)
        _rate_limits[key] = timestamps

        if len(_rate_limits) > 5000:
            keys_to_del = [k for k, v in _rate_limits.items() if not v or v[-1] <= cutoff]
            for k in keys_to_del:
                del _rate_limits[k]

    return True


def rate_limited(bucket: str, max_requests: int = 60, window_seconds: int = 60):
    """Decorator to apply sliding window rate limiting to an aiohttp handler."""
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapped(request, *args, **kwargs):
            if not check_rate_limit(request, bucket, max_requests=max_requests, window_seconds=window_seconds):
                return web.Response(status=429, text="Too Many Requests", headers={"Retry-After": "60"})
            return await handler(request, *args, **kwargs)
        return wrapped
    return decorator


_EXTENSION_MIME_MAP = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".m4a": "audio/m4a",
    ".pdf": "application/pdf",
}

def _guess_media_content_type(filepath: str) -> str:
    if os.path.isfile(filepath):
        try:
            with open(filepath, "rb") as f:
                header = f.read(16)
            if header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP":
                return "image/webp"
            if header.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png"
            if header.startswith(b"\xff\xd8\xff"):
                return "image/jpeg"
            if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
                return "image/gif"
        except Exception:
            pass
    _, ext = os.path.splitext(filepath.lower())
    if ext in _EXTENSION_MIME_MAP:
        return _EXTENSION_MIME_MAP[ext]
    guessed, _ = mimetypes.guess_type(filepath)
    return guessed or "application/octet-stream"


SAFE_HOST_REGEX = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9.\-]*[a-zA-Z0-9])?(:\d{1,5})?$')


def _get_base_url(request) -> str:
    """Resolve the base URL for web handlers with safe host and scheme validation."""
    base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
    if not base_url:
        scheme = request.headers.get("X-Forwarded-Proto", getattr(request, "scheme", "http"))
        if scheme not in ("http", "https"):
            scheme = "http"
        host = request.headers.get("X-Forwarded-Host")
        if not host:
            raw_host = getattr(request, "host", None)
            if isinstance(raw_host, str):
                host = raw_host
            else:
                host = request.headers.get("Host", "localhost")
        if not host or not SAFE_HOST_REGEX.match(str(host).strip()):
            host = "localhost"
        base_url = f"{scheme}://{str(host).strip()}"
    return base_url.strip().rstrip("/")
