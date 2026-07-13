"""Transactional SQLite ledger for paper-only stock simulation."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pa_agent.quant.models import PaperConfig, SignalIntent

CURRENT_SCHEMA_VERSION = 3

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    market TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    created_at_ms INTEGER NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    base_bar_ts_ms INTEGER NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity REAL NOT NULL,
    requested_price REAL,
    eligible_after_ts_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    wait_bars INTEGER NOT NULL DEFAULT 0,
    last_evaluated_ts_ms INTEGER NOT NULL DEFAULT 0,
    fill_ts_ms INTEGER,
    fill_price REAL,
    filled_quantity REAL,
    fee REAL,
    reject_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE TABLE IF NOT EXISTS positions (
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_price REAL NOT NULL,
    opened_at_ts_ms INTEGER NOT NULL,
    available_after_date TEXT,
    entry_fee_basis REAL NOT NULL DEFAULT 0,
    stop_loss REAL,
    take_profit REAL,
    source_signal_id TEXT NOT NULL,
    holding_bars INTEGER NOT NULL DEFAULT 0,
    last_processed_ts_ms INTEGER NOT NULL,
    last_price REAL NOT NULL,
    PRIMARY KEY(market, symbol)
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    ts_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_signal_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders(status, market, symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at_ms DESC);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
);
"""


def _trade_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["realized_pnl"]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnls:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else None,
        "realized_pnl": sum(pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "expectancy": sum(pnls) / len(pnls) if rows else None,
        "max_realized_drawdown": max_drawdown if rows else None,
    }


def _group_outcomes(
    outcomes: list[dict[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in outcomes:
        label = str(row.get(key) or "unknown")
        grouped.setdefault(label, []).append(row)
    return {label: _trade_summary(rows) for label, rows in sorted(grouped.items())}


def _probability_calibration(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        row
        for row in outcomes
        if isinstance(row.get("estimated_win_rate"), (int, float))
        and 0 <= float(row["estimated_win_rate"]) <= 100
    ]
    bins: list[dict[str, Any]] = []
    for lower in range(0, 100, 10):
        upper = lower + 10
        members = [
            row
            for row in rows
            if lower <= float(row["estimated_win_rate"]) < upper
            or (upper == 100 and float(row["estimated_win_rate"]) == 100)
        ]
        if not members:
            continue
        probabilities = [float(row["estimated_win_rate"]) / 100 for row in members]
        outcomes_binary = [1.0 if float(row["realized_pnl"]) > 0 else 0.0 for row in members]
        mean_probability = sum(probabilities) / len(probabilities)
        observed_win_rate = sum(outcomes_binary) / len(outcomes_binary)
        bins.append(
            {
                "lower": lower / 100,
                "upper": upper / 100,
                "count": len(members),
                "mean_probability": mean_probability,
                "observed_win_rate": observed_win_rate,
                "calibration_gap": abs(mean_probability - observed_win_rate),
            }
        )
    briers = [
        (float(row["estimated_win_rate"]) / 100 - (1.0 if float(row["realized_pnl"]) > 0 else 0.0))
        ** 2
        for row in rows
    ]
    return {
        "count": len(rows),
        "mean_brier_score": sum(briers) / len(briers) if briers else None,
        "bins": bins,
    }


class QuantStore:
    """Small persistence boundary; all writes are committed atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(_BASE_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Upgrade legacy ledgers in place without changing trading history."""
        now = int(time.time() * 1000)
        if "last_processed_ts_ms" not in self._columns(conn, "positions"):
            conn.execute(
                "ALTER TABLE positions ADD COLUMN last_processed_ts_ms INTEGER NOT NULL DEFAULT 0"
            )

        row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        version = int(row["version"] or 0)
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"paper database schema {version} is newer than supported "
                f"schema {CURRENT_SCHEMA_VERSION}"
            )
        if version == 0:
            conn.execute(
                "INSERT INTO schema_migrations(version,name,applied_at_ms) VALUES(1,?,?)",
                ("baseline-paper-ledger", now),
            )
            version = 1
        if version < 2:
            self._migrate_v2_local_identity(conn, now)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,applied_at_ms) VALUES(2,?,?)",
                ("stable-local-profile-and-account-ids", now),
            )
            version = 2
        if version < 3:
            self._migrate_v3_execution_evidence(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,applied_at_ms) VALUES(3,?,?)",
                ("idempotent-order-bars-and-net-trade-attribution", now),
            )

        self._validate_schema(conn)

    def _migrate_v2_local_identity(self, conn: sqlite3.Connection, now: int) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_profile (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                profile_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        profile = conn.execute(
            "SELECT profile_id FROM local_profile WHERE singleton=1"
        ).fetchone()
        if profile is None:
            profile_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO local_profile(singleton,profile_id,display_name,created_at_ms) "
                "VALUES(1,?,?,?)",
                (profile_id, "Local Paper Portfolio", now),
            )
        else:
            profile_id = str(profile["profile_id"])

        if "account_id" not in self._columns(conn, "accounts"):
            conn.execute("ALTER TABLE accounts ADD COLUMN account_id TEXT")
        for row in conn.execute("SELECT market,account_id FROM accounts").fetchall():
            if not row["account_id"]:
                conn.execute(
                    "UPDATE accounts SET account_id=? WHERE market=?",
                    (self._account_id(profile_id, str(row["market"])), row["market"]),
                )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_account_id ON accounts(account_id)"
        )

    def _migrate_v3_execution_evidence(self, conn: sqlite3.Connection) -> None:
        if "last_evaluated_ts_ms" not in self._columns(conn, "orders"):
            conn.execute(
                "ALTER TABLE orders ADD COLUMN last_evaluated_ts_ms INTEGER NOT NULL DEFAULT 0"
            )
        if "filled_quantity" not in self._columns(conn, "orders"):
            conn.execute("ALTER TABLE orders ADD COLUMN filled_quantity REAL")
        if "entry_fee_basis" not in self._columns(conn, "positions"):
            conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_fee_basis REAL NOT NULL DEFAULT 0"
            )
            conn.execute(
                """
                UPDATE positions
                SET entry_fee_basis=COALESCE((
                    SELECT SUM(fee) FROM fills
                    WHERE fills.market=positions.market
                      AND fills.symbol=positions.symbol
                      AND fills.side='buy'
                      AND fills.ts_ms>=positions.opened_at_ts_ms
                ), 0)
                """
            )
        if "source_signal_id" not in self._columns(conn, "fills"):
            conn.execute("ALTER TABLE fills ADD COLUMN source_signal_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fills_source_signal ON fills(source_signal_id)"
        )

    def _validate_schema(self, conn: sqlite3.Connection) -> None:
        required_tables = {
            "accounts",
            "signals",
            "orders",
            "positions",
            "fills",
            "schema_migrations",
            "local_profile",
        }
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required_tables - tables)
        if missing:
            raise RuntimeError(f"paper database is missing tables: {', '.join(missing)}")
        if "account_id" not in self._columns(conn, "accounts"):
            raise RuntimeError("paper database migration did not create account_id")
        required_columns = {
            "orders": {"last_evaluated_ts_ms", "filled_quantity"},
            "positions": {"entry_fee_basis"},
            "fills": {"source_signal_id"},
        }
        for table, columns in required_columns.items():
            missing_columns = sorted(columns - self._columns(conn, table))
            if missing_columns:
                raise RuntimeError(
                    f"paper database {table} table is missing columns: "
                    + ", ".join(missing_columns)
                )
        profiles = conn.execute("SELECT COUNT(*) AS count FROM local_profile").fetchone()
        if int(profiles["count"]) != 1:
            raise RuntimeError("paper database must contain exactly one local profile")
        profile = conn.execute(
            "SELECT profile_id FROM local_profile WHERE singleton=1"
        ).fetchone()
        try:
            uuid.UUID(str(profile["profile_id"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("paper database profile_id is not a UUID") from exc
        missing_account_ids = conn.execute(
            "SELECT COUNT(*) AS count FROM accounts WHERE account_id IS NULL OR account_id=''"
        ).fetchone()
        if int(missing_account_ids["count"]):
            raise RuntimeError("paper database contains accounts without stable IDs")

    @staticmethod
    def _account_id(profile_id: str, market: str) -> str:
        # Keep the legacy namespace so existing local paper-account IDs remain stable.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pa-agent-quant:{profile_id}:{market}"))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize_accounts(self, config: PaperConfig) -> None:
        now = int(time.time() * 1000)
        profile_id = self.profile()["profile_id"]
        with self.connection() as conn:
            for market, rule in config.markets.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO accounts
                    (market, currency, starting_cash, cash, realized_pnl, updated_at_ms,
                     account_id)
                    VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        market,
                        rule.currency,
                        rule.starting_cash,
                        rule.starting_cash,
                        now,
                        self._account_id(profile_id, market),
                    ),
                )

    def profile(self) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT profile_id,display_name,created_at_ms FROM local_profile WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("local paper profile is not initialized")
        return dict(row)

    def schema_version(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        return int(row["version"] or 0)

    def health(self) -> dict[str, Any]:
        with self.connection() as conn:
            integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        integrity = [str(row[0]) for row in integrity_rows]
        return {
            "ok": integrity == ["ok"] and not foreign_key_rows,
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_key_rows),
            "journal_mode": journal_mode,
            "schema_version": self.schema_version(),
            "supported_schema_version": CURRENT_SCHEMA_VERSION,
        }

    def add_signal(self, signal: SignalIntent, *, status: str, reason: str = "") -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO signals
                (signal_id, created_at_ms, market, symbol, timeframe, base_bar_ts_ms,
                 side, status, reason, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id,
                    signal.created_at_ms,
                    signal.market,
                    signal.symbol,
                    signal.timeframe,
                    signal.base_bar_ts_ms,
                    signal.side,
                    status,
                    reason or signal.reason,
                    signal.model_dump_json(),
                ),
            )
            return cursor.rowcount == 1

    def update_signal(self, signal_id: str, status: str, reason: str = "") -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE signals SET status=?, reason=? WHERE signal_id=?",
                (status, reason, signal_id),
            )

    def signal(self, signal_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return dict(row) if row else None

    def order_for_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return dict(row) if row else None

    def queue_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one order and mark its signal queued in the same transaction."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO orders
                (order_id, signal_id, market, symbol, side, order_type, quantity,
                 requested_price, eligible_after_ts_ms, status, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    payload["order_id"],
                    payload["signal_id"],
                    payload["market"],
                    payload["symbol"],
                    payload["side"],
                    payload["order_type"],
                    payload["quantity"],
                    payload.get("requested_price"),
                    payload["eligible_after_ts_ms"],
                    payload["created_at_ms"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM orders WHERE signal_id=?", (payload["signal_id"],)
            ).fetchone()
            if row is None:
                raise RuntimeError("paper order could not be queued")
            if row["status"] == "pending":
                conn.execute(
                    "UPDATE signals SET status='queued',reason=? WHERE signal_id=?",
                    ("waiting for next closed bar", payload["signal_id"]),
                )
            return dict(row)

    def cancel_older_pending_entries(
        self,
        *,
        market: str,
        symbol: str,
        before_base_bar_ts_ms: int,
        reason: str,
    ) -> list[str]:
        """Cancel stale unfilled buy theses when a newer closed-bar view exists."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT orders.order_id,orders.signal_id
                FROM orders
                JOIN signals ON signals.signal_id=orders.signal_id
                WHERE orders.status='pending'
                  AND orders.side='buy'
                  AND orders.market=?
                  AND orders.symbol=?
                  AND signals.base_bar_ts_ms<?
                """,
                (market, symbol, before_base_bar_ts_ms),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE orders SET status='cancelled',reject_reason=? "
                    "WHERE order_id=? AND status='pending'",
                    (reason, row["order_id"]),
                )
                conn.execute(
                    "UPDATE signals SET status='cancelled',reason=? WHERE signal_id=?",
                    (reason, row["signal_id"]),
                )
        return [str(row["order_id"]) for row in rows]

    def record_order_bar(
        self,
        order_id: str,
        ts_ms: int,
        *,
        count_wait: bool,
    ) -> int | None:
        """Mark one bar evaluated once and return cumulative wait bars."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,wait_bars,last_evaluated_ts_ms FROM orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            if int(ts_ms) <= int(row["last_evaluated_ts_ms"]):
                return int(row["wait_bars"])
            wait_bars = int(row["wait_bars"]) + (1 if count_wait else 0)
            conn.execute(
                "UPDATE orders SET wait_bars=?,last_evaluated_ts_ms=? WHERE order_id=?",
                (wait_bars, int(ts_ms), order_id),
            )
            return wait_bars

    def account(self, market: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE market=?", (market,)).fetchone()
        if row is None:
            raise ValueError(f"paper account is not initialized for {market}")
        return dict(row)

    def position(self, market: str, symbol: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE market=? AND symbol=?",
                (market, symbol),
            ).fetchone()
        return dict(row) if row else None

    def pending_orders(self, market: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM orders WHERE status='pending'"
        args: list[Any] = []
        if market:
            sql += " AND market=?"
            args.append(market)
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        sql += " ORDER BY created_at_ms"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def open_positions(self, market: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM positions WHERE quantity>0"
        args: list[Any] = []
        if market:
            sql += " AND market=?"
            args.append(market)
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def status(self, *, signal_limit: int = 30) -> dict[str, Any]:
        with self.connection() as conn:
            profile = dict(
                conn.execute(
                    "SELECT profile_id,display_name,created_at_ms FROM local_profile "
                    "WHERE singleton=1"
                ).fetchone()
            )
            schema_version = int(
                conn.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()["version"]
            )
            accounts = [dict(row) for row in conn.execute("SELECT * FROM accounts ORDER BY market")]
            positions = [dict(row) for row in conn.execute("SELECT * FROM positions ORDER BY market,symbol")]
            orders = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM orders ORDER BY created_at_ms DESC LIMIT ?", (signal_limit,)
                )
            ]
            signals = [
                dict(row)
                for row in conn.execute(
                    "SELECT signal_id,created_at_ms,market,symbol,timeframe,side,status,reason "
                    "FROM signals ORDER BY created_at_ms DESC LIMIT ?",
                    (signal_limit,),
                )
            ]
            fills = [dict(row) for row in conn.execute("SELECT * FROM fills ORDER BY fill_id")]
            signal_payloads: dict[str, dict[str, Any]] = {}
            for row in conn.execute("SELECT signal_id,payload_json FROM signals").fetchall():
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                signal_payloads[str(row["signal_id"])] = (
                    payload if isinstance(payload, dict) else {}
                )
        for account in accounts:
            market_positions = [p for p in positions if p["market"] == account["market"]]
            market_value = sum(float(p["quantity"]) * float(p["last_price"]) for p in market_positions)
            unrealized = sum(
                (float(p["last_price"]) - float(p["avg_price"])) * float(p["quantity"])
                - float(p.get("entry_fee_basis") or 0)
                for p in market_positions
            )
            account["market_value"] = market_value
            account["equity"] = float(account["cash"]) + market_value
            account["unrealized_pnl_before_exit_fees"] = unrealized
            account["return_percent"] = (
                account["equity"] / float(account["starting_cash"]) - 1.0
            ) * 100
        exits = [fill for fill in fills if fill["side"] == "sell"]
        outcomes: list[dict[str, Any]] = []
        for fill in exits:
            payload = signal_payloads.get(fill.get("source_signal_id")) or {}
            provenance = payload.get("provenance") if isinstance(payload, dict) else {}
            if not isinstance(provenance, dict):
                provenance = {}
            raw_signal = payload.get("raw_signal") if isinstance(payload, dict) else {}
            if not isinstance(raw_signal, dict):
                raw_signal = {}
            outcomes.append(
                {
                    "fill_id": fill["fill_id"],
                    "market": fill["market"],
                    "symbol": fill["symbol"],
                    "source_signal_id": fill.get("source_signal_id"),
                    "exit_reason": fill["reason"],
                    "realized_pnl": float(fill["realized_pnl"]),
                    "estimated_win_rate": payload.get("estimated_win_rate"),
                    "confidence": payload.get("confidence"),
                    "model": provenance.get("model"),
                    "decision_stance": provenance.get("decision_stance"),
                    "strategy": (
                        ",".join(provenance.get("strategy_files_used") or []) or None
                    ),
                    "invalidation": raw_signal.get("invalidation_condition"),
                }
            )
        summary = _trade_summary(outcomes)
        sample_reviewable = len(exits) >= 30
        return {
            "paper_only": True,
            "live_execution": False,
            "database": str(self.path),
            "schema_version": schema_version,
            "profile": profile,
            "accounts": accounts,
            "positions": positions,
            "orders": orders,
            "signals": signals,
            "fills": fills[-signal_limit:],
            "validation": {
                "closed_trades": summary["trades"],
                "wins": summary["wins"],
                "losses": summary["losses"],
                "win_rate": summary["win_rate"],
                "realized_pnl": summary["realized_pnl"],
                "gross_profit": summary["gross_profit"],
                "gross_loss": summary["gross_loss"],
                "profit_factor": summary["profit_factor"],
                "expectancy": summary["expectancy"],
                "max_realized_drawdown": summary["max_realized_drawdown"],
                "sample_reviewable": sample_reviewable,
                "minimum_closed_trades_for_review": 30,
                "evidence_status": (
                    "reviewable_not_live_authorized"
                    if sample_reviewable
                    else "insufficient_closed_trade_sample"
                ),
                "promotion_ready": False,
                "live_promotion_allowed": False,
                "probability_calibration": _probability_calibration(outcomes),
                "outcomes_by_exit_reason": _group_outcomes(outcomes, "exit_reason"),
                "outcomes_by_model": _group_outcomes(outcomes, "model"),
                "recent_outcomes": outcomes[-signal_limit:],
            },
        }

    @staticmethod
    def signal_payload(row: dict[str, Any]) -> SignalIntent:
        return SignalIntent.model_validate(json.loads(row["payload_json"]))
