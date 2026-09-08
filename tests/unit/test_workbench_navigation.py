"""Navigation preserves the current research view and its manual refresh boundary."""
from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from pa_agent.gui.manual_monitor import ManualMonitorWidget
from tests.unit.test_installment_gui import FakeService
from tests.unit.test_market_overview import snapshot


class MarketService:
    def __init__(self):
        self.calls = 0

    def latest(self):
        return snapshot()

    def refresh(self, cancelled):
        self.calls += 1
        return snapshot()


class WorkbenchNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.market = MarketService()
        self.research = FakeService()
        self.window = ManualMonitorWidget(service=self.market, research_service=self.research)

    def tearDown(self):
        self.research.release.set()
        self.window.close()
        self.app.processEvents()

    def test_switching_sections_preserves_search_and_does_not_fetch(self):
        panel = self.window.installment
        panel.search.setText("MU")
        self.window.nav_buttons[1].click()
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertTrue(self.window.nav_buttons[1].isChecked())
        self.window.nav_buttons[0].click()
        self.assertIs(self.window.installment, panel)
        self.assertEqual(panel.search.text(), "MU")
        self.assertEqual(panel._selected()["symbol"], "MU")
        self.assertEqual(self.market.calls, 0)
        self.assertEqual(self.research.calls, 0)

    def test_busy_research_disables_plan_settings_but_keeps_navigation(self):
        self.window.installment.refresh_data()
        self.assertTrue(self.research.started.wait(1))
        self.assertFalse(self.window.open_plan_button.isEnabled())
        self.assertFalse(self.window.open_model_button.isEnabled())
        self.window.nav_buttons[1].click()
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.research.release.set()
        deadline = time.monotonic() + 4
        while self.window.installment._future is not None and time.monotonic() < deadline:
            self.app.processEvents()
            self.window.installment._finish_refresh()
            time.sleep(.01)
        self.assertTrue(self.window.open_plan_button.isEnabled())
        self.assertTrue(self.window.open_model_button.isEnabled())
        self.assertEqual(self.market.calls, 0)


if __name__ == "__main__":
    unittest.main()
