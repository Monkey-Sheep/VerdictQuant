from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.data import tradingview_proxy
from pa_agent.data.tradingview_proxy import (
    configure_tvdatafeed_proxy,
    resolve_tradingview_proxy,
    tradingview_proxy_diagnostic,
)
from pa_agent.gui.tv_connectivity_dialog import build_tv_connectivity_message


def test_explicit_proxy_has_priority_and_redacts_credentials() -> None:
    proxy = resolve_tradingview_proxy(
        environ={
            "VERDICTQUANT_TRADINGVIEW_PROXY": "http://user:secret@127.0.0.1:7890",
            "HTTPS_PROXY": "http://ignored:8888",
        },
        windows_settings=(True, "127.0.0.1:9999", ""),
    )

    assert proxy is not None
    assert proxy.host == "127.0.0.1"
    assert proxy.port == 7890
    assert proxy.websocket_options()["http_proxy_auth"] == ("user", "secret")
    assert "user" not in proxy.display
    assert "secret" not in proxy.display


def test_windows_https_proxy_is_usable_for_websocket() -> None:
    proxy = resolve_tradingview_proxy(
        environ={},
        windows_settings=(
            True,
            "http=127.0.0.1:7890;https=127.0.0.1:7891;socks=127.0.0.1:7892",
            "",
        ),
    )

    assert proxy is not None
    assert (proxy.host, proxy.port, proxy.proxy_type) == ("127.0.0.1", 7891, "http")


def test_pac_only_diagnostic_explains_limitation() -> None:
    diagnostic = tradingview_proxy_diagnostic(
        environ={},
        windows_settings=(False, "", "http://127.0.0.1/proxy.pac"),
    )

    assert "PAC" in diagnostic
    assert "TUN" in diagnostic


def test_configure_injects_explicit_proxy_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    def _original(url: str, *args, **kwargs):
        del args
        calls.append((url, kwargs))
        return "connected"

    fake_module = SimpleNamespace(create_connection=_original)
    monkeypatch.setenv("VERDICTQUANT_TRADINGVIEW_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setattr(
        tradingview_proxy.importlib,
        "import_module",
        lambda _name: fake_module,
    )

    configure_tvdatafeed_proxy()
    result = fake_module.create_connection("wss://data.tradingview.com")

    assert result == "connected"
    assert calls == [
        (
            "wss://data.tradingview.com",
            {
                "http_proxy_host": "127.0.0.1",
                "http_proxy_port": 1080,
                "proxy_type": "socks5h",
            },
        )
    ]


def test_connectivity_message_is_actionable_and_has_no_upstream_promotion() -> None:
    message = build_tv_connectivity_message(
        "WebSocketProxyException: http://user:password@127.0.0.1:7890",
        proxy_diagnostic="已检测到 Windows 系统代理: http://127.0.0.1:7890",
    )

    assert "NVDA、COIN" in message
    assert "data.tradingview.com:443" in message
    assert "user:password" not in message
    assert "***:***@" in message
    assert "my.feishu" not in message
    assert "云服务器" not in message
