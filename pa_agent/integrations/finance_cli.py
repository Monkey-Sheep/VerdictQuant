"""Structured CLI for Codex and other local AI callers."""
from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Sequence
from typing import Any

from pa_agent.integrations.finance_workspace import (
    FinanceWorkspaceError,
    integrated_engines,
    load_finance_snapshot,
    run_paper_cycle,
)


def _print(payload: dict[str, Any], *, pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verdictquant-finance",
        description="Read or run the integrated finance research workspace. Paper only.",
    )
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--workspace", help="Optional finance workspace override")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Read the latest US, A-share, and Polymarket evidence")
    subparsers.add_parser("engines", help="List every integrated upstream engine and license")
    run = subparsers.add_parser("run", help="Run a fixed external paper/research workflow")
    run.add_argument("scope", choices=["stocks", "polymarket", "all"])
    run.add_argument("--timeout", type=int, default=None, help="Optional timeout in seconds")
    analyze = subparsers.add_parser("analyze", help="Run VerdictQuant headless analysis")
    analyze.add_argument("--market", required=True, choices=["us", "a-share", "crypto"])
    analyze.add_argument("--symbol", required=True)
    analyze.add_argument("--timeframe", default="1d")
    analyze.add_argument("--exchange", default="")
    analyze.add_argument("--bar-count", type=int, default=100)
    analyze.add_argument("--fetch-only", action="store_true")
    analyze.add_argument("--predict-next-bar", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            result = load_finance_snapshot(args.workspace)
        elif args.command == "engines":
            result = {
                "paper_only": True,
                "engines": integrated_engines(args.workspace),
            }
        elif args.command == "analyze":
            from pa_agent.headless import AnalysisRequest, run_analysis

            result = run_analysis(
                AnalysisRequest(
                    market=args.market,
                    symbol=args.symbol,
                    timeframe=args.timeframe,
                    exchange=args.exchange,
                    bar_count=args.bar_count,
                    fetch_only=args.fetch_only,
                    predict_next_bar=args.predict_next_bar,
                )
            )
        else:
            result = run_paper_cycle(
                args.scope,
                args.workspace,
                timeout_seconds=args.timeout,
            )
    except FinanceWorkspaceError as exc:
        _print({"status": "error", "paper_only": True, "error": str(exc)}, pretty=args.pretty)
        return 2
    except subprocess.TimeoutExpired as exc:
        _print(
            {"status": "error", "paper_only": True, "error": f"paper cycle timed out: {exc}"},
            pretty=args.pretty,
        )
        return 3
    except Exception as exc:
        _print(
            {
                "status": "error",
                "paper_only": True,
                "error": f"{type(exc).__name__}: {exc}",
            },
            pretty=args.pretty,
        )
        return 4
    _print(result, pretty=args.pretty)
    return 0 if result.get("ok", result.get("status") in {None, "ok", "fetched"}) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
