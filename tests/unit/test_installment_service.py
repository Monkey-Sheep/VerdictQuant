"""Decision history, manual-only operation and budget ledger integration."""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pa_agent.installment.ai import AnalysisError, evidence_fingerprint, public_packet
from pa_agent.installment.models import SYMBOLS, default_plan
from pa_agent.installment.service import InstallmentService
from pa_agent.monitoring.service import RefreshBusy

NOW = datetime(2026, 9, 8, 1, tzinfo=UTC)


def research():
    node = {"value": 100, "unit": "USD", "period_end": "2026-06-30", "source_refs": ["fin"]}
    return {"identity": {"status": "verified", "cik": 1045810}, "sources": [
        {"id": "fin", "provider": "SEC", "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json", "status": "ok", "sha256": "a", "stale": False},
        {"id": "val", "provider": "Yahoo Finance", "url": "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/NVDA", "status": "ok", "sha256": "b", "stale": False}],
        "financials": {"metrics": {"revenue": {"latest_quarter": node}, "net_income": {"latest_quarter": {**node, "value": 10}}}},
        "valuation": {"metrics": {"pe": {"value": 25, "as_of": "2026-09-04", "unit": "ratio", "source_refs": ["val"]}}},
        "filings": [], "news": [], "errors": []}


def judgment():
    return {"symbol": "NVDA", "business_state": "intact", "valuation_method": "pe_ttm", "fair_multiple_low": 20,
            "fair_multiple_high": 35, "confidence": "medium", "summary": "经营仍成立。", "valuation_explanation": "区间是隔离测试假设。",
            "reasons": ["有经营数据"], "risks": ["增长风险"], "assumptions": ["盈利可持续"], "invalidators": ["盈利下修"],
            "business_evidence": ["fin"], "valuation_evidence": ["val"], "negative_evidence": [], "review_in_days": 7}


class Market:
    calls = 0
    def equity(self, symbol, market, at):
        self.calls += 1
        rows = [{"date": (datetime(2026, 8, 5) + timedelta(days=i)).date().isoformat(), "close": 100.0, "high": 100.0} for i in range(31)]
        return {"symbol": symbol, "qualified": True, "errors": [], "currency": "USD", "rows": rows, "source": {"url": "https://query1.finance.yahoo.com/test"}}


class Public:
    calls = 0
    resets = 0
    def begin_refresh(self): self.resets += 1
    def fetch(self, symbol, at, cancelled=None):
        self.calls += 1
        return research()


class Analyst:
    calls = 0
    model = "offline-fixture"
    def analyze(self, packets, cancelled=None, progress=None):
        self.calls += 1
        self.packets = packets
        return {"judgments": {"NVDA": judgment()}, "provider": {"model": self.model, "status": "completed", "usage": {}}}


class InstallmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "research"
        self.market, self.public, self.analyst = Market(), Public(), Analyst()
        self.now = NOW
        self.service = InstallmentService(self.root, self.market, self.public, self.analyst, ["NVDA"], lambda: self.now,
                                         lambda *args, **kwargs: {"documents": [], "sources": [], "errors": []})

    def tearDown(self): self.tmp.cleanup()

    def test_open_is_read_only_and_does_not_create_directory(self):
        self.assertIsNone(self.service.latest())
        self.assertEqual(self.service.history(), [])
        self.assertIsNone(self.service.load_plan()["monthly_budget_usd"])
        self.assertFalse(self.root.exists())
        self.assertEqual((self.market.calls, self.public.calls, self.analyst.calls), (0, 0, 0))

    def test_real_refresh_contract_and_same_evidence_reuses_model(self):
        first = self.service.refresh()
        second = self.service.refresh()
        self.assertEqual(first["assessments"][0]["decision"], "NORMAL")
        self.assertTrue(second["model_reused"])
        self.assertEqual(self.analyst.calls, 1)
        self.assertEqual(self.public.resets, 2)
        self.assertEqual(self.market.calls, 2)
        self.assertEqual(len(self.service.history()), 2)
        self.assertIsNone(first["assessments"][0]["budget"]["amount_usd"])

    def test_private_plan_never_enters_model_payload_or_snapshot(self):
        plan = default_plan(NOW)
        plan.update(monthly_budget_usd=1234.5678, portfolio_total_usd=43210.9876, horizon_years=5)
        plan["positions_usd"] = {s: 0 for s in plan["positions_usd"]}
        plan["target_weights"]["NVDA"] = 100
        self.service.save_plan(plan)
        self.service.refresh()
        sent = json.dumps(self.analyst.packets)
        for private in ("1234.5678", "43210.9876", "positions_usd", "target_weights", "contributions"):
            self.assertNotIn(private, sent)
        self.assertNotIn("api_key", sent)

    def test_expiry_changes_view_not_immutable_snapshot(self):
        first = self.service.refresh()
        path = self.root / "runs" / first["run_id"] / "bundle.json"
        original = path.read_bytes()
        self.now += timedelta(days=3)
        loaded = self.service.latest()
        self.assertTrue(loaded["expired"])
        self.assertFalse(loaded["actionable"])
        self.assertIsNone(loaded["assessments"][0]["budget"]["amount_usd"])
        self.assertEqual(path.read_bytes(), original)

    def test_new_trading_day_invalidates_even_within_48_hours(self):
        self.service.refresh()
        self.now = datetime(2026, 9, 9, 1, tzinfo=UTC)
        self.assertTrue(self.service.latest()["expired"])

    def test_tampered_current_snapshot_rejected(self):
        result = self.service.refresh()
        path = self.root / "runs" / result["run_id"] / "bundle.json"
        path.write_text(path.read_text(encoding="utf-8").replace("经营仍成立", "篡改内容"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            self.service.latest()

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError): self.service.load_run("../../outside")

    def test_cancel_preserves_previous_pointer(self):
        self.service.refresh()
        pointer = (self.root / "current.json").read_bytes()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CancelledError): self.service.refresh(stop)
        self.assertEqual((self.root / "current.json").read_bytes(), pointer)

    def test_no_valid_market_preserves_previous_pointer(self):
        self.service.refresh()
        pointer = (self.root / "current.json").read_bytes()
        with patch.object(self.market, "equity", side_effect=ValueError("bad public data")):
            with self.assertRaises(AnalysisError): self.service.refresh()
        self.assertEqual((self.root / "current.json").read_bytes(), pointer)

    def test_refresh_lock_is_cross_instance(self):
        import sqlite3
        self.root.mkdir()
        lock = sqlite3.connect(self.root / "refresh-lock.sqlite3")
        try:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaises(RefreshBusy): self.service.refresh()
        finally:
            lock.close()

    def test_record_idempotency_and_month_budget_reconciliation(self):
        plan = default_plan(NOW)
        plan.update(monthly_budget_usd=200, portfolio_total_usd=1000, horizon_years=5)
        plan["positions_usd"] = {s: 0 for s in plan["positions_usd"]}
        plan["target_weights"]["NVDA"] = 100
        self.service.save_plan(plan)
        self.service.refresh()
        self.now += timedelta(minutes=1)
        self.service.record_contribution("NVDA", 90, "2026-09", event_id="entry0001")
        self.service.record_contribution("NVDA", 90, "2026-09", event_id="entry0001")
        self.assertEqual(len(self.service.contributions()), 1)
        current = self.service.latest()
        self.assertEqual(current["assessments"][0]["budget"]["amount_usd"], 10)
        self.assertNotEqual(current["assessments"][0]["decision"], "START")
        with self.assertRaises(ValueError): self.service.record_contribution("NVDA", 91, "2026-09", event_id="entry0001")

    def test_future_or_outside_period_contribution_rejected(self):
        with self.assertRaises(ValueError): self.service.record_contribution("NVDA", 1, "2026-10")
        with self.assertRaises(ValueError): self.service.record_contribution("VOO", 1, "2026-09")

    def test_fingerprint_ignores_fetch_clock_but_not_new_facts(self):
        a = [{"value": 1, "retrieved_at": "a", "sources": [{"sha256": "x", "fetched_at": "a"}]}]
        b = copy.deepcopy(a)
        b[0]["retrieved_at"] = "b"
        b[0]["sources"][0]["fetched_at"] = "b"
        b[0]["sources"][0]["sha256"] = "transport-only-change"
        self.assertEqual(evidence_fingerprint(a, "x"), evidence_fingerprint(b, "x"))
        b[0]["value"] = 2
        self.assertNotEqual(evidence_fingerprint(a, "x"), evidence_fingerprint(b, "x"))

    def test_one_stock_news_does_not_reanalyze_unchanged_peers(self):
        class PerStockAnalyst:
            model = "offline-fixture"
            calls = []
            def analyze(self, packets, cancelled=None, progress=None):
                self.calls.append([p["symbol"] for p in packets])
                return {"judgments": {p["symbol"]: {**judgment(), "symbol": p["symbol"]} for p in packets},
                        "provider": {"model": self.model, "status": "completed", "usage": {}}}
        analyst = PerStockAnalyst()
        service = InstallmentService(self.root, self.market, self.public, analyst, ["NVDA", "TSLA"], lambda: self.now,
                                     lambda *args, **kwargs: {"documents": [], "sources": [], "errors": []})
        service.refresh()
        original_fetch = self.public.fetch
        def updated(symbol, *args):
            value = original_fetch(symbol, *args)
            if symbol == "TSLA": value["news"] = [{"title": "New reported fact", "published_at": NOW.isoformat()}]
            return value
        with patch.object(self.public, "fetch", side_effect=updated):
            result = service.refresh()
        self.assertEqual(analyst.calls, [["NVDA", "TSLA"], ["TSLA"]])
        self.assertEqual(result["reused_symbols"], ["NVDA"])

    def test_cached_model_review_deadline_is_not_renewed_by_refresh(self):
        row = judgment()
        row["review_in_days"] = 1
        with patch.object(self.analyst, "analyze", return_value={"judgments": {"NVDA": row}, "provider": {"model": "offline-fixture", "status": "completed", "usage": {}}}):
            first = self.service.refresh()
            self.now += timedelta(hours=12)
            second = self.service.refresh()
        self.assertTrue(second["model_reused"])
        self.assertEqual(first["assessments"][0]["review_at"], second["assessments"][0]["review_at"])

    def test_invalid_model_output_is_not_cached_for_the_next_refresh(self):
        bad = judgment()
        bad["business_evidence"] = ["nonexistent-source"]
        with patch.object(self.analyst, "analyze", return_value={"judgments": {"NVDA": bad}, "provider": {"model": "offline-fixture", "status": "completed", "usage": {}}}) as call:
            self.service.refresh()
            self.service.refresh()
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__": unittest.main()
