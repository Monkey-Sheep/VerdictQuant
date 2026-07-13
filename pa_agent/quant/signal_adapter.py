"""Normalize VerdictQuant's guarded Stage-2 decision into a paper intent."""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from pa_agent.quant.models import SignalIntent

_BUY = {"做多", "buy", "long", "bullish"}
_SELL = {"做空", "sell", "short", "bearish"}
_NO_ORDER = {"", "不下单", "不交易", "none", "no_order", "wait", "null"}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _side(direction: Any, order_type: Any, actionable: bool) -> str:
    if not actionable or str(order_type or "").strip().lower() in _NO_ORDER:
        return "hold"
    value = str(direction or "").strip().lower()
    if value in _BUY:
        return "buy"
    if value in _SELL:
        return "sell"
    return "hold"


def _order_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"限价单", "limit", "limit_order"}:
        return "limit"
    if normalized in {"突破单", "stop", "stop_order", "breakout"}:
        return "stop"
    return "market"


def signal_from_analysis(result: dict[str, Any]) -> SignalIntent:
    """Build a deterministic signal id and preserve the original PA evidence."""
    signal = result.get("signal") if isinstance(result.get("signal"), dict) else {}
    market = str(result.get("market") or "")
    if market not in {"us", "a-share"}:
        raise ValueError("stock paper simulation supports only us and a-share")
    base_ts = int(result.get("latest_closed_bar_ts_ms") or 0)
    if base_ts <= 0:
        raise ValueError("analysis is missing a valid latest closed-bar timestamp")
    identity = {
        "market": market,
        "symbol": str(result.get("symbol") or "").upper(),
        "timeframe": str(result.get("timeframe") or ""),
        "base_bar_ts_ms": base_ts,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:20]
    side = _side(
        signal.get("order_direction"),
        signal.get("order_type"),
        bool(signal.get("actionable")),
    )
    reason = str(signal.get("invalidation_condition") or "")
    if side == "hold":
        reason = reason or "VerdictQuant returned no actionable stock order"
    return SignalIntent(
        signal_id=f"pa_{digest}",
        created_at_ms=int(time.time() * 1000),
        market=market,
        symbol=identity["symbol"],
        exchange=result.get("exchange"),
        timeframe=identity["timeframe"],
        base_bar_ts_ms=base_ts,
        side=side,
        order_type=_order_type(signal.get("order_type")),
        entry_price=_number(signal.get("entry_price")) or _number(result.get("latest_close")),
        stop_loss=_number(signal.get("stop_loss_price")),
        take_profit=_number(signal.get("take_profit_price")),
        confidence=_number(signal.get("trade_confidence")),
        estimated_win_rate=_number(signal.get("estimated_win_rate")),
        reason=reason,
        analysis_record_path=result.get("record_path"),
        provenance=(
            dict(result["analysis_provenance"])
            if isinstance(result.get("analysis_provenance"), dict)
            else {}
        ),
        raw_signal=signal,
    )
