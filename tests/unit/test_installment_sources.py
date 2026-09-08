"""Accounting, cutoff and public-source isolation tests; fixtures are synthetic."""
import json
import threading
import urllib.error
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest

from pa_agent.installment.sources import (
    PublicResearchClient, TICKERS_EXCHANGE_URL, TICKERS_URL, _Redirect, extract_financials, extract_timeseries,
)


def fact(start, end, value, filed="2026-08-01", accn="0000000001-26-000001", form="10-Q"):
    return {"start": start, "end": end, "val": value, "filed": filed, "accn": accn, "form": form}


def facts(rows, tag="Revenues", unit="USD"):
    return {"cik": 1, "entityName": "Example Inc.", "facts": {"us-gaap": {tag: {"units": {unit: rows}}}}}


def revenue(payload, at=date(2026, 9, 8), accepted=None):
    return extract_financials(payload, at, "sec-test", accepted)["metrics"]["revenue"]


def test_ttm_requires_matching_comparative_and_retains_components():
    rows = [fact("2025-01-01", "2025-12-31", 100, "2026-02-01", "annual", "10-K"),
            fact("2026-01-01", "2026-06-30", 70), fact("2025-01-01", "2025-06-30", 40)]
    metric = revenue(facts(rows))
    assert metric["ttm"]["value"] == 130
    assert metric["ttm"]["period_start"] == "2025-07-01"
    assert metric["ttm"]["period_end"] == "2026-06-30"
    assert metric["latest_quarter"] is None  # Never half-year / 2.
    assert len(metric["ttm"]["components"]) == 3
    assert all(r["source_refs"] == ["sec-test"] for r in metric["ttm"]["components"])
    rows[-1]["accn"] = "unrelated-filing"
    assert revenue(facts(rows))["ttm"] is None


def test_late_restatement_and_future_period_are_excluded():
    rows = [fact("2026-04-01", "2026-06-30", 20),
            fact("2026-04-01", "2026-06-30", 999, "2026-09-09"),
            fact("2026-07-01", "2026-09-30", 999)]
    assert revenue(facts(rows))["latest_quarter"]["value"] == 20


def test_intraday_cutoff_needs_known_acceptance_time():
    row = fact("2026-04-01", "2026-06-30", 20, "2026-09-08")
    at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    assert revenue(facts([row]), at)["latest_quarter"] is None
    assert revenue(facts([row]), at, {row["accn"]: "2026-09-08T13:00:00Z"})["latest_quarter"] is None
    assert revenue(facts([row]), at, {row["accn"]: "2026-09-08T11:00:00Z"})["latest_quarter"]["value"] == 20


def test_latest_tag_wins_over_obsolete_preferred_tag():
    payload = facts([fact("2020-01-01", "2020-12-31", 9, form="10-K")])
    payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {"units": {"USD": [fact("2026-04-01", "2026-06-30", 20)]}}
    assert revenue(payload)["latest_quarter"]["value"] == 20


def test_currency_ambiguity_is_not_silently_converted():
    row = fact("2025-01-01", "2025-12-31", 100, form="20-F")
    payload = facts([row], unit="TWD")
    payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [{**row, "val": 3}]
    metric = revenue(payload)
    assert metric["annual"] is None
    assert metric["available_units"] == ["TWD", "USD"]


def test_eps_not_summed_and_debt_not_invented_from_long_term_debt():
    rows = [fact("2025-01-01", "2025-12-31", 8, form="10-K")]
    payload = facts(rows, "EarningsPerShareDiluted", "USD/shares")
    payload["facts"]["us-gaap"]["LongTermDebtCurrent"] = {"units": {"USD": [{"end": "2026-06-30", "val": 20, "filed": "2026-08-01", "form": "10-Q"}]}}
    output = extract_financials(payload, date(2026, 9, 8), "sec-test")["metrics"]
    assert output["eps"]["annual"]["value"] == 8
    assert output["eps"]["ttm"] is None
    assert output["debt"]["latest"] is None


def test_conflicting_duplicate_facts_and_nonfinite_values_excluded():
    a = fact("2026-04-01", "2026-06-30", 10)
    assert revenue(facts([a, {**a, "val": 11}]))["latest_quarter"] is None
    assert revenue(facts([{**a, "val": float("nan")}]))["latest_quarter"] is None
    assert revenue(facts([{**a, "val": True}]))["latest_quarter"] is None


def test_instant_balance_has_no_fake_quarter_or_ttm():
    row = {"end": "2026-06-30", "val": 0, "filed": "2026-08-01", "form": "10-Q"}
    metric = extract_financials(facts([row], "CashAndCashEquivalentsAtCarryingValue"), date(2026, 9, 8), "sec-test")["metrics"]["cash"]
    assert metric["latest"]["value"] == 0
    assert metric["ttm"] is None and metric["latest_quarter"] is None


class Response:
    def __init__(self, raw, url=TICKERS_URL):
        self.raw, self.url = raw, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.raw[:limit]

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, raw=b'{"test": 1}', failure=None):
        self.raw, self.failure, self.calls = raw, failure, 0

    def open(self, request, timeout):
        self.calls += 1
        if self.failure:
            raise self.failure
        return Response(self.raw, request.full_url)


def test_bounded_http_cache_fallback_is_stale_and_preserves_timestamp(tmp_path):
    client = PublicResearchClient(cache_dir=tmp_path)
    client._opener = Opener()
    payload, source = client._request(TICKERS_URL)
    assert payload == {"test": 1} and source["status"] == "ok"
    offline = PublicResearchClient(cache_dir=tmp_path)
    offline._opener = Opener(failure=urllib.error.URLError("secret query details"))
    cached, record = offline._request(TICKERS_URL)
    assert cached == payload and record["stale"] and record["status"] == "stale"
    assert record["fetched_at"] == source["fetched_at"]
    assert "secret" not in json.dumps(record)
    assert record["id"] == source["id"]


def test_response_limit_allowlist_redirect_and_cancellation():
    client = PublicResearchClient(max_bytes=1024)
    client._opener = Opener(b"x" * 1025)
    payload, record = client._request(TICKERS_URL)
    assert payload is None and record["status"] == "missing"
    for url in ("http://www.sec.gov/files/a.json", "https://user:pass@data.sec.gov/a", "https://127.0.0.1/a"):
        with pytest.raises(ValueError):
            client._request(url)
    with pytest.raises(ValueError):
        _Redirect().redirect_request(None, None, 302, "", {}, "https://private.test/a")
    with pytest.raises(CancelledError):
        client.fetch("NVDA", date(2026, 9, 8), cancelled=lambda: True)


def test_symbol_identity_blocks_unrelated_issuer_and_news_is_filtered():
    client = PublicResearchClient()
    visited = []
    def get(url, cancelled=None):
        visited.append(url)
        source = {"id": str(len(visited)), "status": "ok", "stale": False}
        if url == TICKERS_URL:
            return {"0": {"ticker": "SPCX", "title": "New Issuer", "cik_str": 1}}, source
        if "submissions" in url:
            return {"cik": 1, "name": "Old ETF", "tickers": ["SPCX"]}, source
        if "search" in url:
            return {"news": [{"title": "Unsafe link", "providerPublishTime": 1788820000, "link": "javascript:bad", "relatedTickers": ["SPCX"]},
                             {"title": "Wrong symbol", "providerPublishTime": 1788820000, "link": "https://example.com", "relatedTickers": ["NVDA"]}]}, source
        return {}, source
    client._request = get
    result = client.fetch("SPCX", date(2026, 9, 8))
    assert result["identity"] is None
    assert not any("companyfacts" in url for url in visited)
    assert result["news"] == []


def test_vendor_units_dates_and_no_filing_date_invention():
    payload = {"timeseries": {"result": [
        {"meta": {"symbol": ["TSM"], "type": ["trailingPeRatio"]}, "trailingPeRatio": [{"asOfDate": "2026-09-03", "reportedValue": {"raw": 30}}]},
        {"meta": {"symbol": ["TSM"], "type": ["trailingMarketCap"]}, "trailingMarketCap": [{"asOfDate": "2026-09-03", "reportedValue": {"raw": 100}}]},
        {"meta": {"symbol": ["TSM"], "type": ["quarterlyDilutedEPS"]}, "quarterlyDilutedEPS": [{"asOfDate": "2026-06-30", "currencyCode": "TWD", "reportedValue": {"raw": 20}}]},
        {"meta": {"symbol": ["TSM"], "type": ["quarterlyTotalRevenue"]}, "quarterlyTotalRevenue": [{"asOfDate": "2026-09-30", "currencyCode": "TWD", "reportedValue": {"raw": 999}}]},
    ]}}
    valuation, financials = extract_timeseries(payload, "TSM", date(2026, 9, 8), "yahoo-test")
    assert valuation["metrics"]["pe"]["value"] == 30
    assert valuation["metrics"]["market_cap"] is None
    eps = financials["metrics"]["eps"]["latest_quarter"]
    assert eps["unit"] == "TWD/shares" and eps["filing_date"] is None
    assert eps["value"] == 20
    assert financials["metrics"]["revenue"]["latest_quarter"] is None
    assert not financials["point_in_time_verified"]


def test_historical_cutoff_does_not_fetch_restatement_prone_vendor_series():
    client = PublicResearchClient()
    visited = []
    def get(url, cancelled=None):
        visited.append(url)
        return {}, {"id": "source", "status": "ok", "stale": False}
    client._request = get
    result = client.fetch("NVDA", date(2020, 1, 1))
    assert not any("timeseries" in url for url in visited)
    assert result["valuation"]["warnings"] == ["VENDOR_DISABLED_FOR_HISTORICAL_CUTOFF"]


def test_sec_requests_are_globally_rate_limited_between_client_instances(monkeypatch):
    import time
    stamps, lock = [], threading.Lock()
    class TimedOpener(Opener):
        def open(self, request, timeout):
            with lock:
                stamps.append(time.monotonic())
            return super().open(request, timeout)
    clients = [PublicResearchClient(), PublicResearchClient()]
    for client in clients:
        client._opener = TimedOpener()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda client: client._request(TICKERS_URL), clients))
    assert abs(stamps[1] - stamps[0]) >= .13


def fallback_client(submission=None, sub_status="ok", primary=None, alternate=None):
    client = PublicResearchClient()
    visited = []
    def get(url, cancelled=None):
        visited.append(url)
        source = {"id": "sec-" + str(len(visited)), "status": "ok", "stale": False, "error_code": None}
        if url in {TICKERS_URL, TICKERS_EXCHANGE_URL}:
            data = primary if url == TICKERS_URL else alternate
            if data is None:
                source.update(status="missing", error_code="HTTP_403")
            return data, source
        if "submissions" in url:
            source.update(status=sub_status, stale=sub_status != "ok", error_code=None if sub_status == "ok" else "HTTP_403")
            return submission, source
        return {}, source
    client._request = get
    return client, visited


def test_both_indexes_missing_can_only_verify_against_live_submissions():
    current = {"cik": "0001045810", "name": "NVIDIA CORP", "tickers": ["NVDA"], "exchanges": ["Nasdaq"]}
    client, visited = fallback_client(current)
    result = client.fetch("NVDA", date(2026, 9, 8))
    identity = result["identity"]
    assert identity["status"] == "verified"
    assert identity["resolution"] == "registered_cik_live_sec_submissions"
    assert identity["routing_seed"]["is_identity_evidence"] is False
    assert len(identity["source_refs"]) == 1
    source = next(r for r in result["sources"] if r["id"] == identity["source_refs"][0])
    assert source["status"] == "ok"
    assert TICKERS_EXCHANGE_URL in visited
    assert len([e for e in result["errors"] if "HTTP_403" in e]) == 2


@pytest.mark.parametrize("submission", [
    None,
    {"cik": 1045810, "name": "NVIDIA CORP", "tickers": ["OTHER"]},
    {"cik": 1045810, "name": "Unrelated Issuer", "tickers": ["NVDA"]},
    {"cik": 2, "name": "NVIDIA CORP", "tickers": ["NVDA"]},
])
def test_registered_cik_never_bypasses_current_issuer_or_ticker_checks(submission):
    client, _ = fallback_client(submission)
    assert client.fetch("NVDA", date(2026, 9, 8))["identity"] is None


def test_stale_submissions_never_verifies_registered_cik():
    client, _ = fallback_client({"cik": 1045810, "name": "NVIDIA CORP", "tickers": ["NVDA"]}, sub_status="stale")
    assert client.fetch("NVDA", date(2026, 9, 8))["identity"]["status"] == "stale"


def test_valid_index_without_ticker_does_not_resurrect_registered_symbol():
    primary = {"0": {"ticker": "OTHER", "cik_str": 2, "title": "Another Issuer"}}
    client, visited = fallback_client(primary=primary)
    assert client.fetch("NVDA", date(2026, 9, 8))["identity"] is None
    assert not any("submissions" in url for url in visited)
    assert TICKERS_EXCHANGE_URL not in visited


def test_official_exchange_index_is_a_valid_alternative():
    alternate = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[3, "Example Inc", "EXM", "Nasdaq"]]}
    client, _ = fallback_client({"cik": 3, "name": "Example Inc", "tickers": ["EXM"]}, alternate=alternate)
    identity = client.fetch("EXM", date(2026, 9, 8))["identity"]
    assert identity["status"] == "verified" and identity["resolution"] == "live_sec_exchange_index"
    assert len(identity["source_refs"]) == 2


def test_global_index_failure_cached_only_until_next_manual_refresh():
    client = PublicResearchClient()
    client._opener = Opener(failure=urllib.error.HTTPError(TICKERS_URL, 403, "", {}, None))
    client._request(TICKERS_URL)
    client._request(TICKERS_URL)
    assert client._opener.calls == 1
    client.begin_refresh()
    client._request(TICKERS_URL)
    assert client._opener.calls == 2


def test_sec_quarter_yoy_uses_latest_disclosed_comparative_not_future_restatement():
    rows = [fact("2026-04-01", "2026-06-30", 120, "2026-08-01", "current"),
            fact("2025-04-01", "2025-06-30", 100, "2025-08-01", "old"),
            fact("2025-04-01", "2025-06-30", 80, "2026-08-01", "current"),
            fact("2025-04-01", "2025-06-30", 50, "2026-09-09", "future")]
    metric = revenue(facts(rows))
    assert metric["prior_year_quarter"]["value"] == 80
    change = metric["quarter_yoy_pct"]
    assert change["value"] == 50 and change["unit"] == "percent"
    assert change["period_end"] == "2026-06-30" and change["filing_date"] == "2026-08-01"
    assert [(r["value"], r["period_end"], r["source_refs"]) for r in change["components"]] == [
        (120, "2026-06-30", ["sec-test"]), (80, "2025-06-30", ["sec-test"])]
    # A shortened quarter is not comparable even when the fiscal end dates align.
    shortened = [rows[0], fact("2025-04-11", "2025-06-30", 80)]
    assert revenue(facts(shortened))["quarter_yoy_pct"] is None


def test_nonpositive_prior_profit_never_produces_percentage_growth():
    for prior, transition in ((-10, "loss_to_profit"), (0, "zero_to_profit")):
        payload = facts([fact("2026-04-01", "2026-06-30", 20),
                         fact("2025-04-01", "2025-06-30", prior)], "NetIncomeLoss")
        metric = extract_financials(payload, date(2026, 9, 8), "sec-test")["metrics"]["net_income"]
        assert metric["quarter_yoy_pct"] is None
        assert metric["prior_year_quarter"]["value"] == prior
        assert metric["profit_transition"]["status"] == transition
        assert [r["value"] for r in metric["profit_transition"]["components"]] == [20, prior]


def test_vendor_quarter_yoy_requires_same_currency_and_three_month_periods():
    def row(day, value, currency="USD", period="3M"):
        return {"asOfDate": day, "reportedValue": {"raw": value}, "currencyCode": currency, "periodType": period}
    rows = [row("2026-06-30", 90), row("2025-06-30", 100), row("2026-09-30", 999)]
    item = {"meta": {"symbol": ["COIN"], "type": ["quarterlyTotalRevenue"]}, "quarterlyTotalRevenue": rows}
    payload = {"timeseries": {"result": [item]}}
    def result():
        return extract_timeseries(payload, "COIN", date(2026, 9, 8), "vendor-test")[1]["metrics"]["revenue"]
    change = result()["quarter_yoy_pct"]
    assert change["value"] == pytest.approx(-10)
    assert change["filing_date"] is None and change["source_refs"] == ["vendor-test"]
    assert [r["period_type"] for r in change["components"]] == ["3M", "3M"]
    rows[1] = row("2025-06-30", 100, "EUR")
    assert result()["prior_year_quarter"] is None
    rows[1] = row("2025-06-30", 100, period="6M")
    assert result()["quarter_yoy_pct"] is None
    rows[1] = row("2025-06-30", 100)
    rows.append(row("2025-06-30", 110))
    assert result()["quarter_yoy_pct"] is None  # conflicting vendor revisions
