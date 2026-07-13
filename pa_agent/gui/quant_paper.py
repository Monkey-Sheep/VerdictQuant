"""GUI for arbitrary-stock PA analysis and paper validation."""
# ruff: noqa: RUF001
from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.quant.launcher import quant_cli_command
from pa_agent.quant.service import QuantPaperService


def _text(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


class QuantPaperWidget(QWidget):
    """Paper-only automated quant surface backed by the local SQLite ledger."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._service = QuantPaperService()
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        boundary = QLabel(
            "自动量化模拟：PA 负责分析，风控负责仓位，订单只能进入本地模拟账户。"
            "不会连接券商，也不会使用分析当根 K 线成交。"
        )
        boundary.setWordWrap(True)
        boundary.setStyleSheet(
            "background:#17324d; color:#9dd7ff; border:1px solid #29577f; padding:8px;"
        )
        layout.addWidget(boundary)

        form_row = QHBoxLayout()
        form = QFormLayout()
        self._market = QComboBox()
        self._market.addItem("美股", "us")
        self._market.addItem("A股", "a-share")
        self._market.currentIndexChanged.connect(self._on_market_changed)
        form.addRow("市场", self._market)
        self._symbol = QLineEdit("NVDA")
        self._symbol.setPlaceholderText("例如 NVDA 或 600519")
        form.addRow("股票代码", self._symbol)
        form_row.addLayout(form)

        form2 = QFormLayout()
        self._exchange = QLineEdit("NASDAQ")
        self._exchange.setPlaceholderText("NASDAQ / NYSE；A股留空")
        form2.addRow("交易所", self._exchange)
        self._timeframe = QComboBox()
        self._timeframe.addItems(["1d", "4h", "1h", "15m", "5m"])
        form2.addRow("周期", self._timeframe)
        self._bar_count = QSpinBox()
        self._bar_count.setRange(20, 5000)
        self._bar_count.setValue(100)
        form2.addRow("K线数量", self._bar_count)
        form_row.addLayout(form2)
        form_row.addStretch()

        buttons = QVBoxLayout()
        self._analyze = QPushButton("分析并进入模拟")
        self._analyze.setObjectName("primaryButton")
        self._analyze.clicked.connect(self._run_analysis)
        buttons.addWidget(self._analyze)
        self._settle = QPushButton("结算后续K线")
        self._settle.clicked.connect(self._run_settlement)
        buttons.addWidget(self._settle)
        self._watch = QPushButton("加入自动观察池")
        self._watch.clicked.connect(self._add_watch)
        buttons.addWidget(self._watch)
        self._cycle = QPushButton("运行观察池周期")
        self._cycle.clicked.connect(self._run_cycle)
        buttons.addWidget(self._cycle)
        self._refresh = QPushButton("刷新模拟账户")
        self._refresh.clicked.connect(self.refresh)
        buttons.addWidget(self._refresh)
        self._backup = QPushButton("备份账户")
        self._backup.setToolTip("备份本地模拟账本、风控参数和观察池")
        self._backup.clicked.connect(self._backup_account)
        buttons.addWidget(self._backup)
        self._restore = QPushButton("恢复账户")
        self._restore.setToolTip("从经过完整性校验的本地备份恢复")
        self._restore.clicked.connect(self._restore_account)
        buttons.addWidget(self._restore)
        self._reset = QPushButton("重置账户")
        self._reset.setToolTip("归档当前账本后创建新的本地模拟账户")
        self._reset.clicked.connect(self._reset_account)
        buttons.addWidget(self._reset)
        form_row.addLayout(buttons)
        layout.addLayout(form_row)

        self._account_summary = QLabel()
        self._account_summary.setStyleSheet("font-size:15px; font-weight:600; padding:5px 0;")
        layout.addWidget(self._account_summary)

        self._positions = self._table(
            ["市场", "代码", "数量", "成本", "最新价", "止损", "止盈", "持有K线"]
        )
        layout.addWidget(self._positions)
        self._signals = self._table(["时间", "市场", "代码", "方向", "状态", "原因"])
        layout.addWidget(self._signals)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMaximumHeight(130)
        self._output.setPlaceholderText("分析、风控和模拟成交结果会显示在这里")
        layout.addWidget(self._output)

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    def _on_market_changed(self) -> None:
        is_a_share = self._market.currentData() == "a-share"
        self._exchange.setEnabled(not is_a_share)
        if is_a_share:
            self._exchange.clear()
            self._symbol.setText("600519")
            self._timeframe.setCurrentText("1d")
        else:
            self._exchange.setText("NASDAQ")
            self._symbol.setText("NVDA")

    def refresh(self) -> None:
        status = self._service.status()
        account_parts = []
        for account in status["accounts"]:
            account_parts.append(
                f"{account['market']} {account['currency']}: "
                f"权益 {_text(account['equity'])} | 现金 {_text(account['cash'])} | "
                f"收益率 {_text(account['return_percent'])}%"
            )
        validation = status["validation"]
        profile_id = status["profile"]["profile_id"]
        account_parts.append(f"本地账户 {profile_id[:8]}")
        win_rate = validation["win_rate"]
        win_rate_text = "—" if win_rate is None else f"{float(win_rate) * 100:.1f}%"
        account_parts.append(
            f"已平仓 {validation['closed_trades']} | 胜率 {win_rate_text} | "
            f"证据样本 {'可复核' if validation['sample_reviewable'] else '未满30笔'} | "
            "禁止自动实盘"
        )
        account_parts.append(f"自动观察池 {len(status['config']['watchlist'])} 只")
        self._account_summary.setText("    ".join(account_parts))

        positions = status["positions"]
        self._positions.setRowCount(len(positions))
        for row_index, row in enumerate(positions):
            values = [
                row["market"],
                row["symbol"],
                _text(row["quantity"], 0),
                _text(row["avg_price"]),
                _text(row["last_price"]),
                _text(row["stop_loss"]),
                _text(row["take_profit"]),
                row["holding_bars"],
            ]
            for column, value in enumerate(values):
                self._positions.setItem(row_index, column, QTableWidgetItem(str(value)))

        signals = status["signals"]
        self._signals.setRowCount(len(signals))
        for row_index, row in enumerate(signals):
            values = [
                row["created_at_ms"],
                row["market"],
                row["symbol"],
                row["side"],
                row["status"],
                row["reason"],
            ]
            for column, value in enumerate(values):
                self._signals.setItem(row_index, column, QTableWidgetItem(str(value or "")))

    def _run_analysis(self) -> None:
        symbol = self._symbol.text().strip()
        if not symbol:
            self._output.setPlainText("请输入股票代码")
            return
        args = [
            "--pretty",
            "analyze",
            "--market",
            str(self._market.currentData()),
            "--symbol",
            symbol,
            "--timeframe",
            self._timeframe.currentText(),
            "--bar-count",
            str(self._bar_count.value()),
            "--predict-next-bar",
        ]
        exchange = self._exchange.text().strip()
        if exchange:
            args.extend(["--exchange", exchange])
        self._start(args, "正在获取公开行情并运行 PA 两阶段分析…")

    def _run_settlement(self) -> None:
        self._start(
            ["--pretty", "settle"],
            "正在用后续已收盘K线结算模拟订单…",
        )

    def _add_watch(self) -> None:
        args = [
            "--pretty", "watch-add",
            "--market", str(self._market.currentData()),
            "--symbol", self._symbol.text().strip(),
            "--timeframe", self._timeframe.currentText(),
            "--bar-count", str(self._bar_count.value()),
        ]
        exchange = self._exchange.text().strip()
        if exchange:
            args.extend(["--exchange", exchange])
        self._start(args, "正在加入自动观察池…")

    def _run_cycle(self) -> None:
        self._start(
            ["--pretty", "cycle"],
            "正在结算并分析自动观察池…",
        )

    def _backup_account(self) -> None:
        self._start(
            ["--pretty", "backup"],
            "正在创建本地模拟账户备份…",
        )

    def _restore_account(self) -> None:
        archive, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择模拟账户备份",
            "",
            "VerdictQuant 模拟账户备份 (*.zip)",
        )
        if not archive:
            return
        phrase = self._service.status()["local_account"]["restore_confirmation"]
        entered, accepted = QInputDialog.getText(
            self,
            "确认恢复模拟账户",
            f"恢复前会自动备份当前账户。请输入：\n{phrase}",
        )
        if not accepted:
            return
        if entered != phrase:
            QMessageBox.warning(self, "未恢复", "确认语不匹配，模拟账户没有变化。")
            return
        self._start(
            [
                "--pretty",
                "restore",
                "--archive",
                archive,
                "--confirm",
                phrase,
            ],
            "正在校验备份并恢复本地模拟账户…",
        )

    def _reset_account(self) -> None:
        phrase = self._service.status()["local_account"]["reset_confirmation"]
        entered, accepted = QInputDialog.getText(
            self,
            "确认重置模拟账户",
            f"当前账户会先自动备份。请输入：\n{phrase}",
        )
        if not accepted:
            return
        if entered != phrase:
            QMessageBox.warning(self, "未重置", "确认语不匹配，模拟账户没有变化。")
            return
        self._start(
            [
                "--pretty",
                "reset",
                "--confirm",
                phrase,
            ],
            "正在归档旧账本并创建新的本地模拟账户…",
        )

    def _start(self, args: list[str], message: str) -> None:
        if self._process is not None:
            self._output.setPlainText("已有模拟任务正在运行")
            return
        try:
            command = quant_cli_command(args)
        except FileNotFoundError as exc:
            self._output.setPlainText(str(exc))
            return
        self._set_enabled(False)
        self._output.setPlainText(message)
        self._process = QProcess(self)
        self._process.setProgram(command[0])
        self._process.setArguments(command[1:])
        self._process.finished.connect(self._finished)
        self._process.start()

    def _finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        process = self._process
        if process is None:
            return
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        process.deleteLater()
        self._process = None
        self._set_enabled(True)
        try:
            payload = json.loads(stdout)
            rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            rendered = stdout or stderr or f"任务返回代码 {exit_code}"
        self._output.setPlainText(rendered[-12000:])
        self._service = QuantPaperService()
        self.refresh()

    def _set_enabled(self, enabled: bool) -> None:
        self._analyze.setEnabled(enabled)
        self._settle.setEnabled(enabled)
        self._watch.setEnabled(enabled)
        self._cycle.setEnabled(enabled)
        self._refresh.setEnabled(enabled)
        self._backup.setEnabled(enabled)
        self._restore.setEnabled(enabled)
        self._reset.setEnabled(enabled)

    def shutdown(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        if not self._process.waitForFinished(2000):
            self._process.kill()
        self._process = None
