"""Deterministic next-bar paper execution with stock-market constraints."""
from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import KlineBar
from pa_agent.quant.models import PaperConfig, SignalIntent
from pa_agent.quant.store import QuantStore


class PaperRiskError(ValueError):
    """A signal cannot enter the paper account under configured risk rules."""


def _floor_lot(quantity: float, lot_size: int) -> float:
    return float(math.floor(quantity / lot_size) * lot_size)


def _bar_date(market: str, ts_ms: int) -> str:
    zone = ZoneInfo("Asia/Shanghai") if market == "a-share" else ZoneInfo("America/New_York")
    return datetime.fromtimestamp(ts_ms / 1000, tz=zone).date().isoformat()


def _valid_trading_bar(bar: KlineBar) -> bool:
    values = (bar.ts_open, bar.open, bar.high, bar.low, bar.close, bar.volume)
    if not all(math.isfinite(float(value)) for value in values):
        return False
    if int(bar.ts_open) <= 0 or min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return False
    if bar.volume <= 0:
        return False
    return bool(
        bar.low <= bar.open <= bar.high
        and bar.low <= bar.close <= bar.high
        and bar.high >= bar.low
    )


class PaperEngine:
    """Persistent paper engine; never imports or connects to a broker gateway."""

    execution_reference = "vnpy/vnpy_paperaccount next-tick crossing semantics"

    def __init__(self, store: QuantStore, config: PaperConfig) -> None:
        if not config.paper_only or config.live_execution:
            raise ValueError("paper engine requires paper_only=true and live_execution=false")
        self.store = store
        self.config = config
        self.store.initialize_accounts(config)

    def _account_equity(self, market: str) -> float:
        account = self.store.account(market)
        market_value = sum(
            float(position["quantity"]) * float(position["last_price"])
            for position in self.store.open_positions(market)
        )
        return float(account["cash"]) + market_value

    def _round_price(self, market: str, side: str, price: float) -> float:
        tick = self.config.markets[market].price_tick
        units = price / tick
        rounded = math.ceil(units - 1e-12) if side == "buy" else math.floor(units + 1e-12)
        return max(tick, rounded * tick)

    def _requested_price(self, signal: SignalIntent, reference: float) -> float | None:
        if signal.order_type == "market":
            return None
        tick = self.config.markets[signal.market].price_tick
        units = reference / tick
        if signal.order_type == "limit":
            rounded = math.floor(units + 1e-12) if signal.side == "buy" else math.ceil(
                units - 1e-12
            )
        else:
            rounded = math.ceil(units - 1e-12) if signal.side == "buy" else math.floor(
                units + 1e-12
            )
        return max(tick, rounded * tick)

    def queue_signal(self, signal: SignalIntent) -> dict[str, Any]:
        """Risk-size a PA signal and queue it for a strictly later bar."""
        existing_order = self.store.order_for_signal(signal.signal_id)
        if existing_order is not None:
            return existing_order
        if signal.side == "hold":
            raise PaperRiskError("hold signal does not create an order")
        rule = self.config.markets[signal.market]
        account = self.store.account(signal.market)
        position = self.store.position(signal.market, signal.symbol)
        reference = signal.entry_price
        if reference is None:
            raise PaperRiskError("entry/reference price is required")

        if signal.side == "sell":
            if position is None or float(position["quantity"]) <= 0:
                if not self.config.allow_short:
                    raise PaperRiskError("stock paper accounts are long-only; no position to sell")
                raise PaperRiskError("short-selling backend is not enabled")
            quantity = float(position["quantity"])
        else:
            if (
                signal.confidence is None
                or signal.confidence < self.config.minimum_confidence
            ):
                raise PaperRiskError(
                    f"confidence {signal.confidence!r} is below "
                    f"{self.config.minimum_confidence:g}"
                )
            if position is not None and float(position["quantity"]) > 0:
                raise PaperRiskError(
                    "one auditable paper position per symbol; add-on entries are disabled"
                )
            pending_buys = [
                order
                for order in self.store.pending_orders(signal.market, signal.symbol)
                if order["side"] == "buy"
            ]
            if pending_buys:
                raise PaperRiskError("a pending buy already exists for this symbol")
            if signal.stop_loss is None or signal.stop_loss >= reference:
                raise PaperRiskError("buy signal requires a stop loss below entry")
            equity = self._account_equity(signal.market)
            risk_budget = equity * self.config.risk_per_trade
            risk_per_share = reference - signal.stop_loss
            risk_quantity = risk_budget / risk_per_share
            order_cap = equity * self.config.max_order_fraction
            symbol_cap = equity * self.config.max_symbol_fraction
            notional_cap = max(0.0, min(order_cap, symbol_cap))
            cash_cap = max(0.0, float(account["cash"]) - rule.minimum_commission)
            quantity = _floor_lot(
                min(risk_quantity, notional_cap / reference, cash_cap / reference),
                rule.lot_size,
            )
            if quantity < rule.lot_size:
                raise PaperRiskError("risk-sized quantity is below the market lot size")

        order_id = "ord_" + hashlib.sha256(signal.signal_id.encode()).hexdigest()[:20]
        payload = {
            "order_id": order_id,
            "signal_id": signal.signal_id,
            "market": signal.market,
            "symbol": signal.symbol,
            "side": signal.side,
            "order_type": signal.order_type,
            "quantity": quantity,
            "requested_price": self._requested_price(signal, reference),
            "eligible_after_ts_ms": signal.base_bar_ts_ms,
            "created_at_ms": int(time.time() * 1000),
        }
        return self.store.queue_order(payload)

    def process_bars(
        self,
        market: str,
        symbol: str,
        bars: Iterable[KlineBar],
    ) -> dict[str, Any]:
        """Settle pending orders and positions from closed bars, oldest first."""
        supplied = [bar for bar in bars if bar.closed]
        closed = sorted(
            (bar for bar in supplied if _valid_trading_bar(bar)),
            key=lambda bar: int(bar.ts_open),
        )
        events: list[dict[str, Any]] = []
        for order in self.store.pending_orders(market, symbol):
            last_evaluated = max(
                int(order["eligible_after_ts_ms"]),
                int(order.get("last_evaluated_ts_ms") or 0),
            )
            candidates = [
                bar for bar in closed if int(bar.ts_open) > last_evaluated
            ]
            terminal = False
            for bar in candidates:
                if self._sell_blocked_by_t_plus_one(order, bar):
                    wait_bars = self.store.record_order_bar(
                        order["order_id"], int(bar.ts_open), count_wait=False
                    )
                    if wait_bars is None:
                        terminal = True
                        break
                    events.append(
                        {
                            "type": "t_plus_one_wait",
                            "order_id": order["order_id"],
                            "bar_ts_ms": int(bar.ts_open),
                        }
                    )
                    continue
                price = self._cross_price(order, bar)
                if price is None:
                    wait_bars = self.store.record_order_bar(
                        order["order_id"], int(bar.ts_open), count_wait=True
                    )
                    if wait_bars is None:
                        terminal = True
                        break
                    if wait_bars >= self.config.max_wait_bars:
                        self._expire_order(
                            order, "entry was not reached within max_wait_bars"
                        )
                        events.append({"type": "expired", "order_id": order["order_id"]})
                        terminal = True
                        break
                    continue
                event = self._fill_order(order, bar, price)
                events.append(event)
                terminal = event["type"] in {"filled", "rejected", "skipped"}
                break
            if terminal:
                continue

        position = self.store.position(market, symbol)
        if position is not None:
            for bar in closed:
                if int(bar.ts_open) <= int(position["last_processed_ts_ms"]):
                    continue
                event = self._process_position_bar(position, bar)
                if event:
                    events.append(event)
                    position = self.store.position(market, symbol)
                    if position is None:
                        break
                else:
                    position = self.store.position(market, symbol) or position
        return {
            "market": market,
            "symbol": symbol,
            "events": events,
            "accepted_closed_bars": len(closed),
            "discarded_closed_bars": len(supplied) - len(closed),
        }

    def _sell_blocked_by_t_plus_one(
        self, order: dict[str, Any], bar: KlineBar
    ) -> bool:
        if order["side"] != "sell":
            return False
        if not self.config.markets[order["market"]].t_plus_one:
            return False
        position = self.store.position(order["market"], order["symbol"])
        if position is None:
            return False
        return _bar_date(order["market"], int(bar.ts_open)) <= str(
            position["available_after_date"]
        )

    def _cross_price(self, order: dict[str, Any], bar: KlineBar) -> float | None:
        rule = self.config.markets[order["market"]]
        bps = rule.slippage_bps / 10_000
        side = order["side"]
        requested = float(order["requested_price"] or 0)
        if order["order_type"] == "market":
            base = float(bar.open)
            raw = base * (1 + bps if side == "buy" else 1 - bps)
            return self._round_price(order["market"], side, raw)
        if order["order_type"] == "limit":
            touched = float(bar.low) <= requested if side == "buy" else float(bar.high) >= requested
            if not touched:
                return None
            better = min(float(bar.open), requested) if side == "buy" else max(float(bar.open), requested)
            slipped = better * (1 + bps if side == "buy" else 1 - bps)
            raw = min(slipped, requested) if side == "buy" else max(slipped, requested)
            return self._round_price(order["market"], side, raw)
        touched = float(bar.high) >= requested if side == "buy" else float(bar.low) <= requested
        if not touched:
            return None
        base = max(float(bar.open), requested) if side == "buy" else min(float(bar.open), requested)
        raw = base * (1 + bps if side == "buy" else 1 - bps)
        return self._round_price(order["market"], side, raw)

    def _fee(self, market: str, side: str, notional: float) -> float:
        rule = self.config.markets[market]
        commission = max(rule.minimum_commission, notional * rule.commission_rate)
        tax = notional * rule.sell_tax_rate if side == "sell" else 0.0
        return round(commission + tax, rule.fee_precision)

    def _fill_order(
        self,
        order: dict[str, Any],
        bar: KlineBar,
        price: float,
    ) -> dict[str, Any]:
        ts_ms = int(bar.ts_open)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM orders WHERE order_id=?", (order["order_id"],)
            ).fetchone()
            if current_row is None or current_row["status"] != "pending":
                return {"type": "skipped", "order_id": order["order_id"]}
            current = dict(current_row)
            if ts_ms <= max(
                int(current["eligible_after_ts_ms"]),
                int(current.get("last_evaluated_ts_ms") or 0),
            ):
                return {"type": "skipped", "order_id": order["order_id"]}
            signal = self._signal_for_order_locked(conn, current)
            account = conn.execute(
                "SELECT * FROM accounts WHERE market=?", (current["market"],)
            ).fetchone()
            if account is None:
                raise RuntimeError("paper account missing")
            requested_quantity = float(current["quantity"])
            quantity = requested_quantity
            source_signal_id = signal.signal_id
            if current["side"] == "buy":
                existing = conn.execute(
                    "SELECT * FROM positions WHERE market=? AND symbol=?",
                    (current["market"], current["symbol"]),
                ).fetchone()
                if existing:
                    return self._reject_locked(
                        conn,
                        current,
                        "position already exists at fill; add-on entry blocked",
                        ts_ms,
                    )
                quantity = self._buy_quantity_at_fill(
                    conn=conn,
                    order=current,
                    signal=signal,
                    price=price,
                    requested_quantity=requested_quantity,
                )
                rule = self.config.markets[current["market"]]
                if quantity < rule.lot_size:
                    return self._reject_locked(
                        conn,
                        current,
                        "gap-adjusted risk quantity is below the market lot size",
                        ts_ms,
                    )
                notional = price * quantity
                fee = self._fee(current["market"], "buy", notional)
                total = notional + fee
                if float(account["cash"]) + 1e-9 < total:
                    return self._reject_locked(
                        conn, current, "insufficient paper cash at fill", ts_ms
                    )
                available = _bar_date(current["market"], ts_ms)
                conn.execute(
                    """
                    INSERT INTO positions
                    (market,symbol,quantity,avg_price,opened_at_ts_ms,available_after_date,
                     entry_fee_basis,stop_loss,take_profit,source_signal_id,holding_bars,
                     last_processed_ts_ms,last_price)
                    VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
                    """,
                    (
                        current["market"], current["symbol"], quantity, price, ts_ms,
                        available, fee, signal.stop_loss, signal.take_profit,
                        signal.signal_id, ts_ms, price,
                    ),
                )
                cash = float(account["cash"]) - total
                realized = 0.0
            else:
                position = conn.execute(
                    "SELECT * FROM positions WHERE market=? AND symbol=?",
                    (current["market"], current["symbol"]),
                ).fetchone()
                if position is None:
                    return self._reject_locked(
                        conn, current, "paper sell has no open position", ts_ms
                    )
                if self.config.markets[current["market"]].t_plus_one and _bar_date(
                    current["market"], ts_ms
                ) <= str(position["available_after_date"]):
                    conn.execute(
                        "UPDATE orders SET last_evaluated_ts_ms=? WHERE order_id=?",
                        (ts_ms, current["order_id"]),
                    )
                    return {"type": "t_plus_one_wait", "order_id": current["order_id"]}
                quantity = min(quantity, float(position["quantity"]))
                notional = price * quantity
                fee = self._fee(current["market"], "sell", notional)
                entry_fee = float(position["entry_fee_basis"]) * (
                    quantity / float(position["quantity"])
                )
                realized = (
                    (price - float(position["avg_price"])) * quantity - fee - entry_fee
                )
                cash = float(account["cash"]) + notional - fee
                remaining = float(position["quantity"]) - quantity
                source_signal_id = str(position["source_signal_id"])
                if remaining <= 1e-9:
                    conn.execute(
                        "DELETE FROM positions WHERE market=? AND symbol=?",
                        (current["market"], current["symbol"]),
                    )
                    conn.execute(
                        "UPDATE signals SET status='closed',reason=? WHERE signal_id=?",
                        (f"explicit_sell:{signal.signal_id}", source_signal_id),
                    )
                else:
                    conn.execute(
                        "UPDATE positions SET quantity=?,entry_fee_basis=?,last_price=?,"
                        "last_processed_ts_ms=? "
                        "WHERE market=? AND symbol=?",
                        (
                            remaining,
                            float(position["entry_fee_basis"]) - entry_fee,
                            price,
                            ts_ms,
                            current["market"],
                            current["symbol"],
                        ),
                    )
            conn.execute(
                "UPDATE accounts SET cash=?,realized_pnl=realized_pnl+?,updated_at_ms=? WHERE market=?",
                (cash, realized, int(time.time() * 1000), current["market"]),
            )
            conn.execute(
                "UPDATE orders SET status='filled',fill_ts_ms=?,fill_price=?,filled_quantity=?,"
                "fee=?,last_evaluated_ts_ms=? WHERE order_id=?",
                (ts_ms, price, quantity, fee, ts_ms, current["order_id"]),
            )
            conn.execute(
                "UPDATE signals SET status=?,reason=? WHERE signal_id=?",
                (
                    "open" if current["side"] == "buy" else "closed",
                    "position opened" if current["side"] == "buy" else "explicit exit filled",
                    current["signal_id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO fills(order_id,market,symbol,side,quantity,price,fee,realized_pnl,
                                  ts_ms,reason,source_signal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    current["order_id"], current["market"], current["symbol"],
                    current["side"], quantity, price, fee, realized, ts_ms,
                    "signal_entry" if current["side"] == "buy" else "signal_exit",
                    source_signal_id,
                ),
            )
        return {
            "type": "filled",
            "order_id": current["order_id"],
            "side": current["side"],
            "quantity": quantity,
            "requested_quantity": requested_quantity,
            "resized_at_fill": quantity < requested_quantity,
            "price": price,
            "fee": fee,
        }

    def _reject_locked(
        self,
        conn: Any,
        order: dict[str, Any],
        reason: str,
        ts_ms: int,
    ) -> dict[str, Any]:
        conn.execute(
            "UPDATE orders SET status='rejected',reject_reason=?,last_evaluated_ts_ms=? "
            "WHERE order_id=?",
            (reason, ts_ms, order["order_id"]),
        )
        conn.execute(
            "UPDATE signals SET status='rejected',reason=? WHERE signal_id=?",
            (reason, order["signal_id"]),
        )
        return {"type": "rejected", "order_id": order["order_id"], "reason": reason}

    def _buy_quantity_at_fill(
        self,
        *,
        conn: Any,
        order: dict[str, Any],
        signal: SignalIntent,
        price: float,
        requested_quantity: float,
    ) -> float:
        rule = self.config.markets[order["market"]]
        if signal.stop_loss is None or price <= signal.stop_loss:
            return 0.0
        positions = conn.execute(
            "SELECT quantity,last_price FROM positions WHERE market=?",
            (order["market"],),
        ).fetchall()
        account = conn.execute(
            "SELECT cash FROM accounts WHERE market=?", (order["market"],)
        ).fetchone()
        equity = float(account["cash"]) + sum(
            float(row["quantity"]) * float(row["last_price"]) for row in positions
        )
        risk_quantity = equity * self.config.risk_per_trade / (price - signal.stop_loss)
        notional_cap = min(
            equity * self.config.max_order_fraction,
            equity * self.config.max_symbol_fraction,
        )
        cash_quantity = max(0.0, float(account["cash"]) - rule.minimum_commission) / price
        quantity = _floor_lot(
            min(requested_quantity, risk_quantity, notional_cap / price, cash_quantity),
            rule.lot_size,
        )
        while quantity >= rule.lot_size:
            notional = quantity * price
            if notional + self._fee(order["market"], "buy", notional) <= float(
                account["cash"]
            ) + 1e-9:
                break
            quantity -= rule.lot_size
        return max(0.0, quantity)

    def _process_position_bar(self, position: dict[str, Any], bar: KlineBar) -> dict[str, Any] | None:
        holding = int(position["holding_bars"]) + 1
        market = position["market"]
        eligible = not (
            self.config.markets[market].t_plus_one
            and _bar_date(market, int(bar.ts_open)) <= str(position["available_after_date"])
        )
        stop = float(position["stop_loss"]) if position["stop_loss"] is not None else None
        target = float(position["take_profit"]) if position["take_profit"] is not None else None
        reason = None
        raw_price = None
        if eligible and stop is not None and float(bar.low) <= stop:
            reason = "stop_loss"
            raw_price = min(float(bar.open), stop)
        elif eligible and target is not None and float(bar.high) >= target:
            reason = "take_profit"
            raw_price = max(float(bar.open), target)
        elif eligible and holding >= self.config.max_holding_bars:
            reason = "max_holding_bars"
            raw_price = float(bar.close)

        if reason is not None and raw_price is not None:
            rule = self.config.markets[market]
            price = self._round_price(
                market, "sell", raw_price * (1 - rule.slippage_bps / 10_000)
            )
            return self._close_position(position, bar, price, reason)

        with self.store.connection() as conn:
            conn.execute(
                "UPDATE positions SET holding_bars=?,last_processed_ts_ms=?,last_price=? "
                "WHERE market=? AND symbol=?",
                (holding, int(bar.ts_open), float(bar.close), market, position["symbol"]),
            )
        return None

    def _close_position(
        self,
        position: dict[str, Any],
        bar: KlineBar,
        price: float,
        reason: str,
    ) -> dict[str, Any]:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM positions WHERE market=? AND symbol=?",
                (position["market"], position["symbol"]),
            ).fetchone()
            if current is None:
                return {
                    "type": "skipped",
                    "symbol": position["symbol"],
                    "reason": "position_already_closed",
                }
            quantity = float(current["quantity"])
            notional = price * quantity
            fee = self._fee(current["market"], "sell", notional)
            realized = (
                (price - float(current["avg_price"])) * quantity
                - fee
                - float(current["entry_fee_basis"])
            )
            order_id = f"exit_{current['source_signal_id']}_{int(bar.ts_open)}"
            account = conn.execute(
                "SELECT cash FROM accounts WHERE market=?", (current["market"],)
            ).fetchone()
            cash = float(account["cash"]) + notional - fee
            pending_exits = conn.execute(
                """
                SELECT order_id,signal_id
                FROM orders
                WHERE status='pending' AND side='sell' AND market=? AND symbol=?
                """,
                (current["market"], current["symbol"]),
            ).fetchall()
            cancellation_reason = f"position closed automatically: {reason}"
            for pending in pending_exits:
                conn.execute(
                    "UPDATE orders SET status='cancelled',reject_reason=? "
                    "WHERE order_id=? AND status='pending'",
                    (cancellation_reason, pending["order_id"]),
                )
                conn.execute(
                    "UPDATE signals SET status='cancelled',reason=? WHERE signal_id=?",
                    (cancellation_reason, pending["signal_id"]),
                )
            conn.execute(
                "UPDATE accounts SET cash=?,realized_pnl=realized_pnl+?,updated_at_ms=? WHERE market=?",
                (cash, realized, int(time.time() * 1000), current["market"]),
            )
            conn.execute(
                "DELETE FROM positions WHERE market=? AND symbol=?",
                (current["market"], current["symbol"]),
            )
            conn.execute(
                "UPDATE signals SET status='closed',reason=? WHERE signal_id=?",
                (reason, current["source_signal_id"]),
            )
            conn.execute(
                """
                INSERT INTO fills(order_id,market,symbol,side,quantity,price,fee,realized_pnl,
                                  ts_ms,reason,source_signal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order_id, current["market"], current["symbol"], "sell", quantity,
                    price, fee, realized, int(bar.ts_open), reason,
                    current["source_signal_id"],
                ),
            )
        return {
            "type": "closed",
            "symbol": current["symbol"],
            "reason": reason,
            "price": price,
            "realized_pnl": realized,
            "cancelled_pending_exit_orders": [
                str(pending["order_id"]) for pending in pending_exits
            ],
        }

    def _signal_for_order(self, order: dict[str, Any]) -> SignalIntent:
        with self.store.connection() as conn:
            return self._signal_for_order_locked(conn, order)

    @staticmethod
    def _signal_for_order_locked(conn: Any, order: dict[str, Any]) -> SignalIntent:
        row = conn.execute(
            "SELECT payload_json FROM signals WHERE signal_id=?", (order["signal_id"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("order signal is missing")
        return SignalIntent.model_validate_json(row["payload_json"])

    def _expire_order(self, order: dict[str, Any], reason: str) -> None:
        with self.store.connection() as conn:
            cursor = conn.execute(
                "UPDATE orders SET status='expired',reject_reason=? "
                "WHERE order_id=? AND status='pending'",
                (reason, order["order_id"]),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    "UPDATE signals SET status='expired',reason=? WHERE signal_id=?",
                    (reason, order["signal_id"]),
                )
