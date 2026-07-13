"""Resolve local proxy settings and apply them only to tvdatafeed WebSockets."""
from __future__ import annotations

import importlib
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import wraps
from urllib.parse import unquote, urlsplit

_PATCH_LOCK = threading.Lock()
_ORIGINAL_CREATE_CONNECTION = "_verdictquant_original_create_connection"


@dataclass(frozen=True, slots=True)
class TradingViewProxy:
    """A redaction-safe proxy endpoint for websocket-client."""

    source: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def proxy_type(self) -> str:
        if self.scheme in {"http", "https"}:
            return "http"
        if self.scheme == "socks":
            return "socks5h"
        return self.scheme

    @property
    def display(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.source}: {self.scheme}://{host}:{self.port}"

    def websocket_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "http_proxy_host": self.host,
            "http_proxy_port": self.port,
            "proxy_type": self.proxy_type,
        }
        if self.username:
            options["http_proxy_auth"] = (self.username, self.password)
        return options


def _parse_proxy(value: str, *, source: str, default_scheme: str = "http") -> TradingViewProxy:
    raw = value.strip()
    if not raw:
        raise ValueError("proxy is empty")
    parsed = urlsplit(raw if "://" in raw else f"{default_scheme}://{raw}")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks", "socks4", "socks4a", "socks5", "socks5h"}:
        raise ValueError(f"unsupported proxy scheme: {scheme or '(missing)'}")
    if not parsed.hostname:
        raise ValueError("proxy host is missing")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy port is invalid") from exc
    if port is None:
        port = 1080 if scheme.startswith("socks") else (443 if scheme == "https" else 80)
    return TradingViewProxy(
        source=source,
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def _windows_proxy_settings() -> tuple[bool, str, str]:
    if sys.platform != "win32":
        return False, "", ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except FileNotFoundError:
                server = ""
            try:
                pac_url = str(winreg.QueryValueEx(key, "AutoConfigURL")[0] or "")
            except FileNotFoundError:
                pac_url = ""
            return enabled, server, pac_url
    except (FileNotFoundError, OSError):
        return False, "", ""


def _parse_windows_proxy(server: str) -> TradingViewProxy:
    entries: dict[str, str] = {}
    for item in server.split(";"):
        key, separator, value = item.partition("=")
        if separator and value.strip():
            entries[key.strip().lower()] = value.strip()
    if entries:
        for key, scheme in (("https", "http"), ("http", "http"), ("socks", "socks5h")):
            if key in entries:
                return _parse_proxy(
                    entries[key],
                    source="Windows 系统代理",
                    default_scheme=scheme,
                )
        raise ValueError("Windows system proxy has no HTTPS, HTTP, or SOCKS endpoint")
    return _parse_proxy(server, source="Windows 系统代理")


def resolve_tradingview_proxy(
    *,
    environ: Mapping[str, str] | None = None,
    windows_settings: tuple[bool, str, str] | None = None,
) -> TradingViewProxy | None:
    """Resolve an explicit, environment, or Windows system proxy in that order."""
    env = os.environ if environ is None else environ
    candidates = (
        ("VERDICTQUANT_TRADINGVIEW_PROXY", "VerdictQuant 显式代理"),
        ("HTTPS_PROXY", "HTTPS_PROXY"),
        ("https_proxy", "https_proxy"),
        ("ALL_PROXY", "ALL_PROXY"),
        ("all_proxy", "all_proxy"),
    )
    for key, source in candidates:
        value = str(env.get(key, "") or "").strip()
        if value:
            return _parse_proxy(value, source=source)

    enabled, server, _pac_url = windows_settings or _windows_proxy_settings()
    if enabled and server.strip():
        return _parse_windows_proxy(server)
    return None


def tradingview_proxy_diagnostic(
    *,
    environ: Mapping[str, str] | None = None,
    windows_settings: tuple[bool, str, str] | None = None,
) -> str:
    """Return a secret-free description suitable for logs and support dialogs."""
    settings = windows_settings or _windows_proxy_settings()
    try:
        proxy = resolve_tradingview_proxy(environ=environ, windows_settings=settings)
    except ValueError as exc:
        return f"代理配置无效：{exc}"
    if proxy is not None:
        return f"已检测到 {proxy.display}（用户名/密码不会显示）"
    enabled, _server, pac_url = settings
    if pac_url and not enabled:
        return (
            "仅检测到 Windows PAC 自动代理；WebSocket 无法直接解析 PAC，"
            "请开启 TUN 或系统 HTTP/SOCKS 代理"
        )
    return (
        "未检测到可供 WebSocket 使用的代理；"
        "浏览器插件代理不会自动覆盖桌面程序"
    )


def configure_tvdatafeed_proxy() -> TradingViewProxy | None:
    """Patch tvdatafeed's module-local connection function with explicit proxy options."""
    try:
        proxy = resolve_tradingview_proxy()
    except ValueError:
        proxy = None
    module = importlib.import_module("tvDatafeed.main")
    with _PATCH_LOCK:
        original = getattr(module, _ORIGINAL_CREATE_CONNECTION, None)
        if original is None:
            original = module.create_connection
            setattr(module, _ORIGINAL_CREATE_CONNECTION, original)
        if proxy is None:
            module.create_connection = original
            return None

        options = proxy.websocket_options()

        @wraps(original)
        def _proxied_create_connection(url: str, *args, **kwargs):
            for key, value in options.items():
                kwargs.setdefault(key, value)
            return original(url, *args, **kwargs)

        module.create_connection = _proxied_create_connection
    return proxy
