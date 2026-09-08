"""Plan editor layout and preserved input boundaries, using synthetic local plans."""
import copy
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from pa_agent.gui.investment_plan import InvestmentPlanDialog
from pa_agent.installment.models import CORE_SYMBOLS, SYMBOLS, default_plan, validate_plan


class InvestmentPlanLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # The Windows offscreen plugin has no system font enumeration. Register
        # local fonts only in the test process to verify real Chinese geometry.
        for filename in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/msyh.ttc"):
            if Path(filename).is_file():
                QFontDatabase.addApplicationFont(filename)
        cls.app.setFont(QFont("Microsoft YaHei UI", 10))

    def setUp(self):
        self.dialog = InvestmentPlanDialog(default_plan(), [*SYMBOLS, *CORE_SYMBOLS])
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def assert_inside_dialog(self, widget):
        self.assertTrue(widget.isVisible())
        bounds = QRect(widget.mapTo(self.dialog, QPoint(0, 0)), widget.size())
        self.assertTrue(self.dialog.rect().contains(bounds), (bounds, self.dialog.rect()))

    def test_default_layout_groups_fields_and_keeps_table_and_actions_visible(self):
        self.assertEqual((self.dialog.width(), self.dialog.height()), (920, 760))
        self.assertEqual([self.dialog.tabs.tabText(i) for i in range(2)], ["预算与持仓", "额度规则"])
        self.assertEqual(len(self.dialog.fields), 9)
        self.assertEqual(self.dialog.assets.rowCount(), 14)
        self.assertEqual(self.dialog.budget_scroll.verticalScrollBar().maximum(), 0)
        self.assert_inside_dialog(self.dialog.assets)
        self.assert_inside_dialog(self.dialog.confirm_holdings)
        self.assert_inside_dialog(self.dialog.buttons.button(QDialogButtonBox.StandardButton.Save))
        for row, symbol in enumerate([*SYMBOLS, *CORE_SYMBOLS]):
            self.assertEqual(self.dialog.assets.item(row, 0).text(), symbol)
            self.assertFalse(self.dialog.assets.item(row, 0).flags() & Qt.ItemFlag.ItemIsEditable)
            if symbol in CORE_SYMBOLS:
                self.assertFalse(self.dialog.assets.item(row, 1).flags() & Qt.ItemFlag.ItemIsEditable)

    def test_short_window_scrolls_both_pages_while_confirmation_and_buttons_stay_visible(self):
        self.dialog.resize(720, 480)
        for index, scroll in enumerate((self.dialog.budget_scroll, self.dialog.rules_scroll)):
            self.dialog.tabs.setCurrentIndex(index)
            self.dialog.details_toggle.setChecked(True)
            self.app.processEvents()
            self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assert_inside_dialog(self.dialog.confirm_holdings)
            for button in (QDialogButtonBox.StandardButton.Save, QDialogButtonBox.StandardButton.Cancel):
                self.assert_inside_dialog(self.dialog.buttons.button(button))
        self.assertTrue(self.dialog.details_body.isVisible())
        self.dialog.details_toggle.setChecked(False)
        self.assertFalse(self.dialog.details_body.isVisible())

    def test_unknown_holdings_and_explicit_reconfirmation_are_preserved(self):
        original = default_plan()
        original["confirm_holdings"] = True
        original["position_confirmed_at"]["MU"] = "2026-09-01T02:00:00Z"
        saved = copy.deepcopy(original)
        dialog = InvestmentPlanDialog(original, [*SYMBOLS, *CORE_SYMBOLS])
        try:
            self.assertFalse(dialog.confirm_holdings.isChecked())
            dialog.fields["horizon_years"].setText("5")
            plan = dialog.collect_plan()
            self.assertIsNone(plan["monthly_budget_usd"])
            self.assertIsNone(plan["portfolio_total_usd"])
            self.assertTrue(all(value is None for value in plan["positions_usd"].values()))
            self.assertFalse(set(CORE_SYMBOLS) & set(plan["target_weights"]))
            self.assertEqual(plan["position_confirmed_at"]["MU"], "2026-09-01T02:00:00Z")
            self.assertNotIn("confirm_holdings", plan)
            self.assertEqual(original, saved)
            self.assertEqual(dialog._original, saved)
            validate_plan(plan)
            dialog.confirm_holdings.setChecked(True)
            self.assertIs(dialog.collect_plan()["confirm_holdings"], True)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_validation_error_remains_visible_without_accepting_invalid_plan(self):
        self.dialog.resize(720, 480)
        self.dialog.tabs.setCurrentIndex(1)
        self.dialog.fields["max_single_pct"].setText("40")
        self.dialog.fields["max_theme_pct"].setText("35")
        self.dialog._accept_plan()
        self.app.processEvents()
        self.assertIsNone(self.dialog.plan)
        self.assertEqual(self.dialog.result(), 0)
        self.assertIn("单只股票上限不能大于主题上限", self.dialog.error.text())
        self.assert_inside_dialog(self.dialog.error)
        self.assert_inside_dialog(self.dialog.buttons.button(QDialogButtonBox.StandardButton.Save))


if __name__ == "__main__":
    unittest.main()
