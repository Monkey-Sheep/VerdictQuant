"""Immutable next-bar forecasts and realized-outcome scoring."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.provenance import build_analysis_provenance

_DIRECTIONS = ("bullish", "bearish", "neutral")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_probabilities(prediction: dict[str, Any]) -> dict[str, float] | None:
    raw = prediction.get("probabilities")
    if not isinstance(raw, dict):
        return None
    values = {name: _finite_float(raw.get(name)) for name in _DIRECTIONS}
    if any(value is None or value < 0 for value in values.values()):
        return None
    total = sum(value for value in values.values() if value is not None)
    if total <= 0:
        return None
    return {name: float(values[name]) / total for name in _DIRECTIONS}


def _prediction_from_record(record: Any) -> dict[str, Any] | None:
    stage2 = record.stage2_decision if isinstance(record.stage2_decision, dict) else {}
    prediction = stage2.get("next_bar_prediction")
    if not isinstance(prediction, dict) or prediction.get("unpredictable") is True:
        return None
    direction = str(prediction.get("direction") or "").strip().lower()
    probabilities = _normalized_probabilities(prediction)
    if direction not in _DIRECTIONS or probabilities is None:
        return None
    return {
        "direction": direction,
        "probabilities": {name: round(probabilities[name] * 100, 6) for name in _DIRECTIONS},
        "reasoning": str(prediction.get("reasoning") or ""),
        "features_used": list(prediction.get("features_used") or []),
        "unpredictable": False,
    }


def _forecast_id(
    *,
    market: str,
    data_source: str,
    exchange: str,
    symbol: str,
    timeframe: str,
    base_bar_ts_ms: int,
) -> str:
    raw = "|".join(
        [market, data_source, exchange, symbol, timeframe, str(base_bar_ts_ms)]
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def record_next_bar_forecast(
    *,
    record: Any,
    frame: KlineFrame,
    market: str,
    data_source: str,
    exchange: str = "",
    pending_dir: Path | None = None,
) -> Path | None:
    """Persist the first valid forecast for a symbol/timeframe/base bar."""
    prediction = _prediction_from_record(record)
    if prediction is None or not frame.bars:
        return None

    if pending_dir is None:
        from pa_agent.config.paths import FORECASTS_PENDING_DIR

        pending_dir = FORECASTS_PENDING_DIR

    base_bar = frame.bars[0]
    base_ts = int(base_bar.ts_open)
    forecast_id = _forecast_id(
        market=market,
        data_source=data_source,
        exchange=exchange,
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        base_bar_ts_ms=base_ts,
    )
    path = pending_dir / f"{frame.symbol}_{frame.timeframe}_{base_ts}_{forecast_id}.json"
    if path.exists():
        return path

    atr14 = _finite_float(frame.indicators.atr14[0]) if frame.indicators.atr14 else None
    base_close = float(base_bar.close)
    neutral_threshold = max(abs(base_close) * 0.00001, (atr14 or 0.0) * 0.05)
    provenance = build_analysis_provenance(record)
    payload = {
        "schema_version": "1.0",
        "forecast_id": forecast_id,
        "status": "pending",
        "created_at_ms": int(getattr(record.meta, "timestamp_local_ms", time.time() * 1000)),
        "market": market,
        "data_source": data_source,
        "exchange": exchange or None,
        "symbol": frame.symbol,
        "timeframe": frame.timeframe,
        "base_bar": {
            "ts_open_ms": base_ts,
            "open": float(base_bar.open),
            "high": float(base_bar.high),
            "low": float(base_bar.low),
            "close": base_close,
        },
        "atr14": atr14,
        "neutral_threshold_abs": neutral_threshold,
        "prediction": prediction,
        "model": provenance.get("model"),
        "provenance": provenance,
        "evaluation": None,
    }
    _atomic_write_json(path, payload)
    return path


def find_next_closed_bar(bars: Iterable[KlineBar], base_bar_ts_ms: int) -> KlineBar | None:
    candidates = [
        bar
        for bar in bars
        if bar.closed and int(bar.ts_open) > int(base_bar_ts_ms)
    ]
    return min(candidates, key=lambda bar: int(bar.ts_open), default=None)


def _actual_direction(next_bar: KlineBar, neutral_threshold_abs: float) -> str:
    delta = float(next_bar.close) - float(next_bar.open)
    if abs(delta) <= max(0.0, neutral_threshold_abs):
        return "neutral"
    return "bullish" if delta > 0 else "bearish"


def evaluate_forecast(
    forecast: dict[str, Any],
    next_bar: KlineBar,
    *,
    evaluated_at_ms: int | None = None,
) -> dict[str, Any]:
    """Attach deterministic proper-scoring metrics to a matured forecast."""
    prediction = forecast.get("prediction")
    if not isinstance(prediction, dict):
        raise ValueError("forecast prediction is missing")
    probabilities = _normalized_probabilities(prediction)
    if probabilities is None:
        raise ValueError("forecast probabilities are invalid")

    threshold = _finite_float(forecast.get("neutral_threshold_abs")) or 0.0
    actual = _actual_direction(next_bar, threshold)
    predicted = str(prediction.get("direction") or "").lower()
    brier = sum(
        (probabilities[name] - (1.0 if name == actual else 0.0)) ** 2
        for name in _DIRECTIONS
    )
    actual_probability = max(probabilities[actual], 1e-12)
    bull = probabilities["bullish"]
    bear = probabilities["bearish"]
    directional_total = bull + bear
    conditional_up = bull / directional_total if directional_total > 0 else None
    strict_binary_actual = (
        "up"
        if next_bar.close > next_bar.open
        else "down"
        if next_bar.close < next_bar.open
        else "tie"
    )
    binary_brier = None
    if conditional_up is not None and strict_binary_actual != "tie":
        binary_outcome = 1.0 if strict_binary_actual == "up" else 0.0
        binary_brier = (conditional_up - binary_outcome) ** 2

    result = dict(forecast)
    result["status"] = "evaluated"
    result["evaluation"] = {
        "evaluated_at_ms": int(evaluated_at_ms or time.time() * 1000),
        "next_bar": {
            "ts_open_ms": int(next_bar.ts_open),
            "open": float(next_bar.open),
            "high": float(next_bar.high),
            "low": float(next_bar.low),
            "close": float(next_bar.close),
        },
        "actual_direction": actual,
        "predicted_direction": predicted,
        "correct": predicted == actual,
        "realized_body_return_pct": (
            (float(next_bar.close) / float(next_bar.open) - 1.0) * 100
            if next_bar.open
            else None
        ),
        "brier_score": brier,
        "log_loss": -math.log(actual_probability),
        "strict_binary_actual": strict_binary_actual,
        "conditional_up_probability_ex_neutral": conditional_up,
        "binary_brier_score": binary_brier,
    }
    return result


def summarize_evaluated(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = [record for record in records if isinstance(record.get("evaluation"), dict)]
    if not rows:
        return {
            "evaluated_count": 0,
            "accuracy": None,
            "mean_brier_score": None,
            "mean_log_loss": None,
            "mean_binary_brier_score": None,
            "expected_calibration_error": None,
            "calibration_bins": [],
            "by_model": {},
        }
    correct = [bool(row["evaluation"].get("correct")) for row in rows]
    briers = [
        float(row["evaluation"]["brier_score"])
        for row in rows
        if _finite_float(row["evaluation"].get("brier_score")) is not None
    ]
    log_losses = [
        float(row["evaluation"]["log_loss"])
        for row in rows
        if _finite_float(row["evaluation"].get("log_loss")) is not None
    ]
    binary_briers = [
        float(row["evaluation"]["binary_brier_score"])
        for row in rows
        if _finite_float(row["evaluation"].get("binary_brier_score")) is not None
    ]
    calibration_rows: list[tuple[float, bool]] = []
    for row in rows:
        prediction = row.get("prediction")
        probabilities = (
            _normalized_probabilities(prediction) if isinstance(prediction, dict) else None
        )
        if probabilities is not None:
            calibration_rows.append(
                (max(probabilities.values()), bool(row["evaluation"].get("correct")))
            )
    calibration_bins: list[dict[str, Any]] = []
    weighted_gap = 0.0
    for lower_index in range(10):
        lower = lower_index / 10
        upper = (lower_index + 1) / 10
        members = [
            item
            for item in calibration_rows
            if lower <= item[0] < upper or (upper == 1.0 and item[0] == 1.0)
        ]
        if not members:
            continue
        confidence = sum(item[0] for item in members) / len(members)
        observed_accuracy = sum(1 for item in members if item[1]) / len(members)
        gap = abs(confidence - observed_accuracy)
        weighted_gap += gap * len(members)
        calibration_bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(members),
                "mean_confidence": confidence,
                "observed_accuracy": observed_accuracy,
                "calibration_gap": gap,
            }
        )
    by_model: dict[str, dict[str, Any]] = {}
    models = sorted({str(row.get("model") or "unknown") for row in rows})
    for model in models:
        model_rows = [row for row in rows if str(row.get("model") or "unknown") == model]
        model_correct = [bool(row["evaluation"].get("correct")) for row in model_rows]
        model_briers = [
            float(row["evaluation"]["brier_score"])
            for row in model_rows
            if _finite_float(row["evaluation"].get("brier_score")) is not None
        ]
        by_model[model] = {
            "count": len(model_rows),
            "accuracy": sum(model_correct) / len(model_correct),
            "mean_brier_score": (
                sum(model_briers) / len(model_briers) if model_briers else None
            ),
        }
    return {
        "evaluated_count": len(rows),
        "accuracy": sum(correct) / len(correct),
        "mean_brier_score": sum(briers) / len(briers) if briers else None,
        "mean_log_loss": sum(log_losses) / len(log_losses) if log_losses else None,
        "mean_binary_brier_score": (
            sum(binary_briers) / len(binary_briers) if binary_briers else None
        ),
        "expected_calibration_error": (
            weighted_gap / len(calibration_rows) if calibration_rows else None
        ),
        "calibration_bins": calibration_bins,
        "by_model": by_model,
    }


def load_json_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not directory.exists():
        return records
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records
