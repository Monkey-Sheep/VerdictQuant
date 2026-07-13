"""Actionable diagnostics when TradingView's WebSocket cannot be reached."""
from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.data.tradingview_proxy import tradingview_proxy_diagnostic

_URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://)([^/@\s]+)@", re.IGNORECASE)


def _safe_detail(detail: str | None) -> str:
    text = (detail or "未返回底层异常").strip()
    text = _URL_CREDENTIALS.sub(r"\1***:***@", text)
    return text[:1600]


def build_tv_connectivity_message(
    detail: str | None = None,
    *,
    proxy_diagnostic: str | None = None,
) -> str:
    """Build a copyable, secret-free support message."""
    proxy = proxy_diagnostic or tradingview_proxy_diagnostic()
    return (
        "VerdictQuant 无法建立 TradingView WebSocket 连接。\n\n"
        "这不是 NVDA、COIN 或其他股票代码无效。程序先用 "
        "OANDA:XAUUSD 做通用连通性检查；该检查失败时，所有 TradingView "
        "标的都会被阻止。\n\n"
        "连接目标：wss://data.tradingview.com:443\n"
        f"失败详情：{_safe_detail(detail)}\n"
        f"代理检测：{proxy}\n\n"
        "处理方法：\n"
        "1. 将 VPN 开成全局/TUN 模式，或启用 Windows 系统 HTTP/SOCKS 代理；"
        "仅浏览器插件代理不会覆盖桌面程序。\n"
        "2. 确认代理允许 CONNECT 到 data.tradingview.com:443。\n"
        "3. 也可设置 VERDICTQUANT_TRADINGVIEW_PROXY，例如 "
        "http://127.0.0.1:7890，然后重启程序。\n"
        "4. 若仍失败，点击“复制诊断”发送完整信息；或临时切换到 MT5。"
    )


def show_tv_connectivity_blocked_dialog(
    parent: QWidget | None = None,
    *,
    detail: str | None = None,
) -> str:
    """Show the diagnostic dialog. Returns ``mt5`` or ``cancel``."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("TradingView 连接失败")
    dlg.setMinimumWidth(600)

    message = build_tv_connectivity_message(detail)
    layout = QVBoxLayout(dlg)
    label = QLabel(message)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(label)

    buttons = QHBoxLayout()
    btn_copy = QPushButton("复制诊断")
    buttons.addWidget(btn_copy)
    buttons.addStretch()
    btn_mt5 = QPushButton("切换到 MT5")
    btn_close = QPushButton("关闭")
    buttons.addWidget(btn_mt5)
    buttons.addWidget(btn_close)
    layout.addLayout(buttons)

    result = ["cancel"]

    def _pick(choice: str) -> None:
        result[0] = choice
        dlg.accept()

    def _copy() -> None:
        QApplication.clipboard().setText(message)
        btn_copy.setText("已复制")

    btn_copy.clicked.connect(_copy)
    btn_mt5.clicked.connect(lambda: _pick("mt5"))
    btn_close.clicked.connect(dlg.reject)

    dlg.exec()
    return result[0]
