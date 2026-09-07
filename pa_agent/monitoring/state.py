"""Read explicit user-confirmed state. Prose is never execution evidence."""
from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

MAX_STATE_BYTES = 1_048_576


def aware_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("TIMESTAMP_REQUIRED")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return parsed


def number(value: Any, *, positive: bool = False) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and (value > 0 if positive else value >= 0)
    )


def unknown_state(status: str = "MISSING", reason: str = "STATE_NOT_CONFIRMED") -> dict:
    return {"status": status, "positions": {}, "executions": [],
            "execution_history_complete": False, "errors": [reason]}


def validate_state(payload: Any, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    root_keys = {"schema_version", "positions", "executions", "execution_history_complete"}
    if not isinstance(payload, dict) or set(payload) != root_keys:
        raise ValueError("STATE_SCHEMA_FIELDS")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("STATE_SCHEMA_VERSION")
    if type(payload["execution_history_complete"]) is not bool:
        raise ValueError("EXECUTION_HISTORY_STATUS_REQUIRED")
    if not isinstance(payload["positions"], dict) or len(payload["positions"]) > 32:
        raise ValueError("POSITION_SET_INVALID")
    positions = {}
    allowed = {"status", "quantity", "average_cost", "purchase_date", "confirmed_at", "source_ref", "review_after"}
    for symbol, position in payload["positions"].items():
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.^=-]{0,15}", symbol):
            raise ValueError("POSITION_SYMBOL_INVALID")
        if not isinstance(position, dict) or set(position) - allowed:
            raise ValueError("POSITION_FIELDS_INVALID")
        status = position.get("status")
        if status not in {"unknown", "confirmed", "stale"}:
            raise ValueError("POSITION_STATUS_INVALID")
        if status != "confirmed":
            positions[symbol] = {"status": status, "quantity": None, "details_complete": False}
            continue
        quantity = position.get("quantity")
        if not number(quantity):
            raise ValueError("CONFIRMED_QUANTITY_REQUIRED")
        source = position.get("source_ref")
        if not isinstance(source, str) or not source.strip() or len(source) > 500:
            raise ValueError("CONFIRMATION_SOURCE_REQUIRED")
        confirmed = aware_time(position.get("confirmed_at"))
        if confirmed > now + timedelta(seconds=30):
            raise ValueError("FUTURE_CONFIRMATION")
        review_after = position.get("review_after")
        if review_after is not None and aware_time(review_after) < now:
            positions[symbol] = {"status": "stale", "quantity": None, "details_complete": False}
            continue
        purchase = position.get("purchase_date")
        if purchase is not None:
            if not isinstance(purchase, str) or date.fromisoformat(purchase) > confirmed.date():
                raise ValueError("PURCHASE_DATE_INVALID")
        cost = position.get("average_cost")
        if cost is not None and not number(cost, positive=True):
            raise ValueError("AVERAGE_COST_INVALID")
        positions[symbol] = {"status": "confirmed", "quantity": quantity,
                             "details_complete": quantity == 0 or (cost is not None and purchase is not None)}
    executions = payload["executions"]
    if not isinstance(executions, list) or len(executions) > 2000:
        raise ValueError("EXECUTIONS_INVALID")
    seen = set()
    checked = []
    execution_keys = {"event_id", "proposal_id", "symbol", "action", "quantity", "executed_at", "source_ref"}
    for execution in executions:
        if not isinstance(execution, dict) or set(execution) != execution_keys:
            raise ValueError("EXECUTION_FIELDS_INVALID")
        for key in ("event_id", "proposal_id", "symbol", "source_ref"):
            if not isinstance(execution[key], str) or not execution[key].strip() or len(execution[key]) > 500:
                raise ValueError("EXECUTION_IDENTITY_INVALID")
        if execution["event_id"] in seen:
            raise ValueError("DUPLICATE_EXECUTION_EVENT")
        seen.add(execution["event_id"])
        if execution["action"] not in {"ADD", "REDUCE", "EXIT"} or not number(execution["quantity"], positive=True):
            raise ValueError("EXECUTION_ACTION_INVALID")
        if aware_time(execution["executed_at"]) > now + timedelta(seconds=30):
            raise ValueError("FUTURE_EXECUTION")
        checked.append({key: execution[key] for key in ("event_id", "proposal_id", "symbol", "action")})
    return {"status": "VALID", "positions": positions, "executions": checked,
            "execution_history_complete": payload["execution_history_complete"], "errors": []}


def load_state(path: Path, now: datetime | None = None) -> dict:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return unknown_state("INVALID", "STATE_SIZE_LIMIT")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("DUPLICATE_JSON_KEY")
                result[key] = value
            return result
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
        return validate_state(payload, now)
    except FileNotFoundError:
        return unknown_state()
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        # Never echo malformed state, amounts, private source references or paths.
        return unknown_state("INVALID", "STATE_VALIDATION_FAILED")


def public_state_summary(state: dict) -> dict:
    return {"status": state["status"], "execution_history_complete": state["execution_history_complete"],
            "positions": {symbol: {"status": p["status"], "details_complete": p["details_complete"]}
                          for symbol, p in state["positions"].items()}, "errors": state["errors"]}


def fund_confirmation(state: dict, code: str = "001437") -> dict:
    position = state["positions"].get(code, {})
    confirmed = position.get("status") == "confirmed"
    return {"position_confirmed": confirmed and position["quantity"] > 0,
            "position_details_complete": confirmed and position["details_complete"],
            "position_status": position.get("status", "unknown"),
            "state_source_verified": state["status"] == "VALID" and confirmed}


def reduction_permission(state: dict, symbol: str, proposal_id: str) -> tuple[bool, str]:
    if state["status"] != "VALID" or not state["execution_history_complete"]:
        return False, "EXECUTION_HISTORY_UNCONFIRMED"
    position = state["positions"].get(symbol, {})
    if position.get("status") != "confirmed" or not number(position.get("quantity"), positive=True):
        return False, "CURRENT_POSITION_UNCONFIRMED_OR_FLAT"
    if any(event["proposal_id"] == proposal_id for event in state["executions"]):
        return False, "ALREADY_EXECUTED"
    return True, "MANUAL_REVIEW_ONLY"
