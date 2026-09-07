"""Offline correctness, persistence and real Qt interaction for manual monitoring."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pa_agent.monitoring.calendar import CalendarError, Calendars
from pa_agent.monitoring.market import PublicClient, core_review_checks, indicators
from pa_agent.monitoring.service import MonitoringService, RefreshBusy
from pa_agent.monitoring.state import fund_confirmation, load_state, reduction_permission, validate_state
from pa_agent.monitoring.view import render

AT = datetime(2026, 9, 7, 0, 30, tzinfo=UTC)


def state(quantity=10):
    return {"schema_version": 1, "positions": {"001437": {
        "status": "confirmed", "quantity": quantity, "average_cost": 2,
        "purchase_date": "2026-01-02", "confirmed_at": "2026-09-04T08:00:00Z",
        "source_ref": "synthetic-test-only"}}, "executions": [], "execution_history_complete": True}


def bars(count=550, end=date(2026, 9, 4), close=100):
    return [{"date": (end - timedelta(days=count - i - 1)).isoformat(), "close": close, "high": close} for i in range(count)]


def dataset():
    assets = {}
    for symbol in ("001437", "VOO", "QQQM"):
        rows = bars()
        assets[symbol] = {**indicators(rows), "symbol": symbol, "market": "CN" if symbol == "001437" else "US",
                          "currency": "CNY" if symbol == "001437" else "USD", "qualified": True,
                          "errors": [], "rows": rows, "source": {"url": "https://example.invalid/test"}}
    return {"observed_at": AT.isoformat(), "assets": assets, "errors": {}}


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def collect(self, at, cancelled=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic failure")
        return dataset()


class StateTests(unittest.TestCase):
    def test_missing_state_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.json"
            self.assertEqual(load_state(path, AT)["status"], "MISSING")
            self.assertFalse(path.exists())

    def test_prose_never_confirms_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for text in ("", "001437 尚未提供持仓数量、成本与日期", "not a real investment", "The user confirmed a real investment"):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    flags = fund_confirmation(load_state(path, AT))
                    self.assertFalse(flags["position_confirmed"])
                    self.assertFalse(flags["position_details_complete"])

    def test_confirmed_and_flat_are_distinct(self):
        positive = fund_confirmation(validate_state(state(), AT))
        flat = fund_confirmation(validate_state(state(0), AT))
        self.assertTrue(positive["position_confirmed"])
        self.assertFalse(flat["position_confirmed"])
        self.assertTrue(flat["state_source_verified"])

    def test_cost_and_date_required_for_complete_positive_position(self):
        data = state()
        data["positions"]["001437"]["average_cost"] = None
        self.assertFalse(fund_confirmation(validate_state(data, AT))["position_details_complete"])

    def test_confirmation_requires_actual_nonnegative_number(self):
        for bad in (True, -1, float("nan"), float("inf"), "10", None):
            data = state(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_state(data, AT)

    def test_confirmation_requires_source_and_aware_nonfuture_time(self):
        for key, bad in (("source_ref", ""), ("confirmed_at", "2026-09-04"), ("confirmed_at", "2027-01-01T00:00:00Z")):
            data = state()
            data["positions"]["001437"][key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                validate_state(data, AT)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            self.assertEqual(load_state(path, AT)["status"], "INVALID")

    def test_due_review_expires_confirmation(self):
        data = state()
        data["positions"]["001437"]["review_after"] = "2026-09-05T00:00:00Z"
        self.assertEqual(validate_state(data, AT)["positions"]["001437"]["status"], "stale")

    def test_executed_reduction_is_not_proposed_again(self):
        data = state()
        data["executions"] = [{"event_id": "event-1", "proposal_id": "reduce-1", "symbol": "001437",
                               "action": "REDUCE", "quantity": 1, "executed_at": "2026-09-04T08:00:00Z", "source_ref": "synthetic-test"}]
        normalized = validate_state(data, AT)
        self.assertEqual(reduction_permission(normalized, "001437", "reduce-1"), (False, "ALREADY_EXECUTED"))
        data["executions"].append(copy.deepcopy(data["executions"][0]))
        with self.assertRaises(ValueError):
            validate_state(data, AT)

    def test_unknown_history_and_flat_block_reduction(self):
        data = state()
        data["execution_history_complete"] = False
        self.assertFalse(reduction_permission(validate_state(data, AT), "001437", "new")[0])
        self.assertFalse(reduction_permission(validate_state(state(0), AT), "001437", "new")[0])


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = Calendars()

    def test_us_labor_day_and_tuesday_morning(self):
        self.assertFalse(self.calendar.is_session(date(2026, 9, 7), "US"))
        self.assertEqual(self.calendar.latest_completed(datetime(2026, 9, 8, 0, tzinfo=UTC), "US"), date(2026, 9, 4))

    def test_dst_is_not_a_fixed_offset(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/New_York")
        self.assertEqual(datetime(2026, 9, 8, 14, tzinfo=UTC).astimezone(zone).hour, 10)
        self.assertEqual(datetime(2026, 11, 2, 14, tzinfo=UTC).astimezone(zone).hour, 9)

    def test_black_friday_early_close_and_completion_delay(self):
        self.assertEqual(self.calendar.latest_completed(datetime(2026, 11, 27, 17, 59, tzinfo=UTC), "US"), date(2026, 11, 25))
        self.assertEqual(self.calendar.latest_completed(datetime(2026, 11, 27, 18, 2, tzinfo=UTC), "US"), date(2026, 11, 27))

    def test_china_holidays_and_makeup_weekends(self):
        for day in (date(2026, 2, 23), date(2026, 9, 25), date(2026, 10, 7), date(2026, 10, 10)):
            self.assertFalse(self.calendar.is_session(day, "CN"))
        self.assertTrue(self.calendar.is_session(date(2026, 10, 8), "CN"))

    def test_korean_calendar_is_separate(self):
        for day in (date(2026, 7, 17), date(2026, 9, 24), date(2026, 9, 25), date(2026, 12, 31)):
            self.assertFalse(self.calendar.is_session(day, "KR"))
        self.assertTrue(self.calendar.is_session(date(2026, 9, 28), "KR"))

    def test_china_intraday_does_not_use_today_close(self):
        self.assertEqual(self.calendar.latest_completed(datetime(2026, 9, 7, 6, tzinfo=UTC), "CN"), date(2026, 9, 4))

    def test_unsupported_year_does_not_guess_weekday(self):
        with self.assertRaises(CalendarError):
            self.calendar.is_session(date(2027, 1, 4), "US")


class IndicatorTests(unittest.TestCase):
    def test_two_closes_use_their_own_200_day_average(self):
        rows = bars(220)
        rows[-2].update(close=101, high=101)
        rows[-1].update(close=1, high=1)
        self.assertFalse(indicators(rows)["two_completed_closes_below_own_ma200"])
        rows[-2].update(close=99, high=99)
        self.assertTrue(indicators(rows)["two_completed_closes_below_own_ma200"])

    def test_short_history_keeps_long_trends_unknown(self):
        result = indicators(bars(41))
        self.assertIsNone(result["ma"]["50"])
        self.assertIsNone(result["ma"]["200"])
        self.assertFalse(result["full_52week_coverage"])

    def test_invalid_or_duplicate_prices_rejected(self):
        for value in (True, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                indicators(bars(1, close=value))
        with self.assertRaises(ValueError):
            indicators(bars(1) * 2)

    def test_price_gate_is_not_an_order(self):
        policy = json.loads(Path(__import__('pa_agent.monitoring.service', fromlist=['x']).__file__).with_name('policy.json').read_text())
        data = dataset()
        data['assets']['QQQM']['two_completed_closes_below_own_ma200'] = True
        result = core_review_checks(data, policy)['QQQM']
        self.assertTrue(result['manual_review_required'])
        self.assertFalse(result['order_authorized'])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "results"
        self.client = FakeClient()
        self.service = MonitoringService(self.root, Path(self.tmp.name), client=self.client)

    def tearDown(self):
        self.tmp.cleanup()

    def test_opening_is_read_only_and_offline(self):
        self.assertIsNone(self.service.latest(AT))
        self.assertEqual(self.client.calls, 0)
        self.assertFalse(self.root.exists())

    def test_refresh_persists_and_reopening_does_not_fetch(self):
        result = self.service.refresh()
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.service.latest(AT)['run_id'], result['run_id'])
        self.assertEqual(self.client.calls, 1)
        self.assertFalse(result['notifications'])
        self.assertEqual(result['action_proposals'], [])

    def test_failure_preserves_previous_result(self):
        self.service.refresh()
        original = (self.root / 'current.json').read_bytes()
        self.client.fail = True
        with self.assertRaises(RuntimeError):
            self.service.refresh()
        self.assertEqual((self.root / 'current.json').read_bytes(), original)

    def test_cross_process_lock_prevents_second_fetch(self):
        self.root.mkdir()
        con = sqlite3.connect(self.root / 'refresh-lock.sqlite3')
        con.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(RefreshBusy):
                self.service.refresh()
            self.assertEqual(self.client.calls, 0)
        finally:
            con.close()

    def test_corrupt_snapshot_is_not_silently_loaded(self):
        result = self.service.refresh()
        (self.root / 'runs' / result['run_id'] / 'bundle.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.service.latest(AT)

    def test_expired_snapshot_is_not_shown_as_current(self):
        self.service.refresh()
        result = self.service.latest(datetime(2026, 9, 9, 0, tzinfo=UTC))
        self.assertFalse(result['data']['assets']['VOO']['qualified'])
        self.assertEqual(result['core_checks']['VOO']['status'], 'DATA_UNQUALIFIED')

    def test_cached_live_quote_expires_without_network(self):
        data = dataset()
        data['assets']['VOO']['quote'] = {'price': 100, 'quoted_at': '2026-09-04T19:00:00Z', 'fresh': True}
        with patch.object(self.client, 'collect', return_value=data):
            self.service.refresh()
        cached = self.service.latest(AT)
        self.assertFalse(cached['data']['assets']['VOO']['quote']['fresh'])

    def test_private_amounts_not_persisted(self):
        (Path(self.tmp.name) / 'portfolio_state.json').write_text(json.dumps(state(123.4567)))
        result = self.service.refresh()
        raw = (self.root / 'runs' / result['run_id'] / 'bundle.json').read_text(encoding='utf-8')
        self.assertNotIn('123.4567', raw)
        self.assertNotIn('synthetic-test-only', raw)

    def test_cancel_preserves_previous_result(self):
        from concurrent.futures import CancelledError
        self.service.refresh()
        original = (self.root / 'current.json').read_bytes()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(CancelledError):
            self.service.refresh(cancelled)
        self.assertEqual((self.root / 'current.json').read_bytes(), original)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.policy = MonitoringService(Path(tempfile.gettempdir()) / 'unused-monitor-test').policy

    def test_fx_uses_provider_local_date_and_closing_rate(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Europe/London')
        rows = bars(30)
        payload = {'chart': {'result': [{'meta': {'symbol': 'KRW=X', 'currency': 'KRW', 'exchangeTimezoneName': 'Europe/London'},
                   'timestamp': [int(datetime.combine(date.fromisoformat(r['date']), datetime.min.time(), zone).timestamp()) for r in rows],
                   'indicators': {'quote': [{'close': [100] * 30, 'high': [99] * 30}]}}]}}
        client = PublicClient(self.policy, Calendars())
        with patch.object(client, 'request', return_value=(payload, {'url': 'https://example.invalid/test'})):
            result = client.equity('KRW=X', 'FX', AT)
        self.assertEqual(result['date'], '2026-09-04')
        self.assertTrue(result['qualified'])
        self.assertIn('closing rate', result['basis'])

    def test_fund_requests_current_window_and_waits_for_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'nav.sqlite3'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE fund_nav_records(code TEXT,date TEXT,nav REAL)')
                con.executemany('INSERT INTO fund_nav_records VALUES(?,?,?)', [('001437', r['date'], r['close']) for r in bars(end=date(2026, 9, 3))])
            con.close()
            original = db.read_bytes()
            client = PublicClient(self.policy, Calendars(), db)
            payload = {'status': 1, 'data': {'data': [{'navDate': '2026-09-03', 'netValue': 100}]}}
            with patch.object(client, 'request', return_value=(payload, {})) as request:
                result = client.fund(datetime(2026, 9, 4, 9, tzinfo=UTC))
            self.assertEqual(request.call_args.args[1]['endDate'], '2026-09-04')
            self.assertEqual(result['publication'], 'PENDING_PUBLICATION')
            self.assertFalse(result['qualified'])
            self.assertEqual(db.read_bytes(), original)


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_open_no_fetch_repeated_click_only_one_job(self):
        from pa_agent.gui.manual_monitor import ManualMonitorWidget
        started, release = threading.Event(), threading.Event()
        class Service:
            calls = 0
            def latest(self): return None
            def refresh(self, cancelled):
                self.calls += 1
                started.set()
                release.wait(2)
                return {'checked_at': AT.isoformat(), 'state': {'status': 'MISSING'},
                        'data': dataset(), 'core_checks': {}, 'universe': ['VOO', 'QQQM'], 'research_gaps': []}
        service = Service()
        widget = ManualMonitorWidget(service=service)
        self.assertEqual(service.calls, 0)
        widget.refresh_button.click()
        self.assertTrue(started.wait(1))
        widget.refresh_data()
        self.assertEqual(service.calls, 1)
        self.assertFalse(widget.refresh_button.isEnabled())
        release.set()
        deadline = time.monotonic() + 3
        while widget._future is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(widget.refresh_button.isEnabled())
        self.assertIn('基金', widget.browser.toPlainText())
        self.assertIn('美股', widget.browser.toPlainText())
        widget.close()

    def test_provider_strings_are_escaped(self):
        result = {'checked_at': AT.isoformat(), 'state': {'status': 'MISSING'}, 'data': dataset(),
                  'core_checks': {}, 'universe': [], 'research_gaps': ['<script>bad</script>']}
        text = render(result)
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)


class SnapshotIntegrationTests(unittest.TestCase):
    def test_legacy_prose_cannot_confirm_but_typed_state_can(self):
        from pa_agent.integrations.finance_workspace import load_finance_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'closed_loop').mkdir()
            (root / 'closed_loop/config.json').write_text('{"paper_only":true}', encoding='utf-8')
            (root / 'fund_watch_state.md').write_text('001437 尚未提供持仓数量、成本与日期; not a real investment', encoding='utf-8')
            result = load_finance_snapshot(root)
            self.assertFalse(result['fund']['position_confirmed'])
            self.assertFalse(result['fund']['position_details_complete'])
            (root / 'portfolio_state.json').write_text(json.dumps(state()), encoding='utf-8')
            result = load_finance_snapshot(root)
            self.assertTrue(result['fund']['position_confirmed'])
            self.assertTrue(result['fund']['position_details_complete'])
            self.assertTrue(result['fund']['source_available'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
