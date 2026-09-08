"""Investment plan editor: compact presentation, unchanged plan/holding semantics."""
from __future__ import annotations

import copy
import math
import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QScrollArea, QSizePolicy,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from pa_agent.installment.models import CORE_SYMBOLS, NAMES, SYMBOLS, THEMES, current_period
from .workbench_ui import apply_workbench_style


_PLAN_STYLE = """
QDialog#investmentPlan { background: #f4f6f9; color: #182337; }
QDialog#investmentPlan QLabel, QDialog#investmentPlan QCheckBox {
    background: transparent; color: #182337; font-size: 13px;
}
QDialog#investmentPlan QLabel[planRole="title"] { font-size: 25px; font-weight: 700; }
QDialog#investmentPlan QLabel[planRole="section"] { font-size: 16px; font-weight: 600; }
QDialog#investmentPlan QLabel[planRole="muted"] { color: #657187; font-size: 12px; }
QDialog#investmentPlan QLabel[planRole="field"] { color: #657187; font-size: 12px; }
QDialog#investmentPlan QLabel[planRole="error"] { color: #b94b4a; font-size: 13px; }
QDialog#investmentPlan QLabel#planCurrency {
    color: #2446d7; background: #eaf0ff; border-radius: 6px; padding: 5px 10px;
    font-size: 12px; font-weight: 600;
}
QDialog#investmentPlan QFrame[planPanel="true"] {
    background: white; border: 1px solid #e4e9f0; border-radius: 12px;
}
QDialog#investmentPlan QFrame#planFooter {
    background: white; border: none; border-top: 1px solid #e4e9f0;
}
QDialog#investmentPlan QScrollArea, QDialog#investmentPlan QWidget#planPage {
    background: #f4f6f9; border: none;
}
QDialog#investmentPlan QTabWidget::pane { border: none; background: #f4f6f9; }
QDialog#investmentPlan QTabBar::tab {
    background: transparent; color: #657187; border: none; border-bottom: 2px solid transparent;
    padding: 12px 18px; margin-right: 8px; font-size: 14px; font-weight: 600;
}
QDialog#investmentPlan QTabBar::tab:selected { color: #2446d7; border-bottom: 2px solid #2446d7; }
QDialog#investmentPlan QTabBar::tab:hover { color: #2446d7; background: #edf2fb; }
QDialog#investmentPlan QLineEdit {
    background: #fff; color: #182337; border: 1px solid #d9e1ec; border-radius: 7px;
    padding: 7px 11px; font-size: 14px; selection-background-color: #2446d7;
}
QDialog#investmentPlan QLineEdit:focus { border: 1px solid #2446d7; background: #fafcff; }
QDialog#investmentPlan QTableWidget {
    background: white; alternate-background-color: #f8fafc; color: #182337;
    border: 1px solid #e4e9f0; border-radius: 8px; gridline-color: #edf1f6;
    selection-background-color: #eaf0ff; selection-color: #182337; font-size: 13px;
}
QDialog#investmentPlan QTableWidget::item { padding: 8px 12px; border: none; }
QDialog#investmentPlan QTableWidget::item:alternate { background: #f8fafc; color: #182337; }
QDialog#investmentPlan QTableWidget::item:selected { background: #eaf0ff; color: #182337; }
QDialog#investmentPlan QHeaderView::section {
    background: #f4f7fb; color: #657187; border: none; border-bottom: 1px solid #e4e9f0;
    padding: 9px 12px; font-size: 12px; font-weight: 500;
}
QDialog#investmentPlan QToolButton#planDetailsToggle {
    color: #2446d7; background: transparent; border: none; padding: 7px 0;
    font-size: 12px; text-align: left;
}
QDialog#investmentPlan QPushButton#planSave {
    color: white; background: #2446d7; border: 1px solid #2446d7;
    border-radius: 7px; padding: 9px 24px; font-size: 13px; font-weight: 600;
}
QDialog#investmentPlan QPushButton#planSave:hover { background: #1b39bf; }
QDialog#investmentPlan QPushButton#planCancel {
    color: #182337; background: white; border: 1px solid #d9e1ec;
    border-radius: 7px; padding: 9px 22px; font-size: 13px;
}
QDialog#investmentPlan QPushButton#planCancel:hover { background: #f4f7fb; }
"""


def _label(text="", role=None):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if role:
        label.setProperty("planRole", role)
    return label


def _panel(title, help_text=None):
    panel = QFrame()
    panel.setProperty("planPanel", "true")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(10)
    layout.addWidget(_label(title, "section"))
    if help_text:
        layout.addWidget(_label(help_text, "muted"))
    return panel, layout


class InvestmentPlanDialog(QDialog):
    """Blank numeric entries stay unknown; displayed examples are never saved."""

    def __init__(self, plan, symbols, parent=None):
        super().__init__(parent)
        self.setObjectName("investmentPlan")
        self.setWindowTitle("投资计划 · 所有金额均为 USD")
        self.resize(920, 760)
        self.setMinimumSize(680, 440)
        self._original = copy.deepcopy(plan)
        self.plan = None
        apply_workbench_style(self)
        self.setStyleSheet(self.styleSheet() + _PLAN_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        heading = QHBoxLayout()
        heading.setContentsMargins(24, 20, 24, 10)
        heading_text = QVBoxLayout()
        heading_text.setSpacing(4)
        heading_text.addWidget(_label("投资计划", "title"))
        heading_text.addWidget(_label("设置预算与额度；未确认的金额和年限留空。", "muted"))
        heading.addLayout(heading_text, 1)
        badge = _label("全部金额 USD")
        badge.setObjectName("planCurrency")
        badge.setWordWrap(False)
        heading.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(heading)

        self.fields = {}
        specs = [
            ("monthly_budget_usd", "每月新增预算（USD）", None),
            ("horizon_years", "预计投资年限", None),
            ("portfolio_total_usd", "总投资组合市值（USD）", None),
            ("max_single_pct", "单只股票上限（占总组合 %）", 10),
            ("max_theme_pct", "同一主题上限（占总组合 %）", 35),
            ("higher_risk_installment_pct", "高风险时：单次使用本股剩余额度 %", 25),
            ("normal_installment_pct", "正常时：单次使用本股剩余额度 %", 50),
            ("attractive_installment_pct", "更有吸引力时：单次使用本股剩余额度 %", 100),
            ("period", "本期（YYYY-MM）", current_period()),
        ]
        for key, label, default in specs:
            value = plan.get(key, default)
            edit = QLineEdit("" if value is None else str(value))
            edit.setObjectName(key)
            edit.setAccessibleName(label)
            edit.setPlaceholderText("留空：未知" if key != "period" else "YYYY-MM")
            edit.setMinimumHeight(36)
            edit.setAlignment(Qt.AlignmentFlag.AlignRight)
            edit.setToolTip(label)
            self.fields[key] = edit

        self.tabs = QTabWidget()
        self.tabs.setObjectName("planTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumHeight(0)
        pages = QWidget()
        pages_layout = QVBoxLayout(pages)
        pages_layout.setContentsMargins(20, 0, 20, 12)
        pages_layout.addWidget(self.tabs)
        root.addWidget(pages, 1)

        self.budget_scroll, budget_page = self._scroll_page()
        self.tabs.addTab(self.budget_scroll, "预算与持仓")
        budget_card, budget_layout = _panel("本期预算")
        budget_grid = QGridLayout()
        budget_grid.setHorizontalSpacing(14)
        for column, (key, title) in enumerate((
            ("monthly_budget_usd", "每月新增预算 · USD"),
            ("portfolio_total_usd", "总组合市值 · USD"),
            ("horizon_years", "投资年限 · 年"),
            ("period", "本期 · YYYY-MM"),
        )):
            budget_grid.addWidget(self._field_box(key, title), 0, column)
            budget_grid.setColumnStretch(column, 1)
        budget_layout.addLayout(budget_grid)
        budget_page.addWidget(budget_card)

        holdings_card, holdings_layout = _panel("持仓与分配")
        holdings_header = QHBoxLayout()
        holdings_header.addWidget(holdings_layout.takeAt(0).widget())
        holdings_header.addStretch(1)
        weights_note = _label("目标权重总和 ≤ 100% · 未知市值留空", "muted")
        weights_note.setWordWrap(False)
        holdings_header.addWidget(weights_note)
        holdings_layout.insertLayout(0, holdings_header)
        self.assets = QTableWidget(len(symbols), 3)
        self.assets.setObjectName("planAssets")
        self.assets.setAccessibleName("新增预算目标权重与当前持仓市值")
        self.assets.setHorizontalHeaderLabels(["资产", "新增预算目标权重 %", "当前持仓市值 USD"])
        self.assets.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.assets.setColumnWidth(0, 128)
        self.assets.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.assets.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.assets.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.assets.verticalHeader().setVisible(False)
        self.assets.verticalHeader().setDefaultSectionSize(40)
        self.assets.setAlternatingRowColors(True)
        self.assets.setShowGrid(False)
        self.assets.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.assets.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.assets.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.assets.setMinimumHeight(240)
        self.assets.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        for row, symbol in enumerate(symbols):
            item = QTableWidgetItem(symbol)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(NAMES.get(symbol, "核心基金" if symbol in CORE_SYMBOLS else symbol))
            self.assets.setItem(row, 0, item)
            for col, key in ((1, "target_weights"), (2, "positions_usd")):
                value = (plan.get(key) or {}).get(symbol)
                cell = QTableWidgetItem("" if value is None else str(value))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 1 and symbol in CORE_SYMBOLS:
                    cell.setText("不参与个股分配")
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    cell.setForeground(QColor("#657187"))
                    cell.setBackground(QColor("#f4f6f9"))
                    cell.setToolTip("核心基金只记录持仓市值，不参与此处新增个股预算。")
                else:
                    cell.setToolTip("留空表示未知；持仓市值须包含此前已完成的投入。" if col == 2 else "用于分配新增预算，所有股票的目标权重总和不超过 100%。")
                self.assets.setItem(row, col, cell)
        holdings_layout.addWidget(self.assets, 1)
        holdings_layout.addWidget(_label("核心基金只记录市值。非美元资产按你确认的汇率折算为 USD。", "muted"))
        budget_page.addWidget(holdings_card, 1)

        self.rules_scroll, rules_page = self._scroll_page()
        self.tabs.addTab(self.rules_scroll, "额度规则")
        limits_card, limits_layout = _panel("组合上限", "两项均占总组合市值；单股上限不能高于主题上限。")
        limits_row = QHBoxLayout()
        limits_row.setSpacing(18)
        limits_row.addWidget(self._field_box("max_single_pct", "单只股票上限 · %"), 1)
        limits_row.addWidget(self._field_box("max_theme_pct", "同一主题上限 · %"), 1)
        limits_layout.addLayout(limits_row)
        rules_page.addWidget(limits_card)

        fractions_card, fractions_layout = _panel("分批投入比例", "每次占该股票本期尚未投入计划额度的比例。")
        fractions_row = QHBoxLayout()
        fractions_row.setSpacing(18)
        for key, title in (("higher_risk_installment_pct", "高风险时 · %"),
                           ("normal_installment_pct", "正常时 · %"),
                           ("attractive_installment_pct", "更有吸引力时 · %")):
            fractions_row.addWidget(self._field_box(key, title), 1)
        fractions_layout.addLayout(fractions_row)
        fractions_layout.addWidget(_label("高风险 ≤ 正常 ≤ 更有吸引力；默认比例可自行调整。", "muted"))
        rules_page.addWidget(fractions_card)

        details_card, details_layout = _panel("规则说明")
        self.details_toggle = QToolButton()
        self.details_toggle.setObjectName("planDetailsToggle")
        self.details_toggle.setText("查看共享主题与计算口径")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        details_layout.addWidget(self.details_toggle)
        self.details_body = QWidget()
        details_body_layout = QVBoxLayout(self.details_body)
        details_body_layout.setContentsMargins(0, 0, 0, 0)
        details_body_layout.setSpacing(10)
        for name, members in THEMES.items():
            details_body_layout.addWidget(_label(name, "field"))
            details_body_layout.addWidget(_label(" · ".join(s for s in SYMBOLS if s in members)))
        details_body_layout.addWidget(_label("高不确定性成长属于风险预算分组，不表示同一行业。基金重叠尚未穿透。", "muted"))
        details_body_layout.addWidget(_label("分批比例只用于本股本期剩余额度，不是全账户比例。研究判断不会自动成为投入记录。", "muted"))
        self.details_body.setVisible(False)
        details_layout.addWidget(self.details_body)
        self.details_toggle.toggled.connect(self._toggle_details)
        rules_page.addWidget(details_card)
        rules_page.addStretch(1)

        footer = QFrame()
        footer.setObjectName("planFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(24, 12, 24, 12)
        footer_layout.setSpacing(5)
        self.confirm_holdings = QCheckBox("我已逐项更新并确认所有持仓市值（含已投入）")
        self.confirm_holdings.setChecked(False)
        self.confirm_holdings.setToolTip("只有你已逐项重新核对持仓时才勾选。普通保存预算或年限不更新未编辑股票的持仓确认时间。")
        footer_layout.addWidget(self.confirm_holdings)
        footer_layout.addWidget(_label("仅修改预算或年限无需勾选；未编辑持仓的确认时间保持原样。", "muted"))
        self.error = _label(role="error")
        self.error.setAccessibleName("投资计划校验错误")
        footer_layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        actions.addWidget(_label("保存计划不会产生投入记录。", "muted"), 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存计划")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("planSave")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("planCancel")
        self.buttons.accepted.connect(self._accept_plan)
        self.buttons.rejected.connect(self.reject)
        actions.addWidget(self.buttons)
        footer_layout.addLayout(actions)
        root.addWidget(footer)

    @staticmethod
    def _scroll_page():
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(0)
        page = QWidget()
        page.setObjectName("planPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 8, 0)
        layout.setSpacing(12)
        scroll.setWidget(page)
        return scroll, layout

    def _field_box(self, key, title):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = _label(title, "field")
        label.setBuddy(self.fields[key])
        layout.addWidget(label)
        layout.addWidget(self.fields[key])
        return box

    def _toggle_details(self, checked):
        self.details_body.setVisible(checked)
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    @staticmethod
    def _parse(text, label, maximum=None, integer=False, required=False):
        text = text.strip()
        if not text:
            if required:
                raise ValueError(f"请填写{label}。")
            return None
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{label}须为有效数字。") from None
        if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
            raise ValueError(f"{label}须在 0 至 {maximum} 之间。" if maximum is not None else f"{label}须为非负有限数字。")
        if integer and (not value.is_integer() or value < 1):
            raise ValueError(f"{label}须为大于零的整数。")
        return int(value) if integer else value

    def collect_plan(self):
        plan = copy.deepcopy(self._original)
        plan.pop("confirm_holdings", None)
        for key in ("monthly_budget_usd", "portfolio_total_usd"):
            plan[key] = self._parse(self.fields[key].text(), "金额（USD）", maximum=1_000_000_000)
        plan["horizon_years"] = self._parse(self.fields["horizon_years"].text(), "投资年限", maximum=50, integer=True)
        for key in ("max_single_pct", "max_theme_pct"):
            plan[key] = self._parse(self.fields[key].text(), "比例上限", maximum=100, required=True)
            if plan[key] < 0.1:
                raise ValueError("比例上限至少为 0.1%。")
        if plan["max_single_pct"] > plan["max_theme_pct"]:
            raise ValueError("单只股票上限不能大于主题上限。")
        for key in ("higher_risk_installment_pct", "normal_installment_pct", "attractive_installment_pct"):
            plan[key] = self._parse(self.fields[key].text(), "单次分批比例", maximum=100, required=True)
        if not plan["higher_risk_installment_pct"] <= plan["normal_installment_pct"] <= plan["attractive_installment_pct"]:
            raise ValueError("单次分批比例须满足：高风险 ≤ 正常 ≤ 更有吸引力。")
        period = self.fields["period"].text().strip()
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period):
            raise ValueError("本期须为有效的 YYYY-MM，例如 2026-09。")
        plan["period"] = period
        weights, positions = {}, {}
        for row in range(self.assets.rowCount()):
            symbol = self.assets.item(row, 0).text()
            if symbol not in CORE_SYMBOLS:
                weight = self._parse(self.assets.item(row, 1).text(), f"{symbol} 目标权重", maximum=100)
                if weight is not None:
                    weights[symbol] = weight
            positions[symbol] = self._parse(self.assets.item(row, 2).text(), f"{symbol} 持仓金额（USD）", maximum=1_000_000_000)
        if sum(weights.values()) > 100 + 1e-8:
            raise ValueError("新增预算目标权重总和不能超过 100%。")
        if plan["portfolio_total_usd"] is not None and sum(v or 0 for v in positions.values()) > plan["portfolio_total_usd"] + .01:
            raise ValueError("已填持仓市值之和不能超过总投资组合市值。")
        plan.update(target_weights=weights, positions_usd=positions)
        if self.confirm_holdings.isChecked():
            plan["confirm_holdings"] = True
        return plan

    def _accept_plan(self):
        try:
            self.plan = self.collect_plan()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()
