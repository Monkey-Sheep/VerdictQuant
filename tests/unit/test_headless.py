from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.data.base import KlineBar
from pa_agent.headless import AnalysisRequest, HeadlessAnalysisError, run_analysis
from pa_agent.records.schema import AnalysisRecord, RecordMeta
from pa_agent.util.threading import OrchestratorEvent


class FakeSource:
    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self.exchange = ""
        self.fallback_enabled = False
        self.subscription: tuple[str, str] | None = None
        self.requested_bars = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def set_exchange(self, exchange: str) -> None:
        self.exchange = exchange

    def enable_baostock_fallback(self) -> None:
        self.fallback_enabled = True

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.subscription = (symbol, timeframe)

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        self.requested_bars = n
        start_ms = 1_800_000_000_000
        return [
            KlineBar(
                seq=i + 1,
                ts_open=start_ms - i * 86_400_000,
                open=100.0 - i,
                high=102.0 - i,
                low=99.0 - i,
                close=101.0 - i,
                volume=1_000.0 + i,
                closed=True,
            )
            for i in range(n)
        ]


def _record(symbol: str = "COIN", timeframe: str = "1d") -> AnalysisRecord:
    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2027-01-15T10:00:00+08:00",
            timestamp_local_ms=1_800_000_000_000,
            symbol=symbol,
            timeframe=timeframe,
            bar_count=20,
            ai_provider={"model": "test"},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response={},
        stage1_diagnosis={"trend": "up"},
        stage2_messages=[],
        stage2_response={},
        stage2_decision={"decision": {"order_type": "no_order"}},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={"total_tokens": 10},
    )


def test_request_validation_normalizes_us_symbol_and_exchange() -> None:
    req = AnalysisRequest(
        market="US",
        symbol="coin",
        timeframe="1d",
        exchange="nasdaq",
        bar_count=20,
    ).validated()

    assert req.market == "us"
    assert req.symbol == "COIN"
    assert req.exchange == "NASDAQ"


@pytest.mark.parametrize("symbol", ["../COIN", "COIN/../../x", "COIN:NASDAQ"])
def test_request_validation_rejects_unsafe_symbols(symbol: str) -> None:
    with pytest.raises(HeadlessAnalysisError, match="invalid US symbol"):
        AnalysisRequest(market="us", symbol=symbol, bar_count=20).validated()


def test_request_validation_accepts_crypto_five_minute_market() -> None:
    req = AnalysisRequest(
        market="crypto",
        symbol="btcusdt",
        timeframe="5m",
        exchange="binance",
        bar_count=20,
        predict_next_bar=True,
    ).validated()

    assert req.symbol == "BTCUSDT"
    assert req.exchange == "BINANCE"
    assert req.predict_next_bar


def test_request_validation_rejects_a_share_exchange() -> None:
    with pytest.raises(HeadlessAnalysisError, match="exchange is only valid"):
        AnalysisRequest(
            market="a-share",
            symbol="600519",
            exchange="SSE",
            bar_count=20,
        ).validated()


def test_fetch_only_uses_public_source_without_context_or_ai() -> None:
    source = FakeSource()
    context_called = False

    def context_factory():
        nonlocal context_called
        context_called = True
        raise AssertionError("fetch-only must not initialize the AI context")

    result = run_analysis(
        AnalysisRequest(
            market="us",
            symbol="coin",
            exchange="nasdaq",
            timeframe="1d",
            bar_count=20,
            fetch_only=True,
        ),
        source_factory=lambda kind: source,
        context_factory=context_factory,
    )

    assert result["status"] == "fetched"
    assert result["symbol"] == "COIN"
    assert result["latest_close"] == 101.0
    assert source.connected
    assert source.disconnected
    assert source.exchange == "NASDAQ"
    assert source.subscription == ("COIN", "1d")
    assert source.requested_bars == 75
    assert not context_called


def test_analysis_returns_machine_readable_record_summary() -> None:
    source = FakeSource()
    context = SimpleNamespace(
        settings=SimpleNamespace(
            provider=SimpleNamespace(api_key="stored-secret"),
            general=SimpleNamespace(enable_next_bar_prediction=False),
        )
    )

    class FakeOrchestrator:
        def submit(self, *, frame, cancel_token, on_event):
            assert frame.symbol == "COIN"
            assert not cancel_token.is_set()
            on_event(OrchestratorEvent.Stage1Started)
            on_event(OrchestratorEvent.RecordSaved)
            return _record()

    result = run_analysis(
        AnalysisRequest(
            market="us",
            symbol="COIN",
            exchange="NASDAQ",
            timeframe="1d",
            bar_count=20,
        ),
        source_factory=lambda kind: source,
        context_factory=lambda: context,
        orchestrator_factory=lambda ctx: FakeOrchestrator(),
    )

    assert result["status"] == "ok"
    assert result["events"] == ["Stage1Started", "RecordSaved"]
    assert result["signal"] == {
        "actionable": False,
        "order_direction": None,
        "order_type": "no_order",
        "trade_confidence": None,
        "estimated_win_rate": None,
        "estimated_win_rate_reasoning": None,
        "entry_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "invalidation_condition": None,
        "next_bar_prediction": None,
        "next_cycle_prediction": None,
    }
    assert result["diagnosis"] == {"trend": "up"}
    assert result["decision"] == {"decision": {"order_type": "no_order"}}
    assert result["record_path"].endswith("_COIN_1d.json")
    assert source.disconnected


def test_analysis_requires_stored_api_key() -> None:
    source = FakeSource()
    context = SimpleNamespace(settings=SimpleNamespace(provider=SimpleNamespace(api_key="")))

    with pytest.raises(HeadlessAnalysisError, match="no API key configured"):
        run_analysis(
            AnalysisRequest(market="a-share", symbol="600519", bar_count=20),
            source_factory=lambda kind: source,
            context_factory=lambda: context,
        )

    assert source.disconnected


def test_a_share_headless_enables_baostock_fallback() -> None:
    source = FakeSource()

    result = run_analysis(
        AnalysisRequest(
            market="a-share",
            symbol="600519",
            timeframe="1d",
            bar_count=20,
            fetch_only=True,
        ),
        source_factory=lambda kind: source,
    )

    assert result["status"] == "fetched"
    assert result["data_source"] == "akshare"
    assert source.fallback_enabled


def test_crypto_analysis_can_enable_next_bar_prediction() -> None:
    source = FakeSource()
    context = SimpleNamespace(
        settings=SimpleNamespace(
            provider=SimpleNamespace(api_key="stored-secret"),
            general=SimpleNamespace(enable_next_bar_prediction=False),
        )
    )

    class FakeOrchestrator:
        def submit(self, *, frame, cancel_token, on_event):
            assert context.settings.general.enable_next_bar_prediction
            return _record(symbol="BTCUSDT", timeframe="5m")

    result = run_analysis(
        AnalysisRequest(
            market="crypto",
            symbol="BTCUSDT",
            exchange="BINANCE",
            timeframe="5m",
            bar_count=20,
            predict_next_bar=True,
        ),
        source_factory=lambda kind: source,
        context_factory=lambda: context,
        orchestrator_factory=lambda ctx: FakeOrchestrator(),
    )

    assert result["status"] == "ok"
    assert result["market"] == "crypto"
