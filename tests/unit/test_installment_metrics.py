import pytest

from pa_agent.installment.metrics import price_context, reconcile_valuation
from pa_agent.installment.decision import validate_judgment
from tests.unit.test_installment_service import judgment


def fixture():
    source = {"id": "sec", "status": "ok", "stale": False}
    quarter = {"value": 10, "unit": "USD", "period_end": "2026-06-30", "source_refs": ["sec"]}
    return {"sources": [source, {**source, "id": "vendor"}], "financials": {"metrics": {
        "net_income": {"ttm": quarter}, "revenue": {"ttm": {**quarter, "value": 100}}}},
        "valuation": {"metrics": {"market_cap": {"value": 1000, "unit": "USD", "as_of": "2026-09-04", "source_refs": ["vendor"]},
                                    "pe": {"value": 300}, "ps": {"value": 30}}}}


def test_aggregate_multiples_use_traced_financial_denominator():
    result = reconcile_valuation(fixture())
    assert result["valuation"]["metrics"]["pe"]["value"] == 100
    assert result["valuation"]["metrics"]["ps"]["value"] == 10
    assert result["valuation"]["original_vendor_metrics"]["pe"]["value"] == 300
    assert set(result["valuation"]["metrics"]["pe"]["source_refs"]) == {"sec", "vendor"}


def test_foreign_currency_is_not_silently_converted_for_adr():
    data = fixture()
    data["financials"]["metrics"]["net_income"]["ttm"]["unit"] = "TWD"
    assert reconcile_valuation(data)["valuation"]["metrics"]["pe"]["value"] == 300


def test_nonpositive_profit_removes_pe_instead_of_negative_bargain():
    data = fixture()
    data["financials"]["metrics"]["net_income"]["ttm"]["value"] = -10
    assert reconcile_valuation(data)["valuation"]["metrics"]["pe"] is None


def test_failed_financial_source_cannot_replace_multiple():
    data = fixture()
    data["sources"][0]["status"] = "missing"
    assert reconcile_valuation(data)["valuation"]["metrics"]["pe"]["value"] == 300


def test_spcx_history_starts_with_current_issuer():
    data = {"currency": "USD", "qualified": True, "rows": [
        {"date": "2025-12-31", "close": 20, "high": 20},
        *[{"date": f"2026-06-{d}", "close": 135 + d, "high": 135 + d} for d in range(12, 19)]]}
    result = price_context(data, "SPCX")
    assert result["history_days"] == 7
    assert result["low"] > 20
    assert result["qualified"]


@pytest.mark.parametrize("text", ["营收302.97亿美元", "净利十一亿美元", "净利54.1百万美元", "约20倍市盈率"])
def test_model_cannot_restate_financial_amounts_in_unchecked_prose(text):
    row = judgment()
    row["reasons"] = [text]
    with pytest.raises(ValueError, match="MODEL_UNVERIFIED_NUMERIC_NARRATIVE"):
        validate_judgment(row, "NVDA", {"fin", "val"})
