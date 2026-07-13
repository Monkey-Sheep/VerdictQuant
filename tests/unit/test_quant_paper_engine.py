from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pa_agent.data.base import KlineBar
from pa_agent.quant.models import PaperConfig, SignalIntent
from pa_agent.quant.paper_engine import PaperEngine, PaperRiskError
from pa_agent.quant.store import QuantStore


def _bar(ts: int, *, open_: float, high: float, low: float, close: float) -> KlineBar:
    return KlineBar(1, ts, open_, high, low, close, 1000, closed=True)


def _signal(
    *,
    market: str = "us",
    symbol: str = "TEST",
    base_ts: int = 1_000,
    entry: float = 100,
    stop: float = 90,
    target: float = 120,
) -> SignalIntent:
    return SignalIntent(
        signal_id=f"sig-{market}-{symbol}-{base_ts}",
        created_at_ms=base_ts,
        market=market,
        symbol=symbol,
        timeframe="1d",
        base_bar_ts_ms=base_ts,
        side="buy",
        order_type="market",
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        confidence=80,
    )


def _engine(tmp_path: Path) -> tuple[QuantStore, PaperEngine]:
    store = QuantStore(tmp_path / "paper.db")
    engine = PaperEngine(store, PaperConfig())
    return store, engine


def test_market_order_never_fills_on_analysis_bar(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal()
    store.add_signal(signal, status="observed")
    order = engine.queue_signal(signal)

    result = engine.process_bars("us", "TEST", [_bar(1_000, open_=100, high=110, low=90, close=105)])

    assert result["events"] == []
    assert store.pending_orders()[0]["order_id"] == order["order_id"]
    assert store.position("us", "TEST") is None


def test_risk_sized_entry_and_conservative_stop_first_exit(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal()
    store.add_signal(signal, status="observed")
    order = engine.queue_signal(signal)
    assert order["quantity"] == 50

    entry = engine.process_bars(
        "us",
        "TEST",
        [_bar(2_000, open_=101, high=103, low=99, close=102)],
    )
    assert entry["events"][0]["type"] == "filled"
    assert entry["events"][0]["resized_at_fill"] is True
    assert store.position("us", "TEST")["quantity"] == 45

    exit_result = engine.process_bars(
        "us",
        "TEST",
        [_bar(3_000, open_=102, high=130, low=85, close=125)],
    )
    assert exit_result["events"][0]["reason"] == "stop_loss"
    assert exit_result["events"][0]["price"] < 90
    assert store.position("us", "TEST") is None
    assert store.status()["validation"]["closed_trades"] == 1


def test_buy_requires_stop_and_minimum_confidence(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    missing_stop = _signal().model_copy(update={"signal_id": "missing-stop", "stop_loss": None})
    store.add_signal(missing_stop, status="observed")
    with pytest.raises(PaperRiskError, match="stop loss"):
        engine.queue_signal(missing_stop)

    low_confidence = _signal().model_copy(
        update={"signal_id": "low-confidence", "confidence": 59}
    )
    store.add_signal(low_confidence, status="observed")
    with pytest.raises(PaperRiskError, match="below"):
        engine.queue_signal(low_confidence)


def test_existing_position_exit_is_not_blocked_by_low_confidence(
    tmp_path: Path,
) -> None:
    store, engine = _engine(tmp_path)
    entry = _signal()
    store.add_signal(entry, status="observed")
    engine.queue_signal(entry)
    engine.process_bars(
        "us", "TEST", [_bar(2_000, open_=100, high=102, low=99, close=101)]
    )
    exit_signal = entry.model_copy(
        update={
            "signal_id": "low-confidence-risk-exit",
            "created_at_ms": 2_500,
            "base_bar_ts_ms": 2_500,
            "side": "sell",
            "confidence": 1,
        }
    )
    store.add_signal(exit_signal, status="observed")

    order = engine.queue_signal(exit_signal)

    assert order["side"] == "sell"
    assert order["quantity"] == store.position("us", "TEST")["quantity"]


def test_a_share_lot_size_and_t_plus_one(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    zone = ZoneInfo("Asia/Shanghai")
    base = int(datetime(2026, 7, 13, 9, 30, tzinfo=zone).timestamp() * 1000)
    fill_ts = int(datetime(2026, 7, 13, 10, 0, tzinfo=zone).timestamp() * 1000)
    same_day = int(datetime(2026, 7, 13, 14, 0, tzinfo=zone).timestamp() * 1000)
    next_day = int(datetime(2026, 7, 14, 10, 0, tzinfo=zone).timestamp() * 1000)
    signal = _signal(
        market="a-share",
        symbol="600000",
        base_ts=base,
        entry=10,
        stop=9,
        target=11,
    )
    store.add_signal(signal, status="observed")
    order = engine.queue_signal(signal)
    assert order["quantity"] % 100 == 0

    first = engine.process_bars(
        "a-share",
        "600000",
        [
            _bar(fill_ts, open_=10, high=10.5, low=9.8, close=10.2),
            _bar(same_day, open_=10.2, high=12, low=10, close=11.5),
        ],
    )
    assert first["events"][0]["type"] == "filled"
    assert store.position("a-share", "600000") is not None

    second = engine.process_bars(
        "a-share",
        "600000",
        [_bar(next_day, open_=11.2, high=11.5, low=10.8, close=11.3)],
    )
    assert second["events"][0]["reason"] == "take_profit"
    assert store.position("a-share", "600000") is None


def test_realized_pnl_includes_entry_and_exit_fees(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal()
    store.add_signal(signal, status="observed")
    engine.queue_signal(signal)

    entry = engine.process_bars(
        "us", "TEST", [_bar(2_000, open_=100, high=102, low=99, close=101)]
    )["events"][0]
    position = store.position("us", "TEST")
    assert position is not None
    assert position["entry_fee_basis"] == entry["fee"]

    closed = engine.process_bars(
        "us", "TEST", [_bar(3_000, open_=120, high=121, low=119, close=120)]
    )["events"][0]
    expected = (
        (closed["price"] - entry["price"]) * entry["quantity"]
        - entry["fee"]
        - engine._fee("us", "sell", closed["price"] * entry["quantity"])
    )
    assert closed["realized_pnl"] == pytest.approx(expected)
    assert store.account("us")["realized_pnl"] == pytest.approx(expected)


def test_unfilled_bar_is_counted_only_once_across_retries(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal().model_copy(
        update={"order_type": "limit", "entry_price": 95, "stop_loss": 90}
    )
    store.add_signal(signal, status="observed")
    engine.queue_signal(signal)
    first_bar = _bar(2_000, open_=100, high=101, low=96, close=100)

    engine.process_bars("us", "TEST", [first_bar])
    assert store.pending_orders()[0]["wait_bars"] == 1
    engine.process_bars("us", "TEST", [first_bar])
    assert store.pending_orders()[0]["wait_bars"] == 1

    engine.process_bars(
        "us", "TEST", [_bar(3_000, open_=100, high=101, low=97, close=100)]
    )
    assert store.pending_orders()[0]["wait_bars"] == 2


def test_concurrent_settlement_cannot_fill_the_same_order_twice(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal()
    store.add_signal(signal, status="observed")
    engine.queue_signal(signal)
    bar = _bar(2_000, open_=100, high=102, low=99, close=101)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: engine.process_bars("us", "TEST", [bar]), range(2)))

    event_types = [event["type"] for result in results for event in result["events"]]
    assert event_types.count("filled") == 1
    with store.connection() as conn:
        fills = conn.execute("SELECT COUNT(*) FROM fills WHERE side='buy'").fetchone()[0]
    assert fills == 1


def test_late_expiry_cannot_rewrite_an_already_filled_signal(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal()
    store.add_signal(signal, status="observed")
    order = engine.queue_signal(signal)
    engine.process_bars(
        "us", "TEST", [_bar(2_000, open_=100, high=102, low=99, close=101)]
    )
    signal_status = store.signal(signal.signal_id)["status"]

    engine._expire_order(order, "late expiry")

    assert store.order_for_signal(signal.signal_id)["status"] == "filled"
    assert store.signal(signal.signal_id)["status"] == signal_status == "open"


def test_automatic_exit_cancels_a_stale_pending_sell(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    entry = _signal()
    store.add_signal(entry, status="observed")
    engine.queue_signal(entry)
    engine.process_bars(
        "us", "TEST", [_bar(2_000, open_=100, high=102, low=99, close=101)]
    )
    exit_signal = entry.model_copy(
        update={
            "signal_id": "pending-explicit-exit",
            "created_at_ms": 2_500,
            "base_bar_ts_ms": 2_500,
            "side": "sell",
            "order_type": "limit",
            "entry_price": 150,
        }
    )
    store.add_signal(exit_signal, status="observed")
    exit_order = engine.queue_signal(exit_signal)

    result = engine.process_bars(
        "us", "TEST", [_bar(3_000, open_=100, high=110, low=80, close=90)]
    )

    closed = next(event for event in result["events"] if event["type"] == "closed")
    assert closed["reason"] == "stop_loss"
    assert closed["cancelled_pending_exit_orders"] == [exit_order["order_id"]]
    assert store.pending_orders() == []
    assert store.order_for_signal(exit_signal.signal_id)["status"] == "cancelled"
    assert store.signal(exit_signal.signal_id)["status"] == "cancelled"


def test_invalid_or_zero_volume_bar_cannot_fill(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    signal = _signal()
    store.add_signal(signal, status="observed")
    engine.queue_signal(signal)
    bad = KlineBar(1, 2_000, 100, 101, 99, 100, 0, closed=True)

    result = engine.process_bars("us", "TEST", [bad])

    assert result["discarded_closed_bars"] == 1
    assert result["events"] == []
    assert store.position("us", "TEST") is None


def test_a_share_sell_waits_without_consuming_entry_wait_budget(tmp_path: Path) -> None:
    store, engine = _engine(tmp_path)
    zone = ZoneInfo("Asia/Shanghai")
    base = int(datetime(2026, 7, 13, 9, 30, tzinfo=zone).timestamp() * 1000)
    fill_ts = int(datetime(2026, 7, 13, 10, 0, tzinfo=zone).timestamp() * 1000)
    signal = _signal(
        market="a-share", symbol="600000", base_ts=base, entry=10, stop=9, target=20
    )
    store.add_signal(signal, status="observed")
    engine.queue_signal(signal)
    engine.process_bars(
        "a-share", "600000", [_bar(fill_ts, open_=10, high=10.2, low=9.9, close=10.1)]
    )
    sell_ts = int(datetime(2026, 7, 13, 10, 30, tzinfo=zone).timestamp() * 1000)
    sell = signal.model_copy(
        update={
            "signal_id": "sell-600000",
            "created_at_ms": sell_ts,
            "base_bar_ts_ms": sell_ts,
            "side": "sell",
            "entry_price": 10.1,
            "stop_loss": None,
            "take_profit": None,
        }
    )
    store.add_signal(sell, status="observed")
    engine.queue_signal(sell)
    same_day_bars = [
        _bar(
            int(datetime(2026, 7, 13, hour, 0, tzinfo=zone).timestamp() * 1000),
            open_=10.1,
            high=10.2,
            low=10,
            close=10.1,
        )
        for hour in (11, 13, 14)
    ]

    engine.process_bars("a-share", "600000", same_day_bars)
    pending = store.pending_orders("a-share", "600000")[0]
    assert pending["wait_bars"] == 0

    next_day = int(datetime(2026, 7, 14, 10, 0, tzinfo=zone).timestamp() * 1000)
    result = engine.process_bars(
        "a-share", "600000", [_bar(next_day, open_=10.2, high=10.3, low=10.1, close=10.2)]
    )
    assert result["events"][0]["type"] == "filled"
    assert result["events"][0]["side"] == "sell"
