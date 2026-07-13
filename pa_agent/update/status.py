"""One-shot status exchange between the updater and the restarted GUI."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

from pa_agent.config.paths import USER_DATA_ROOT

UPDATE_STATUS_PATH = USER_DATA_ROOT / "updates" / "last-result.json"
_MAX_STATUS_BYTES = 64 * 1024


def consume_update_result(path: Path = UPDATE_STATUS_PATH) -> dict[str, object] | None:
    """Read and delete one updater result so it is never shown twice."""
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_STATUS_BYTES:
            return {
                "ok": False,
                "message": "Updater status file exceeded the safety limit.",
            }
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Updater status was not an object."}
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "message": f"Unable to read updater status: {type(exc).__name__}",
        }
    finally:
        with suppress(OSError):
            path.unlink(missing_ok=True)
