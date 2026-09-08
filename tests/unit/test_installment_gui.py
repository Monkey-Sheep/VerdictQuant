"""Offline Qt checks for cache-only loading and deliberate research actions."""
from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit, QMessageBox

from pa_agent.gui.installment_research import DEFAULT_ENGINE, EngineSettingsDialog, InstallmentResearchWidget, InvestmentPlanDialog
from pa_agent.installment.models import default_plan, validate_plan


def result(run_id="latest"):
    return {
        "run_id": run_id, "generated_at": "2026-09-08T02:00:00Z", "price_as_of": "2026-09-04",
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "provider": {"model": DEFAULT_ENGINE["model"], "status": "ready", "usage": {}},
        "engine": dict(DEFAULT_ENGINE),
        "summary": "Synthetic offline test only", "errors": [],
        "plan_status": {"month": "2026-09", "ready": False, "gaps": ["本月预算未填写"],
                        "budget_usd": None, "confirmed_spent_usd": 0, "proposed_usd": 0,
                        "remaining_unallocated_usd": 0},
        "comparison": {},
        "assessments": [{"symbol": "MU", "name": "美光", "decision": "REVIEW", "decision_label": "待复核",
            "confidence": "low", "summary": "<b>untrusted</b>",
            "reasons": ["<img src='https://bad.invalid/leak'>"], "risks": ["周期风险"],
            "next_review": "下一次业绩", "invalidators": ["需求放缓"], "errors": [],
            "price": {"close": None, "date": None, "label": "待核实", "rows": [
                {"date": "2026-09-04", "close": 120}, {"date": "2026-09-03", "close": None},
                {"date": "2026-09-02", "close": 100}, {"date": "bad", "close": 80},
                {"date": "2026-09-01", "close": float("nan")}]},
            "valuation": {"status": "missing", "label": "待核实", "metrics": {}},
            "fundamentals": {}, "budget": {"amount_usd": None, "reason": "预算未确认"},
            "sources": [{"id": "source-1", "title": "<strong>source</strong>",
                         "url": "https://example.invalid/official?a=1&b=2", "provider": "official"},
                        {"id": "source-2", "title": "bad link", "url": "file:///C:/secret"}]}],
    }


def nested_result():
    """Same nested shape as SEC company facts and Yahoo timeseries adapters."""
    data = result()
    facts = {"id": "sec-facts", "provider": "SEC", "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
             "status": "ok", "retrieved_at": "2026-09-08T03:00:00Z"}
    vendor = {"id": "yahoo-series", "provider": "Yahoo Finance", "url": "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/MU",
              "status": "ok", "retrieved_at": "2026-09-08T03:01:00Z"}
    directory = {"id": "sec-index", "provider": "SEC", "url": "https://data.sec.gov/submissions/CIK0000000001.json",
                 "status": "ok", "retrieved_at": "2026-09-08T03:00:00Z"}
    news_source = {"id": "yahoo-news", "provider": "Yahoo Finance", "url": "https://query1.finance.yahoo.com/v1/finance/search?q=MU",
                   "status": "ok", "retrieved_at": "2026-09-08T03:02:00Z"}
    sources = [facts, vendor, directory, news_source]

    def node(value, unit="USD", source="sec-facts", end="2026-06-30", filed="2026-08-01"):
        return {"value": value, "unit": unit, "period_start": "2026-04-01", "period_end": end,
                "filing_date": filed, "source_refs": [source], "tag": "internal-gaap-tag", "components": [{"raw": "must not render"}]}

    item = data["assessments"][0]
    item["sources"] = sources
    item["price"].update(drawdown_pct=-12.3456789, percentile=67.8912345, history_days=210)
    item["fundamentals"] = {
        "official": {"metrics": {
            "revenue": {"latest_quarter": node(200_000_000), "ttm": {**node(900_000_000), "period_start": "2025-07-01", "basis": "annual + current YTD - prior YTD"}},
            "net_income": {"latest_quarter": node(0)},
            "operating_cash_flow": {"ttm": {**node(30_000_000), "period_start": "2025-07-01"}},
            "capex": {"latest_quarter": node(-2_000_000)}}},
        "vendor": {"metrics": {"revenue": {"latest_quarter": node(300_000_000, "KRW", "yahoo-series", filed=None)}}},
        "warnings": ["INTERNAL_ERROR_NOT_USER_TEXT"], "valuation": {"raw": "DO_NOT_RENDER_DICT"}}
    item["valuation"]["metrics"] = {"pe": {"value": 20.123456, "as_of": "2026-09-04", "source_refs": ["yahoo-series"]},
        "ps": {"value": 3.56789, "as_of": "2026-09-04", "source_refs": ["yahoo-series"]}, "ev_revenue": None,
        "market_cap": {"value": 9999999999, "unit": "USD"}}
    filing_url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/example-10q.htm"
    document_url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/example-ex99.htm"
    data["public_evidence"] = {"MU": {"sources": sources,
        "filings": [{"form": "10-Q", "title": "2026 第二季度正式财报", "filing_date": "2026-08-01", "url": filing_url, "source_refs": ["sec-index"]}],
        "documents": [{"title": "业绩公告正文节选", "url": document_url, "published_at": "2026-08-01",
                       "retrieved_at": "2026-09-08T03:04:00Z", "source_refs": ["sec-index"], "text": "SECRET_RAW_DOCUMENT_MUST_NOT_RENDER"}],
        "news": [{"title": "<b>最新公开新闻</b>", "publisher": "测试通讯社", "published_at": "2026-09-07T12:00:00Z",
                  "url": "https://example.invalid/news/memory", "source_refs": ["yahoo-news"]}]}}
    data["comparison"] = {"previous_run_id": "internal-run-id", "changes": [
        {"symbol": "MU", "previous": "REVIEW", "current": "NORMAL", "reason": "经营数据已补齐"},
        {"symbol": "NVDA", "previous": "NORMAL", "current": "PAUSE", "reason": "OTHER_SYMBOL_REASON_MUST_NOT_RENDER"}]}
    return data


class FakeService:
    def __init__(self):
        self.calls = 0
        self.reads = 0
        self.current = result()
        self.release = threading.Event()
        self.started = threading.Event()
        self.fail = False
        self.plan = default_plan()
        self.records = []
        self.engine = dict(DEFAULT_ENGINE)
        self.saved_engines = []

    def load_engine(self):
        return copy.deepcopy(self.engine)

    def save_engine(self, config):
        self.saved_engines.append(copy.deepcopy(config))
        self.engine = copy.deepcopy(config)
        return self.engine

    def latest(self):
        self.reads += 1
        return copy.deepcopy(self.current)

    def refresh(self, cancelled, progress):
        self.calls += 1
        self.started.set()
        progress("synthetic-secret-provider-message")
        self.release.wait(3)
        if self.fail:
            raise RuntimeError("secret-key-must-never-appear")
        return result("updated")

    def history(self):
        return [{"run_id": "old", "generated_at": "2026-09-01", "summary": "old snapshot"}]

    def load_run(self, run_id):
        return result(run_id)

    def load_plan(self):
        return copy.deepcopy(self.plan)

    def save_plan(self, plan):
        self.plan = copy.deepcopy(plan)
        return self.plan

    def record_contribution(self, symbol, amount_usd, period, source_ref=""):
        self.records.append((symbol, amount_usd, period, source_ref))

    def contributions(self):
        return [{"event_id": str(n), "symbol": s, "amount_usd": amount, "period": period,
                 "source_ref": ref, "confirmed_at": "2026-09-08T02:00:00Z"}
                for n, (s, amount, period, ref) in enumerate(self.records)]


class InstallmentQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.widget = InstallmentResearchWidget(service=self.service)

    def tearDown(self):
        self.service.release.set()
        self.widget.close()
        self.app.processEvents()

    def finish(self):
        deadline = time.monotonic() + 4
        while self.widget._future is not None and time.monotonic() < deadline:
            self.app.processEvents()
            self.widget._finish_refresh()
            time.sleep(.01)
        self.assertIsNone(self.widget._future)

    def test_open_only_reads_cache_and_unknown_is_not_zero(self):
        self.assertEqual(self.service.calls, 0)
        self.assertEqual(self.service.reads, 1)
        self.assertEqual(self.widget.table.item(0, 2).text(), "待确认")
        self.assertEqual(self.widget.table.item(0, 3).text(), "待确认")
        self.assertIn("本月预算未填写", self.widget.plan_summary.text())
        self.assertEqual(self.service.records, [])

    def test_text_is_escaped_and_only_https_can_open(self):
        self.assertIn("<b>untrusted</b>", self.widget.details.toPlainText())
        self.assertIn("<img src=", self.widget.details.toPlainText())
        self.assertNotIn('<img src="https://bad.invalid', self.widget.details.toHtml())
        self.assertIn("<strong>source</strong>", self.widget.sources.toPlainText())
        self.assertNotIn('href="file:', self.widget.sources.toHtml())
        with patch("pa_agent.gui.installment_research.QDesktopServices.openUrl") as opened:
            for bad in ("file:///C:/secret", "http://example.invalid", "javascript:alert(1)",
                        "https://user:password@example.invalid", "https:"):
                self.widget._open_source(QUrl(bad))
            opened.assert_not_called()
            self.widget._open_source(QUrl("https://example.invalid/official"))
            opened.assert_called_once()

    def test_plot_contains_only_finite_real_observations(self):
        if self.widget.chart is None:
            self.skipTest("pyqtgraph unavailable")
        plots = self.widget.chart.listDataItems()
        self.assertEqual(len(plots), 1)
        x, y = plots[0].getData()
        self.assertEqual(list(y), [100, 120])
        self.assertLess(x[0], x[1])
        self.assertIn("2 个有效交易日", self.widget.chart_note.text())

    def test_refresh_runs_off_ui_thread_and_is_single_flight(self):
        ui_thread = threading.get_ident()
        worker_thread = []
        original = self.service.refresh

        def tracked(**kwargs):
            worker_thread.append(threading.get_ident())
            return original(**kwargs)

        self.service.refresh = tracked
        self.widget.refresh_button.click()
        self.assertTrue(self.service.started.wait(1))
        self.widget.refresh_data()
        self.assertEqual(self.service.calls, 1)
        self.assertNotEqual(worker_thread[0], ui_thread)
        self.assertFalse(self.widget.refresh_button.isEnabled())
        self.assertFalse(self.widget.plan_button.isEnabled())
        self.assertEqual(self.widget._result["run_id"], "latest")
        self.assertNotIn("synthetic-secret", self.widget.status.text())
        self.service.release.set()
        self.finish()
        self.assertEqual(self.widget._result["run_id"], "updated")
        self.assertTrue(self.widget.refresh_button.isEnabled())
        self.assertEqual(self.service.records, [])

    def test_cancel_keeps_old_result_even_if_worker_returns(self):
        self.widget.refresh_data()
        self.assertTrue(self.service.started.wait(1))
        self.widget.cancel_button.click()
        self.service.release.set()
        self.finish()
        self.assertEqual(self.widget._result["run_id"], "latest")
        self.assertIn("取消", self.widget.status.text())

    def test_error_keeps_old_result_without_secret(self):
        self.service.fail = True
        self.widget.refresh_data()
        self.service.release.set()
        self.finish()
        self.assertEqual(self.widget._result["run_id"], "latest")
        self.assertNotIn("secret-key", self.widget.status.text())
        self.assertIn("未完成", self.widget.status.text())

    def test_history_read_failure_does_not_misreport_successful_refresh(self):
        self.service.history = lambda: (_ for _ in ()).throw(OSError("synthetic-secret"))
        self.widget.refresh_data()
        self.service.release.set()
        self.finish()
        self.assertEqual(self.widget._result["run_id"], "updated")
        self.assertIn("分析已更新", self.widget.status.text())
        self.assertNotIn("synthetic-secret", self.widget.status.text())

    def test_close_requests_cancellation_without_waiting_for_worker(self):
        self.widget.refresh_data()
        self.assertTrue(self.service.started.wait(1))
        self.widget.close()
        self.assertTrue(self.widget._cancelled.is_set())
        self.assertFalse(self.widget._poll.isActive())
        self.assertEqual(self.widget._result["run_id"], "latest")

    def test_history_is_local_and_prevents_contribution(self):
        self.widget.history_table.selectRow(0)
        self.widget.history_open_button.click()
        self.assertEqual(self.widget._result["run_id"], "old")
        self.assertIn("历史", self.widget.metadata.text())
        self.assertFalse(self.widget.contribution_button.isEnabled())
        self.widget.show_latest()
        self.assertEqual(self.widget._result["run_id"], "latest")
        self.assertEqual(self.service.calls, 0)

    def test_expired_snapshot_is_visible_but_not_a_current_budget(self):
        old = result()
        old["valid_until"] = "2020-01-01T00:00:00Z"
        old["assessments"][0]["budget"]["amount_usd"] = 400
        self.widget._render(old)
        self.assertIn("有效期已过", self.widget.metadata.text())
        self.assertEqual(self.widget.table.item(0, 3).text(), "已过期待更新")
        self.assertIn("仅供复盘", self.widget.details.toPlainText())
        self.widget._render(old, historical=True)
        self.assertEqual(self.widget.table.item(0, 3).text(), "$400.00")

    def test_changed_plan_masks_previous_amounts_until_local_recalculation(self):
        self.widget._mark_plan_changed("计划已修改")
        self.assertEqual(self.widget.table.item(0, 3).text(), "待更新计划")
        self.widget.show_latest()
        self.assertEqual(self.widget.table.item(0, 3).text(), "待确认")
        self.assertNotIn("旧计划", self.widget.details.toPlainText())
        self.assertIn("本地预算已重算", self.widget.status.text())
        self.assertEqual(self.service.calls, 0)

    def test_plan_blank_fields_remain_none_and_core_weights_are_disabled(self):
        dialog = InvestmentPlanDialog(default_plan(), ["MU", "001437", "VOO", "QQQM"])
        plan = dialog.collect_plan()
        self.assertIsNone(plan["monthly_budget_usd"])
        self.assertIsNone(plan["portfolio_total_usd"])
        self.assertIsNone(plan["horizon_years"])
        self.assertIsNone(plan["positions_usd"]["MU"])
        self.assertNotIn("VOO", plan["target_weights"])
        self.assertFalse(dialog.assets.item(2, 1).flags() & Qt.ItemFlag.ItemIsEditable)
        validate_plan(plan)
        dialog.close()

    def test_invalid_plan_never_becomes_confirmed_input(self):
        dialog = InvestmentPlanDialog(default_plan(), ["MU", "SNDK"])
        for bad in ("nan", "inf", "-10", "not a number"):
            dialog.fields["monthly_budget_usd"].setText(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dialog.collect_plan()
        dialog.fields["monthly_budget_usd"].setText("")
        dialog.assets.item(0, 1).setText("70")
        dialog.assets.item(1, 1).setText("40")
        with self.assertRaises(ValueError):
            dialog.collect_plan()
        dialog.assets.item(1, 1).setText("30")
        dialog.fields["period"].setText("2026-13")
        with self.assertRaises(ValueError):
            dialog.collect_plan()
        dialog.close()

    def test_installment_fractions_are_explicit_editable_and_ordered(self):
        dialog = InvestmentPlanDialog(default_plan(), ["MU"])
        self.assertEqual(float(dialog.fields["higher_risk_installment_pct"].text()), 25)
        self.assertEqual(float(dialog.fields["normal_installment_pct"].text()), 50)
        self.assertEqual(float(dialog.fields["attractive_installment_pct"].text()), 100)
        dialog.fields["normal_installment_pct"].setText("10")
        with self.assertRaises(ValueError):
            dialog.collect_plan()
        dialog.fields["higher_risk_installment_pct"].setText("5")
        plan = dialog.collect_plan()
        self.assertEqual(plan["normal_installment_pct"], 10)
        self.assertEqual(plan["higher_risk_installment_pct"], 5)
        self.assertIsNone(plan["monthly_budget_usd"])
        validate_plan(plan)
        dialog.fields["higher_risk_installment_pct"].setText("0")
        self.assertEqual(dialog.collect_plan()["higher_risk_installment_pct"], 0)
        dialog.close()

    def test_holdings_reconfirmation_requires_a_fresh_explicit_check(self):
        original = default_plan()
        original["confirm_holdings"] = True  # A prior control flag must never pre-check this dialog.
        original["position_confirmed_at"] = {"MU": "2026-09-01T02:00:00Z"}
        dialog = InvestmentPlanDialog(original, ["MU"])
        self.assertFalse(dialog.confirm_holdings.isChecked())
        dialog.fields["horizon_years"].setText("5")
        untouched = dialog.collect_plan()
        self.assertNotIn("confirm_holdings", untouched)
        self.assertEqual(untouched["position_confirmed_at"]["MU"], "2026-09-01T02:00:00Z")
        dialog.confirm_holdings.setChecked(True)
        self.assertIs(dialog.collect_plan()["confirm_holdings"], True)
        dialog.confirm_holdings.setChecked(False)
        self.assertNotIn("confirm_holdings", dialog.collect_plan())
        dialog.close()

    def test_engine_dialog_defaults_and_api_save_only_changes_routing(self):
        with patch("pa_agent.config.settings.load_settings") as settings:
            dialog = EngineSettingsDialog({})
            self.assertEqual(dialog.collect_config(), DEFAULT_ENGINE)
            dialog.model.setText("gpt-5.3-codex-spark")
            dialog.reasoning.setCurrentText("xhigh")
            self.assertEqual(dialog.collect_config()["reasoning_effort"], "xhigh")
            dialog.kind.setCurrentIndex(1)
            self.assertEqual(dialog.collect_config(), {"kind": "api"})
            self.assertIn("单独计费", dialog.explanation.text())
            settings.assert_not_called()
            dialog.close()

    def test_engine_save_does_not_request_analysis_and_marks_prior_model(self):
        def choose_api():
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, EngineSettingsDialog)
            dialog.kind.setCurrentIndex(1)
            dialog._accept_config()

        with patch("pa_agent.config.settings.load_settings") as settings:
            QTimer.singleShot(0, choose_api)
            self.widget.edit_model()
            settings.assert_not_called()
        self.assertEqual(self.service.saved_engines, [{"kind": "api"}])
        self.assertEqual(self.service.calls, 0)
        self.assertEqual(self.widget._result["provider"]["model"], DEFAULT_ENGINE["model"])
        self.assertIn("旧模型结果", self.widget.metadata.text())
        self.assertEqual(self.widget.table.item(0, 3).text(), "旧模型待更新")
        self.assertIn("API", self.widget.engine_note.text())
        self.assertIn("单独计费", self.widget.engine_note.text())

    def test_codex_model_save_is_feature_local_and_defers_invocation(self):
        def choose_codex():
            dialog = self.app.activeModalWidget()
            dialog.reasoning.setCurrentText("medium")
            dialog._accept_config()

        QTimer.singleShot(0, choose_codex)
        self.widget.edit_model()
        self.assertEqual(self.service.saved_engines, [{"kind": "codex_cli", "model": "gpt-5.3-codex-spark", "reasoning_effort": "medium"}])
        self.assertEqual(self.service.calls, 0)
        self.assertIn("订阅额度", self.widget.engine_note.text())
        self.assertNotIn("API 调用", self.widget.engine_note.text())

    def test_usage_displays_only_reported_numbers_and_marks_reuse(self):
        data = result()
        data["provider"]["usage"] = {"input_tokens": 1234, "output_tokens": 56, "estimated_cost": 999, "total_tokens": None}
        data["model_reused"] = True
        self.widget._render(data)
        text = self.widget.usage_note.text()
        self.assertIn("输入 1,234 tokens", text)
        self.assertIn("输出 56 tokens", text)
        self.assertIn("原调用返回", text)
        self.assertNotIn("999", text)
        self.assertNotIn("合计", text)

    def test_legacy_api_snapshot_is_not_presented_as_new_codex_analysis(self):
        data = result()
        data.pop("engine")
        data["provider"]["model"] = "deepseek-reasoner"
        self.widget._render(data)
        self.assertIn("旧模型结果", self.widget.metadata.text())
        self.assertIn("deepseek-reasoner", self.widget.metadata.text())
        self.assertIn("订阅额度", self.widget.engine_note.text())
        self.assertEqual(self.widget.table.item(0, 3).text(), "旧模型待更新")
        self.assertEqual(self.service.calls, 0)

    def test_contribution_requires_explicit_input_and_second_confirmation(self):
        seen = []

        def fill_dialog():
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            edits = dialog.findChildren(QLineEdit)
            self.assertEqual(edits[0].text(), "")
            edits[0].setText("123.45")
            edits[1].setText("2026-09")
            dialog.findChild(QDialogButtonBox).accepted.emit()
            if dialog.isVisible():
                dialog.reject()

        def confirm(box):
            seen.append(box.text())
            return QMessageBox.StandardButton.Yes

        with patch.object(QMessageBox, "exec", confirm):
            QTimer.singleShot(0, fill_dialog)
            self.widget.record_contribution()
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.service.records, [("MU", 123.45, "2026-09", "用户在软件确认")])
        self.assertEqual(self.widget.ledger_table.rowCount(), 1)
        self.assertEqual(self.widget.ledger_table.item(0, 3).text(), "$123.45")
        self.assertIn("重新计算", self.widget.status.text())
        self.assertEqual(self.service.calls, 0)

    def test_service_expiry_flag_blocks_stale_trading_day_budget(self):
        data = result()
        data.update(expired=True, actionable=False)
        self.widget._render(data)
        self.assertEqual(self.widget.table.item(0, 3).text(), "已过期待更新")

    def test_nested_financials_are_compact_currency_and_period_aware(self):
        self.widget._render(nested_result())
        text = self.widget.details.toPlainText()
        for expected in ("正式披露", "供应商数据", "最近季度", "TTM", "经营现金流", "资本开支",
                         "2.00 亿 USD", "3.00 亿 KRW", "0.00 USD", "-2.00 百万 USD",
                         "2026-06-30", "2026-08-01", "据披露计算", "未取得"):
            self.assertIn(expected, text)
        for forbidden in ("latest_quarter", "source_refs", "internal-gaap-tag", "DO_NOT_RENDER_DICT", "INTERNAL_ERROR_NOT_USER_TEXT"):
            self.assertNotIn(forbidden, text)
        data = nested_result()
        data["assessments"][0]["fundamentals"]["official"]["metrics"]["revenue"]["latest_quarter"]["unit"] = None
        self.widget._render(data)
        self.assertIn("币种缺失，金额待核实", self.widget.details.toPlainText())

    def test_nested_valuation_and_comparison_are_selected_stock_only(self):
        self.widget._render(nested_result())
        text = self.widget.details.toPlainText()
        for expected in ("20.12 倍", "3.57 倍", "企业价值 / 销售额", "观测日期", "-12.35%", "67.89%",
                         "模型判断把握（非上涨概率）", "上次：本次证据不足", "本次：可按正常节奏投入", "经营数据已补齐"):
            self.assertIn(expected, text)
        for forbidden in ("9999999999", "internal-run-id", "OTHER_SYMBOL_REASON_MUST_NOT_RENDER", "as_of", "source_refs"):
            self.assertNotIn(forbidden, text)
        data = nested_result()
        data["comparison"]["changes"] = data["comparison"]["changes"][1:]
        self.widget._render(data)
        self.assertIn("暂无该股票的判断变化", self.widget.details.toPlainText())

    def test_source_cards_show_real_documents_titles_dates_and_fetch_status(self):
        self.widget._render(nested_result())
        text, rendered = self.widget.sources.toPlainText(), self.widget.sources.toHtml()
        for expected in ("SEC · 财报结构化", "Yahoo Finance · 估值快照", "Yahoo Finance · 新闻线索",
                         "2026 第二季度正式财报", "业绩公告正文节选", "<b>最新公开新闻</b>",
                         "已取得正文节选", "仅目录线索", "新闻标题线索，未审阅正文", "实际发布日期：2026-08-01",
                         "实际发布日期：2026-09-07T12:00:00Z", "数据取得时间：2026-09-08T03:04:00Z",
                         "实际发布日期：未提供"):
            self.assertIn(expected, text)
        self.assertIn('href="https://example.invalid/news/memory"', rendered)
        self.assertIn("example-10q.htm", rendered)
        self.assertIn("example-ex99.htm", rendered)
        self.assertNotIn("SECRET_RAW_DOCUMENT_MUST_NOT_RENDER", text)

    def test_real_service_cache_plan_ledger_contract_offline(self):
        from pa_agent.installment.service import InstallmentService
        from pa_agent.installment.decision import DECISION_VERSION
        from pa_agent.monitoring.service import atomic_json
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory(prefix="installment-ui-test-") as temporary:
            root = Path(temporary)
            forbidden = Mock(side_effect=AssertionError("Network or model work is forbidden in this test"))
            service = InstallmentService(data_root=root, market_client=Mock(equity=forbidden),
                research_client=Mock(fetch=forbidden), analyst=Mock(analyze=forbidden),
                clock=lambda: now, symbols=("MU",))
            service.refresh = forbidden
            self.assertIsNone(service.latest())
            self.assertEqual(service.history(), [])
            self.assertEqual(service.contributions(), [])
            self.assertEqual(service.load_engine(), DEFAULT_ENGINE)
            self.assertEqual(service.save_engine({"kind": "api"}), {"kind": "api"})
            self.assertEqual(service.load_engine(), {"kind": "api"})
            self.assertEqual(service.save_engine(dict(DEFAULT_ENGINE)), DEFAULT_ENGINE)
            plan = default_plan(now)
            plan.update(monthly_budget_usd=1000, horizon_years=5, portfolio_total_usd=10000)
            plan["target_weights"]["MU"] = 100
            plan["positions_usd"] = {s: 0 for s in plan["positions_usd"]}
            service.save_plan(plan)
            run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-012345abcdef"
            data = result(run_id)
            data.update(schema_version=1, manual_only=True, orders=False,
                        decision_policy_version=DECISION_VERSION, engine=service.load_engine())
            data["assessments"][0].update(decision="NORMAL", confidence="high")
            data["assessments"][0]["price"]["rows"] = []
            run_dir = root / "runs" / run_id
            run_dir.mkdir(parents=True)
            raw = atomic_json(run_dir / "bundle.json", data)
            atomic_json(root / "current.json", {"run_id": run_id, "sha256": hashlib.sha256(raw).hexdigest()})
            widget = InstallmentResearchWidget(service=service)
            try:
                self.assertEqual(widget._result["assessments"][0]["budget"]["amount_usd"], 500)
                self.assertEqual(widget.history_table.rowCount(), 1)
                service.record_contribution("MU", 100, plan["period"], event_id="offline-test-event")
                widget._reload_local_state("离线预算与台账已更新")
                self.assertEqual(widget.ledger_table.rowCount(), 1)
                self.assertEqual(widget.ledger_table.item(0, 3).text(), "$100.00")
                self.assertEqual(widget._result["assessments"][0]["budget"]["spent_this_month_usd"], 100)
                self.assertEqual(widget._result["assessments"][0]["budget"]["amount_usd"], 450)
                self.assertEqual((run_dir / "bundle.json").read_bytes(), raw)
                widget.history_table.selectRow(0)
                widget.open_history()
                self.assertTrue(widget._historical)
                self.assertFalse(widget.contribution_button.isEnabled())
                forbidden.assert_not_called()
            finally:
                widget.close()


if __name__ == "__main__":
    unittest.main()
