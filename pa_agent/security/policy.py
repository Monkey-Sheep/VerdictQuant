"""Central security policy for provider and transport configuration."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

DEFAULT_PROVIDER_MODEL = "deepseek-v4-flash"
DEFAULT_PROVIDER_BASE_URL = "https://api.deepseek.com"

_DISABLED_MODEL_PREFIXES = ("openclaw",)
_DISABLED_PROVIDER_HOST_SUFFIXES = frozenset(
    {
        "copilot.tencent.com",
    }
)


def is_disabled_provider(model: str | None, base_url: str | None = None) -> bool:
    """Return True for connector routes disabled in the hardened build."""
    normalized_model = (model or "").strip().lower()
    if normalized_model.startswith(_DISABLED_MODEL_PREFIXES):
        return True

    try:
        host = (urlsplit((base_url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _DISABLED_PROVIDER_HOST_SUFFIXES
    )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def provider_configuration_error(model: str, base_url: str) -> str | None:
    """Return a user-facing policy error, or None when the route is allowed."""
    model = model.strip()
    base_url = base_url.strip()
    if not model:
        return "Model name is required."
    if is_disabled_provider(model, base_url):
        return (
            "QClaw, WorkBuddy, and Cursor agent routes are disabled in this "
            "hardened build because they can expose local credentials or tools."
        )
    if model.startswith(("http://", "https://")):
        return "Model and Base URL appear to be reversed."
    if not base_url:
        return "Base URL is required."
    if "\\" in base_url or any(ord(ch) < 32 or ch.isspace() for ch in base_url):
        return "Base URL must not contain whitespace, controls, or backslashes."

    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname or ""
        _ = parsed.port
    except ValueError:
        return "Base URL contains an invalid host or port."

    if parsed.scheme not in {"http", "https"} or not host:
        return "Base URL must be an absolute HTTP(S) URL."
    if parsed.username is not None or parsed.password is not None:
        return "Base URL must not contain embedded credentials."
    if parsed.query or parsed.fragment:
        return "Base URL must not contain a query string or fragment."
    if parsed.scheme == "http" and not _is_loopback_host(host):
        return "Plain HTTP is allowed only for loopback hosts; use HTTPS remotely."
    return None


def ensure_provider_allowed(model: str, base_url: str) -> None:
    """Raise ValueError when provider settings violate the hardened policy."""
    error = provider_configuration_error(model, base_url)
    if error:
        raise ValueError(error)
