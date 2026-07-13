from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.forecast_ledger import (
    evaluate_forecast,
    find_next_closed_bar,
    record_next_bar_forecast,
    summarize_evaluated,
)


def _frame() -> KlineFrame:
    bars = (
        KlineBar(1, 2_000, 100.0, 102.0, 99.0, 101.0, 1_000.0, closed=True),
        KlineBar(2, 1_000, 99.0, 101.0, 98.0, 100.0, 900.0, closed=True),
    )
    return KlineFrame(
        symbol="BTCUSDT",
        timeframe="5m",
        bars=bars,
        indicators=IndicatorBundle(ema20=(100.0, 99.0), atr14=(2.0, 2.0)),
        snapshot_ts_local_ms=2_500,
    )


def _record(direction: str = "bullish") -> SimpleNamespace:
    return SimpleNamespace(
        meta=SimpleNamespace(
            timestamp_local_ms=2_500,
            ai_provider={"model": "test-model"},
        ),
        stage2_decision={
            "next_bar_prediction": {
                "direction": direction,
                "probabilities": {
                    "bullish": 60,
                    "bearish": 30,
                    "neutral": 10,
                },
                "reasoning": "test",
                "features_used": ["price_action"],
                "unpredictable": False,
            }
        },
    )


def test_record_is_immutable_for_same_base_bar(tmp_path) -> None:
    frame = _frame()
    first_path = record_next_bar_forecast(
        record=_record("bullish"),
        frame=frame,
        market="crypto",
        data_source="tradingview",
        exchange="BINANCE",
        pending_dir=tmp_path,
    )
    assert first_path is not None
    first_payload = json.loads(first_path.read_text(encoding="utf-8"))

    second_path = record_next_bar_forecast(
        record=_record("bearish"),
        frame=frame,
        market="crypto",
        data_source="tradingview",
        exchange="BINANCE",
        pending_dir=tmp_path,
    )
    second_payload = json.loads(second_path.read_text(encoding="utf-8"))

    assert second_path == first_path
    assert second_payload == first_payload
    assert first_payload["prediction"]["direction"] == "bullish"
    assert first_payload["neutral_threshold_abs"] == pytest.approx(0.1)


def test_evaluate_forecast_uses_proper_scores(tmp_path) -> None:
    path = record_next_bar_forecast(
        record=_record(),
        frame=_frame(),
        market="crypto",
        data_source="tradingview",
        exchange="BINANCE",
        pending_dir=tmp_path,
    )
    assert path is not None
    forecast = json.loads(path.read_text(encoding="utf-8"))
    next_bar = KlineBar(
        1,
        3_000,
        100.0,
        102.0,
        99.0,
        101.0,
        1_200.0,
        closed=True,
    )

    evaluated = evaluate_forecast(forecast, next_bar, evaluated_at_ms=3_500)
    result = evaluated["evaluation"]

    assert evaluated["status"] == "evaluated"
    assert result["actual_direction"] == "bullish"
    assert result["correct"] is True
    assert result["brier_score"] == pytest.approx(0.26)
    assert result["log_loss"] == pytest.approx(-math.log(0.6))
    assert result["conditional_up_probability_ex_neutral"] == pytest.approx(2 / 3)
    assert result["binary_brier_score"] == pytest.approx((2 / 3 - 1) ** 2)


def test_find_next_bar_and_summary() -> None:
    bars = [
        KlineBar(1, 4_000, 100.0, 101.0, 98.0, 99.0, 1.0, closed=False),
        KlineBar(2, 3_000, 100.0, 102.0, 99.0, 101.0, 1.0, closed=True),
        KlineBar(3, 1_000, 99.0, 100.0, 98.0, 99.5, 1.0, closed=True),
    ]
    next_bar = find_next_closed_bar(bars, 2_000)
    assert next_bar is not None
    assert next_bar.ts_open == 3_000

    summary = summarize_evaluated(
        [
            {
                "model": "model-a",
                "prediction": {
                    "direction": "bullish",
                    "probabilities": {"bullish": 60, "bearish": 30, "neutral": 10},
                },
                "evaluation": {
                    "correct": True,
                    "brier_score": 0.2,
                    "log_loss": 0.3,
                    "binary_brier_score": 0.1,
                }
            },
            {
                "model": "model-a",
                "prediction": {
                    "direction": "bullish",
                    "probabilities": {"bullish": 60, "bearish": 30, "neutral": 10},
                },
                "evaluation": {
                    "correct": False,
                    "brier_score": 0.4,
                    "log_loss": 0.7,
                    "binary_brier_score": 0.3,
                }
            },
            {"status": "pending", "evaluation": None},
        ]
    )
    assert summary["evaluated_count"] == 2
    assert summary["accuracy"] == 0.5
    assert summary["mean_brier_score"] == pytest.approx(0.3)
    assert summary["mean_log_loss"] == pytest.approx(0.5)
    assert summary["mean_binary_brier_score"] == pytest.approx(0.2)
    assert summary["expected_calibration_error"] == pytest.approx(0.1)
    assert summary["calibration_bins"][0]["observed_accuracy"] == 0.5
    assert summary["by_model"]["model-a"]["count"] == 2
