"""Transparent price context, never a standalone buy/sell rule."""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

from .models import HISTORY_FLOORS


def price_context(asset: dict | None, symbol: str) -> dict:
    if not asset:
        return {"close": None, "date": None, "rows": [], "qualified": False,
                "history_days": 0, "label": "行情获取失败", "errors": ["PRICE_UNAVAILABLE"]}
    rows = [r for r in asset.get("rows", []) if r["date"] >= HISTORY_FLOORS.get(symbol, "1900-01-01")]
    rows.sort(key=lambda row: row["date"])
    if not rows:
        return {"close": None, "date": None, "rows": [], "qualified": False,
                "history_days": 0, "label": "当前发行人历史不足", "errors": ["ISSUER_HISTORY_EMPTY"]}
    if len({r["date"] for r in rows}) != len(rows):
        raise ValueError("DUPLICATE_PRICE_DATES")
    values = [r["close"] for r in rows]
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError("INVALID_PRICE")
    # Yahoo chart closes are split-adjusted. A large discontinuity is withheld
    # for review instead of silently interpreted as a bargain after a split.
    discontinuity = any(max(a / b, b / a) > 2.5 for a, b in zip(values, values[1:]))
    since = (date.fromisoformat(rows[-1]["date"]) - timedelta(days=365)).isoformat()
    window = [r for r in rows if r["date"] >= since]
    w = [r["close"] for r in window]
    last = w[-1]
    highest, lowest = max(w), min(w)
    rank = sum(x <= last for x in w) / len(w) * 100
    span = (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days
    history_label = "近一年收盘" if span >= 365 else "当前发行人上市以来收盘"
    errors = list(asset.get("errors", []))
    if discontinuity:
        errors.append("PRICE_DISCONTINUITY_REVIEW")
    returns = {str(n): (last / values[-n - 1] - 1) * 100 if len(values) > n else None for n in (5, 20, 60)}
    return {"close": last, "date": rows[-1]["date"], "currency": asset.get("currency"),
            "drawdown_pct": (last / highest - 1) * 100,
            "percentile": rank, "range_position_pct": (last - lowest) / (highest - lowest) * 100 if highest != lowest else None,
            "high": highest, "low": lowest, "history_days": len(rows), "calendar_days": span,
            "label": history_label + "；分位仅表示价格位置，不表示低估",
            "returns_pct": returns, "rows": rows, "source": asset.get("source", {}),
            "qualified": bool(asset.get("qualified")) and not discontinuity and len(rows) >= 5,
            "errors": errors, "basis": "来源拆股调整收盘价；非含分红总回报；新发行人历史单独截断"}


def metric_value(node):
    if not isinstance(node, dict):
        return None
    value = node.get("value")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def simplify_financials(research: dict) -> dict:
    """Preserve source basis; do not silently overwrite filings with vendor data."""
    return {"official": research.get("financials", {}), "vendor": research.get("vendor_financials", {}),
            "valuation": research.get("valuation", {}), "warnings": research.get("errors", [])}


def reconcile_valuation(research: dict) -> dict:
    """Use aggregate USD market cap / confirmed USD TTM where available.

    Never derive per-ADR EPS or convert foreign accounting currency implicitly.
    The original vendor multiples remain visible for comparison.
    """
    valuation = research.get("valuation", {})
    metrics = valuation.get("metrics", {})
    mc = metrics.get("market_cap") or {}
    amount = metric_value(mc)
    if amount is None or amount <= 0 or mc.get("unit") != "USD" or valuation.get("stale"):
        return research
    sources = {s["id"] for s in research.get("sources", []) if s.get("status") == "ok" and not s.get("stale")}
    if not mc.get("source_refs") or set(mc["source_refs"]) - sources:
        return research
    for output, metric in (("pe", "net_income"), ("ps", "revenue")):
        node = research.get("financials", {}).get("metrics", {}).get(metric, {}).get("ttm") or {}
        numerator = metric_value(node)
        if numerator is None or node.get("unit") != "USD" or node.get("stale") or research.get("financials", {}).get("stale"):
            continue
        if not node.get("source_refs") or set(node["source_refs"]) - sources:
            continue
        try:
            if not 0 <= (date.fromisoformat(mc["as_of"]) - date.fromisoformat(node["period_end"])).days <= 180:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        value = amount / numerator if numerator > 0 else None
        original = metrics.get(output)
        valuation.setdefault("original_vendor_metrics", {})[output] = original
        if value is None:
            metrics[output] = None
            valuation.setdefault("warnings", []).append(output + ":NONPOSITIVE_CONFIRMED_DENOMINATOR")
            continue
        metrics[output] = {"value": value, "as_of": mc["as_of"], "unit": "ratio",
                           "provider": "本机核算：供应商总市值 / SEC 同币种过去十二个月数据",
                           "source_refs": list(dict.fromkeys(mc["source_refs"] + node["source_refs"])),
                           "basis": "aggregate issuer USD market capitalization / SEC USD TTM; no EPS or ADR conversion",
                           "denominator_period_end": node["period_end"], "denominator": numerator,
                           "market_cap": amount, "vendor_value": metric_value(original)}
        if metric_value(original) and abs(value / original["value"] - 1) > .20:
            valuation.setdefault("warnings", []).append(output + ":VENDOR_MULTIPLE_REPLACED_WITH_TRACED_CALCULATION")
    return research


def verified_facts(research: dict) -> list[str]:
    """Concise statements computed from accepted values, never LLM arithmetic."""
    sources = {s["id"] for s in research.get("sources", []) if s.get("status") == "ok" and not s.get("stale")}
    labels = {"revenue": "营收", "net_income": "净利润", "operating_cash_flow": "经营现金流"}
    facts = []
    for metric, label in labels.items():
        for kind, name in (("financials", "正式披露"), ("vendor_financials", "供应商快照")):
            section = research.get(kind, {})
            if section.get("stale"):
                continue
            group = section.get("metrics", {}).get(metric, {})
            node = group.get("latest_quarter") or group.get("ttm") or {}
            value = metric_value(node)
            if value is None or node.get("stale") or not node.get("source_refs") or set(node["source_refs"]) - sources:
                continue
            period = "季度" if group.get("latest_quarter") else "过去十二个月"
            sign = "为正" if value > 0 else "为负" if value < 0 else "为零"
            phrase = f"{name}：截至 {node.get('period_end', '未知日期')} 的{period}{label}{sign}。"
            yoy = group.get("quarter_yoy_pct")
            if isinstance(yoy, dict) and metric_value(yoy) is not None:
                phrase += f" 同口径季度同比 {yoy['value']:+.2f}%。"
            facts.append(phrase)
            break
    return facts
