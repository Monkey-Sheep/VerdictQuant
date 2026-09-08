"""Manual, cache-first installment research. No brokerage or execution actions."""
from __future__ import annotations

import copy
import html
import math
import re
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import date, datetime, timezone

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from pa_agent.installment.models import SYMBOLS, CORE_SYMBOLS, current_period

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


def _https_url(value):
    url = QUrl(str(value or ""))
    return url if (url.isValid() and url.scheme() == "https" and url.host()
                   and not url.userName() and not url.password()) else None


def _percent(value):
    number = _number(value)
    return "待核实" if number is None else f"{number:.2f}%"


def _link(title, value):
    url = _https_url(value)
    return f'<a href="{_text(url.toString())}" style="color:#7dd3fc">{_text(title)}</a>' if url else _text(title)


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
             '<table width="100%" cellspacing="0" cellpadding="6" border="1"><tr><th>指标 / 期间</th><th>正式披露</th><th>供应商数据</th></tr>']
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
    parts = ['<table width="100%" cellspacing="0" cellpadding="6" border="1"><tr><th>观测指标</th><th>倍数</th><th>观测日期</th><th>来源 / 状态</th></tr>']
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


class InvestmentPlanDialog(QDialog):
    """Blank numeric entries stay unknown; displayed examples are never saved."""

    def __init__(self, plan, symbols, parent=None):
        super().__init__(parent)
        self.setWindowTitle("投资计划 · 所有金额均为 USD")
        self.resize(720, 760)
        self._original = copy.deepcopy(plan)
        self.plan = None
        root = QVBoxLayout(self)
        root.addWidget(_plain_label("填写你实际确认的计划。留空表示未知；研究判断不会自动成为投入记录。"))
        form = QFormLayout()
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
            edit.setPlaceholderText("留空：未知" if key != "period" else "YYYY-MM")
            self.fields[key] = edit
            form.addRow(label, edit)
        root.addLayout(form)
        root.addWidget(_plain_label("单次分批比例只作用于该股票本月尚未投入的计划额度，不是全账户比例；须满足：高风险 ≤ 正常 ≤ 更有吸引力。可自行调整默认比例。"))
        root.addWidget(_plain_label("目标权重用于分配新增预算，总和不能超过 100%。当前持仓不清楚时请留空。"))
        root.addWidget(_plain_label("核心基金只填写市值，不参与此处新增个股预算；非美元资产请按你确认的汇率折算为 USD。"))
        root.addWidget(_plain_label("填写或重新确认持仓时，金额应包括此前已完成的投入。只修改预算或年限不更新未编辑持仓的确认时间；保存计划不代表交易。"))
        self.assets = QTableWidget(len(symbols), 3)
        self.assets.setHorizontalHeaderLabels(["股票", "新增预算目标权重 %", "当前持仓市值 USD"])
        self.assets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.assets.verticalHeader().setVisible(False)
        for row, symbol in enumerate(symbols):
            item = QTableWidgetItem(symbol)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.assets.setItem(row, 0, item)
            for col, key in ((1, "target_weights"), (2, "positions_usd")):
                value = (plan.get(key) or {}).get(symbol)
                cell = QTableWidgetItem("" if value is None else str(value))
                if col == 1 and symbol in CORE_SYMBOLS:
                    cell.setText("不参与个股分配")
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.assets.setItem(row, col, cell)
        root.addWidget(self.assets, 1)
        self.confirm_holdings = QCheckBox("我已逐项更新并确认所有持仓市值（含已投入）")
        self.confirm_holdings.setChecked(False)
        self.confirm_holdings.setToolTip("只有你已逐项重新核对持仓时才勾选。普通保存预算或年限不更新未编辑股票的持仓确认时间。")
        root.addWidget(self.confirm_holdings)
        self.error = _plain_label()
        self.error.setStyleSheet("color:#f59e0b")
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存计划")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_plan)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

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


class EngineSettingsDialog(QDialog):
    """Feature-local routing only. API credentials remain in their existing settings."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
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
    def __init__(self, parent=None, service=None):
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
        self.setWindowTitle("VerdictQuant · 分批投资研究")
        self.resize(1220, 850)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        header = QHBoxLayout()
        title = _plain_label("分批投资研究")
        title.setStyleSheet("font-size:24px;font-weight:700")
        header.addWidget(title)
        header.addStretch()
        self.refresh_button = QPushButton("更新分析")
        self.refresh_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("取消更新")
        self.cancel_button.setEnabled(False)
        self.plan_button = QPushButton("投资计划")
        self.model_button = QPushButton("模型设置")
        for button in (self.refresh_button, self.cancel_button, self.plan_button, self.model_button):
            button.setMinimumHeight(36)
            header.addWidget(button)
        root.addLayout(header)
        self.engine_note = _plain_label()
        self._update_engine_note()
        root.addWidget(self.engine_note)
        self.metadata = _plain_label("尚无保存结果")
        root.addWidget(self.metadata)
        self.usage_note = _plain_label("尚无模型返回的实际用量；不估算剩余额度或费用。")
        root.addWidget(self.usage_note)
        self.summary = _plain_label("先填写投资计划，再手动更新分析。未知预算与持仓不会按零计算。")
        root.addWidget(self.summary)
        self.plan_summary = _plain_label("投资计划尚待确认；金额为 USD。")
        root.addWidget(self.plan_summary)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["股票", "当前判断", "收盘 USD", "本期建议 USD"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumWidth(410)
        left_layout.addWidget(self.table, 1)
        self.contribution_button = QPushButton("记录本期已投入（USD）")
        self.contribution_button.setEnabled(False)
        self.contribution_button.setToolTip("只登记你已完成的投入，不下单，不把研究建议当作成交。")
        left_layout.addWidget(self.contribution_button)
        left_layout.addWidget(_plain_label("建议金额不是成交记录。所有股票金额为 USD；真实持仓只以你的确认记录为准。"))
        splitter.addWidget(left)
        self.tabs = QTabWidget()
        self.details = self._browser()
        self.tabs.addTab(self.details, "判断详情")
        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        self.chart_note = _plain_label("仅显示真实历史收盘价，不生成预测曲线。")
        chart_layout.addWidget(self.chart_note)
        self.chart = None
        try:
            import pyqtgraph as pg
            self.chart = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(utcOffset=0)})
            self.chart.setBackground("#0a0e14")
            self.chart.showGrid(x=True, y=True, alpha=0.2)
            self.chart.setLabel("left", "历史收盘价", units="USD")
            self.chart.setLabel("bottom", "交易日期")
            self.chart.setMenuEnabled(False)
            chart_layout.addWidget(self.chart, 1)
        except ImportError:
            chart_layout.addWidget(_plain_label("图表组件不可用，仍可在判断详情中查看价格和数据缺口。"))
        self.tabs.addTab(chart_page, "走势图")
        self.sources = self._browser()
        self.tabs.addTab(self.sources, "来源")
        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_layout.addWidget(_plain_label("历史判断仅供复盘。选中记录查看当时的数据与来源，不能视为当前建议。"))
        self.history_table = QTableWidget(0, 2)
        self.history_table.setHorizontalHeaderLabels(["分析时间", "摘要"])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        history_layout.addWidget(self.history_table, 1)
        history_buttons = QHBoxLayout()
        self.history_open_button = QPushButton("查看所选历史")
        self.latest_button = QPushButton("返回最新保存结果")
        history_buttons.addWidget(self.history_open_button)
        history_buttons.addWidget(self.latest_button)
        history_layout.addLayout(history_buttons)
        history_layout.addWidget(_plain_label("已确认投入台账（本地） · 用户登记，未经券商核验"))
        self.ledger_note = _plain_label("尚无实际投入记录。")
        history_layout.addWidget(self.ledger_note)
        self.ledger_table = QTableWidget(0, 5)
        self.ledger_table.setHorizontalHeaderLabels(["确认时间", "月份", "股票", "已投入 USD", "确认来源"])
        self.ledger_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ledger_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ledger_table.verticalHeader().setVisible(False)
        self.ledger_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.ledger_table.horizontalHeader().setStretchLastSection(True)
        history_layout.addWidget(self.ledger_table, 1)
        self.ledger_refresh_button = QPushButton("重读本地台账与预算（不联网）")
        self.ledger_refresh_button.clicked.connect(lambda: self._reload_local_state("本地台账与预算已重读；未联网、未调用模型。"))
        history_layout.addWidget(self.ledger_refresh_button)
        self.tabs.addTab(history_page, "历史")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)
        self.status = _plain_label("仅读取本地保存结果；不会定时更新，也不会执行交易。")
        root.addWidget(self.status)
        self.refresh_button.clicked.connect(self.refresh_data)
        self.cancel_button.clicked.connect(self.cancel_refresh)
        self.plan_button.clicked.connect(self.edit_plan)
        self.model_button.clicked.connect(self.edit_model)
        self.contribution_button.clicked.connect(self.record_contribution)
        self.table.currentCellChanged.connect(self._show_selection)
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

    def _browser(self):
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setOpenLinks(False)
        browser.anchorClicked.connect(self._open_source)
        browser.setStyleSheet("QTextBrowser { padding:12px; font-size:14px; }")
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
        self._result = result if isinstance(result, dict) else None
        self._historical = historical
        if not self._result:
            self.table.setRowCount(0)
            self.metadata.setText("尚无保存的研究结果")
            self.summary.setText("手动更新分析后显示逐股判断；本地台账仍可在历史页查看。")
            self.plan_summary.setText("投资计划与台账仅在本地保存，未产生新的模型判断。")
            self.contribution_button.setEnabled(False)
            self._show_usage(None)
            self.details.setHtml("<h2>尚无保存的研究</h2><p>填写投资计划，然后点击“更新分析”。</p>")
            self.sources.setHtml("<p>手动更新后显示实际使用的公开来源。</p>")
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
        self._show_usage(result)
        self.summary.setText(str(result.get("summary") or "部分研究仍待核实，请逐只查看详情。"))
        plan_status = result.get("plan_status") or {}
        if isinstance(plan_status, dict):
            self.plan_summary.setText(f"计划月份：{plan_status.get('month') or '待确认'}  |  "
                f"本月预算：{_money(plan_status.get('budget_usd'))} USD  |  "
                f"已确认投入：{_money(plan_status.get('confirmed_spent_usd'))} USD  |  "
                f"本次建议合计：{_money(plan_status.get('proposed_usd'))} USD  |  "
                f"尚未分配：{_money(plan_status.get('remaining_unallocated_usd'))} USD\n"
                + ("计划信息已齐备。" if plan_status.get("ready") else "计划缺口：" + "；".join(str(v) for v in plan_status.get("gaps", [])))
                + str(plan_status.get("note") or ""))
        assessments = result.get("assessments") or []
        self.table.setRowCount(len(assessments))
        for row, item in enumerate(assessments):
            price = item.get("price") or {}
            budget = item.get("budget") or {}
            stale = not historical and (self._plan_dirty or self._expired or engine_stale)
            values = [item.get("symbol", "未知"), "待更新" if stale else item.get("decision_label") or "待复核",
                      _money(price.get("close")), ("待更新计划" if self._plan_dirty else "旧模型待更新" if engine_stale else "已过期待更新")
                      if stale else _money(budget.get("amount_usd"))]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if col >= 2:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, cell)
        if assessments:
            self.table.selectRow(0)
            self._show_selection()
        else:
            self.details.setHtml("<p>本次没有可显示的股票判断，请查看更新状态并重试。</p>")
            self.sources.clear()
            if self.chart:
                self.chart.clear()
        self.contribution_button.setEnabled(bool(assessments) and not historical and self._future is None)

    def _selected(self):
        row = self.table.currentRow()
        items = (self._result or {}).get("assessments") or []
        return items[row] if 0 <= row < len(items) else None

    def _show_selection(self, *_):
        item = self._selected()
        if not item:
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
        history = "<p><b>历史快照：以下内容不代表当前建议。</b></p>" if self._historical else ""
        dirty = "<p><b>计划或投入记录已变化：本页金额来自旧计划，需手动更新后使用。</b></p>" if self._plan_dirty and not self._historical else ""
        expired = "<p><b>有效期已过或无法确认：以下是原快照，仅供复盘，请手动更新后再决定本期投入。</b></p>" if self._expired and not self._historical else ""
        engine_warning = "<p><b>引擎设置已变更：以下仍是旧模型的结果，需手动更新才使用新引擎分析。</b></p>" if self._engine_is_stale(self._result) and not self._historical else ""
        self.details.setHtml(history + dirty + expired + engine_warning + f"<h2>{_text(item.get('symbol'))} · {_text(item.get('name'))}</h2>"
            f"<h3>{_text(item.get('decision_label'))}</h3><p>模型判断把握（非上涨概率）：{confidence}</p>"
            f"<p>模型判断生成时间：{_text(item.get('model_generated_at') or (self._result or {}).get('generated_at'))}。"
            + ("本次该股证据未变，复用有效判断。</p>" if item.get("model_reused") else "</p>")
            +
            f"<p>{_text(item.get('summary'))}</p>"
            f"<p><b>本期建议：{_money(budget.get('amount_usd'))} USD</b>　{_text(budget.get('label'))}</p>"
            f"<p>{_text(budget.get('reason'))}</p>"
            f"<p>本期已确认投入：{_money(budget.get('spent_this_month_usd'))} USD。{_text(budget.get('note'))}</p>"
            f"<h3>判断依据</h3>{_items(item.get('reasons'))}"
            f"<h3>需要承担的风险</h3>{_items(item.get('risks'))}"
            f"<h3>价格与估值</h3><p>最近收盘：{_money(price.get('close'))} USD；"
            f"交易日期：{_text(price.get('date'))}。{_text(price.get('label'))}</p>"
            f"<p>相对区间高点变动：{_percent(price.get('drawdown_pct'))}；区间价格分位：{_percent(price.get('percentile'))}；"
            f"有效历史天数：{_text(price.get('history_days'))}。价格分位不代表估值或上涨概率。</p>"
            f"<p>{_text(valuation.get('label'))} · {method}</p>"
            f"<p>{_text(valuation.get('explanation'))}</p>{_valuation_table(valuation, source_index)}"
            f"<p>模型假设推算价格区间：{scenario_text}。</p>"
            f"<p>{_text(valuation.get('warning'), '该区间不是价格预测或收益保证。')}</p>"
            f"<p><b>区间依赖的假设</b></p>{_items(valuation.get('assumptions'))}"
            f"<h3>经营事实</h3>{_financial_table(item.get('fundamentals'), source_index)}"
            f"<h3>何时复核</h3><p>{_text(item.get('next_review'))}</p>"
            f"<p>结果有效至：{_text((self._result or {}).get('valid_until'))}</p>"
            f"<h3>什么变化会推翻判断</h3>{_items(item.get('invalidators'))}"
            f"<h3>与上次研究的差异</h3>{_comparison_html((self._result or {}).get('comparison'), item.get('symbol'))}"
            + ("<p><b>部分数据或模型结果未通过校验，不完整信息已保留为待核实。</b></p>" if item.get("errors") else ""))
        self.sources.setHtml(_sources_html(list(source_rows.values()), evidence))
        self._plot(price)

    def _plot(self, price):
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
        self.chart_note.setText(f"真实历史收盘价 · {len(points)} 个有效交易日 · USD。非预测；缺失值未按零补齐。" if points else "暂无有效历史价格，不能绘制走势图。缺失数据不按零处理。")
        if points:
            import pyqtgraph as pg
            ordered = sorted(points)
            self.chart.plot(ordered, [points[x] for x in ordered], pen=pg.mkPen("#38bdf8", width=2),
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

    def _load_history(self):
        self._history = self.service.history() or []
        self.history_table.setRowCount(len(self._history))
        for row, item in enumerate(self._history):
            self.history_table.setItem(row, 0, QTableWidgetItem(str(item.get("generated_at") or item.get("run_id") or "未知")))
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
            values = [item.get("confirmed_at") or "待核实", item.get("period") or "待核实",
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
            self.status.setText("正在查看历史快照；点击“历史 → 返回最新保存结果”恢复。")
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
        for row in range(self.table.rowCount()):
            self.table.setItem(row, 3, QTableWidgetItem("待更新计划"))
        self._show_selection()
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
