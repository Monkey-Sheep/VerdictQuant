"""Native presentation checks using explicit synthetic monitoring snapshots."""
from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from pa_agent.gui.market_overview import MarketOverviewWidget


def snapshot():
    def asset(symbol, close, currency="USD", daily=1.23):
        return {"symbol": symbol, "close": close, "currency": currency, "date": "2026-09-04",
                "market": "CN" if symbol == "001437" else "US", "daily_pct": daily,
                "five_session_pct": -2.5, "drawdown_52week_pct": -10.12,
                "ma": {"20": 101, "50": 99, "200": 95}, "ma200_twenty_session_slope_pct": 1.22,
                "samples": 502, "full_52week_coverage": True, "qualified": True, "errors": [],
                "expected_completed_date": "2026-09-04", "quote": {"price": 123456.78, "fresh": False, "quoted_at": "2026-08-01T12:00:00Z"},
                "source": {"url": "https://example.invalid/public/" + symbol, "retrieved_at": "2026-09-08T01:00:00Z"},
                "rows": [{"date": "2026-09-03", "close": close / 1.01}, {"date": "2026-09-04", "close": close}]}
    return {"checked_at": "2026-09-08T01:00:00Z", "manual_only": True, "state": {"status": "MISSING"},
            "universe": ["VOO", "QQQM", "MU", "SNDK"],
            "data": {"assets": {"001437": asset("001437", 2.4321, "CNY", 0), "VOO": asset("VOO", 630.12),
                                "QQQM": asset("QQQM", 265.13, daily=-1.25), "MU": asset("MU", 1016.59),
                                "BTC-USD": asset("BTC-USD", 90000)}, "errors": {"SNDK": ["PUBLIC_FETCH_FAILED:Example"]}},
            "core_checks": {"VOO": {"status": "PRICE_GATE_NOT_REACHED"}, "QQQM": {"status": "PRICE_GATE_REACHED"}},
            "research_gaps": ["最新公告与经理变更需另行核验", "<b>UNTRUSTED_RESEARCH_GAP</b>"]}


class MarketOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = MarketOverviewWidget()
        self.widget.resize(1000, 650)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_empty_state_is_explicit_and_never_invents_prices(self):
        self.widget.set_result(None)
        text = self.widget.toPlainText()
        self.assertIn("还没有监控结果", text)
        self.assertIn("基金", text)
        self.assertIn("美股", text)
        self.assertEqual(self.widget.table.rowCount(), 0)
        self.assertEqual(self.widget.cards["001437"].value.text(), "待核验")
        self.assertNotIn("0.0000", text)

    def test_snapshot_renders_dates_currency_and_real_zero_change(self):
        self.widget.set_result(snapshot())
        fund = self.widget.cards["001437"]
        self.assertEqual(fund.value.text(), "2.4321")
        self.assertEqual(fund.change.text(), "0.00%  较上次净值")
        self.assertIn("CNY", fund.unit.text())
        self.assertIn("2026/09/04", fund.date.text())
        self.assertIn("2026/09/08 09:00 北京时间", self.widget.updated.text())
        self.assertEqual(self.widget.cards["QQQM"].badge.text(), "需复核")
        self.assertIn("#b94b4a", self.widget.cards["QQQM"].change.styleSheet())
        self.assertIn("#16724c", self.widget.cards["VOO"].change.styleSheet())
        self.assertEqual(self.widget.table.rowCount(), 4)

    def test_missing_and_stale_values_are_marked_where_they_appear(self):
        data = snapshot()
        fund = data["data"]["assets"]["001437"]
        fund.update(qualified=False, publication="PENDING_PUBLICATION", errors=["PENDING_PUBLICATION"])
        data["data"]["assets"]["VOO"].update(qualified=False, errors=["SAVED_DATA_NEEDS_REFRESH"])
        self.widget.set_result(data)
        self.assertEqual(self.widget.cards["001437"].badge.text(), "待披露")
        self.assertEqual(self.widget.cards["001437"].value.text(), "2.4321")
        self.assertIn("尚待披露", self.widget.detail_warning.text())
        self.assertEqual(self.widget.cards["VOO"].badge.text(), "需更新")
        self.assertEqual(self.widget.table.item(3, 6).text(), "未取得")
        self.assertEqual(self.widget.table.item(3, 1).text(), "待核验")
        self.assertNotIn("0.00", self.widget.table.item(3, 1).text())

    def test_prices_cannot_be_nan_bool_or_zero_but_percent_zero_is_valid(self):
        for invalid in (None, float("nan"), float("inf"), True, 0):
            data = snapshot()
            data["data"]["assets"]["001437"]["close"] = invalid
            self.widget.set_result(data)
            self.assertEqual(self.widget.cards["001437"].value.text(), "待核验")
            self.assertEqual(self.widget.cards["001437"].badge.text(), "价格缺失")
            self.assertIn("0.00%", self.widget.cards["001437"].change.text())

    def test_selected_asset_and_background_are_real_visible_data(self):
        self.widget.set_result(snapshot())
        self.widget.table.selectRow(1)
        self.assertEqual(self.widget.detail_title.text(), "QQQM")
        self.assertIn("不是减仓指令", self.widget.detail_message.text())
        self.widget.group.setCurrentIndex(1)
        self.assertEqual(self.widget.table.rowCount(), 1)
        self.assertEqual(self.widget.table.item(0, 0).text(), "BTC-USD")
        self.widget.table.selectRow(0)
        self.assertIn("仅作为 COIN 的背景", self.widget.detail_message.text())
        self.widget.cards["VOO"].button.click()
        self.assertEqual(self.widget.group.currentData(), "us")
        self.assertEqual(self.widget.detail_title.text(), "VOO")

    def test_old_quote_is_not_displayed_as_a_new_quote(self):
        data = snapshot()
        data["data"]["assets"]["QQQM"]["quote"] = {"price": 266.25, "fresh": True, "quoted_at": "2026-09-08T01:00:00Z"}
        self.widget.set_result(data)
        self.assertEqual(self.widget.table.item(0, 5).text(), "—")
        self.assertEqual(self.widget.table.item(1, 5).text(), "266.25")
        self.assertNotIn("123456.78", self.widget.toPlainText())
        self.widget.select_symbol("VOO")
        self.widget.more_button.setChecked(True)
        self.assertIn("旧报价未展示", self.widget.toPlainText())

    def test_collapsed_text_is_not_a_hidden_report_and_untrusted_text_is_plain(self):
        self.widget.set_result(snapshot())
        self.assertNotIn("UNTRUSTED_RESEARCH_GAP", self.widget.toPlainText())
        self.assertNotIn("200日均线斜率", self.widget.toPlainText())
        self.widget.gaps_button.setChecked(True)
        self.assertIn("<b>UNTRUSTED_RESEARCH_GAP</b>", self.widget.toPlainText())
        self.assertEqual(self.widget.gap_text.textFormat(), Qt.TextFormat.PlainText)
        self.widget.more_button.setChecked(True)
        self.assertIn("200日均线斜率", self.widget.toPlainText())
        self.assertIn("https://example.invalid/public/001437", self.widget.toPlainText())

    def test_no_network_no_persistence_and_input_is_unchanged(self):
        data = snapshot()
        original = copy.deepcopy(data)
        with patch("urllib.request.urlopen") as network, patch("pathlib.Path.write_text") as writes:
            self.widget.set_result(data)
            self.widget.select_symbol("MU")
            self.widget.more_button.setChecked(True)
            self.widget.gaps_button.setChecked(True)
            self.widget.group.setCurrentIndex(1)
            self.widget.toPlainText()
            network.assert_not_called()
            writes.assert_not_called()
        self.assertEqual(data, original)
        data["data"]["assets"]["MU"]["close"] = 999999
        self.assertEqual(self.widget._assets["MU"]["close"], 1016.59)

    def test_compact_window_remains_scrollable_and_table_is_read_only(self):
        self.widget.set_result(snapshot())
        self.widget.show()
        self.app.processEvents()
        self.assertLessEqual(self.widget.width(), 1000)
        self.assertGreater(self.widget.scroll.viewport().width(), 800)
        self.assertEqual(self.widget.table.editTriggers().value, 0)
        self.widget.gaps_button.setChecked(True)
        self.widget.more_button.setChecked(True)
        self.app.processEvents()
        self.assertGreater(self.widget.scroll.verticalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main()
