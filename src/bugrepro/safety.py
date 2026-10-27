"""Target validation for the local browser.

Only loopback HTTP(S) origins are accepted. This keeps the prototype from
being pointed at arbitrary internet hosts while the action schema is still
small and the executor is not sandboxed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_URL_LENGTH = 2048


class TargetError(ValueError):
    """Raised when a target URL or path is not allowed."""


def validate_base_url(raw: str) -> str:
    """Return a normalized loopback base URL such as ``http://127.0.0.1:8101``.

    Rejects credentials, paths, queries, fragments, whitespace tricks and any
    hostname that is not a literal loopback name.
    """
    if not isinstance(raw, str):
        raise TargetError("Base URL must be a string.")
    if not raw or raw != raw.strip():
        raise TargetError("Base URL must be a non-empty string without surrounding whitespace.")
    if len(raw) > _MAX_URL_LENGTH:
        raise TargetError("Base URL is too long.")
    if any(char in raw for char in "\r\n\t\\"):
        raise TargetError("Base URL contains a forbidden character.")

    parts = urlsplit(raw)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise TargetError("Base URL must use http or https.")
    if parts.username is not None or parts.password is not None:
        raise TargetError("Base URL must not contain credentials.")
    if parts.query or parts.fragment:
        raise TargetError("Base URL must not contain a query string or fragment.")
    if parts.path not in ("", "/"):
        raise TargetError("Base URL must not contain a path; use an action path instead.")

    host = parts.hostname
    if host is None:
        raise TargetError("Base URL must include a hostname.")
    if host.lower() not in LOOPBACK_HOSTS:
        raise TargetError(
            "Only loopback targets (127.0.0.1, localhost, ::1) are allowed."
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise TargetError("Base URL has an invalid port.") from exc
    if port is not None and not (1 <= port <= 65535):
        raise TargetError("Base URL port must be between 1 and 65535.")

    # Rebuild without a trailing slash so simple concatenation with paths is safe.
    return f"{parts.scheme}://{parts.netloc}"


def validate_path(path: str) -> str:
    """Validate a same-origin action path such as ``/profile`` or ``/search?q=1``."""
    if not isinstance(path, str) or not path:
        raise TargetError("Action path must be a non-empty string.")
    if len(path) > _MAX_URL_LENGTH:
        raise TargetError("Action path is too long.")
    if not path.startswith("/") or path.startswith("//"):
        raise TargetError("Action path must start with a single '/'.")
    if any(char in path for char in "\r\n\t\\\x00"):
        raise TargetError("Action path contains a forbidden character.")
    return path


def resolve_target(base_url: str, path: str) -> str:
    """Combine a validated base URL and path into a URL the browser may open."""
    base = validate_base_url(base_url)
    suffix = validate_path(path)
    return base + suffix


def is_loopback_url(url: str) -> bool:
    """True when a page-initiated request (including a redirect) stays on loopback."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in _ALLOWED_SCHEMES:
        return False
    if parts.hostname is None:
        return False
    return parts.hostname.lower() in LOOPBACK_HOSTS