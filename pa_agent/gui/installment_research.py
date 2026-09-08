"""Manual, cache-first installment research. No brokerage or execution actions."""
from __future__ import annotations

import html
import math
import re
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import date, datetime, timezone

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from pa_agent.installment.models import SYMBOLS, CORE_SYMBOLS, THEMES, current_period
from pa_agent.gui.investment_plan import InvestmentPlanDialog
from pa_agent.gui.workbench_ui import (
    Disclosure, FitTextBrowser, MetricCard, apply_workbench_style, document_html, empty_html,
    format_time, label, set_tone, toolbar_button,
)

DEFAULT_ENGINE = {"kind": "codex_cli", "model": "gpt-5.3-codex-spark", "reasoning_effort": "high"}


def _text(value, fallback="待核实"):
    return html.escape(str(value if value not in (None, "") else fallback), quote=True)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _money(value):
    value = _number(value)
    return "待确认" if value is None else f"${value:,.2f}"


def _plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _items(values):
    if not isinstance(values, list) or not values:
        return "<p>待核实</p>"
    return "<ul>" + "".join(f"<li>{_text(v)}</li>" for v in values) + "</ul>"


def _overlap_html(symbol):
    rows = [f"{name}：{'、'.join(s for s in SYMBOLS if s in members)}。同组共享你设置的上限。"
            for name, members in THEMES.items() if symbol in members]
    return ("<h3>组合重叠与额度</h3>" + _items(rows)
            + "<p>AI与半导体组共同受算力投资影响；CEG与VST同受发电及电力需求影响。"
              "“高不确定性成长”是风险预算分组，不代表属于同一行业。"
              "已有指数基金和主动基金还可能重复持有相关股票；当前只计算你填写的直接持仓，未做基金穿透，实际重叠可能更高。</p>")


def _https_url(value):
    url = QUrl(str(value or ""))
    return url if (url.isValid() and url.scheme() == "https" and url.host()
                   and not url.userName() and not url.password()) else None


def _percent(value):
    number = _number(value)
    return "待核实" if number is None else f"{number:.2f}%"


def _link(title, value):
    url = _https_url(value)
    return f'<a href="{_text(url.toString())}" style="color:#2446d7">{_text(title)}</a>' if url else _text(title)


def _source_category(source):
    url = str(source.get("url") or "").lower()
    if "companyfacts" in url:
        return "财报结构化"
    if "timeseries" in url:
        return "估值快照"
    if "/search" in url:
        return "新闻线索"
    if "submissions" in url:
        return "公告目录"
    if "ticker" in url:
        return "发行人身份"
    if "/chart/" in url:
        return "历史行情"
    return "公告正文"


def _source_refs(node, source_index):
    refs = node.get("source_refs") or []
    links = []
    for ref in refs:
        source = source_index.get(str(ref), {})
        label = source.get("provider") or node.get("provider") or "来源待核实"
        link = _link(label, source.get("url"))
        if link not in links:
            links.append(link)
    return "、".join(links) or _text(node.get("provider"), "来源未提供")


def _financial_cell(node, section, source_index):
    if not isinstance(node, dict) or _number(node.get("value")) is None:
        return "未取得"
    value = _number(node["value"])
    currency = str(node.get("unit") or "")
    if re.fullmatch(r"[A-Z]{3}", currency):
        if abs(value) >= 100_000_000:
            amount = f"{value / 100_000_000:,.2f} 亿 {currency}"
        elif abs(value) >= 1_000_000:
            amount = f"{value / 1_000_000:,.2f} 百万 {currency}"
        else:
            amount = f"{value:,.2f} {currency}"
    else:
        amount = "币种缺失，金额待核实"
    end = node.get("period_end") or node.get("as_of")
    start = node.get("period_start")
    period = f"{_text(start)} 至 {_text(end)}" if start else f"截至 {_text(end)}"
    filing = _text(node.get("filing_date"), "未提供")
    calculated = " · 据披露计算" if str(node.get("basis", "")).startswith("annual +") else ""
    stale = " · 旧缓存待更新" if section.get("stale") or node.get("stale") else ""
    return (f"<b>{_text(amount)}</b>{calculated}{stale}<br>"
            f"报告期：{period}<br>披露日期：{filing}<br>来源：{_source_refs(node, source_index)}")


def _financial_table(fundamentals, source_index):
    fundamentals = fundamentals if isinstance(fundamentals, dict) else {}
    official, vendor = (fundamentals.get(key) or {} for key in ("official", "vendor"))
    parts = ['<p>各格保留原币种，不做汇率换算；TTM 指过去十二个月。缺失季度不会用全年或半年平均替代。</p>',
             '<table width="100%" cellspacing="0" cellpadding="10" border="0"><tr><th>指标 / 期间</th><th>正式披露</th><th>供应商数据</th></tr>']
    for key, name in (("revenue", "营收"), ("net_income", "净利润"),
                      ("operating_cash_flow", "经营现金流"), ("capex", "资本开支")):
        for period, label in (("latest_quarter", "最近季度"), ("ttm", "TTM")):
            cells = []
            for section in (official, vendor):
                metric = (section.get("metrics") or {}).get(key) or {}
                cells.append(_financial_cell(metric.get(period), section, source_index))
            parts.append(f"<tr><td>{name}<br>{label}</td><td>{cells[0]}</td><td>{cells[1]}</td></tr>")
    parts.append("</table><p>供应商披露日期可能未提供，数据取得时间不能替代披露日期。资本开支保留来源符号，负值可能表示现金流出。</p>")
    return "".join(parts)


def _valuation_table(valuation, source_index):
    parts = ['<table width="100%" cellspacing="0" cellpadding="10" border="0"><tr><th>观测指标</th><th>倍数</th><th>观测日期</th><th>来源 / 状态</th></tr>']
    metrics = valuation.get("metrics") or {}
    for key, label in (("pe", "市盈率 PE"), ("ps", "市销率 PS"), ("ev_revenue", "企业价值 / 销售额")):
        node = metrics.get(key) or {}
        value = _number(node.get("value"))
        display = "缺失" if value is None else f"{value:.2f} 倍"
        if key == "pe" and value is not None and value <= 0:
            display += "（不作便宜依据）"
        stale = valuation.get("stale") or node.get("stale") or any(
            source_index.get(str(ref), {}).get("stale") for ref in node.get("source_refs", []))
        status = "旧缓存待更新" if stale else "供应商观测值，口径需核实" if value is not None else "未取得"
        parts.append(f"<tr><td>{label}</td><td>{display}</td><td>{_text(node.get('as_of') or node.get('period_end'), '未提供')}</td>"
                     f"<td>{_source_refs(node, source_index)}<br>{status}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _comparison_html(comparison, symbol):
    labels = {"START": "可开始小额分批", "NORMAL": "可按正常节奏投入", "INCREASE": "价格更有吸引力",
              "PAUSE": "本期暂缓新增", "REVIEW": "本次证据不足"}
    changes = comparison.get("changes", []) if isinstance(comparison, dict) else []
    selected = [row for row in changes if isinstance(row, dict) and row.get("symbol") == symbol]
    if not selected:
        return "<p>暂无该股票的判断变化；首次研究没有上次结果可比。</p>"
    return "".join(f"<p>上次：{_text(labels.get(row.get('previous'), row.get('previous')))}<br>"
                   f"本次：{_text(labels.get(row.get('current'), row.get('current')))}<br>"
                   f"变化原因：{_text(row.get('reason'))}</p>" for row in selected)


def _source_card(source, title=None, published=None, status=None):
    provider = source.get("provider") or source.get("publisher") or "公开来源"
    title = title or source.get("title") or f"{provider} · {_source_category(source)}"
    states = {"ok": "已取得", "success": "已取得", "cached": "缓存", "stale": "旧缓存待更新",
              "missing": "未取得", "index_only": "仅目录线索", "excerpt": "已取得正文节选"}
    state = status or source.get("status") or "未确认"
    state = states.get(state, state)
    if source.get("stale") and "旧缓存" not in state:
        state += " · 旧缓存待更新"
    published = published or source.get("published_at") or source.get("publication_date")
    return (f"<p><b>{_link(title, source.get('url'))}</b><br>"
            f"来源：{_text(provider)}；状态：{_text(state)}<br>"
            f"实际发布日期：{_text(published, '未提供')}；数据取得时间：{_text(source.get('retrieved_at'), '未提供')}</p>")


def _sources_html(sources, evidence):
    sources = [s for s in sources if isinstance(s, dict)]
    index = {str(s.get("id") or s.get("source_id")): s for s in sources}
    by_url = {s.get("url"): s for s in sources if s.get("url")}
    documents = evidence.get("documents") or []
    document_by_url = {d.get("url"): d for d in documents if isinstance(d, dict)}
    parts = ["<h2>来源与原文</h2><p>实际发布日期与数据取得时间分别列示。新闻标题仅作线索；点击 HTTPS 链接可查看原文。</p>"]
    listed = set()
    for key, heading in (("documents", "公告正文"), ("filings", "正式披露目录"), ("news", "新闻线索")):
        records = evidence.get(key) or []
        cards = []
        for row in records:
            if not isinstance(row, dict) or row.get("url") in listed:
                continue
            refs = row.get("source_refs") or [row.get("source_id")]
            source = by_url.get(row.get("url")) or next((index[str(ref)] for ref in refs if str(ref) in index), {})
            merged = {**source, **{k: v for k, v in row.items() if k not in {"text", "source_refs"}}}
            merged["provider"] = row.get("publisher") or source.get("provider") or "公开来源"
            status = "excerpt" if key == "documents" else "index_only" if key == "filings" else "新闻标题线索，未审阅正文"
            cards.append(_source_card(merged, title=row.get("title") or f"{merged['provider']} · {heading}",
                                      published=row.get("published_at") or row.get("publication_date") or row.get("filing_date"), status=status))
            listed.add(row.get("url"))
        if cards:
            parts.extend([f"<h3>{heading}</h3>", *cards])
    parts.append("<h3>数据快照与获取状态</h3>")
    for source in sources:
        doc = document_by_url.get(source.get("url"), {})
        parts.append(_source_card(source, title=doc.get("title")))
    if not sources and not listed:
        parts.append("<p>暂无可核验来源；不能把缺少出处的判断当作已确认事实。</p>")
    return "".join(parts)


class EngineSettingsDialog(QDialog):
    """Feature-local routing only. API credentials remain in their existing settings."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        apply_workbench_style(self)
        self.setWindowTitle("分批投资研究 · 模型设置")
        self.resize(620, 340)
        self.config = None
        self.api_settings_changed = False
        root = QVBoxLayout(self)
        root.addWidget(_plain_label("只配置本功能使用的引擎，保存不会联网或调用模型。"))
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("本机 Codex（订阅额度）", "codex_cli")
        self.kind.addItem("已有 API 配置（单独计费）", "api")
        form.addRow("分析引擎", self.kind)
        self.codex_fields = QWidget()
        codex_form = QFormLayout(self.codex_fields)
        codex_form.setContentsMargins(0, 0, 0, 0)
        self.model = QLineEdit(str(config.get("model") or DEFAULT_ENGINE["model"]) if config.get("kind", "codex_cli") == "codex_cli" else DEFAULT_ENGINE["model"])
        self.reasoning = QComboBox()
        self.reasoning.addItems(["low", "medium", "high", "xhigh"])
        effort = config.get("reasoning_effort") if config.get("kind", "codex_cli") == "codex_cli" else "high"
        self.reasoning.setCurrentText(effort if effort in {"low", "medium", "high", "xhigh"} else "high")
        codex_form.addRow("Codex 模型", self.model)
        codex_form.addRow("推理强度", self.reasoning)
        form.addRow(self.codex_fields)
        root.addLayout(form)
        self.explanation = _plain_label()
        root.addWidget(self.explanation)
        self.api_button = QPushButton("打开已有 API 配置")
        self.api_button.clicked.connect(self.open_api_settings)
        root.addWidget(self.api_button)
        self.error = _plain_label()
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存引擎选择")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_config)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self._update_kind)
        self.kind.setCurrentIndex(1 if config.get("kind") == "api" else 0)
        self._update_kind()

    def _update_kind(self, *_):
        codex = self.kind.currentData() == "codex_cli"
        self.codex_fields.setVisible(codex)
        self.api_button.setVisible(not codex)
        self.explanation.setText("使用本机官方 Codex 的现有 ChatGPT 订阅登录，消耗订阅额度。此设置不修改全局 Codex 配置；模型是否可用以实际调用结果为准。"
                                 if codex else "沿用已有 API 提供商、模型及密钥，API 用量单独计费。保存此选择只切换路由，不覆盖已有 API 配置。")

    def collect_config(self):
        if self.kind.currentData() == "api":
            return {"kind": "api"}
        model = self.model.text().strip()
        if not re.fullmatch(r"gpt-[A-Za-z0-9._-]{1,70}", model):
            raise ValueError("请填写有效的 Codex GPT 模型名称。")
        return {"kind": "codex_cli", "model": model, "reasoning_effort": self.reasoning.currentText()}

    def _accept_config(self):
        try:
            self.config = self.collect_config()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()

    def open_api_settings(self):
        if self.kind.currentData() != "api":
            return
        try:
            from pa_agent.config.settings import load_settings
            from pa_agent.gui.ai_model_settings_dialog import AIModelSettingsDialog
            if AIModelSettingsDialog(load_settings(), self).exec() == QDialog.DialogCode.Accepted:
                self.api_settings_changed = True
                self.error.setText("已有 API 配置已保存。选择 API 引擎并手动更新时才调用。")
        except Exception:
            self.error.setText("已有 API 配置暂时无法打开；未显示密钥或原始错误。")


class InstallmentResearchWidget(QWidget):
    refresh_state_changed = pyqtSignal(bool)

    def __init__(self, parent=None, service=None, *, embedded=False):
        super().__init__(parent)
        if service is None:
            from pa_agent.installment.service import InstallmentService
            service = InstallmentService()
        self.service = service
        self._engine = dict(DEFAULT_ENGINE)
        self._engine_changed = False
        try:
            self._engine = self.service.load_engine()
        except (AttributeError, FileNotFoundError):
            pass
        except Exception:
            self._engine = {"kind": "unknown"}
        self._executor = None
        self._future = None
        self._cancelled = threading.Event()
        self._progress_count = 0
        self._result = None
        self._latest_result = None
        self._historical = False
        self._history = []
        self._contributions = []
        self._plan_dirty = False
        self._expired = False
        self._chart_price = {}
        self.setObjectName("installmentResearch")
        apply_workbench_style(self)
        self.setWindowTitle("VerdictQuant · 分批投资研究")
        self.resize(1140, 860)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 23, 26, 16)
        root.setSpacing(12)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(5)
        heading.addWidget(label("股票研究", "pageTitle"))
        self.freshness = label("从上次研究继续，按计划分批投入。", "mutedLabel")
        heading.addWidget(self.freshness)
        header.addLayout(heading)
        header.addStretch()
        self.refresh_button = toolbar_button("更新分析", "refresh", primary=True)
        self.cancel_button = QPushButton("取消更新")
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.plan_button = toolbar_button("投资计划", "plan")
        self.model_button = toolbar_button("模型设置", "settings")
        for button in (self.model_button, self.plan_button, self.cancel_button, self.refresh_button):
            header.addWidget(button)
        self.model_button.setVisible(not embedded)
        self.plan_button.setVisible(not embedded)
        root.addLayout(header)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.condition_card = MetricCard("符合分批条件", "—", "等待研究结果")
        self.monthly_card = MetricCard("本月预算", "待填写", "由投资计划设定")
        self.spent_card = MetricCard("已登记投入", "—", "本机确认记录")
        self.proposed_card = MetricCard("本期建议合计", "待确认", "先完成投资计划")
        for card in (self.condition_card, self.monthly_card, self.spent_card, self.proposed_card):
            cards.addWidget(card, 1)
        root.addLayout(cards)

        self.plan_notice = QFrame()
        self.plan_notice.setObjectName("noticeCard")
        notice_layout = QHBoxLayout(self.plan_notice)
        notice_layout.setContentsMargins(13, 9, 12, 9)
        self.plan_notice_text = label("完善预算与持仓，即可计算本期金额。", wrap=True)
        self.plan_notice_text.setStyleSheet("color:#8a5b11;font-size:12px")
        notice_layout.addWidget(self.plan_notice_text, 1)
        notice_button = QPushButton("完善计划 →")
        notice_button.setObjectName("textButton")
        notice_button.clicked.connect(self.edit_plan)
        notice_layout.addWidget(notice_button)
        root.addWidget(self.plan_notice)

        information = QWidget()
        information_layout = QVBoxLayout(information)
        information_layout.setContentsMargins(12, 0, 12, 4)
        information_layout.setSpacing(6)
        self.engine_note = _plain_label()
        self._update_engine_note()
        self.metadata = _plain_label("尚无保存结果")
        self.usage_note = _plain_label("尚无模型返回的实际用量；不估算剩余额度或费用。")
        self.summary = _plain_label("手动更新后显示公司层判断。")
        self.plan_summary = _plain_label("投资计划尚待确认；金额为 USD。")
        for information_label in (self.metadata, self.engine_note, self.usage_note, self.summary, self.plan_summary):
            information_label.setObjectName("mutedLabel")
            information_layout.addWidget(information_label)
        self.information = Disclosure("研究信息与计划明细", information)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        left = QFrame()
        left.setObjectName("surfaceCard")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 14, 12, 12)
        left_layout.setSpacing(12)
        watch_header = QHBoxLayout()
        watch_header.addWidget(label("研究清单", "sectionTitle"))
        watch_header.addStretch()
        self.watch_count = label("0 只", "mutedLabel")
        watch_header.addWidget(self.watch_count)
        left_layout.addLayout(watch_header)
        filters = QHBoxLayout()
        filters.setSpacing(7)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索代码或名称")
        self.search.setAccessibleName("搜索研究股票")
        self.search.setClearButtonEnabled(True)
        self.filter = QComboBox()
        self.filter.setAccessibleName("按研究判断筛选")
        for name, value in (("全部判断", "all"), ("可分批", "eligible"), ("暂缓新增", "pause"), ("待核验", "review")):
            self.filter.addItem(name, value)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.filter)
        left_layout.addLayout(filters)
        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName("股票研究清单")
        self.table.setHorizontalHeaderLabels(["标的", "判断", "收盘 USD", "本期 USD"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setColumnHidden(2, True)
        self.table.setMinimumWidth(300)
        left_layout.addWidget(self.table, 1)
        self.no_matches = label("没有匹配的股票，试试其他代码或判断。", "mutedLabel", wrap=True)
        self.no_matches.hide()
        left_layout.addWidget(self.no_matches)
        self.contribution_button = QPushButton("登记已完成的投入")
        self.contribution_button.setEnabled(False)
        self.contribution_button.setToolTip("只登记你已完成的投入，不下单，不把研究建议当作成交。")
        left_layout.addWidget(self.contribution_button)
        left_layout.addWidget(label("名单代表研究范围，金额统一为 USD。", "metricNote"))
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("surfaceCard")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 17, 18, 12)
        right_layout.setSpacing(12)
        identity = QHBoxLayout()
        identity_text = QVBoxLayout()
        identity_text.setSpacing(3)
        self.stock_title = label("选择一只股票", "stockTitle")
        self.stock_subtitle = label("查看价格、判断与依据", "mutedLabel")
        identity_text.addWidget(self.stock_title)
        identity_text.addWidget(self.stock_subtitle)
        identity.addLayout(identity_text, 1)
        self.decision_badge = label("待研究", "decisionBadge")
        set_tone(self.decision_badge, "muted")
        identity.addWidget(self.decision_badge)
        right_layout.addLayout(identity)
        key_metrics = QHBoxLayout()
        self.stock_metrics = {}
        for key, caption in (("price", "最近收盘 · USD"), ("drawdown", "较区间高点"), ("budget", "本期建议 · USD")):
            metric = QVBoxLayout()
            metric.setSpacing(5)
            metric.addWidget(label(caption, "eyebrow"))
            value = label("—")
            value.setStyleSheet("font-size:21px;font-weight:600;color:#182337")
            metric.addWidget(value)
            key_metrics.addLayout(metric, 1)
            self.stock_metrics[key] = value
        right_layout.addLayout(key_metrics)
        self.selection_warning = label("", wrap=True)
        self.selection_warning.setStyleSheet("color:#8a5b11;background:#fff9ed;border-radius:6px;padding:8px;font-size:12px")
        self.selection_warning.hide()
        right_layout.addWidget(self.selection_warning)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        overview_scroll = QScrollArea()
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, 13, 0, 0)
        overview_layout.setSpacing(8)
        self.details = self._browser(auto_height=True)
        overview_layout.addWidget(self.details)
        chart_header = QHBoxLayout()
        chart_header.addWidget(label("价格走势", "sectionTitle"))
        chart_header.addStretch()
        self.chart_range = QComboBox()
        self.chart_range.setAccessibleName("走势图时间范围")
        for caption, days in (("近 1 月", 30), ("近 3 月", 90), ("近 1 年", 365), ("全部历史", None)):
            self.chart_range.addItem(caption, days)
        self.chart_range.setCurrentIndex(3)
        chart_header.addWidget(self.chart_range)
        overview_layout.addLayout(chart_header)
        self.chart_note = _plain_label("仅显示真实历史收盘价，不生成预测曲线。")
        self.chart_note.setObjectName("metricNote")
        self.chart = None
        try:
            import pyqtgraph as pg
            self.chart = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(utcOffset=0)})
            self.chart.setBackground("#ffffff")
            self.chart.setMinimumHeight(180)
            self.chart.setMaximumHeight(200)
            self.chart.showGrid(x=False, y=True, alpha=0.12)
            for axis_name in ("left", "bottom"):
                axis = self.chart.getAxis(axis_name)
                axis.setPen(pg.mkPen("#d7deea"))
                axis.setTextPen(pg.mkPen("#657187"))
                axis.setStyle(tickFont=QFont("Segoe UI", 9))
            self.chart.setMenuEnabled(False)
            self.chart.setMouseEnabled(x=False, y=False)
            overview_layout.addWidget(self.chart)
        except ImportError:
            overview_layout.addWidget(label("图表暂不可用，价格与依据仍可查看。", "mutedLabel"))
        overview_layout.addWidget(self.chart_note)
        overview_layout.addStretch()
        overview_scroll.setWidget(overview)
        self.tabs.addTab(overview_scroll, "概览")
        self.thesis = self._browser()
        self.tabs.addTab(self.thesis, "依据与风险")
        self.financials = self._browser()
        self.tabs.addTab(self.financials, "估值与财务")
        self.sources = self._browser()
        self.tabs.addTab(self.sources, "来源")
        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_layout.setContentsMargins(0, 14, 0, 0)
        history_layout.addWidget(label("历史研究", "sectionTitle"))
        history_layout.addWidget(label("选择一次分析，回看当时的判断与来源。", "mutedLabel"))
        self.history_table = QTableWidget(0, 2)
        self.history_table.setHorizontalHeaderLabels(["分析时间", "摘要"])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.verticalHeader().setDefaultSectionSize(44)
        self.history_table.setShowGrid(False)
        history_layout.addWidget(self.history_table, 1)
        history_buttons = QHBoxLayout()
        self.history_open_button = QPushButton("查看所选历史")
        self.latest_button = QPushButton("返回最新保存结果")
        history_buttons.addWidget(self.history_open_button)
        history_buttons.addWidget(self.latest_button)
        history_layout.addLayout(history_buttons)
        history_layout.addWidget(label("已登记投入", "sectionTitle"))
        self.ledger_note = _plain_label("尚无实际投入记录。")
        history_layout.addWidget(self.ledger_note)
        self.ledger_table = QTableWidget(0, 5)
        self.ledger_table.setHorizontalHeaderLabels(["确认时间", "月份", "股票", "已投入 USD", "确认来源"])
        self.ledger_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ledger_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ledger_table.verticalHeader().setVisible(False)
        self.ledger_table.verticalHeader().setDefaultSectionSize(42)
        self.ledger_table.setShowGrid(False)
        self.ledger_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.ledger_table.horizontalHeader().setStretchLastSection(True)
        history_layout.addWidget(self.ledger_table, 1)
        self.ledger_refresh_button = QPushButton("重读台账与预算")
        self.ledger_refresh_button.setToolTip("只重读本机记录，不联网、不调用模型。")
        self.ledger_refresh_button.clicked.connect(lambda: self._reload_local_state("本地台账与预算已重读；未联网、未调用模型。"))
        history_layout.addWidget(self.ledger_refresh_button)
        self.tabs.addTab(history_page, "记录")
        right_layout.addWidget(self.tabs, 1)
        splitter.addWidget(right)
        splitter.setSizes([350, 680])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)
        self.status = label("本地保存结果 · 点击更新才联网", "statusLabel", wrap=True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer = QHBoxLayout()
        footer.addWidget(self.information, 3)
        footer.addWidget(self.status, 2)
        root.addLayout(footer)
        self.refresh_button.clicked.connect(self.refresh_data)
        self.cancel_button.clicked.connect(self.cancel_refresh)
        self.plan_button.clicked.connect(self.edit_plan)
        self.model_button.clicked.connect(self.edit_model)
        self.contribution_button.clicked.connect(self.record_contribution)
        self.table.currentCellChanged.connect(self._show_selection)
        self.search.textChanged.connect(self._filter_rows)
        self.filter.currentIndexChanged.connect(self._filter_rows)
        self.chart_range.currentIndexChanged.connect(lambda: self._plot(self._chart_price))
        self.history_open_button.clicked.connect(self.open_history)
        self.history_table.cellDoubleClicked.connect(lambda *_: self.open_history())
        self.latest_button.clicked.connect(self.show_latest)
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._finish_refresh)
        try:
            self._latest_result = self.service.latest()
            self._render(self._latest_result)
            self._load_history()
        except Exception:
            self.status.setText("保存结果暂时无法读取；请手动更新，原记录仍保留。")
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)

    def _browser(self, *, auto_height=False):
        browser = FitTextBrowser() if auto_height else QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setOpenLinks(False)
        browser.anchorClicked.connect(self._open_source)
        browser.document().setDefaultFont(QFont("Microsoft YaHei UI", 10))
        browser.document().setDocumentMargin(8)
        browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return browser

    def _open_source(self, value):
        url = _https_url(value.toString())
        if url:
            QDesktopServices.openUrl(url)

    def _update_engine_note(self):
        kind = self._engine.get("kind")
        note = (f"本机 Codex · {self._engine.get('model') or DEFAULT_ENGINE['model']}：使用现有 ChatGPT 登录，消耗订阅额度。"
                if kind == "codex_cli" else "已有 API 配置：用量由 API 提供商单独计费。" if kind == "api"
                else "引擎配置未能读取，请在模型设置中核对。")
        self.engine_note.setText("打开只读已保存结果，点击更新才联网。" + note)

    def _billing_notice(self):
        return "已发出的 Codex 请求可能已消耗订阅额度。" if self._engine.get("kind") == "codex_cli" else "已发出的 API 请求可能已计费。"

    def _engine_is_stale(self, result):
        prior = (result or {}).get("engine")
        if isinstance(prior, dict) and prior.get("kind"):
            fields = ("kind", "model", "reasoning_effort") if self._engine.get("kind") == "codex_cli" else ("kind",)
            return self._engine_changed or any(prior.get(key) != self._engine.get(key) for key in fields)
        model = ((result or {}).get("provider") or {}).get("model")
        if self._engine.get("kind") == "codex_cli" and model:
            return self._engine_changed or model != self._engine.get("model")
        return self._engine_changed

    def _show_usage(self, result):
        provider = (result or {}).get("provider") or {}
        usage = provider.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        values = []
        for keys, label in ((("input_tokens", "prompt_tokens"), "输入"),
                            (("cached_input_tokens", "cached_tokens"), "缓存输入"),
                            (("output_tokens", "completion_tokens"), "输出"), (("total_tokens",), "合计")):
            value = next((_number(usage.get(key)) for key in keys if _number(usage.get(key)) is not None), None)
            if value is not None and value >= 0 and value.is_integer():
                values.append(f"{label} {int(value):,} tokens")
        if ((result or {}).get("provider") or {}).get("cache_only"):
            self.usage_note.setText("本次逐股复用有效判断，未发起新的模型调用；原判断时间见股票详情。")
            return
        prefix = "本次复用已有模型结果，以下为原调用返回的用量：" if (result or {}).get("model_reused") else "该快照模型返回的实际用量："
        self.usage_note.setText(prefix + "；".join(values) + "。不据此估算剩余额度或费用。" if values
                               else "该快照未提供实际模型用量；不估算剩余额度或费用。")

    def _render(self, result, historical=False):
        previous_symbol = (self._selected() or {}).get("symbol")
        self._result = result if isinstance(result, dict) else None
        self._historical = historical
        if not self._result:
            self.table.setRowCount(0)
            self._expired = False
            self.metadata.setText("尚无保存的研究结果")
            self.freshness.setText("暂无研究结果 · 点击更新分析开始")
            self.summary.setText("手动更新分析后显示逐股判断；本地台账仍可在记录页查看。")
            self.plan_summary.setText("投资计划与台账仅在本地保存，未产生新的模型判断。")
            self.contribution_button.setEnabled(False)
            self._show_usage(None)
            self.condition_card.update_value("—", "等待研究结果")
            self.monthly_card.update_value("待填写", "在投资计划中确认")
            self.spent_card.update_value("—", "本机确认记录")
            self.proposed_card.update_value("待确认", "更新研究并完善计划")
            self.watch_count.setText("0 只")
            self.plan_notice.show()
            self._clear_selection()
            return
        provider = result.get("provider") or {}
        engine_stale = self._engine_is_stale(result)
        try:
            valid_until = datetime.fromisoformat(str(result.get("valid_until")).replace("Z", "+00:00"))
            self._expired = (bool(result.get("expired")) or result.get("actionable") is False
                             or valid_until.tzinfo is None or valid_until <= datetime.now(timezone.utc))
        except (TypeError, ValueError):
            self._expired = True
        prefix = "历史快照 · " if historical else "最新保存结果 · "
        if not historical and self._expired:
            prefix += "有效期已过或未确认，请手动更新 · "
        if not historical and engine_stale:
            prefix += "旧模型结果，待手动更新 · "
        elif not result.get("engine"):
            prefix += "原快照未记录引擎 · "
        self.metadata.setText(prefix + f"分析时间：{result.get('generated_at') or '未知'}  |  "
                              f"价格截至：{result.get('price_as_of') or '待核实'}  |  "
                              f"模型：{provider.get('model') or '未配置'}  |  状态：{provider.get('status') or '未知'}")
        freshness_state = "历史快照" if historical else "需更新" if self._expired or engine_stale else "已保存"
        self.freshness.setText(f"{freshness_state} · 行情截至 {result.get('price_as_of') or '待核实'} · 分析 {format_time(result.get('generated_at'), short=True)}（北京时间）")
        self.freshness.setToolTip(self.metadata.text())
        self._show_usage(result)
        self.summary.setText(str(result.get("summary") or "部分研究仍待核实，请逐只查看详情。"))
        plan_status = result.get("plan_status") or {}
        if not isinstance(plan_status, dict):
            plan_status = {}
        self.plan_summary.setText(f"计划月份：{plan_status.get('month') or '待确认'}  |  "
            f"本月预算：{_money(plan_status.get('budget_usd'))} USD  |  "
            f"已确认投入：{_money(plan_status.get('confirmed_spent_usd'))} USD  |  "
            f"本次建议合计：{_money(plan_status.get('proposed_usd'))} USD  |  "
            f"尚未分配：{_money(plan_status.get('remaining_unallocated_usd'))} USD。 "
            + ("计划信息已齐备。" if plan_status.get("ready") else "计划缺口：" + "；".join(str(v) for v in plan_status.get("gaps", [])))
            + str(plan_status.get("note") or ""))
        assessments = result.get("assessments") or []
        eligible = sum(item.get("decision") in {"START", "NORMAL", "INCREASE"} for item in assessments)
        stale = not historical and (self._plan_dirty or self._expired or engine_stale)
        self.condition_card.update_value("待更新" if stale else f"{eligible} / {len(assessments)}", "公司层判断 · 金额受计划约束", "brand")
        self.monthly_card.update_value(_money(plan_status.get("budget_usd")), f"{plan_status.get('month') or current_period()} · USD")
        self.spent_card.update_value(_money(plan_status.get("confirmed_spent_usd")), "已确认的本机投入记录 · USD")
        proposed = "待更新" if stale else _money(plan_status.get("proposed_usd")) if plan_status.get("ready") else "待完善计划"
        self.proposed_card.update_value(proposed, "历史金额，仅供复盘" if historical else "按计划计算 · USD")
        self.plan_notice.setVisible(not plan_status.get("ready") and not historical)
        self.plan_notice_text.setText("预算与持仓尚待确认，先看公司判断；完善计划后显示本期金额。")
        self.plan_notice_text.setToolTip("；".join(str(value) for value in plan_status.get("gaps") or []))
        self.watch_count.setText(f"{len(assessments)} 只")
        short_labels = {"START": "小额开始", "NORMAL": "正常投入", "INCREASE": "更有吸引力", "PAUSE": "暂缓新增", "REVIEW": "待核验"}
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(assessments))
            for row, item in enumerate(assessments):
                price = item.get("price") or {}
                budget = item.get("budget") or {}
                values = [f"{item.get('symbol', '未知')}\n{item.get('name') or ''}", "待更新" if stale else short_labels.get(item.get("decision"), item.get("decision_label") or "待复核"),
                          _money(price.get("close")), ("待更新计划" if self._plan_dirty else "旧模型待更新" if engine_stale else "已过期待更新")
                          if stale else _money(budget.get("amount_usd"))]
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setToolTip(str(item.get("decision_label") or "") if col == 1 else str(budget.get("reason") or "") if col == 3 else str(value))
                    if col == 1:
                        color = "#657187" if stale else {"START": "#16724c", "NORMAL": "#16724c", "INCREASE": "#2446d7", "PAUSE": "#9d6a1a"}.get(item.get("decision"), "#657187")
                        cell.setForeground(QColor(color))
                    if col >= 2:
                        cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.table.setItem(row, col, cell)
        finally:
            self.table.blockSignals(False)
        if assessments:
            selected_row = next((n for n, item in enumerate(assessments) if item.get("symbol") == previous_symbol), 0)
            self.table.selectRow(selected_row)
            self._show_selection()
        else:
            self._clear_selection()
        self._filter_rows()

    def _filter_rows(self, *_):
        query = self.search.text().strip().casefold()
        mode = self.filter.currentData()
        visible = []
        stale = not self._historical and (self._plan_dirty or self._expired or self._engine_is_stale(self._result))
        for row, item in enumerate((self._result or {}).get("assessments") or []):
            decision = "REVIEW" if stale else item.get("decision")
            matches = query in f"{item.get('symbol', '')} {item.get('name', '')}".casefold()
            matches = matches and (mode == "all" or mode == "eligible" and decision in {"START", "NORMAL", "INCREASE"}
                                   or mode == "pause" and decision == "PAUSE" or mode == "review" and decision == "REVIEW")
            self.table.setRowHidden(row, not matches)
            if matches:
                visible.append(row)
        self.no_matches.setVisible(not visible and bool(self._result))
        if not visible:
            self.table.setCurrentCell(-1, -1)
            self._clear_selection()
        elif self.table.currentRow() not in visible:
            self.table.selectRow(visible[0])
        self.contribution_button.setEnabled(bool(visible) and not self._historical and self._future is None)

    def _clear_selection(self):
        self.stock_title.setText("选择一只股票")
        self.stock_subtitle.setText("查看价格、判断与依据")
        self.decision_badge.setText("待选择")
        set_tone(self.decision_badge, "muted")
        for value in self.stock_metrics.values():
            value.setText("—")
        self.selection_warning.hide()
        self.details.setHtml(empty_html("从一只股票开始", "选择研究清单中的股票，或手动更新分析。"))
        for browser in (self.thesis, self.financials, self.sources):
            browser.setHtml(empty_html("暂无内容", "选择股票后查看对应的依据和来源。"))
        self._plot({})

    def _selected(self):
        row = self.table.currentRow()
        items = (self._result or {}).get("assessments") or []
        return items[row] if 0 <= row < len(items) else None

    def _show_selection(self, *_):
        item = self._selected()
        if not item:
            self._clear_selection()
            return
        price, valuation, budget = (item.get(k) or {} for k in ("price", "valuation", "budget"))
        evidence = ((self._result or {}).get("public_evidence") or {}).get(item.get("symbol")) or {}
        source_rows = {}
        for source in [*(evidence.get("sources") or []), *(item.get("sources") or [])]:
            if isinstance(source, dict):
                source_rows[str(source.get("id") or source.get("source_id") or source.get("url"))] = source
        source_index = {str(source.get("id") or source.get("source_id")): source for source in source_rows.values()}
        confidence = {"low": "低", "medium": "中", "high": "高"}.get(item.get("confidence"), "待核实")
        method = {"pe_ttm": "过去十二个月市盈率", "normalized_pe": "正常化盈利市盈率（含假设）",
                  "ps_ttm": "过去十二个月市销率", "ev_sales": "企业价值与销售额之比"}.get(valuation.get("method"), "待核实")
        scenario = valuation.get("scenario_price_range")
        scenario_text = (f"{_money(scenario[0])} 至 {_money(scenario[1])} USD" if isinstance(scenario, list) and len(scenario) == 2 else "待核实")
        warnings = []
        if self._historical:
            warnings.append("历史快照，仅供复盘，不代表当前建议。")
        else:
            if self._plan_dirty:
                warnings.append("计划或投入记录已变化：旧计划金额暂不使用。")
            if self._expired:
                warnings.append("有效期已过或未确认：仅供复盘，请手动更新。")
            if self._engine_is_stale(self._result):
                warnings.append("模型设置已变更：当前仍为旧模型结果，请手动更新。")
        warning_html = "".join(f'<p style="color:#8a5b11"><b>{_text(warning)}</b></p>' for warning in warnings)
        self.selection_warning.setText(" ".join(warnings))
        self.selection_warning.setVisible(bool(warnings))
        stale = bool(warnings) and not self._historical
        decision = item.get("decision")
        short_labels = {"START": "小额开始", "NORMAL": "正常投入", "INCREASE": "更有吸引力", "PAUSE": "暂缓新增", "REVIEW": "待核验"}
        self.stock_title.setText(f"{item.get('symbol') or '未知'} · {item.get('name') or '名称待核实'}")
        self.stock_subtitle.setText(f"行情日期 {price.get('date') or '待核实'} · {'历史研究' if self._historical else '公司层研究'}")
        self.decision_badge.setText("需要更新" if stale else short_labels.get(decision, "待核验"))
        self.decision_badge.setToolTip(str(item.get("decision_label") or "待核验"))
        set_tone(self.decision_badge, "warning" if stale else {"START": "positive", "NORMAL": "positive", "INCREASE": "brand", "PAUSE": "warning"}.get(decision, "muted"))
        self.stock_metrics["price"].setText(_money(price.get("close")))
        self.stock_metrics["drawdown"].setText(_percent(price.get("drawdown_pct")))
        amount = "待更新" if stale else _money(budget.get("amount_usd"))
        self.stock_metrics["budget"].setText(amount)
        self.stock_metrics["budget"].setToolTip(str(budget.get("reason") or ""))
        self.details.setHtml(document_html(warning_html
            + f"<h3>本期判断 · {_text(item.get('decision_label'))}</h3>"
            + f"<p>{_text(item.get('summary'))}</p>"
            + f"<p style='color:#657187'>下次复核：{_text(item.get('next_review'))}</p>"))
        self.thesis.setHtml(document_html(warning_html
            + f"<h2>判断依据</h2><p>{_text(item.get('summary'))}</p>{_items(item.get('reasons'))}"
            + f"<h3>需要承担的风险</h3>{_items(item.get('risks'))}"
            + f"<h3>本期投入安排</h3><p><b>{_money(budget.get('amount_usd'))} USD · {_text(budget.get('label'))}</b></p>"
            + f"<p>{_text(budget.get('reason'))}</p>"
            + f"<p>本期已确认投入：{_money(budget.get('spent_this_month_usd'))} USD。{_text(budget.get('note'))}</p>"
            + _overlap_html(item.get("symbol"))
            + f"<h3>何时复核</h3><p>{_text(item.get('next_review'))}</p>"
            + f"<p>结果有效至：{_text(format_time((self._result or {}).get('valid_until')))}（北京时间）</p>"
            + f"<h3>什么变化会推翻判断</h3>{_items(item.get('invalidators'))}"
            + f"<h3>与上次研究的差异</h3>{_comparison_html((self._result or {}).get('comparison'), item.get('symbol'))}"
            + f"<h3>研究记录</h3><p>模型判断把握（非上涨概率）：{confidence}</p>"
            + f"<p>模型判断生成时间：{_text(format_time(item.get('model_generated_at') or (self._result or {}).get('generated_at')))}（北京时间）。"
            + ("本次该股证据未变，复用有效判断。</p>" if item.get("model_reused") else "</p>")
            + ("<p><b>部分数据或模型结果未通过校验，不完整信息已保留为待核实。</b></p>" if item.get("errors") else "")))
        self.financials.setHtml(document_html(warning_html
            + "<h2>价格与估值</h2>"
            + f"<p>最近收盘：{_money(price.get('close'))} USD · 交易日期 {_text(price.get('date'))}</p>"
            + f"<p>相对区间高点变动：{_percent(price.get('drawdown_pct'))} · 区间价格分位：{_percent(price.get('percentile'))} · "
            + f"有效历史天数：{_text(price.get('history_days'))}。价格分位不代表估值或上涨概率。</p>"
            + f"<h3>{_text(valuation.get('label'))} · {method}</h3>"
            + f"<p>{_text(valuation.get('explanation'))}</p>{_valuation_table(valuation, source_index)}"
            + f"<h3>情景假设</h3><p>模型假设推算价格区间：<b>{scenario_text}</b></p>"
            + f"<p>{_text(valuation.get('warning'), '该区间不是价格预测或收益保证。')}</p>"
            + _items(valuation.get('assumptions'))
            + f"<h2>经营事实</h2>{_financial_table(item.get('fundamentals'), source_index)}"))
        self.sources.setHtml(document_html(_sources_html(list(source_rows.values()), evidence)))
        self._plot(price)

    def _plot(self, price):
        self._chart_price = price
        if self.chart is None:
            return
        self.chart.clear()
        points = {}
        for row in price.get("rows") or []:
            if not isinstance(row, dict):
                continue
            close = _number(row.get("close"))
            if close is None or close <= 0:
                continue
            try:
                day = date.fromisoformat(str(row.get("date")))
                timestamp = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            points[timestamp] = close
        days = self.chart_range.currentData()
        if points and days is not None:
            cutoff = max(points) - int(days) * 86400
            points = {stamp: close for stamp, close in points.items() if stamp >= cutoff}
        self.chart_note.setText(f"真实历史收盘价 · {len(points)} 个有效交易日 · USD。非预测；缺失值未按零补齐。" if points else "暂无有效历史价格，不能绘制走势图。缺失数据不按零处理。")
        if points:
            import pyqtgraph as pg
            ordered = sorted(points)
            self.chart.plot(ordered, [points[x] for x in ordered], pen=pg.mkPen("#2446d7", width=2.4),
                            symbol="o" if len(ordered) == 1 else None, symbolSize=6)
            self.chart.enableAutoRange()

    def refresh_data(self):
        if self._future is not None:
            return
        self._cancelled.clear()
        self._progress_count = 0
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="installment-research")
        for button in (self.refresh_button, self.plan_button, self.model_button, self.contribution_button):
            button.setEnabled(False)
        self.refresh_button.setText("更新中…")
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.progress_bar.show()
        self.refresh_state_changed.emit(True)
        self.status.setText("正在更新公开数据与模型分析，仍可查看上次结果。" + ("本机 Codex 将消耗订阅额度。" if self._engine.get("kind") == "codex_cli" else "API 调用单独计费。"))
        self._future = self._executor.submit(self.service.refresh, cancelled=self._cancelled, progress=self._progress)
        self._poll.start()

    def _progress(self, *_args, **_kwargs):
        # Worker callbacks never touch Qt or expose raw provider/error strings.
        self._progress_count += 1

    def cancel_refresh(self):
        if self._future is not None:
            self._cancelled.set()
            self.cancel_button.setEnabled(False)
            self.status.setText("已请求取消，等待当前请求结束；保留上次显示结果。" + self._billing_notice())

    def _finish_refresh(self):
        if self._future is None or not self._future.done():
            return
        self._poll.stop()
        try:
            result = self._future.result()
            if self._cancelled.is_set():
                raise CancelledError()
            if not isinstance(result, dict) or not isinstance(result.get("assessments"), list):
                raise ValueError("Invalid research result")
            self._latest_result = result
            self._plan_dirty = False
            self._engine_changed = False
            self._render(result)
            try:
                self._load_history()
            except Exception:
                self.status.setText("本次分析已更新，但历史列表暂时无法读取；稍后重新打开查看。")
                return
            self.status.setText("更新完成；请结合来源、风险和计划查看逐股判断。" if not result.get("errors")
                                else "更新完成，部分信息待核实；请查看逐股详情，缺失信息不作为零或通过处理。")
        except CancelledError:
            self.status.setText("更新已取消，上次显示结果已保留。" + self._billing_notice())
        except Exception:
            self.status.setText("更新未完成，上次结果已保留。请检查网络与模型设置后重试；未显示原始服务错误。")
        finally:
            self._future = None
            for button in (self.refresh_button, self.plan_button, self.model_button):
                button.setEnabled(True)
            self.contribution_button.setEnabled(self._selected() is not None and not self._historical)
            self.cancel_button.setEnabled(False)
            self.refresh_button.setText("更新分析")
            self.cancel_button.hide()
            self.progress_bar.hide()
            self.refresh_state_changed.emit(False)

    def _load_history(self):
        self._history = self.service.history() or []
        self.history_table.setRowCount(len(self._history))
        for row, item in enumerate(self._history):
            self.history_table.setItem(row, 0, QTableWidgetItem(format_time(item.get("generated_at") or item.get("run_id"))))
            self.history_table.setItem(row, 1, QTableWidgetItem(str(item.get("summary") or "保存的研究快照")))
        self.history_table.resizeColumnToContents(0)
        self._load_ledger()

    def _load_ledger(self):
        self._contributions = self.service.contributions()
        self.ledger_table.setRowCount(len(self._contributions))
        current_total = 0.0
        for row, item in enumerate(reversed(self._contributions)):
            if item.get("period") == current_period():
                amount = _number(item.get("amount_usd"))
                if amount is not None:
                    current_total += amount
            values = [format_time(item.get("confirmed_at")), item.get("period") or "待核实",
                      item.get("symbol") or "待核实", _money(item.get("amount_usd")), item.get("source_ref") or "待核实"]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip("记录编号：" + str(item.get("event_id") or "待核实"))
                self.ledger_table.setItem(row, col, cell)
        self.ledger_note.setText(f"共 {len(self._contributions)} 笔确认记录；{current_period()} 已确认投入合计 {_money(current_total)} USD。"
                                if self._contributions else "尚无实际投入记录。研究建议不会自动写入此台账。")

    def _reload_local_state(self, message):
        if self._future is not None:
            self.status.setText("更新进行中；完成或取消后可重读本地台账。")
            return
        try:
            self._load_ledger()
            latest = self.service.latest()
            self._latest_result = latest
            self._plan_dirty = False
            self._render(latest)
            self.status.setText(message)
        except Exception:
            self._mark_plan_changed("本地台账或预算读取未完成；当前金额暂不使用。请重读本地台账核对，避免重复登记。")

    def open_history(self):
        row = self.history_table.currentRow()
        if not 0 <= row < len(self._history):
            self.status.setText("请先选择一条历史记录。")
            return
        try:
            result = self.service.load_run(self._history[row]["run_id"])
            if not isinstance(result, dict):
                raise ValueError("Missing run")
            self._render(result, historical=True)
            self.tabs.setCurrentIndex(0)
            self.status.setText("正在查看历史快照；点击“记录 → 返回最新保存结果”恢复。")
        except Exception:
            self.status.setText("这条历史记录暂时无法读取，当前页面已保留。")

    def show_latest(self):
        if self._future is None:
            self._reload_local_state("已返回最新保存结果；本地预算已重算，未联网更新。")
        else:
            self._render(self._latest_result)
        self.tabs.setCurrentIndex(0)

    def edit_plan(self):
        if self._future is not None:
            return
        try:
            plan = self.service.load_plan()
            symbols = sorted(set((plan.get("target_weights") or {}).keys())
                             | set((plan.get("positions_usd") or {}).keys())
                             | {a["symbol"] for a in (self._latest_result or {}).get("assessments", [])})
            if not symbols:
                symbols = list(SYMBOLS) + list(CORE_SYMBOLS)
            dialog = InvestmentPlanDialog(plan, symbols, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self.service.save_plan(dialog.plan)
            self._reload_local_state("投资计划已保存，本地预算已重新计算；经营判断与公开数据未更新，保存计划不代表交易。")
        except Exception:
            self.status.setText("计划未能保存，请检查填写内容后重试；未显示原始服务错误。")

    def _mark_plan_changed(self, text):
        self._plan_dirty = True
        self._render(self._result, historical=self._historical)
        self.status.setText(text)

    def edit_model(self):
        if self._future is not None:
            return
        try:
            current = self.service.load_engine()
            dialog = EngineSettingsDialog(current, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                if dialog.api_settings_changed and current.get("kind") == "api":
                    self._engine_changed = True
                    self._render(self._result, historical=self._historical)
                    self.status.setText("已有 API 配置已修改，旧结果仍来自原配置；下次手动更新才调用。")
                return
            self._engine = self.service.save_engine(dialog.config)
            self._engine_changed = self._engine_changed or self._engine != current or dialog.api_settings_changed
            self._update_engine_note()
            self._render(self._result, historical=self._historical)
            self.status.setText("本功能引擎设置已保存。旧结果仍来自原模型；下次手动更新才使用新引擎，本次未联网调用。")
        except Exception:
            self.status.setText("引擎设置未能完成，请检查本地配置；未显示密钥或原始错误。")

    def record_contribution(self):
        item = self._selected()
        if not item or self._historical or self._future is not None:
            return
        try:
            plan = self.service.load_plan()
        except Exception:
            self.status.setText("无法读取投资计划，本次未登记投入。")
            return
        symbol = str(item.get("symbol"))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"登记 {symbol} 已完成的一笔投入")
        layout = QVBoxLayout(dialog)
        layout.addWidget(_plain_label("只填写你实际完成的新增投入金额。这是新增一笔记录，不会覆盖本期累计值，也不会执行交易。"))
        form = QFormLayout()
        amount = QLineEdit()
        amount.setPlaceholderText("实际投入金额，不自动填入建议金额")
        period = QLineEdit(current_period())
        period.setReadOnly(True)
        form.addRow("这笔已投入金额（USD）", amount)
        form.addRow("所属月份（仅当前月份）", period)
        layout.addLayout(form)
        error = _plain_label()
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("确认实际投入并登记")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def save():
            try:
                value = InvestmentPlanDialog._parse(amount.text(), "投入金额（USD）", maximum=1_000_000_000, required=True)
                month = period.text().strip()
                if value < .01:
                    raise ValueError("新增投入金额须至少为 0.01 USD。")
                if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
                    raise ValueError("月份须为有效的 YYYY-MM。")
            except ValueError as exc:
                error.setText(str(exc))
                return
            confirm = QMessageBox(self)
            confirm.setWindowTitle("确认实际投入")
            confirm.setTextFormat(Qt.TextFormat.PlainText)
            confirm.setText(f"确认你在 {month} 已实际投入 {symbol} {_money(value)} USD？\n这将新增一笔记录，请勿重复登记同一笔投入。")
            confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            try:
                self.service.record_contribution(symbol, value, month, source_ref="用户在软件确认")
            except Exception:
                error.setText("登记结果未确认，请核对记录后重试，避免重复登记。")
                return
            dialog.accept()

        buttons.accepted.connect(save)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload_local_state("已登记用户确认的实际投入，台账与本期剩余预算已在本机重新计算；未联网、未执行交易。")

    def shutdown(self):
        self._cancelled.set()
        self._poll.stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
