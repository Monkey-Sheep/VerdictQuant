"""Read and run the workspace's external-only finance evidence pipelines.

This module deliberately contains no trading strategy or execution engine. It
normalizes reports produced by the pinned upstream projects under
``closed_loop`` and exposes a fixed paper-only command allowlist.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typing import Any, Literal

from pa_agent.monitoring.state import fund_confirmation, load_state

PaperScope = Literal["stocks", "polymarket", "all"]

_SCRIPT_BY_SCOPE: dict[PaperScope, str] = {
    "stocks": "run_stock_loop.ps1",
    "polymarket": "run_polymarket_loop.ps1",
    "all": "run_closed_loop.ps1",
}

_INTEGRATED_ENGINES: tuple[dict[str, Any], ...] = (
    {
        "project": "VerdictQuant",
        "repository": "https://github.com/Monkey-Sheep/VerdictQuant",
        "role": "Host workbench, safety boundary, evidence routing, and paper validation",
        "license": "AGPL-3.0-or-later",
        "integration": "host-source",
    },
    {
        "project": "PA Agent",
        "repository": "https://github.com/rosemarycox5334-debug/PA_Agent",
        "role": "Upstream two-stage price-action analysis core",
        "license": "AGPL-3.0-or-later",
        "integration": "modified-upstream-source",
    },
    {
        "project": "TradingAgents",
        "repository": "https://github.com/TauricResearch/TradingAgents",
        "role": "US company, news, fundamental, and technical second opinion",
        "license": "Apache-2.0",
        "integration": "external-source-adapter",
    },
    {
        "project": "TradingView Screener",
        "repository": "https://github.com/shner-elmo/TradingView-Screener",
        "role": "Current US and A-share cross-sectional discovery",
        "license": "MIT",
        "integration": "external-source-adapter",
    },
    {
        "project": "Microsoft Qlib",
        "repository": "https://github.com/microsoft/qlib",
        "role": "Factor research and walk-forward evaluation",
        "license": "MIT",
        "integration": "external-source-adapter",
    },
    {
        "project": "Backtrader",
        "repository": "https://github.com/mementum/backtrader",
        "role": "Fixed upstream stock reference strategies and simulation",
        "license": "GPL-3.0-or-later",
        "integration": "isolated-external-source",
    },
    {
        "project": "RQAlpha",
        "repository": "https://github.com/ricequant/rqalpha",
        "role": "A-share T+1 and market-rule validation for private research",
        "license": "custom non-commercial",
        "integration": "optional-private-research-source",
    },
    {
        "project": "Polymarket Paper Trader",
        "repository": "https://github.com/agent-next/polymarket-paper-trader",
        "role": "Paper accounts, order-book walking, fills, fees, and P/L",
        "license": "MIT",
        "integration": "external-source-adapter",
    },
    {
        "project": "NautilusTrader",
        "repository": "https://github.com/nautechsystems/nautilus_trader",
        "role": "Independent Polymarket data and deterministic backtest",
        "license": "LGPL-3.0-or-later",
        "integration": "isolated-external-source",
    },
)


class FinanceWorkspaceError(RuntimeError):
    """Raised when the external finance workspace is missing or malformed."""


def integrated_engines(workspace: str | Path | None = None) -> list[dict[str, Any]]:
    """Return visible provenance and installation state for every source engine."""
    root = resolve_finance_workspace(workspace)
    directory_by_project = {
        "VerdictQuant": root / "PA_Agent_hardened",
        "PA Agent": root / "PA_Agent_hardened",
        "TradingAgents": root / "external_tools" / "TradingAgents",
        "TradingView Screener": root / "external_tools" / "TradingView-Screener",
        "Microsoft Qlib": root / "external_tools" / "qlib",
        "Backtrader": root / "external_tools" / "backtrader",
        "RQAlpha": root / "external_tools" / "rqalpha",
        "Polymarket Paper Trader": root / "external_tools" / "polymarket-paper-trader",
        "NautilusTrader": root / "external_tools" / "nautilus_trader",
    }
    return [
        {
            **engine,
            "installed": directory_by_project[engine["project"]].is_dir(),
            "path": str(directory_by_project[engine["project"]]),
        }
        for engine in _INTEGRATED_ENGINES
    ]


def resolve_finance_workspace(explicit: str | Path | None = None) -> Path:
    """Return the parent workspace that owns ``closed_loop``.

    Resolution order is explicit argument, environment override, then the
    parent of this VerdictQuant checkout.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = (
        os.environ.get("VERDICTQUANT_FINANCE_WORKSPACE", "").strip()
        or os.environ.get("PA_AGENT_FINANCE_WORKSPACE", "").strip()
    )
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).resolve().parents[3])

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "closed_loop" / "config.json").is_file():
            return resolved
    checked = ", ".join(str(item.expanduser()) for item in candidates)
    raise FinanceWorkspaceError(f"finance workspace not found; checked: {checked}")


def _read_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise FinanceWorkspaceError(f"required finance evidence is missing: {path}") from None
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise FinanceWorkspaceError(f"invalid finance evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FinanceWorkspaceError(f"finance evidence must be a JSON object: {path}")
    return payload


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise FinanceWorkspaceError(f"unable to read finance state {path}: {exc}") from exc


def _capture_number(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return _number(match.group(1).replace(",", "")) if match else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _stock_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(item.get("symbol") or ""),
        "market": str(item.get("market") or ""),
        "status": str(item.get("status") or "UNKNOWN"),
        "bars": int(item.get("bars") or 0),
        "last_date": item.get("last_date"),
        "last_close": _number(item.get("last_close")),
        "buy_hold_return_percent": _number(item.get("buy_hold_return_percent")),
        "consensus": str(item.get("external_consensus") or "UNKNOWN"),
    }


def _polymarket_row(item: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    return {
        "name": str(item.get("name") or ""),
        "new_entries_enabled": bool(item.get("new_entries_enabled")),
        "gate_reason": str(item.get("gate_reason") or ""),
        "total_value": _number(stats.get("total_value")),
        "pnl": _number(stats.get("pnl")),
        "roi_percent": _number(stats.get("roi_pct")),
        "trades": int(stats.get("total_trades") or 0),
        "positions": len(stats.get("positions") or []),
        "open_orders": len(stats.get("open_orders") or []),
    }


def evidence_paths(workspace: str | Path | None = None) -> dict[str, Path]:
    root = resolve_finance_workspace(workspace)
    reports = root / "closed_loop" / "reports"
    return {
        "config": root / "closed_loop" / "config.json",
        "stocks": reports / "stocks" / "latest.json",
        "rqalpha": reports / "rqalpha" / "latest.json",
        "polymarket": reports / "polymarket" / "latest.json",
        "promotion": reports / "promotion-status.json",
        "research": reports / "research-focus-latest.json",
        "stock_state": root / "stock_watch_state.md",
        "fund_state": root / "fund_watch_state.md",
        "financial_board": root / "FINANCIAL_BOARD.md",
    }


def load_finance_snapshot(workspace: str | Path | None = None) -> dict[str, Any]:
    """Load a compact, machine-readable snapshot for GUI and AI consumers."""
    root = resolve_finance_workspace(workspace)
    paths = evidence_paths(root)
    config = _read_json(paths["config"], required=True)
    stocks = _read_json(paths["stocks"])
    rqalpha = _read_json(paths["rqalpha"])
    polymarket = _read_json(paths["polymarket"])
    promotion = _read_json(paths["promotion"])
    research = _read_json(paths["research"])
    stock_state = _read_text(paths["stock_state"])
    fund_state = _read_text(paths["fund_state"])
    confirmed_fund = fund_confirmation(load_state(root / "portfolio_state.json"))

    file_state = []
    for name, path in paths.items():
        try:
            stat = path.stat()
            file_state.append(
                {
                    "name": name,
                    "path": str(path),
                    "exists": True,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    "bytes": stat.st_size,
                }
            )
        except FileNotFoundError:
            file_state.append(
                {"name": name, "path": str(path), "exists": False, "modified_at": None, "bytes": 0}
            )

    rq_bundle = rqalpha.get("bundle_health") if isinstance(rqalpha.get("bundle_health"), dict) else {}
    stock_promotion = promotion.get("stocks") if isinstance(promotion.get("stocks"), dict) else {}
    poly_promotion = (
        promotion.get("polymarket") if isinstance(promotion.get("polymarket"), dict) else {}
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "paper_only": True,
        "live_execution": False,
        "real_account_connection": False,
        "workspace": str(root),
        "portfolio": {
            "currency": "HKD",
            "total_balance": _capture_number(stock_state, r"Total balance:\s*([0-9,]+(?:\.[0-9]+)?)"),
            "invested": _capture_number(stock_state, r"Invested:\s*([0-9,]+(?:\.[0-9]+)?)"),
            "cash": _capture_number(
                stock_state, r"Remaining cash:\s*(?:about\s*)?([0-9,]+(?:\.[0-9]+)?)"
            ),
            "holdings": [
                {
                    "symbol": symbol,
                    "shares": _capture_number(
                        stock_state, rf"-\s*{re.escape(symbol)}:\s*([0-9,]+(?:\.[0-9]+)?)\s*shares"
                    ),
                }
                for symbol in ("VOO", "QQQM")
            ]
            + [
                {
                    "symbol": "COIN",
                    "shares": _capture_number(
                        stock_state,
                        r"Current COIN position:\s*([0-9,]+(?:\.[0-9]+)?)\s*shares",
                    ),
                }
            ],
            "source": str(paths["stock_state"]),
            "advisory_only": True,
        },
        "fund": {
            "code": "001437" if "001437" in fund_state or confirmed_fund["state_source_verified"] else None,
            **confirmed_fund,
            "actions": ["HOLD", "STOP_ADDING", "WATCH_CLOSELY", "REDUCE", "EXIT_PROPOSAL"],
            "source": str(root / "portfolio_state.json"),
            "source_available": (root / "portfolio_state.json").is_file(),
            "manual_action_only": True,
        },
        "us": {
            "watchlist": config.get("us_symbols") or [],
            "report_generated_at": stocks.get("generated_at"),
            "rows": [_stock_row(item) for item in stocks.get("us") or [] if isinstance(item, dict)],
            "screened_candidates": len(stocks.get("us_screen_backtests") or []),
            "research_queue": research.get("candidates") or [],
            "promotion_state": str(stock_promotion.get("state") or "UNKNOWN"),
        },
        "a_shares": {
            "watchlist": config.get("a_share_symbols") or [],
            "report_generated_at": stocks.get("generated_at"),
            "rows": [
                _stock_row(item) for item in stocks.get("a_shares") or [] if isinstance(item, dict)
            ],
            "screened_candidates": len(stocks.get("a_share_screen_backtests") or []),
            "rqalpha_status": str(rq_bundle.get("status") or "UNKNOWN"),
            "rqalpha_bundle_as_of": rq_bundle.get("bundle_as_of"),
            "rqalpha_candidates": rqalpha.get("candidate_backtests") or [],
        },
        "polymarket": {
            "starting_balance": _number(polymarket.get("starting_balance")),
            "report_generated_at": polymarket.get("generated_at"),
            "accounts": [
                _polymarket_row(item)
                for item in polymarket.get("accounts") or []
                if isinstance(item, dict)
            ],
            "promotion_state": str(poly_promotion.get("state") or "UNKNOWN"),
            "market_data_quality": polymarket.get("market_data_quality") or {},
        },
        "engines": integrated_engines(root),
        "evidence": file_state,
        "boundary": promotion.get("real_account_boundary") or {
            "automatic_connection_allowed": False,
            "automatic_order_allowed": False,
            "bank_or_broker_inspection_allowed": False,
        },
    }


def paper_script(scope: PaperScope, workspace: str | Path | None = None) -> Path:
    """Resolve one allowlisted paper workflow script."""
    if scope not in _SCRIPT_BY_SCOPE:
        raise FinanceWorkspaceError(f"unsupported paper scope: {scope}")
    script = resolve_finance_workspace(workspace) / "closed_loop" / _SCRIPT_BY_SCOPE[scope]
    if not script.is_file():
        raise FinanceWorkspaceError(f"paper workflow is missing: {script}")
    return script


def paper_command(scope: PaperScope, workspace: str | Path | None = None) -> list[str]:
    script = paper_script(scope, workspace)
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]


def run_paper_cycle(
    scope: PaperScope,
    workspace: str | Path | None = None,
    *,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one allowlisted external paper cycle and return bounded evidence."""
    root = resolve_finance_workspace(workspace)
    command = paper_command(scope, root)
    completed = subprocess.run(
        command,
        cwd=root / "closed_loop",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        "scope": scope,
        "paper_only": True,
        "live_execution": False,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "stdout_tail": completed.stdout[-6000:],
        "stderr_tail": completed.stderr[-6000:],
        "snapshot": load_finance_snapshot(root),
    }
