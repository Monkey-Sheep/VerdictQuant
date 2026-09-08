"""Offline decision-contract regressions; all financial values are synthetic."""
from datetime import UTC, datetime, timedelta

import pytest

from pa_agent.installment.decision import allocate, assess
from pa_agent.installment.models import CORE_SYMBOLS, SYMBOLS, default_plan


NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
SNAPSHOT = NOW - timedelta(days=2)


def node(value, *, source="sec", period="2026-06-30", **extra):
    return {"value": value, "period_end": period, "source_refs": [source],
            "unit": "USD", **extra}


@pytest.fixture
def evidence():
    price = {"qualified": True, "currency": "USD", "close": 100.0,
             "date": "2026-09-08", "percentile": 100, "drawdown_pct": 0,
             "rows": [{"date": "2026-09-01", "close": 100.0},
                      {"date": "2026-09-08", "close": 100.0}]}
    research = {
        "identity": {"status": "verified"},
        "sources": [{"id": "sec", "status": "ok", "stale": False,
                     "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"},
                    {"id": "quote", "status": "ok", "stale": False,
                     "url": "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/NVDA"},
                    {"id": "news", "status": "ok", "stale": False,
                     "url": "https://query1.finance.yahoo.com/v1/finance/search"}],
        "financials": {"metrics": {
            "revenue": {"latest": node(100)},
            "net_income": {"latest": node(10)}}},
        "valuation": {"metrics": {
            "pe": node(10, source="quote", as_of="2026-09-01"),
            "ev_revenue": node(2, source="quote", as_of="2026-09-01")}},
        "news": [{"title": "Synthetic share-price decline", "source_refs": ["news"]}],
    }
    judgment = {
        "symbol": "NVDA", "business_state": "intact", "valuation_method": "pe_ttm",
        "confidence": "medium", "summary": "Synthetic research conclusion",
        "valuation_explanation": "Synthetic scenario, not a forecast",
        "reasons": ["Synthetic operating evidence"], "risks": ["Demand may decline"],
        "assumptions": ["Synthetic sustainable earnings"],
        "invalidators": ["Reassess after material operating changes"],
        "business_evidence": ["sec"], "valuation_evidence": ["quote"],
        "negative_evidence": [], "fair_multiple_low": 8, "fair_multiple_high": 20,
        "review_in_days": 7,
    }
    return price, research, judgment


def evaluate(evidence):
    price, research, judgment = evidence
    return assess(judgment["symbol"], price, research, judgment, NOW)


def plan(**changes):
    result = default_plan(NOW)
    result.update(monthly_budget_usd=200, portfolio_total_usd=1000,
                  horizon_years=5, confirmed_at=SNAPSHOT.isoformat())
    result["target_weights"]["NVDA"] = 100
    result["positions_usd"] = {s: 0.0 for s in (*SYMBOLS, *CORE_SYMBOLS)}
    result.update(changes)
    return result


def contribution(amount, at, symbol="NVDA", period="2026-09"):
    return {"symbol": symbol, "period": period, "amount_usd": amount,
            "confirmed_at": at.isoformat()}


def candidate(symbol="NVDA", decision="NORMAL"):
    return {"symbol": symbol, "decision": decision, "confidence": "medium"}


def test_new_price_high_does_not_preclude_reasonable_valuation(evidence):
    assert evaluate(evidence)["decision"] == "NORMAL"


def test_deep_drawdown_does_not_override_expensive_valuation(evidence):
    evidence[0].update(percentile=1, drawdown_pct=-70)
    evidence[1]["valuation"]["metrics"]["pe"]["value"] = 50
    assert evaluate(evidence)["decision"] == "PAUSE"


@pytest.mark.parametrize("symbol", ["MU", "SNDK", "SKHY"])
def test_cyclical_peak_static_pe_cannot_justify_buying(evidence, symbol):
    evidence[2]["symbol"] = symbol
    assert evaluate(evidence)["decision"] == "REVIEW"


def test_ev_sales_rebases_equity_not_net_debt(evidence):
    price, research, judgment = evidence
    price["close"] = price["rows"][-1]["close"] = 200
    research["valuation"]["metrics"].update(
        market_cap=node(100, source="quote", as_of="2026-09-01"),
        enterprise_value=node(200, source="quote", as_of="2026-09-01"))
    judgment.update(valuation_method="ev_sales", fair_multiple_low=2.5,
                    fair_multiple_high=3.5)
    result = evaluate(evidence)
    assert result["decision"] == "NORMAL"
    assert result["valuation"]["aligned_multiple"] == pytest.approx(3)
    assert result["valuation"]["scenario_price_range"] == pytest.approx([150, 250])


def test_ev_bridge_missing_cannot_silently_use_price_ratio(evidence):
    evidence[2]["valuation_method"] = "ev_sales"
    result = evaluate(evidence)
    assert result["decision"] == "REVIEW"
    assert result["valuation"]["scenario_price_range"] is None


def test_financial_source_failure_cannot_be_laundered_through_news(evidence):
    evidence[1]["sources"][0]["status"] = "failed"
    evidence[2]["business_evidence"] = ["news"]
    assert evaluate(evidence)["decision"] == "REVIEW"


def test_headline_only_cannot_establish_business_impairment(evidence):
    evidence[2].update(business_state="impaired", negative_evidence=["news"])
    assert evaluate(evidence)["decision"] == "REVIEW"


def test_actual_business_impairment_has_review_trigger(evidence):
    evidence[2].update(business_state="impaired", negative_evidence=["sec"])
    result = evaluate(evidence)
    assert result["decision"] == "PAUSE"
    assert result["invalidators"]
    assert result["next_review"] == "2026-09-15"


def test_unknown_model_citation_fails_closed(evidence):
    evidence[2]["business_evidence"] = ["invented-source"]
    with pytest.raises(ValueError, match="MODEL_UNKNOWN_SOURCE"):
        evaluate(evidence)


def test_valuation_citation_must_match_used_metric(evidence):
    evidence[2]["valuation_evidence"] = ["news"]
    assert evaluate(evidence)["decision"] == "REVIEW"


@pytest.mark.parametrize("mutation", ["missing", "stale", "future"])
def test_financial_gap_cannot_be_filled_by_prices(evidence, mutation):
    financials = evidence[1]["financials"]
    if mutation == "missing":
        financials["metrics"] = {}
    elif mutation == "stale":
        financials["stale"] = True
    else:
        for metric in financials["metrics"].values():
            metric["latest"]["period_end"] = "2027-06-30"
    assert evaluate(evidence)["decision"] == "REVIEW"


@pytest.mark.parametrize("stamp", ["2026-07-01", "2026-09-09"])
def test_expired_or_future_valuation_cannot_support_current_buying(evidence, stamp):
    evidence[1]["valuation"]["metrics"]["pe"]["as_of"] = stamp
    assert evaluate(evidence)["decision"] == "REVIEW"


def test_same_period_same_unit_source_conflict_requires_resolution(evidence):
    # The same reported metric differs tenfold, with no reconciliation supplied.
    research = evidence[1]
    research["vendor_financials"] = {"metrics": {
        "revenue": {"latest": node(1000, source="quote")},
        "net_income": {"latest": node(100, source="quote")}}}
    for section in ("financials", "vendor_financials"):
        for metric in research[section]["metrics"].values():
            metric["latest"].update(period_start="2026-04-01",
                                    period_type="quarterly", accounting_basis="GAAP")
    assert evaluate(evidence)["decision"] == "REVIEW"


def test_unknown_budget_does_not_erase_company_judgment(evidence):
    item = evaluate(evidence)
    summary = allocate([item], default_plan(NOW), [], NOW)
    assert item["decision"] == "NORMAL"
    assert item["budget"]["amount_usd"] is None
    assert summary["ready"] is False


def test_unknown_theme_positions_make_allocation_not_ready():
    p = plan()
    p["positions_usd"]["TSM"] = None
    items = [candidate()]
    result = allocate(items, p, [], NOW)
    assert result["plan_fields_ready"] is True
    assert result["ready"] is result["allocation_ready"] is False
    assert items[0]["budget"]["amount_usd"] is None


def test_post_snapshot_spend_consumes_single_stock_capacity():
    items = [candidate()]
    result = allocate(items, plan(), [contribution(90, SNAPSHOT + timedelta(hours=1))], NOW)
    assert items[0]["budget"]["amount_usd"] == pytest.approx(10)
    assert items[0]["decision"] != "START"
    assert result["confirmed_spent_usd"] + result["proposed_usd"] <= 100


@pytest.mark.parametrize("at", [SNAPSHOT - timedelta(hours=1), SNAPSHOT])
def test_contribution_already_in_snapshot_not_counted_twice(at):
    p = plan()
    p["positions_usd"]["NVDA"] = 90
    items = [candidate()]
    allocate(items, p, [contribution(90, at)], NOW)
    assert items[0]["budget"]["amount_usd"] == pytest.approx(10)


def test_post_snapshot_spend_consumes_shared_theme_capacity():
    p = plan(max_single_pct=100, max_theme_pct=10)
    p["target_weights"].update(NVDA=50, TSM=50)
    items = [candidate(), candidate("TSM")]
    result = allocate(items, p, [contribution(90, SNAPSHOT + timedelta(hours=1), "ASML")], NOW)
    assert result["proposed_usd"] == pytest.approx(10)
    assert sum(x["budget"]["amount_usd"] for x in items) == pytest.approx(10)


def test_prior_month_post_snapshot_spend_counts_only_toward_holdings():
    p = plan(confirmed_at="2026-08-30T00:00:00+00:00")
    items = [candidate()]
    prior = contribution(90, datetime(2026, 8, 31, tzinfo=UTC), period="2026-08")
    result = allocate(items, p, [prior], NOW)
    assert result["confirmed_spent_usd"] == 0
    assert items[0]["budget"]["amount_usd"] == pytest.approx(10)


def test_unreconciled_contribution_timestamp_blocks_amounts():
    row = contribution(10, SNAPSHOT)
    row.pop("confirmed_at")
    items = [candidate()]
    result = allocate(items, plan(), [row], NOW)
    assert result["ready"] is False
    assert items[0]["budget"]["amount_usd"] is None


def test_spent_entire_month_means_zero_additional_budget():
    p = plan(max_single_pct=100, max_theme_pct=100)
    items = [candidate()]
    result = allocate(items, p, [contribution(200, SNAPSHOT + timedelta(hours=1))], NOW)
    assert result["proposed_usd"] == 0
    assert items[0]["budget"]["amount_usd"] == 0


def test_repeat_evaluation_does_not_invent_another_contribution(evidence):
    p = plan(max_single_pct=100)
    left, right = [evaluate(evidence)], [evaluate(evidence)]
    assert allocate(left, p, [], NOW) == allocate(right, p, [], NOW)
    assert left[0]["budget"] == right[0]["budget"]


def test_user_installment_policy_not_model_confidence_sets_fraction():
    p = plan(max_single_pct=100, normal_installment_pct=20)
    medium, low = [candidate()], [candidate()]
    low[0]["confidence"] = "low"
    allocate(medium, p, [], NOW)
    allocate(low, p, [], NOW)
    assert medium[0]["budget"]["amount_usd"] == low[0]["budget"]["amount_usd"] == 40


def isolated_service(tmp_path, monkeypatch, evidence, clock):
    """An explicitly offline service double; never evidence of live AI success."""
    from copy import deepcopy
    from types import SimpleNamespace
    from pa_agent.installment.service import InstallmentService

    analyst = SimpleNamespace(
        model="offline-regression-fixture",
        analyze=lambda *args: {"judgments": {"NVDA": deepcopy(evidence[2])},
                               "provider": {"model": "offline-regression-fixture",
                                            "status": "completed"}})
    service = InstallmentService(tmp_path / "isolated", market_client=object(),
                                 research_client=object(), analyst=analyst,
                                 symbols=["NVDA"], clock=lambda: clock[0])
    monkeypatch.setattr(service, "_collect_one",
                        lambda *args: deepcopy((evidence[0], evidence[1])))
    return service


def test_plan_preference_edit_does_not_rebase_unchanged_holdings(tmp_path, monkeypatch, evidence):
    clock = [SNAPSHOT]
    service = isolated_service(tmp_path, monkeypatch, evidence, clock)
    service.save_plan(plan())
    clock[0] += timedelta(hours=1)
    service.record_contribution("NVDA", 90, "2026-09", event_id="synthetic001")
    clock[0] += timedelta(hours=1)
    edited = service.load_plan()
    edited["horizon_years"] = 6  # No new holdings valuation was entered.
    service.save_plan(edited)
    items = [candidate()]
    allocate(items, service.load_plan(), service.contributions(), clock[0])
    assert items[0]["budget"]["amount_usd"] <= 10


def test_shorter_model_review_deadline_expires_during_closed_market(tmp_path, monkeypatch, evidence):
    clock = [datetime(2026, 9, 4, 21, tzinfo=UTC)]
    evidence[0]["date"] = evidence[0]["rows"][-1]["date"] = "2026-09-04"
    evidence[2]["review_in_days"] = 1
    service = isolated_service(tmp_path, monkeypatch, evidence, clock)
    first = service.refresh()
    assert first["assessments"][0]["next_review"] == "2026-09-05"
    # Sunday: latest completed session is still Friday and 48h have not elapsed.
    clock[0] = datetime(2026, 9, 6, 10, tzinfo=UTC)
    latest = service.latest()
    assert latest["expired"] is True
    assert latest["actionable"] is False


def test_cancel_after_model_returns_preserves_published_snapshot(tmp_path, monkeypatch, evidence):
    import threading
    from concurrent.futures import CancelledError

    clock = [NOW]
    service = isolated_service(tmp_path, monkeypatch, evidence, clock)
    first = service.refresh()
    pointer = (service.root / "current.json").read_bytes()
    stop = threading.Event()
    previous_analyze = service.analyst.analyze

    def cancel_after_response(*args):
        result = previous_analyze(*args)
        stop.set()
        return result

    monkeypatch.setattr(service.analyst, "analyze", cancel_after_response)
    evidence[0]["percentile"] = 99  # Fresh packet, avoiding judgment cache.
    with pytest.raises(CancelledError):
        service.refresh(stop)
    assert (service.root / "current.json").read_bytes() == pointer
    assert service.load_run(first["run_id"])["historical"] is True
