"""Integrated US stock, A-share, and Polymarket research center."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import QProcess, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pa_agent.integrations.finance_workspace import (
    FinanceWorkspaceError,
    load_finance_snapshot,
    paper_command,
    resolve_finance_workspace,
)


def _number(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _percent(value: object) -> str:
    return "—" if value is None else f"{_number(value)}%"


def _item(value: object, *, color: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem("—" if value is None else str(value))
    if color:
        item.setForeground(QColor(color))
    return item


class FinanceHubWidget(QWidget):
    """Read-only evidence dashboard with allowlisted paper workflow controls."""

    symbol_selected = pyqtSignal(str, str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace: Path | None = None
        self._snapshot: dict[str, Any] = {}
        self._process: QProcess | None = None
        self._process_scope = ""
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("综合研究中心")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        subtitle = QLabel("PA价格行为 + 外部股票研究/回测 + Polymarket模拟账本")
        subtitle.setObjectName("mutedLabel")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self._refresh_btn = QPushButton("刷新证据")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        self._run_stock_btn = QPushButton("运行股票模拟")
        self._run_stock_btn.setObjectName("primaryButton")
        self._run_stock_btn.clicked.connect(lambda: self._start_cycle("stocks"))
        header.addWidget(self._run_stock_btn)
        self._run_poly_btn = QPushButton("运行 Polymarket 模拟")
        self._run_poly_btn.setObjectName("primaryButton")
        self._run_poly_btn.clicked.connect(lambda: self._start_cycle("polymarket"))
        header.addWidget(self._run_poly_btn)
        self._btc_btn = QPushButton("载入 BTC 5m 到 PA")
        self._btc_btn.clicked.connect(
            lambda: self.symbol_selected.emit("crypto", "BTCUSDT", "BINANCE")
        )
        header.addWidget(self._btc_btn)
        layout.addLayout(header)

        boundary = QLabel(
            "纸面研究边界: 禁止连接银行、券商、钱包、私钥和真实下单接口。"
            "表格中的策略状态不是买卖指令。"
        )
        boundary.setWordWrap(True)
        boundary.setStyleSheet(
            "background:#3b2d10; color:#f2c66d; border:1px solid #6f5822; "
            "padding:8px 10px; font-weight:600;"
        )
        layout.addWidget(boundary)

        summary = QGroupBox("当前闭环")
        summary_layout = QGridLayout(summary)
        self._summary_labels: dict[str, QLabel] = {}
        metrics = [
            ("stocks", "股票验证"),
            ("a_share", "A股执行模型"),
            ("polymarket", "Polymarket验证"),
            ("boundary", "真实执行"),
        ]
        for column, (key, label_text) in enumerate(metrics):
            label = QLabel(label_text)
            label.setObjectName("mutedLabel")
            value = QLabel("—")
            value.setStyleSheet("font-size:16px; font-weight:700; padding:2px 0 8px;")
            summary_layout.addWidget(label, 0, column)
            summary_layout.addWidget(value, 1, column)
            self._summary_labels[key] = value
        layout.addWidget(summary)

        self._tabs = QTabWidget()
        from pa_agent.gui.manual_monitor import ManualMonitorWidget
        self._manual_monitor = ManualMonitorWidget(self)
        self._tabs.addTab(self._manual_monitor, "组合监控")
        self._us_table = self._make_table(
            ["代码", "最新收盘", "日期", "K线数", "策略共识", "买入持有", "状态"]
        )
        self._us_table.cellDoubleClicked.connect(self._on_us_double_clicked)
        self._tabs.addTab(self._us_table, "美股")

        self._a_table = self._make_table(
            ["代码", "最新收盘", "日期", "K线数", "策略共识", "买入持有", "状态"]
        )
        self._a_table.cellDoubleClicked.connect(self._on_a_share_double_clicked)
        self._tabs.addTab(self._a_table, "A股")

        self._poly_table = self._make_table(
            ["策略", "总价值", "盈亏", "ROI", "交易", "持仓", "开放订单", "新增交易"]
        )
        self._tabs.addTab(self._poly_table, "Polymarket")

        self._evidence_table = self._make_table(["证据", "修改时间", "大小", "状态"])
        self._tabs.addTab(self._evidence_table, "证据与复盘")

        self._research_text = QPlainTextEdit()
        self._research_text.setReadOnly(True)
        self._research_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._tabs.addTab(self._research_text, "主动研究队列")

        self._portfolio_table = self._make_table(["资产", "数量", "币种", "用途", "执行边界"])
        self._tabs.addTab(self._portfolio_table, "组合与基金")

        self._engine_table = self._make_table(["项目", "职责", "许可证", "接入方式", "安装"])
        self._tabs.addTab(self._engine_table, "集成引擎")
        from pa_agent.gui.quant_paper import QuantPaperWidget

        self._quant_paper = QuantPaperWidget(self)
        self._tabs.addTab(self._quant_paper, "自动量化模拟")
        layout.addWidget(self._tabs, 1)

        footer = QHBoxLayout()
        self._status = QLabel("准备读取证据")
        self._status.setObjectName("mutedLabel")
        footer.addWidget(self._status, 1)
        self._workspace_label = QLabel("")
        self._workspace_label.setObjectName("mutedLabel")
        footer.addWidget(self._workspace_label)
        layout.addLayout(footer)

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    def refresh(self) -> None:
        try:
            self._workspace = resolve_finance_workspace()
            self._snapshot = load_finance_snapshot(self._workspace)
        except FinanceWorkspaceError as exc:
            self._status.setText(str(exc))
            self._set_run_enabled(False)
            return
        self._set_run_enabled(self._process is None)
        self._workspace_label.setText(str(self._workspace))
        self._populate_summary()
        self._populate_stock_table(self._us_table, self._snapshot["us"]["rows"])
        self._populate_stock_table(self._a_table, self._snapshot["a_shares"]["rows"])
        self._populate_polymarket()
        self._populate_evidence()
        self._populate_research()
        self._populate_portfolio()
        self._populate_engines()
        self._status.setText(f"证据已刷新: {self._snapshot['generated_at']}")

    def _populate_summary(self) -> None:
        self._summary_labels["stocks"].setText(self._snapshot["us"]["promotion_state"])
        self._summary_labels["a_share"].setText(self._snapshot["a_shares"]["rqalpha_status"])
        self._summary_labels["polymarket"].setText(
            self._snapshot["polymarket"]["promotion_state"]
        )
        self._summary_labels["boundary"].setText("已禁用")

    @staticmethod
    def _populate_stock_table(table: QTableWidget, rows: list[dict[str, Any]]) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            consensus = row["consensus"]
            color = "#3fb950" if "AGREE_LONG" in consensus else "#d29922"
            values = [
                _item(row["symbol"]),
                _item(_number(row["last_close"])),
                _item(row["last_date"]),
                _item(row["bars"]),
                _item(consensus.replace("EXTERNAL_STRATEGIES_", ""), color=color),
                _item(_percent(row["buy_hold_return_percent"])),
                _item(row["status"]),
            ]
            for column, item in enumerate(values):
                table.setItem(row_index, column, item)

    def _populate_polymarket(self) -> None:
        rows = self._snapshot["polymarket"]["accounts"]
        self._poly_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                _item(row["name"]),
                _item(_number(row["total_value"])),
                _item(_number(row["pnl"])),
                _item(_percent(row["roi_percent"])),
                _item(row["trades"]),
                _item(row["positions"]),
                _item(row["open_orders"]),
                _item("允许" if row["new_entries_enabled"] else "封锁"),
            ]
            for column, item in enumerate(values):
                self._poly_table.setItem(row_index, column, item)

    def _populate_evidence(self) -> None:
        rows = self._snapshot["evidence"]
        self._evidence_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                _item(row["name"]),
                _item(row["modified_at"]),
                _item(f"{row['bytes'] / 1024:,.0f} KB"),
                _item("就绪" if row["exists"] else "缺失"),
            ]
            for column, item in enumerate(values):
                self._evidence_table.setItem(row_index, column, item)

    def _populate_research(self) -> None:
        queue = self._snapshot["us"].get("research_queue") or []
        if not queue:
            self._research_text.setPlainText("当前没有主动研究候选。")
            return
        lines = []
        for candidate in queue:
            lines.extend(
                [
                    f"{candidate.get('symbol', 'UNKNOWN')}  ·  {candidate.get('state', 'UNKNOWN')}",
                    f"论点: {candidate.get('thesis', '—')}",
                    f"失效: {candidate.get('invalidation', '—')}",
                    "",
                ]
            )
        self._research_text.setPlainText("\n".join(lines).rstrip())

    def _populate_engines(self) -> None:
        rows = self._snapshot.get("engines") or []
        self._engine_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                _item(row["project"]),
                _item(row["role"]),
                _item(row["license"]),
                _item(row["integration"]),
                _item("已安装" if row["installed"] else "缺失"),
            ]
            for column, item in enumerate(values):
                self._engine_table.setItem(row_index, column, item)

    def _populate_portfolio(self) -> None:
        portfolio = self._snapshot.get("portfolio") or {}
        fund = self._snapshot.get("fund") or {}
        rows = [
            {
                "asset": "账户总额",
                "amount": _number(portfolio.get("total_balance")),
                "currency": portfolio.get("currency", "HKD"),
                "role": "用户报告的组合基线",
                "boundary": "只读建议",
            },
            {
                "asset": "剩余现金",
                "amount": _number(portfolio.get("cash")),
                "currency": portfolio.get("currency", "HKD"),
                "role": "新仓资金约束",
                "boundary": "只读建议",
            },
        ]
        for holding in portfolio.get("holdings") or []:
            rows.append(
                {
                    "asset": holding.get("symbol"),
                    "amount": _number(holding.get("shares"), 0),
                    "currency": "shares",
                    "role": "用户报告的实际持仓",
                    "boundary": "禁止自动交易",
                }
            )
        rows.append(
            {
                "asset": fund.get("code") or "001437",
                "amount": "已确认持有" if fund.get("position_confirmed") else "待确认",
                "currency": "fund",
                "role": "每日基金风险监控",
                "boundary": "仅人工赎回建议",
            }
        )
        self._portfolio_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                _item(row["asset"]),
                _item(row["amount"]),
                _item(row["currency"]),
                _item(row["role"]),
                _item(row["boundary"]),
            ]
            for column, item in enumerate(values):
                self._portfolio_table.setItem(row_index, column, item)

    def _on_us_double_clicked(self, row: int, _column: int) -> None:
        item = self._us_table.item(row, 0)
        if item:
            self.symbol_selected.emit("us", item.text(), "")

    def _on_a_share_double_clicked(self, row: int, _column: int) -> None:
        item = self._a_table.item(row, 0)
        if not item:
            return
        symbol = item.text()
        exchange = "SSE" if symbol.endswith(".SS") else "SZSE"
        self.symbol_selected.emit("a-share", symbol.split(".")[0], exchange)

    def _set_run_enabled(self, enabled: bool) -> None:
        self._run_stock_btn.setEnabled(enabled)
        self._run_poly_btn.setEnabled(enabled)
        self._refresh_btn.setEnabled(enabled)
        self._btc_btn.setEnabled(enabled)

    def _start_cycle(self, scope: str) -> None:
        if self._process is not None:
            QMessageBox.information(self, "任务运行中", "已有一个模拟闭环正在运行。")
            return
        try:
            command = paper_command(scope, self._workspace)
        except FinanceWorkspaceError as exc:
            QMessageBox.warning(self, "无法运行", str(exc))
            return
        self._process_scope = scope
        self._process = QProcess(self)
        self._process.setProgram(command[0])
        self._process.setArguments(command[1:])
        if self._workspace:
            self._process.setWorkingDirectory(str(self._workspace / "closed_loop"))
        self._process.finished.connect(self._on_cycle_finished)
        self._process.errorOccurred.connect(self._on_cycle_error)
        self._set_run_enabled(False)
        self._status.setText(f"正在运行 {scope} 模拟闭环…")
        self._process.start()

    def _on_cycle_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        scope = self._process_scope
        process = self._process
        stderr = ""
        if process is not None:
            stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")[-2000:]
            process.deleteLater()
        self._process = None
        self._process_scope = ""
        self._set_run_enabled(True)
        self.refresh()
        if exit_code == 0:
            self._status.setText(f"{scope} 模拟闭环完成, 证据已刷新。")
        else:
            QMessageBox.warning(
                self,
                "模拟闭环失败",
                f"{scope} 返回代码 {exit_code}。\n\n{stderr or '请查看 closed_loop 报告。'}",
            )

    def _on_cycle_error(self, error: QProcess.ProcessError) -> None:
        self._status.setText(f"模拟进程启动失败: {error.name}")

    def shutdown(self) -> None:
        self._quant_paper.shutdown()
        process = self._process
        if process is None:
            return
        process.terminate()
        if not process.waitForFinished(2000):
            process.kill()
            process.waitForFinished(1000)
        self._process = None
