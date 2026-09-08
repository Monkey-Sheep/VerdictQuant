"""Public research universe and locally confirmed investment-plan inputs."""
from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SYMBOLS = ("NVDA", "TSLA", "SPCX", "COIN", "TSM", "ASML", "MU", "CEG", "VST", "SNDK", "SKHY")
CORE_SYMBOLS = ("001437", "VOO", "QQQM")
NAMES = dict(zip(SYMBOLS, ("英伟达", "特斯拉", "SpaceX", "Coinbase", "台积电", "ASML", "美光", "星座能源", "Vistra", "闪迪", "SK 海力士")))
THEMES = {"AI与半导体": {"NVDA", "TSM", "ASML", "MU", "SNDK", "SKHY"},
          "发电与电力": {"CEG", "VST"}, "高不确定性成长": {"TSLA", "SPCX", "COIN"}}
CYCLICAL = {"MU", "SNDK", "SKHY"}
HIGH_UNCERTAINTY = {"TSLA", "SPCX", "COIN"}
# Identity boundary, not a price/earnings forecast. Reused ticker histories must
# never be joined across unrelated issuers.
HISTORY_FLOORS = {"SPCX": "2026-06-12", "SKHY": "2026-07-10", "SNDK": "2025-02-24"}
IDENTITY_SOURCES = {
    "SPCX": "https://content.spacex.com/cms-assets/FINAL_Documents%20and%20Updates/SpaceX_PricingAnnouncement.pdf",
    "SKHY": "https://news.skhynix.com/en/skhynix-lists-adrs-on-nasdaq/",
    "SNDK": "https://www.sandisk.com/company/newsroom/press-releases/2025/2025-02-24-sandisk-completes-separation-from-western-digital-begins-trading-on-nasdaq",
}


def finite(value, *, minimum=None, maximum=None, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("请输入有效数字，不能使用未知值代替零。")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError("输入超出允许范围。")
    return float(value)


def current_period(now=None):
    return (now or datetime.now(UTC)).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")


def default_plan(now=None):
    return {"schema_version": 1, "period": current_period(now), "monthly_budget_usd": None,
            "horizon_years": None, "portfolio_total_usd": None, "max_single_pct": 10.0,
            "max_theme_pct": 35.0, "target_weights": {s: 0.0 for s in SYMBOLS},
            "normal_installment_pct": 50.0, "higher_risk_installment_pct": 25.0,
            "attractive_installment_pct": 100.0,
            "positions_usd": {s: None for s in (*SYMBOLS, *CORE_SYMBOLS)},
            "position_confirmed_at": {s: None for s in (*SYMBOLS, *CORE_SYMBOLS)},
            "confirmed_at": None, "scope": "manual_installment_research", "live_execution": False}


def validate_plan(value, now=None):
    import re
    if not isinstance(value, dict) or value.get("live_execution", False) is not False:
        raise ValueError("投资计划格式不正确。")
    result = default_plan(now)
    period = value.get("period", result["period"])
    if not isinstance(period, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period):
        raise ValueError("计划月份格式应为 YYYY-MM。")
    result["period"] = period
    for key in ("monthly_budget_usd", "portfolio_total_usd"):
        result[key] = finite(value.get(key), minimum=0, maximum=1_000_000_000, optional=True)
    result["horizon_years"] = finite(value.get("horizon_years"), minimum=1, maximum=50, optional=True)
    for key in ("max_single_pct", "max_theme_pct"):
        result[key] = finite(value.get(key, result[key]), minimum=0.1, maximum=100)
    for key in ("normal_installment_pct", "higher_risk_installment_pct", "attractive_installment_pct"):
        result[key] = finite(value.get(key, result[key]), minimum=0, maximum=100)
    if result["higher_risk_installment_pct"] > result["normal_installment_pct"] or result["normal_installment_pct"] > result["attractive_installment_pct"]:
        raise ValueError("单次分批比例应满足：高风险 ≤ 正常 ≤ 更有吸引力。")
    weights, positions = value.get("target_weights", {}), value.get("positions_usd", {})
    if not isinstance(weights, dict) or not isinstance(positions, dict):
        raise ValueError("标的配置格式不正确。")
    if set(weights) - set(SYMBOLS) or set(positions) - set((*SYMBOLS, *CORE_SYMBOLS)):
        raise ValueError("计划包含研究范围以外的代码。")
    result["target_weights"] = {s: finite(weights.get(s, 0), minimum=0, maximum=100) for s in SYMBOLS}
    if sum(result["target_weights"].values()) > 100.00001:
        raise ValueError("本期资金分配比例合计不能超过 100%。剩余比例留作现金。")
    result["positions_usd"] = {s: finite(positions.get(s), minimum=0, maximum=1_000_000_000, optional=True) for s in (*SYMBOLS, *CORE_SYMBOLS)}
    stamps = value.get("position_confirmed_at", {})
    if not isinstance(stamps, dict) or set(stamps) - set((*SYMBOLS, *CORE_SYMBOLS)):
        raise ValueError("持仓确认时间格式不正确。")
    for symbol, stamp in stamps.items():
        if stamp is not None:
            try:
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError("持仓确认时间必须包含时区。") from None
        result["position_confirmed_at"][symbol] = stamp
    total = result["portfolio_total_usd"]
    if total is not None and sum(v or 0 for v in result["positions_usd"].values()) > total + .01:
        raise ValueError("已填持仓市值之和不能超过全部投资资产市值。")
    result["confirmed_at"] = value.get("confirmed_at")
    return result
