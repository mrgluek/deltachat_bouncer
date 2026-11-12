"""Validate invite links before templates escape them for their output context."""
from urllib.parse import urlsplit


def invite_url(value: str) -> str:
    for prefix in ("OPEN-CHAT:", "OPEN:", "dcqr://"):
        if value.startswith(prefix):
            value = "https://i.delta.chat/#" + value[len(prefix):]
            break
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in ("http", "https"):
            return value if parts.netloc else ""
        if parts.scheme.lower() in ("dc", "openpgp4fpr"):
            return value
    except ValueError:
        pass
    return ""
