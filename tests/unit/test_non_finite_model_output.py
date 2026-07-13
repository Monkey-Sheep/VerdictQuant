"""Regression tests for non-finite model numbers."""
from __future__ import annotations

import math

from pa_agent.ai.json_validator import JsonValidator, ValidationError, _find_non_finite_numbers
from pa_agent.util.trade_metrics import (
    compute_risk_reward,
    validate_take_profit_2_geometry,
)


def test_recursive_non_finite_paths() -> None:
    payload = {"decision": {"entry": math.nan, "targets": [1.0, math.inf]}}

    assert _find_non_finite_numbers(payload) == [
        "$.decision.entry",
        "$.decision.targets[1]",
    ]


def test_validator_rejects_non_standard_json_nan() -> None:
    validator = JsonValidator()
    validator._schemas["stage1"] = {"type": "object"}
    validator.normalize_parsed = lambda _stage, obj, **_kwargs: obj  # type: ignore[method-assign]

    result = validator.validate("stage1", '{"price": NaN}')

    assert isinstance(result, ValidationError)
    assert result.category == "c"
    assert result.invalid_fields == ["$.price"]


def test_validator_rejects_non_finite_value_created_by_normalization() -> None:
    validator = JsonValidator()
    validator._schemas["stage1"] = {"type": "object"}
    validator.normalize_parsed = (  # type: ignore[method-assign]
        lambda _stage, _obj, **_kwargs: {"price": math.inf}
    )

    result = validator.validate("stage1", "{}")

    assert isinstance(result, ValidationError)
    assert result.category == "c"
    assert result.invalid_fields == ["$.price"]


def test_trade_metrics_reject_non_finite_and_overflow_values() -> None:
    assert compute_risk_reward(math.inf, 2.0, 0.0, "long") is None
    assert compute_risk_reward(1e308, -1e308, -1e308, "short") is None
    errors = validate_take_profit_2_geometry(
        {
            "entry_price": 1.0,
            "take_profit_price": 2.0,
            "take_profit_price_2": math.inf,
            "stop_loss_price": 0.0,
            "order_direction": "long",
        }
    )
    assert errors and "finite number" in errors[0]
