"""Native, read-only market dashboard fed exclusively by saved monitoring data."""
from __future__ import annotations

import copy
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

COLORS = {"text": "#182337", "muted": "#657187", "blue": "#2446d7",
          "positive": "#16724c", "negative": "#b94b4a", "warning": "#9d6a1a"}
NAMES = {"001437": "易方达瑞享混合 I", "VOO": "标普 500 ETF", "QQQM": "纳斯达克 100 ETF",
         "NVDA": "英伟达", "TSLA": "特斯拉", "TSM": "台积电", "ASML": "阿斯麦",
         "MU": "美光", "SNDK": "闪迪", "SKHY": "SK 海力士", "COIN": "Coinbase",
         "CEG": "星座能源", "VST": "Vistra", "SPCX": "SpaceX", "BTC-USD": "比特币 · COIN 背景",
         "000660.KS": "SK 海力士 · 韩国", "KRW=X": "美元兑韩元", "SPY": "标普 500 ETF",
         "RSP": "标普 500 等权 ETF", "IWM": "罗素 2000 ETF", "HYG": "高收益债 ETF",
         "^VIX": "波动率指数", "^TNX": "美国十年期收益率指标", "399006.SZ": "创业板指", "000688.SS": "科创 50"}
ERRORS = {"STALE_COMPLETED_HISTORY": "最近完成交易日数据缺失", "INSUFFICIENT_HISTORY": "历史样本不足",
          "INSUFFICIENT_NAV_HISTORY": "净值历史样本不足", "PUBLIC_NAV_HISTORY_CACHE_MISSING": "净值历史缓存缺失",
          "PENDING_PUBLICATION": "最新交易日的正式净值尚待披露", "STALE": "正式净值尚未更新",
          "SAVED_DATA_NEEDS_REFRESH": "保存数据需要手动刷新", "EMPTY_OR_DUPLICATE_DATES": "来源日期缺失或重复",
          "HIGH_BELOW_CLOSE": "最高价与收盘价口径不一致", "INVALID_PRICE": "来源价格无效",
          "PUBLIC_SYMBOL_MISMATCH": "股票身份尚未核验", "OFFICIAL_NAV_UNAVAILABLE": "正式净值未取得",
          "UNCOMPLETED_OFFICIAL_NAV_DATE": "来源净值日期尚未完成"}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _value(value, digits=2, *, price=False):
    number = _num(value)
    return "待核验" if number is None or (price and number <= 0) else f"{number:,.{digits}f}"


def _pct(value):
    number = _num(value)
    return "待核验" if number is None else f"{number:+.2f}%" if number != 0 else "0.00%"


def _tone(value):
    number = _num(value)
    return COLORS["muted"] if number is None or number == 0 else COLORS["positive"] if number > 0 else COLORS["negative"]


def _date(value, *, with_time=False):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "日期待核验"
    if not with_time:
        return stamp.strftime("%Y/%m/%d")
    if stamp.tzinfo is not None:
        return stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y/%m/%d %H:%M") + " 北京时间"
    return stamp.strftime("%Y/%m/%d %H:%M") + "（时区未提供）"


def _label(text="", name="", parent=None):
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _surface():
    frame = QFrame()
    frame.setObjectName("surfaceCard")
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return frame


class _QuoteCard(QFrame):
    selected = pyqtSignal(str)

    def __init__(self, symbol, featured=False):
        super().__init__()
        self.symbol = symbol
        self.setObjectName("surfaceCard")
        self.setMinimumHeight(154)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(5)
        heading = QHBoxLayout()
        self.name = _label("001437 · 基金净值" if featured else symbol, "eyebrow")
        heading.addWidget(self.name)
        heading.addStretch()
        self.badge = _label("待刷新", "statusBadge")
        heading.addWidget(self.badge)
        layout.addLayout(heading)
        self.subtitle = _label(NAMES.get(symbol, symbol), "mutedLabel")
        layout.addWidget(self.subtitle)
        metric_row = QHBoxLayout()
        self.value = _label("待核验", "metricValue")
        self.value.setStyleSheet(f"font-size:{34 if featured else 29}px; font-weight:700; color:#182337;")
        metric_row.addWidget(self.value)
        self.unit = _label("单位净值 / CNY" if featured else "USD", "mutedLabel")
        metric_row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        metric_row.addStretch()
        layout.addLayout(metric_row)
        footer = QHBoxLayout()
        self.change = _label("变化待核验")
        footer.addWidget(self.change)
        footer.addStretch()
        self.button = QPushButton("详情 ›")
        self.button.setObjectName("quietButton")
        self.button.setAccessibleName(f"查看 {symbol} 详情")
        self.button.clicked.connect(lambda: self.selected.emit(self.symbol))
        footer.addWidget(self.button)
        layout.addLayout(footer)
        self.date = _label("等待手动刷新", "mutedLabel")
        layout.addWidget(self.date)

    def update_asset(self, asset, status, tone):
        asset = asset or {}
        is_fund = self.symbol == "001437"
        self.value.setText(_value(asset.get("close"), 4 if is_fund else 2, price=True))
        self.unit.setText(("单位净值 / " if is_fund else "") + str(asset.get("currency") or "币种待核验"))
        self.change.setText(_pct(asset.get("daily_pct")) + ("  较上次净值" if is_fund else "  日变化"))
        self.change.setStyleSheet("font-weight:600; color:" + _tone(asset.get("daily_pct")))
        self.date.setText(("净值日期  " if is_fund else "完成交易日  ") + _date(asset.get("date")))
        self.badge.setText(status)
        self.badge.setStyleSheet("font-size:11px; font-weight:600; color:" + COLORS[tone])


class MarketOverviewWidget(QWidget):
    """Presentation only: no service construction, I/O, timers or model calls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("marketOverview")
        self._result = None
        self._assets = {}
        self._rows = []
        self._groups = {"us": [], "background": []}
        self._selected_symbol = "001437"
        self.setStyleSheet("""
            QWidget#marketOverview, QWidget#marketBody { background:#f4f6f9; color:#182337; }
            QLabel { background:transparent; color:#182337; font-size:12px; }
            QLabel#mutedLabel, QLabel#eyebrow { color:#657187; font-size:11px; }
            QLabel#sectionTitle { color:#182337; font-size:16px; font-weight:700; }
            QFrame#surfaceCard { background:white; border:1px solid #e4e9f0; border-radius:12px; }
            QScrollArea { background:#f4f6f9; border:none; }
            QTableWidget { background:white; alternate-background-color:#f8faff; color:#182337;
                border:none; gridline-color:#edf0f5; selection-background-color:#eaf0ff; selection-color:#2446d7; font-size:12px; }
            QTableWidget::item { padding:7px 6px; border-bottom:1px solid #edf0f5; }
            QTableWidget::item:alternate { background:#f8faff; color:#182337; }
            QHeaderView::section { background:#f8f9fc; color:#657187; font-size:11px; font-weight:600;
                border:none; border-bottom:1px solid #e4e9f0; padding:9px 6px; }
            QPushButton#quietButton { background:transparent; color:#2446d7; border:none; padding:3px 0; font-size:11px; }
            QPushButton#quietButton:hover { color:#182f9e; }
            QToolButton { background:transparent; border:none; color:#657187; padding:7px 0; text-align:left; font-size:11px; }
            QToolButton:checked { color:#2446d7; }
            QComboBox { background:white; color:#182337; border:1px solid #e4e9f0; border-radius:6px; padding:5px 8px; font-size:11px; }
            QComboBox QAbstractItemView { background:white; color:#182337; selection-background-color:#eaf0ff; }
            QLineEdit { background:#f8f9fc; color:#657187; border:1px solid #e4e9f0; border-radius:5px; padding:5px; font-size:10px; }
            QSplitter::handle { background:transparent; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("marketBody")
        root = QVBoxLayout(self.body)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(14)
        title_row = QHBoxLayout()
        self.header_layout = title_row
        heading = QVBoxLayout()
        heading.setSpacing(3)
        heading.addWidget(_label("持有资产与公开市场", "eyebrow"))
        self.title = _label("基金与行情", "sectionTitle")
        self.title.setStyleSheet("font-size:23px;font-weight:700;color:#182337")
        heading.addWidget(self.title)
        title_row.addLayout(heading)
        title_row.addStretch()
        self.updated = _label("尚无保存结果", "mutedLabel")
        self.updated.setMinimumWidth(190)
        self.updated.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.updated)
        root.addLayout(title_row)
        cards = QGridLayout()
        cards.setSpacing(12)
        self.cards = {}
        for col, symbol in enumerate(("001437", "VOO", "QQQM")):
            card = _QuoteCard(symbol, featured=col == 0)
            card.selected.connect(self.select_symbol)
            cards.addWidget(card, 0, col)
            self.cards[symbol] = card
        cards.setColumnStretch(0, 5)
        cards.setColumnStretch(1, 3)
        cards.setColumnStretch(2, 3)
        root.addLayout(cards)
        self.notice = _label("手动刷新后显示正式净值与已完成交易日行情。", "mutedLabel")
        root.addWidget(self.notice)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(14)
        market = _surface()
        market.setMinimumWidth(510)
        market_layout = QVBoxLayout(market)
        market_layout.setContentsMargins(0, 0, 0, 0)
        market_layout.setSpacing(0)
        market_heading = QHBoxLayout()
        market_heading.setContentsMargins(14, 12, 14, 10)
        self.market_title = _label("美股行情", "sectionTitle")
        market_heading.addWidget(self.market_title)
        market_heading.addStretch()
        self.group = QComboBox()
        self.group.setAccessibleName("选择美股或背景行情分组")
        self.group.addItem("美股", "us")
        self.group.addItem("背景资产", "background")
        self.group.currentIndexChanged.connect(self._populate_table)
        market_heading.addWidget(self.group)
        market_layout.addLayout(market_heading)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["标的", "收盘 / 币种", "日变化", "五交易日", "完成日期", "新报价", "状态"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(37)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(270)
        self.table.currentCellChanged.connect(self._table_selection)
        market_layout.addWidget(self.table, 1)
        self.table_note = _label("已完成日线 · 新报价不计入未完成日线", "mutedLabel")
        self.table_note.setContentsMargins(14, 8, 14, 10)
        market_layout.addWidget(self.table_note)
        split.addWidget(market)
        detail = _surface()
        detail.setMinimumWidth(265)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(16, 14, 16, 14)
        detail_layout.setSpacing(8)
        detail_top = QHBoxLayout()
        self.detail_title = _label("001437", "sectionTitle")
        detail_top.addWidget(self.detail_title)
        detail_top.addStretch()
        self.detail_status = _label("待刷新", "eyebrow")
        detail_top.addWidget(self.detail_status)
        detail_layout.addLayout(detail_top)
        self.detail_name = _label("基金净值", "mutedLabel")
        detail_layout.addWidget(self.detail_name)
        self.detail_price = _label("待核验", "metricValue")
        self.detail_price.setStyleSheet("font-size:25px;font-weight:700;color:#182337")
        detail_layout.addWidget(self.detail_price)
        self.detail_date = _label("日期待核验", "mutedLabel")
        detail_layout.addWidget(self.detail_date)
        detail_metrics = QGridLayout()
        detail_metrics.addWidget(_label("52周高点回撤", "mutedLabel"), 0, 0)
        detail_metrics.addWidget(_label("五交易日变化", "mutedLabel"), 0, 1)
        self.drawdown = _label("待核验")
        self.five_days = _label("待核验")
        detail_metrics.addWidget(self.drawdown, 1, 0)
        detail_metrics.addWidget(self.five_days, 1, 1)
        detail_layout.addLayout(detail_metrics)
        self.detail_message = _label()
        self.detail_message.setContentsMargins(0, 4, 0, 0)
        detail_layout.addWidget(self.detail_message)
        self.detail_warning = _label()
        self.detail_warning.setStyleSheet("color:#9d6a1a;font-size:11px")
        detail_layout.addWidget(self.detail_warning)
        self.more_button = QToolButton()
        self.more_button.setText("指标与数据依据")
        self.more_button.setCheckable(True)
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more_button.setArrowType(Qt.ArrowType.RightArrow)
        detail_layout.addWidget(self.more_button)
        self.more = QWidget()
        more_layout = QVBoxLayout(self.more)
        more_layout.setContentsMargins(0, 0, 0, 0)
        more_layout.setSpacing(7)
        self.technical = _label("", "mutedLabel")
        self.quote_detail = _label("", "mutedLabel")
        self.source_detail = _label("", "mutedLabel")
        self.source_url = QLineEdit()
        self.source_url.setReadOnly(True)
        self.source_url.setAccessibleName("行情来源链接，只读，可复制")
        self.source_url.setPlaceholderText("来源链接未提供")
        more_layout.addWidget(self.technical)
        more_layout.addWidget(self.quote_detail)
        more_layout.addWidget(self.source_detail)
        more_layout.addWidget(self.source_url)
        more_layout.addWidget(_label("均线与回撤只是价格背景，不直接决定买卖，也不是未来上涨概率。", "mutedLabel"))
        detail_layout.addWidget(self.more)
        self.more.setVisible(False)
        self.more_button.toggled.connect(lambda show: self._toggle(self.more_button, self.more, show))
        detail_layout.addStretch()
        split.addWidget(detail)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        self.gaps_button = QToolButton()
        self.gaps_button.setText("监控边界与研究缺口")
        self.gaps_button.setCheckable(True)
        self.gaps_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.gaps_button.setArrowType(Qt.ArrowType.RightArrow)
        root.addWidget(self.gaps_button)
        self.gaps = _surface()
        gap_layout = QVBoxLayout(self.gaps)
        gap_layout.setContentsMargins(16, 12, 16, 12)
        self.gap_text = _label()
        self.boundary_text = _label("公开行情不等于完整研究；这里不生成申购、赎回或买卖数量，也不会把旧建议视为已执行。", "mutedLabel")
        gap_layout.addWidget(self.gap_text)
        gap_layout.addWidget(self.boundary_text)
        gap_layout.addWidget(_label("未来 5–20 个交易日前瞻、潜在底部和概率尚未完成核验。股票投入研究请切换「股票研究」。BTC 仅作为 COIN 背景。", "mutedLabel"))
        root.addWidget(self.gaps)
        self.gaps.setVisible(False)
        self.gaps_button.toggled.connect(lambda show: self._toggle(self.gaps_button, self.gaps, show))
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)
        self.set_result(None)

    @staticmethod
    def _toggle(button, widget, shown):
        button.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        widget.setVisible(shown)

    def _errors(self, symbol):
        asset = self._assets.get(symbol) or {}
        global_errors = ((self._result or {}).get("data") or {}).get("errors") or {}
        return list(dict.fromkeys([*asset.get("errors", []), *global_errors.get(symbol, [])]))

    def _status(self, symbol):
        asset = self._assets.get(symbol)
        if not asset:
            return "未取得", "warning"
        close = _num(asset.get("close"))
        if close is None or close <= 0:
            return "价格缺失", "warning"
        if _date(asset.get("date")) == "日期待核验":
            return "日期缺失", "warning"
        errors = self._errors(symbol)
        if any(e in errors for e in ("SAVED_DATA_NEEDS_REFRESH", "STALE", "STALE_COMPLETED_HISTORY")):
            return "需更新", "warning"
        if "PENDING_PUBLICATION" in errors or asset.get("publication") == "PENDING_PUBLICATION":
            return "待披露", "warning"
        if any("INSUFFICIENT" in str(e) or e == "PUBLIC_NAV_HISTORY_CACHE_MISSING" for e in errors):
            return "历史不足", "warning"
        if not asset.get("currency"):
            return "币种待核验", "warning"
        if not asset.get("qualified") or errors:
            return "待核验", "warning"
        check = ((self._result or {}).get("core_checks") or {}).get(symbol) or {}
        if check.get("status") == "PRICE_GATE_REACHED":
            return "需复核", "warning"
        return ("净值已披露" if symbol == "001437" else "已完成"), "muted"

    def set_result(self, result: dict | None):
        """Render a defensive copy; no price inference, I/O or service calls."""
        self._result = copy.deepcopy(result) if isinstance(result, dict) else None
        data = (self._result or {}).get("data") or {}
        self._assets = {str(k): v for k, v in (data.get("assets") or {}).items() if isinstance(v, dict)}
        universe = (self._result or {}).get("universe") or []
        us = list(dict.fromkeys(str(s) for s in universe if str(s) != "001437"))
        if not us:
            us = [s for s in ("VOO", "QQQM") if s in self._assets]
        extra = set(self._assets) | set((data.get("errors") or {}))
        self._groups = {"us": us, "background": sorted(extra - set(us) - {"001437"})}
        for symbol, card in self.cards.items():
            card.update_asset(self._assets.get(symbol), *self._status(symbol))
        self.updated.setText("手动刷新  " + _date(result.get("checked_at"), with_time=True) if self._result else "尚无保存结果 · 仅手动刷新")
        issue_symbols = [s for s in set(us) | extra | {"001437"} if self._status(s)[1] == "warning"]
        state = (self._result or {}).get("state") or {}
        if self._result:
            self.notice.setText((f"{len(issue_symbols)} 项数据或价格线需留意" if issue_symbols else "净值与行情已载入")
                               + (" · 持仓尚未确认，仅展示公开行情" if state.get("status") != "VALID" else " · 公开行情，不生成买卖数量"))
        else:
            self.notice.setText("还没有监控结果。点击工作台「刷新行情」获取公开行情；本页不会自动联网。")
        gaps = (self._result or {}).get("research_gaps") or []
        lines = [f"• {s}：" + "；".join(ERRORS.get(e, "来源或交易日尚未通过校验") for e in self._errors(s))
                 for s in sorted(extra) if self._errors(s)]
        lines.extend("• " + str(gap) for gap in gaps)
        self.gap_text.setText("\n".join(lines) if lines else "暂无新增数据缺口；公告、持仓和完整研究仍需另行核对。")
        self.gaps_button.setText("监控边界与研究缺口" + (f" · {len(lines)} 项" if lines else ""))
        self.group.setItemText(0, f"美股 · {len(us)}")
        self.group.setItemText(1, f"背景资产 · {len(self._groups['background'])}")
        self._populate_table()
        self.select_symbol(self._selected_symbol, sync_table=False)

    def _populate_table(self, *_):
        self._rows = self._groups.get(self.group.currentData(), [])
        self.market_title.setText("美股行情" if self.group.currentData() == "us" else "市场背景")
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._rows))
        for row, symbol in enumerate(self._rows):
            asset = self._assets.get(symbol) or {}
            status, tone = self._status(symbol)
            currency = asset.get("currency") or "币种未知"
            quote = asset.get("quote") or {}
            live = _value(quote.get("price"), price=True) if quote.get("fresh") else "—"
            values = [symbol, _value(asset.get("close"), price=True) + (" " + str(currency) if _num(asset.get("close")) is not None else ""),
                      _pct(asset.get("daily_pct")), _pct(asset.get("five_session_pct")), _date(asset.get("date")), live, status]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col in (1, 2, 3, 5):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col in (2, 3):
                    cell.setForeground(QColor(_tone(asset.get("daily_pct" if col == 2 else "five_session_pct"))))
                elif col == 6:
                    cell.setForeground(QColor(COLORS[tone]))
                if col == 0:
                    cell.setToolTip(NAMES.get(symbol, symbol))
                if col == 5:
                    cell.setToolTip("报价时间：" + _date(quote.get("quoted_at"), with_time=True) if quote.get("fresh") else "没有足够新的报价；未使用旧报价补齐")
                self.table.setItem(row, col, cell)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self.table_note.setText("已完成日线 · 新报价需有有效来源时间，不计入未完成日线" if self._rows
                               else "本组暂无已保存行情；请手动刷新后查看。")

    def _table_selection(self, row, *_):
        if 0 <= row < len(self._rows):
            self.select_symbol(self._rows[row], sync_table=False)

    def select_symbol(self, symbol, sync_table=True):
        self._selected_symbol = str(symbol)
        if sync_table:
            group = "us" if symbol in self._groups["us"] else "background" if symbol in self._groups["background"] else None
            if group:
                self.group.setCurrentIndex(0 if group == "us" else 1)
                self.table.blockSignals(True)
                self.table.selectRow(self._rows.index(symbol))
                self.table.blockSignals(False)
            else:
                self.table.clearSelection()
        asset = self._assets.get(symbol) or {}
        status, tone = self._status(symbol)
        self.detail_title.setText(symbol)
        self.detail_name.setText(NAMES.get(symbol, symbol))
        self.detail_status.setText(status)
        self.detail_status.setStyleSheet("color:" + COLORS[tone])
        currency = str(asset.get("currency") or "币种待核验")
        self.detail_price.setText(_value(asset.get("close"), 4 if symbol == "001437" else 2, price=True) + " " + currency)
        self.detail_date.setText(("正式净值  " if symbol == "001437" else "完成日期  ") + _date(asset.get("date")))
        self.drawdown.setText(_pct(asset.get("drawdown_52week_pct")))
        self.drawdown.setStyleSheet("font-weight:600;color:" + _tone(asset.get("drawdown_52week_pct")))
        self.five_days.setText(_pct(asset.get("five_session_pct")))
        self.five_days.setStyleSheet("font-weight:600;color:" + _tone(asset.get("five_session_pct")))
        check = ((self._result or {}).get("core_checks") or {}).get(symbol) or {}
        if not asset:
            message = "本标的数据未取得。缺失不代表价格为零，也不代表风险已排除。"
        elif symbol == "001437":
            message = "展示正式单位净值，不生成申购或赎回指令。"
        elif check.get("status") == "PRICE_GATE_REACHED":
            message = "已触及价格复核线。请结合其他证据人工复核；这不是减仓指令。"
        elif symbol in {"VOO", "QQQM"}:
            message = "未触及价格复核线。核心资产保持观察。" if check.get("status") == "PRICE_GATE_NOT_REACHED" else "价格复核尚未完成，当前不生成买卖数量。"
        elif symbol == "BTC-USD":
            message = "仅作为 COIN 的背景，不生成 BTC 投入建议。"
        elif symbol in self._groups["background"]:
            message = "背景资产保留来源的原始币种与口径，不与美股价格混合比较。"
        else:
            message = "分批投入判断请前往「股票研究」。"
        self.detail_message.setText(message)
        warnings = [ERRORS.get(e, "来源或交易日尚未通过校验") for e in self._errors(symbol)]
        if not asset.get("full_52week_coverage", False) and asset:
            warnings.append("不足完整52周：回撤仅覆盖现有样本")
        self.detail_warning.setText("；".join(dict.fromkeys(warnings)))
        self.detail_warning.setVisible(bool(warnings))
        ma = asset.get("ma") or {}
        samples = asset.get("samples")
        self.technical.setText("20 / 50 / 200 日均线\n" + " / ".join(_value(ma.get(n), price=True) for n in ("20", "50", "200"))
            + f"\n200日均线斜率  {_pct(asset.get('ma200_twenty_session_slope_pct'))}"
            + f"\n日变化  {_pct(asset.get('daily_pct'))}"
            + "\n有效历史样本  " + (str(samples) if isinstance(samples, int) and not isinstance(samples, bool) else "待核验")
            + "\n应有完成日期  " + _date(asset.get("expected_completed_date")))
        quote = asset.get("quote") or {}
        self.quote_detail.setText("有效新报价  " + _value(quote.get("price"), price=True) + " " + currency
            + "\n报价时间  " + _date(quote.get("quoted_at"), with_time=True) if quote.get("fresh") else "暂无有效新报价；旧报价未展示。")
        source = asset.get("source") or {}
        self.source_detail.setText("来源取得  " + _date(source.get("retrieved_at"), with_time=True))
        self.source_url.setText(str(source.get("url") or ""))

    def toPlainText(self):
        """Compatibility summary of visible native controls, never a hidden report."""
        texts = [widget.text() for widget in self.findChildren(QLabel) if widget.isVisibleTo(self) and widget.text()]
        texts.extend(widget.text() for widget in self.findChildren(QToolButton) if widget.isVisibleTo(self) and widget.text())
        texts.extend(widget.text() for widget in self.findChildren(QLineEdit) if widget.isVisibleTo(self) and widget.text())
        if self.table.isVisibleTo(self):
            texts.extend(self.table.horizontalHeaderItem(col).text() for col in range(self.table.columnCount()))
            texts.extend(self.table.item(row, col).text() for row in range(self.table.rowCount())
                         for col in range(self.table.columnCount()) if self.table.item(row, col))
        return "\n".join(texts)
