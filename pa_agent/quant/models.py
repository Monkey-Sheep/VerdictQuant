"""Canonical contracts shared by PA analysis and paper execution."""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal["us", "a-share"]
Side = Literal["buy", "sell", "hold"]
PaperOrderType = Literal["market", "limit", "stop"]


class SignalIntent(BaseModel):
    """Immutable normalized intent derived from one PA analysis record."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: str = "1.0"
    signal_id: str
    created_at_ms: int
    market: Market
    symbol: str
    exchange: str | None = None
    timeframe: str
    base_bar_ts_ms: int
    side: Side
    order_type: PaperOrderType = "market"
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=100)
    estimated_win_rate: float | None = Field(default=None, ge=0, le=100)
    reason: str = ""
    source: str = "pa-agent-two-stage"
    analysis_record_path: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    raw_signal: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at_ms", "base_bar_ts_ms")
    @classmethod
    def positive_timestamp(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timestamp must be positive")
        return value

    @field_validator("symbol", "timeframe")
    @classmethod
    def nonempty_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("symbol and timeframe must not be empty")
        return normalized

    @field_validator("entry_price", "stop_loss", "take_profit")
    @classmethod
    def positive_price(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("price must be positive")
        return value


class MarketPaperRule(BaseModel):
    """Deterministic execution rules for one stock market."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    currency: str
    starting_cash: float = Field(gt=0)
    lot_size: int = Field(ge=1)
    slippage_bps: float = Field(default=5.0, ge=0, le=100)
    commission_rate: float = Field(default=0.0005, ge=0, le=0.05)
    minimum_commission: float = Field(default=1.0, ge=0)
    sell_tax_rate: float = Field(default=0.0, ge=0, le=0.05)
    price_tick: float = Field(default=0.01, gt=0, le=1)
    fee_precision: int = Field(default=2, ge=0, le=6)
    t_plus_one: bool = False


class WatchItem(BaseModel):
    """One recurring stock-analysis target."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    market: Market
    symbol: str
    exchange: str = ""
    timeframe: str = "1d"
    bar_count: int = Field(default=100, ge=20, le=5000)
    predict_next_bar: bool = True


class PaperConfig(BaseModel):
    """Portable paper-account and risk defaults."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: str = "1.0"
    paper_only: bool = True
    live_execution: bool = False
    minimum_confidence: float = Field(default=60.0, ge=0, le=100)
    risk_per_trade: float = Field(default=0.005, gt=0, le=0.05)
    max_order_fraction: float = Field(default=0.10, gt=0, le=0.50)
    max_symbol_fraction: float = Field(default=0.20, gt=0, le=1.0)
    max_wait_bars: int = Field(default=3, ge=1, le=20)
    max_holding_bars: int = Field(default=20, ge=1, le=500)
    allow_short: bool = False
    watchlist: list[WatchItem] = Field(default_factory=list)
    markets: dict[Market, MarketPaperRule] = Field(
        default_factory=lambda: {
            "us": MarketPaperRule(
                currency="USD",
                starting_cash=100_000,
                lot_size=1,
                slippage_bps=5,
                commission_rate=0.0005,
                minimum_commission=1,
            ),
            "a-share": MarketPaperRule(
                currency="CNY",
                starting_cash=1_000_000,
                lot_size=100,
                slippage_bps=10,
                commission_rate=0.0003,
                minimum_commission=5,
                sell_tax_rate=0.0005,
                t_plus_one=True,
            ),
        }
    )
