from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.integrations.finance_workspace import (
    FinanceWorkspaceError,
    integrated_engines,
    load_finance_snapshot,
    paper_command,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "finance"
    _write_json(
        root / "closed_loop" / "config.json",
        {"paper_only": True, "us_symbols": ["SPCX"], "a_share_symbols": ["600900.SS"]},
    )
    _write_json(
        root / "closed_loop" / "reports" / "stocks" / "latest.json",
        {
            "generated_at": "2026-07-12T00:00:00Z",
            "us": [{"symbol": "SPCX", "status": "INSUFFICIENT_HISTORY", "bars": 19}],
            "a_shares": [{"symbol": "600900.SS", "status": "OK", "bars": 753}],
        },
    )
    _write_json(
        root / "closed_loop" / "reports" / "polymarket" / "latest.json",
        {"starting_balance": 300, "accounts": []},
    )
    _write_json(
        root / "closed_loop" / "reports" / "promotion-status.json",
        {
            "stocks": {"state": "PAPER_VALIDATING"},
            "polymarket": {"state": "PAPER_VALIDATING"},
        },
    )
    for script in ("run_stock_loop.ps1", "run_polymarket_loop.ps1", "run_closed_loop.ps1"):
        (root / "closed_loop" / script).write_text("exit 0\n", encoding="utf-8")
    (root / "stock_watch_state.md").write_text(
        """# Stock Watch State
- Total balance: 257,028 HKD
- Invested: 254,503 HKD
- Remaining cash: about 2,525 HKD
  - VOO: 37 shares
  - QQQM: 24 shares
- Current COIN position: 0 shares.
""",
        encoding="utf-8",
    )
    (root / "fund_watch_state.md").write_text(
        """# Fund Watch State
- Code: `001437`
- The user has confirmed this is a real investment.
- Current holding amount, average cost, purchase date, and redemption constraints are unknown.
""",
        encoding="utf-8",
    )
    (root / "FINANCIAL_BOARD.md").write_text("# Financial Board\n", encoding="utf-8")
    return root


def test_load_snapshot_keeps_hard_paper_boundary(tmp_path: Path) -> None:
    snapshot = load_finance_snapshot(_workspace(tmp_path))
    assert snapshot["paper_only"] is True
    assert snapshot["live_execution"] is False
    assert snapshot["real_account_connection"] is False
    assert snapshot["us"]["rows"][0]["symbol"] == "SPCX"
    assert snapshot["a_shares"]["rows"][0]["symbol"] == "600900.SS"
    assert len(snapshot["engines"]) == 9
    assert snapshot["portfolio"]["total_balance"] == 257_028
    assert snapshot["portfolio"]["holdings"][0] == {"symbol": "VOO", "shares": 37.0}
    assert snapshot["fund"]["code"] == "001437"
    assert snapshot["fund"]["position_details_complete"] is False


def test_paper_command_is_fixed_allowlist(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    command = paper_command("stocks", root)
    assert command[:5] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]
    assert command[-1].endswith("run_stock_loop.ps1")
    with pytest.raises(FinanceWorkspaceError, match="unsupported paper scope"):
        paper_command("live-broker", root)  # type: ignore[arg-type]


def test_finance_hub_loads_all_market_tabs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: pytest.FixtureRequest,
) -> None:
    from pa_agent.gui.finance_hub import FinanceHubWidget

    root = _workspace(tmp_path)
    monkeypatch.setenv("VERDICTQUANT_FINANCE_WORKSPACE", str(root))
    monkeypatch.setenv("VERDICTQUANT_QUANT_DB", str(tmp_path / "paper.db"))
    monkeypatch.setenv("VERDICTQUANT_QUANT_CONFIG", str(tmp_path / "quant-config.json"))
    widget = FinanceHubWidget()
    qtbot.addWidget(widget)  # type: ignore[attr-defined]
    assert widget._tabs.count() == 9
    assert widget._us_table.rowCount() == 1
    assert widget._a_table.rowCount() == 1
    assert widget._poly_table.rowCount() == 0
    assert widget._portfolio_table.rowCount() == 6
    assert widget._quant_paper._service.status()["paper_only"] is True


def test_integrated_engine_provenance_is_visible(tmp_path: Path) -> None:
    engines = integrated_engines(_workspace(tmp_path))
    names = {engine["project"] for engine in engines}
    assert "VerdictQuant" in names
    assert "PA Agent" in names
    assert "TradingAgents" in names
    assert "RQAlpha" in names
    assert "Polymarket Paper Trader" in names
    assert all(engine["repository"].startswith("https://github.com/") for engine in engines)
