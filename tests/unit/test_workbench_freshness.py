"""An open workbench must age saved evidence without fetching or rewriting it."""
from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from pa_agent.gui.installment_research import InstallmentResearchWidget
from pa_agent.gui.manual_monitor import ManualMonitorWidget
from pa_agent.installment.models import default_plan
from pa_agent.installment.service import InstallmentService
from pa_agent.monitoring.service import MonitoringService
from tests.unit.test_installment_gui import FakeService
from tests.unit.test_installment_service import Analyst, Market, NOW, Public
from tests.unit.test_manual_monitor import FakeClient


class Clock(datetime):
    at = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at.replace(tzinfo=None)


class FreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.windows = []
        Clock.at = NOW
        for module in ("pa_agent.gui.installment_research", "pa_agent.monitoring.service"):
            patched = patch(module + ".datetime", Clock)
            patched.start()
            self.addCleanup(patched.stop)

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def hashes(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*.json")}

    def research(self):
        market, public, analyst = Market(), Public(), Analyst()
        service = InstallmentService(self.root / "research", market, public, analyst, ["NVDA"],
                                    lambda: Clock.at, lambda *a, **k: {"documents": [], "sources": [], "errors": []})
        plan = default_plan(NOW)
        plan.update(monthly_budget_usd=200, portfolio_total_usd=1000, horizon_years=5)
        plan["positions_usd"] = {s: 0 for s in plan["positions_usd"]}
        plan["target_weights"]["NVDA"] = 100
        service.save_plan(plan)
        service.refresh()
        window = InstallmentResearchWidget(service=service)
        self.windows.append(window)
        return service, window

    def tick(self, window):
        window._freshness_timer.setInterval(10)
        QTest.qWait(40)

    def test_research_deadline_hides_amount_while_open_without_fetching_or_writing(self):
        service, window = self.research()
        self.assertFalse(window._expired)
        self.assertIn("100", window.table.item(0, 3).text())
        before = self.hashes()
        window.search.setText("NVDA")
        window.tabs.setCurrentIndex(1)
        Clock.at = datetime.fromisoformat(window._result["valid_until"]) + timedelta(seconds=1)
        with patch.object(service, "refresh", side_effect=AssertionError("unexpected fetch")):
            self.tick(window)
        self.assertTrue(window._expired)
        self.assertIn("需更新", window.freshness.text())
        self.assertNotIn("100", window.table.item(0, 3).text())
        self.assertIn("本次建议合计：待更新", window.plan_summary.text())
        self.assertNotIn("$100.00", window.plan_summary.text())
        self.assertEqual(window.search.text(), "NVDA")
        self.assertEqual(window._selected()["symbol"], "NVDA")
        self.assertEqual(window.tabs.currentIndex(), 1)
        self.assertEqual(self.hashes(), before)
        self.assertEqual((service.market.calls, service.public.calls, service.analyst.calls), (1, 1, 1))

    def test_new_completed_session_marks_research_stale_before_model_deadline(self):
        service, window = self.research()
        Clock.at = datetime(2026, 9, 8, 20, 3, tzinfo=UTC)
        self.assertLess(Clock.at, datetime.fromisoformat(window._result["valid_until"]))
        self.tick(window)
        self.assertTrue(window._expired)
        self.assertIn("完成交易日", " ".join(window._result["expiry_reasons"]))
        self.assertEqual(service.analyst.calls, 1)

    def test_history_view_and_inflight_refresh_are_not_replaced_by_local_check(self):
        service, window = self.research()
        historical = service.load_run(window._result["run_id"])
        window._render(historical, historical=True)
        Clock.at += timedelta(days=3)
        with patch.object(service, "latest", side_effect=AssertionError("history replaced")):
            self.tick(window)
        self.assertTrue(window._historical)
        self.assertIs(window._result, historical)
        window._historical = False
        window._future = object()
        try:
            with patch.object(service, "latest", side_effect=AssertionError("inflight replaced")):
                self.tick(window)
        finally:
            window._future = None

    def test_unchanged_cache_does_not_rebuild_view_and_shutdown_stops_timer(self):
        _, window = self.research()
        with patch.object(window, "_render", wraps=window._render) as render:
            self.tick(window)
            render.assert_not_called()
        window.shutdown()
        self.assertFalse(window._freshness_timer.isActive())


    def test_invalid_monitor_paths_keeps_window_usable_and_recovers_after_repair(self):
        root = self.root / "monitor"
        service = MonitoringService(root, client=FakeClient())
        service.refresh()
        config = root / "paths.json"
        config.write_text("[]", encoding="utf-8")
        with patch.dict(os.environ, {"VERDICTQUANT_MONITOR_ROOT": str(root)}), \
                patch("pa_agent.monitoring.market.PublicClient.collect", side_effect=AssertionError("unexpected fetch")):
            window = ManualMonitorWidget(research_service=FakeService())
            self.windows.append(window)
            self.assertIsNone(window.service)
            self.assertIn("无法校验", window.status.text())
            self.assertEqual(window.installment.table.rowCount(), 1)
            window.refresh_button.click()
            self.assertIsNone(window._future)
            self.assertTrue(window.refresh_button.isEnabled())
            self.assertEqual(config.read_text(encoding="utf-8"), "[]")
            config.write_text("{}", encoding="utf-8")
            self.tick(window)
        self.assertIsNotNone(window.service)
        self.assertIn("恢复读取", window.status.text())
        self.assertEqual(window.browser.cards["VOO"].badge.text(), "已完成")

    def test_market_cache_read_failure_marks_old_quotes_unusable_then_recovers(self):
        service = MonitoringService(self.root / "monitor", client=FakeClient())
        service.refresh()
        window = ManualMonitorWidget(service=service, research_service=FakeService())
        self.windows.append(window)
        before = self.hashes()
        with patch.object(service, "latest", side_effect=ValueError("private-error")):
            self.tick(window)
        self.assertEqual(window.browser.cards["VOO"].badge.text(), "待核验")
        self.assertEqual(window.browser.cards["VOO"].value.text(), "100.00")
        self.assertNotIn("private-error", window.status.text())
        self.tick(window)
        self.assertEqual(window.browser.cards["VOO"].badge.text(), "已完成")
        self.assertEqual(self.hashes(), before)

    def test_repeated_invalid_nested_research_fields_restore_safe_view(self):
        service, window = self.research()
        bad = copy.deepcopy(window._result)
        bad["provider"] = [1]
        with patch.object(service, "latest", return_value=bad):
            self.tick(window)
            self.tick(window)
        self.assertTrue(window._expired)
        self.assertIsInstance(window._result["provider"], dict)
        self.assertIn("需更新", window.freshness.text())
        self.tick(window)
        self.assertFalse(window._expired)

    def test_repeated_invalid_market_render_restores_previous_prices(self):
        service = MonitoringService(self.root / "monitor", client=FakeClient())
        service.refresh()
        window = ManualMonitorWidget(service=service, research_service=FakeService())
        self.windows.append(window)
        bad = copy.deepcopy(window._market_result)
        bad["data"]["assets"]["001437"].update(source=[1], close=999)
        with patch.object(service, "latest", return_value=bad):
            self.tick(window)
            self.tick(window)
        self.assertEqual(window.browser.cards["001437"].value.text(), "100.0000")
        self.assertFalse(window.browser._assets["001437"]["qualified"])
        self.assertIsInstance(window.browser._assets["001437"]["source"], dict)

    def test_changed_plan_and_engine_hide_amount_in_details_and_summary(self):
        _, window = self.research()
        self.assertIn("$100.00", window.thesis.toPlainText())
        for change in ("_plan_dirty", "_engine_changed"):
            with self.subTest(change=change):
                setattr(window, change, True)
                window._render(window._result)
                self.assertNotIn("$100.00", window.thesis.toPlainText())
                self.assertNotIn("$100.00", window.plan_summary.text())
                self.assertIn("本次建议合计：待更新", window.plan_summary.text())
                self.assertIn("$200.00", window.plan_summary.text())
                setattr(window, change, False)

    def test_unreadable_research_cache_retires_amount_and_can_recover(self):
        service, window = self.research()
        before = self.hashes()
        with patch.object(service, "latest", side_effect=ValueError("private-error")):
            self.tick(window)
        self.assertTrue(window._expired)
        self.assertNotIn("100", window.table.item(0, 3).text())
        self.assertNotIn("private-error", window.status.text())
        self.tick(window)
        self.assertFalse(window._expired)
        self.assertIn("100", window.table.item(0, 3).text())
        self.assertEqual(self.hashes(), before)

    def test_market_quote_and_completed_day_age_while_open_without_network(self):
        client = FakeClient()
        service = MonitoringService(self.root / "monitor", client=client)
        original_collect = client.collect

        def collect(at, cancelled=None):
            data = original_collect(at, cancelled)
            data["assets"]["VOO"]["quote"] = {"price": 101, "fresh": True, "quoted_at": NOW.isoformat()}
            return data

        client.collect = collect
        service.refresh()
        window = ManualMonitorWidget(service=service, research_service=FakeService())
        self.windows.append(window)
        before = self.hashes()
        self.assertEqual(window.browser.cards["VOO"].badge.text(), "已完成")
        self.assertTrue(window.browser._assets["VOO"]["quote"]["fresh"])
        Clock.at += timedelta(seconds=service.policy["quote_max_age_seconds"] + 1)
        with patch.object(service, "refresh", side_effect=AssertionError("unexpected fetch")):
            self.tick(window)
        self.assertFalse(window.browser._assets["VOO"]["quote"]["fresh"])
        self.assertEqual(window.browser.cards["VOO"].badge.text(), "已完成")
        Clock.at = datetime(2026, 9, 8, 20, 3, tzinfo=UTC)
        self.tick(window)
        self.assertEqual(window.browser.cards["VOO"].badge.text(), "需更新")
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.hashes(), before)
        window.shutdown()
        self.assertFalse(window._freshness_timer.isActive())


if __name__ == "__main__":
    unittest.main()
