"""Structured paper-quant CLI for people and AI callers."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pa_agent.headless import AnalysisRequest
from pa_agent.quant.models import WatchItem
from pa_agent.quant.service import QuantPaperService


def _print(payload: dict[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verdictquant-paper",
        description="VerdictQuant stock research and paper validation. Never sends live orders.",
    )
    parser.add_argument("--db", type=Path, help="Optional paper SQLite path")
    parser.add_argument("--config", type=Path, help="Optional paper risk config path")
    parser.add_argument("--pretty", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create paper accounts and print their state")
    sub.add_parser("status", help="Show accounts, positions, signals, and validation metrics")
    sub.add_parser("doctor", help="Verify local database, dependencies, and paper-only boundary")
    sub.add_parser("cycle", help="Settle and analyze every configured watch item")
    backup = sub.add_parser("backup", help="Create a verified local paper-account backup")
    backup.add_argument("--output", type=Path)
    restore = sub.add_parser("restore", help="Restore a verified paper-account backup")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--confirm", required=True)
    reset = sub.add_parser("reset", help="Reset the local paper ledger after explicit confirmation")
    reset.add_argument("--confirm", required=True)
    analyze = sub.add_parser("analyze", help="Analyze any US/A-share stock and queue paper intent")
    analyze.add_argument("--market", required=True, choices=["us", "a-share"])
    analyze.add_argument("--symbol", required=True)
    analyze.add_argument("--exchange", default="")
    analyze.add_argument("--timeframe", default="1d")
    analyze.add_argument("--bar-count", type=int, default=100)
    analyze.add_argument("--predict-next-bar", action="store_true")
    analyze.add_argument("--no-queue", action="store_true", help="Analyze and record without paper order")
    settle = sub.add_parser("settle", help="Settle pending paper orders from later public bars")
    settle.add_argument("--market", choices=["us", "a-share"])
    settle.add_argument("--symbol")
    watch_add = sub.add_parser("watch-add", help="Add a recurring stock target")
    watch_add.add_argument("--market", required=True, choices=["us", "a-share"])
    watch_add.add_argument("--symbol", required=True)
    watch_add.add_argument("--exchange", default="")
    watch_add.add_argument("--timeframe", default="1d")
    watch_add.add_argument("--bar-count", type=int, default=100)
    watch_remove = sub.add_parser("watch-remove", help="Remove a recurring stock target")
    watch_remove.add_argument("--market", required=True, choices=["us", "a-share"])
    watch_remove.add_argument("--symbol", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        service = QuantPaperService(db_path=args.db, config_path=args.config)
        if args.command in {"init", "status"}:
            result = service.status()
        elif args.command == "doctor":
            result = service.doctor()
        elif args.command == "cycle":
            result = service.run_watch_cycle()
        elif args.command == "backup":
            result = service.backup(args.output)
        elif args.command == "restore":
            result = service.restore(args.archive, confirmation=args.confirm)
        elif args.command == "reset":
            result = service.reset(confirmation=args.confirm)
        elif args.command == "analyze":
            result = service.analyze_and_queue(
                AnalysisRequest(
                    market=args.market,
                    symbol=args.symbol,
                    exchange=args.exchange,
                    timeframe=args.timeframe,
                    bar_count=args.bar_count,
                    predict_next_bar=args.predict_next_bar,
                ),
                queue=not args.no_queue,
            )
        elif args.command == "settle":
            result = service.settle(market=args.market, symbol=args.symbol)
        elif args.command == "watch-add":
            result = service.add_watch(
                WatchItem(
                    market=args.market,
                    symbol=args.symbol,
                    exchange=args.exchange,
                    timeframe=args.timeframe,
                    bar_count=args.bar_count,
                )
            )
        else:
            result = service.remove_watch(market=args.market, symbol=args.symbol)
    except Exception as exc:
        _print(
            {
                "paper_only": True,
                "live_execution": False,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            },
            args.pretty,
        )
        return 2
    _print(result, args.pretty)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
