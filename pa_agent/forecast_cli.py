"""Evaluate and summarize VerdictQuant next-bar forecasts."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from pa_agent.forecast_ledger import (
    _atomic_write_json,
    evaluate_forecast,
    find_next_closed_bar,
    load_json_records,
    summarize_evaluated,
)


def _source_for_forecast(forecast: dict[str, Any]) -> Any:
    from pa_agent.data.factory import create_data_source

    kind = str(forecast.get("data_source") or "")
    source = create_data_source(kind)
    if kind == "akshare":
        enable_fallback = getattr(source, "enable_baostock_fallback", None)
        if callable(enable_fallback):
            enable_fallback()
    return source


def evaluate_pending(
    pending_dir: Path,
    evaluated_dir: Path,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    evaluated_dir.mkdir(parents=True, exist_ok=True)
    counts = {"evaluated": 0, "not_matured": 0, "errors": 0}
    details: list[dict[str, Any]] = []
    for pending_path in sorted(pending_dir.glob("*.json"))[:limit]:
        source = None
        try:
            forecast = json.loads(pending_path.read_text(encoding="utf-8"))
            source = _source_for_forecast(forecast)
            source.connect()
            exchange = str(forecast.get("exchange") or "")
            set_exchange = getattr(source, "set_exchange", None)
            if exchange and callable(set_exchange):
                set_exchange(exchange)
            source.subscribe(str(forecast["symbol"]), str(forecast["timeframe"]))
            bars = source.latest_snapshot(100)
            base_ts = int(forecast["base_bar"]["ts_open_ms"])
            next_bar = find_next_closed_bar(bars, base_ts)
            if next_bar is None:
                counts["not_matured"] += 1
                continue
            evaluated = evaluate_forecast(forecast, next_bar)
            out_path = evaluated_dir / pending_path.name
            _atomic_write_json(out_path, evaluated)
            pending_path.unlink()
            counts["evaluated"] += 1
            details.append(
                {
                    "forecast_id": forecast.get("forecast_id"),
                    "symbol": forecast.get("symbol"),
                    "timeframe": forecast.get("timeframe"),
                    "correct": evaluated["evaluation"]["correct"],
                    "brier_score": evaluated["evaluation"]["brier_score"],
                }
            )
        except Exception as exc:
            counts["errors"] += 1
            details.append({"path": str(pending_path), "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if source is not None:
                with suppress(Exception):
                    source.disconnect()
    return {**counts, "details": details}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verdictquant-forecast")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate", help="Score forecasts whose next bar has closed")
    evaluate.add_argument("--pending-dir", type=Path)
    evaluate.add_argument("--evaluated-dir", type=Path)
    evaluate.add_argument("--limit", type=int, default=100)
    evaluate.add_argument("--pretty", action="store_true")
    summary = sub.add_parser("summary", help="Summarize evaluated forecast accuracy")
    summary.add_argument("--evaluated-dir", type=Path)
    summary.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from pa_agent.config.paths import FORECASTS_EVALUATED_DIR, FORECASTS_PENDING_DIR

    if args.command == "evaluate":
        result = evaluate_pending(
            args.pending_dir or FORECASTS_PENDING_DIR,
            args.evaluated_dir or FORECASTS_EVALUATED_DIR,
            limit=max(1, args.limit),
        )
    else:
        records = load_json_records(args.evaluated_dir or FORECASTS_EVALUATED_DIR)
        result = summarize_evaluated(records)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
