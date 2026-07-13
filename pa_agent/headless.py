"""Non-interactive VerdictQuant market analysis.

This module exposes the existing data and two-stage AI pipeline without
starting Qt. It only reads public market data and writes analysis records; it
does not connect to a broker or place orders.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pa_agent.data.bar_close_wait import reference_now_ms
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, build_analysis_frame

_TRADINGVIEW_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._=-]{1,32}$")
_A_SHARE_SYMBOL_RE = re.compile(r"^(?:[0-9]{6}|(?:sh|sz)[0-9]{6})$", re.IGNORECASE)
_EXCHANGE_RE = re.compile(r"^[A-Za-z0-9_]{0,32}$")
_MARKET_SOURCE = {"us": "tradingview", "a-share": "akshare", "crypto": "tradingview"}
_A_SHARE_TIMEFRAMES = frozenset({"1h", "4h", "1d"})
_US_TIMEFRAMES = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "45m", "1h", "2h", "3h", "4h", "1d", "1w", "1M"}
)
_NO_ORDER_TYPES = frozenset(
    {"", "none", "null", "no_order", "no order", "wait", "\u4e0d\u4e0b\u5355", "\u4e0d\u4ea4\u6613"}
)


class HeadlessAnalysisError(RuntimeError):
    """Expected input, data, or configuration failure."""


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    market: str
    symbol: str
    timeframe: str = "1d"
    exchange: str = ""
    bar_count: int = 100
    fetch_only: bool = False
    predict_next_bar: bool = False

    def validated(self) -> AnalysisRequest:
        market = self.market.strip().lower()
        if market not in _MARKET_SOURCE:
            raise HeadlessAnalysisError("market must be 'us', 'a-share', or 'crypto'")

        symbol = self.symbol.strip()
        if market in {"us", "crypto"}:
            if not _TRADINGVIEW_SYMBOL_RE.fullmatch(symbol):
                market_label = "US" if market == "us" else "crypto"
                raise HeadlessAnalysisError(f"invalid {market_label} symbol")
            symbol = symbol.upper()
            allowed_timeframes = _US_TIMEFRAMES
        else:
            if not _A_SHARE_SYMBOL_RE.fullmatch(symbol):
                raise HeadlessAnalysisError("A-share symbol must be six digits or sh/sz plus six digits")
            symbol = symbol.lower()
            allowed_timeframes = _A_SHARE_TIMEFRAMES

        timeframe = self.timeframe.strip()
        if timeframe not in allowed_timeframes:
            choices = ", ".join(sorted(allowed_timeframes))
            raise HeadlessAnalysisError(
                f"timeframe {timeframe!r} is not supported for {market}; use {choices}"
            )

        exchange = self.exchange.strip().upper()
        if not _EXCHANGE_RE.fullmatch(exchange):
            raise HeadlessAnalysisError("invalid exchange")
        if market == "a-share" and exchange:
            raise HeadlessAnalysisError("exchange is only valid for the US TradingView source")
        if not 20 <= self.bar_count <= 5000:
            raise HeadlessAnalysisError("bar-count must be between 20 and 5000")

        return AnalysisRequest(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
            bar_count=self.bar_count,
            fetch_only=self.fetch_only,
            predict_next_bar=self.predict_next_bar,
        )


def _default_source_factory(kind: str) -> Any:
    from pa_agent.data.factory import create_data_source

    return create_data_source(kind)


def _default_context_factory() -> Any:
    from pa_agent.app_context import AppContext

    return AppContext.bootstrap(connect_data_source=False)


def _default_orchestrator_factory(ctx: Any) -> Any:
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

    return TwoStageOrchestrator(
        client=ctx.client,
        assembler=ctx.assembler,
        router=ctx.router,
        validator=ctx.validator,
        pending_writer=ctx.pending_writer,
        exp_reader=ctx.exp_reader,
        settings=ctx.settings,
    )


def _record_path(record: Any) -> Path:
    from pa_agent.config.paths import RECORDS_PENDING_DIR
    from pa_agent.records.pending_writer import _build_basename

    return RECORDS_PENDING_DIR / f"{_build_basename(record)}.json"


def _event_name(event: Any) -> str:
    return str(getattr(event, "name", event))


def _signal_summary(record: Any) -> dict[str, Any]:
    stage2 = record.stage2_decision if isinstance(record.stage2_decision, dict) else {}
    decision = stage2.get("decision") if isinstance(stage2.get("decision"), dict) else {}
    order_type = decision.get("order_type")
    order_direction = decision.get("order_direction")
    normalized_order = str(order_type or "").strip().lower()
    actionable = bool(
        record.exception is None
        and order_direction
        and normalized_order not in _NO_ORDER_TYPES
    )
    return {
        "actionable": actionable,
        "order_direction": order_direction,
        "order_type": order_type,
        "trade_confidence": decision.get("trade_confidence"),
        "estimated_win_rate": decision.get("estimated_win_rate"),
        "estimated_win_rate_reasoning": decision.get("estimated_win_rate_reasoning"),
        "entry_price": decision.get("entry_price"),
        "stop_loss_price": decision.get("stop_loss_price"),
        "take_profit_price": decision.get("take_profit_price"),
        "invalidation_condition": decision.get("invalidation_condition"),
        "next_bar_prediction": stage2.get("next_bar_prediction"),
        "next_cycle_prediction": stage2.get("next_cycle_prediction"),
    }


def run_analysis(
    request: AnalysisRequest,
    *,
    source_factory: Callable[[str], Any] = _default_source_factory,
    context_factory: Callable[[], Any] = _default_context_factory,
    orchestrator_factory: Callable[[Any], Any] = _default_orchestrator_factory,
) -> dict[str, Any]:
    """Fetch a closed-bar frame and optionally run the two-stage AI pipeline."""
    req = request.validated()
    source_kind = _MARKET_SOURCE[req.market]
    source = source_factory(source_kind)
    try:
        if source_kind == "akshare":
            enable_fallback = getattr(source, "enable_baostock_fallback", None)
            if callable(enable_fallback):
                enable_fallback()
        source.connect()
        if source_kind == "tradingview":
            set_exchange = getattr(source, "set_exchange", None)
            if callable(set_exchange):
                set_exchange(req.exchange)
        source.subscribe(req.symbol, req.timeframe)
        fetch_count = req.bar_count + INDICATOR_WARMUP_BARS + 5
        bars = source.latest_snapshot(fetch_count)
        frame = build_analysis_frame(
            bars,
            req.bar_count,
            req.symbol,
            req.timeframe,
            now_ms=reference_now_ms(data_source=source),
        )
        if frame is None:
            raise HeadlessAnalysisError(
                f"insufficient closed bars: need {req.bar_count}, received {len(bars)} total bars"
            )

        base_result: dict[str, Any] = {
            "status": "fetched" if req.fetch_only else "running",
            "market": req.market,
            "data_source": source_kind,
            "symbol": req.symbol,
            "exchange": req.exchange or None,
            "timeframe": req.timeframe,
            "bar_count": len(frame.bars),
            "snapshot_ts_local_ms": frame.snapshot_ts_local_ms,
            "latest_closed_bar_ts_ms": int(frame.bars[0].ts_open),
            "latest_close": frame.bars[0].close,
        }
        if req.fetch_only:
            return base_result

        ctx = context_factory()
        settings = getattr(ctx, "settings", None)
        provider = getattr(settings, "provider", None)
        api_key = str(getattr(provider, "api_key", "") or "")
        if not api_key:
            raise HeadlessAnalysisError(
                "no API key configured; save it once in VerdictQuant settings"
            )
        if req.predict_next_bar:
            general = getattr(settings, "general", None)
            if general is None:
                raise HeadlessAnalysisError("settings do not expose next-bar prediction")
            general.enable_next_bar_prediction = True

        from pa_agent.util.threading import CancelToken

        events: list[str] = []
        orchestrator = orchestrator_factory(ctx)
        record = orchestrator.submit(
            frame=frame,
            cancel_token=CancelToken(),
            on_event=lambda event: events.append(_event_name(event)),
        )
        from pa_agent.forecast_ledger import record_next_bar_forecast
        from pa_agent.provenance import build_analysis_provenance

        analysis_provenance = build_analysis_provenance(record)
        forecast_path = record_next_bar_forecast(
            record=record,
            frame=frame,
            market=req.market,
            data_source=source_kind,
            exchange=req.exchange,
        )
        base_result.update(
            {
                "status": "ok" if record.exception is None else "error",
                "events": events,
                "signal": _signal_summary(record),
                "diagnosis": record.stage1_diagnosis,
                "decision": record.stage2_decision,
                "exception": record.exception,
                "usage_total": record.usage_total,
                "analysis_provenance": analysis_provenance,
                "record_path": str(_record_path(record).resolve()),
                "forecast_path": str(forecast_path.resolve()) if forecast_path else None,
            }
        )
        return base_result
    finally:
        with suppress(Exception):
            source.disconnect()


def _write_json_atomic(path: Path, payload: dict[str, Any], *, pretty: bool) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        allow_nan=False,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verdictquant-analyze",
        description="Run VerdictQuant analysis without the GUI. No broker connection or order execution.",
    )
    parser.add_argument("--market", required=True, choices=sorted(_MARKET_SOURCE))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--exchange", default="", help="TradingView exchange, e.g. NASDAQ or NYSE")
    parser.add_argument("--bar-count", type=int, default=100)
    parser.add_argument("--fetch-only", action="store_true", help="Fetch data without calling the AI")
    parser.add_argument(
        "--predict-next-bar",
        action="store_true",
        help="Request bullish/bearish/neutral probabilities for the next bar",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON summary path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = AnalysisRequest(
        market=args.market,
        symbol=args.symbol,
        timeframe=args.timeframe,
        exchange=args.exchange,
        bar_count=args.bar_count,
        fetch_only=args.fetch_only,
        predict_next_bar=args.predict_next_bar,
    )
    try:
        result = run_analysis(request)
    except HeadlessAnalysisError as exc:
        result = {"status": "error", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 3

    if args.output is not None:
        _write_json_atomic(args.output, result, pretty=args.pretty)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            allow_nan=False,
        )
    )
    return 0 if result.get("status") in {"ok", "fetched"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
