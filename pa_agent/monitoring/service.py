"""Manual refresh and immutable local snapshots; no scheduler or notifications."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from concurrent.futures import CancelledError
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .calendar import Calendars
from .market import PublicClient, core_review_checks
from .state import aware_time, load_state, public_state_summary


class RefreshBusy(RuntimeError):
    pass


def _local_json(raw: bytes, invalid: str):
    try:
        return json.loads(raw.decode("utf-8-sig"),
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError(invalid)))
    except (UnicodeError, ValueError):
        raise ValueError(invalid) from None


def atomic_json(path: Path, value: dict) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    temp = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    with temp.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    return raw


def default_data_root() -> Path:
    explicit = os.environ.get("VERDICTQUANT_MONITOR_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    finance = Path("D:/CodexData/finance")
    if os.name == "nt" and finance.is_dir():
        return finance / "monitoring/desktop"
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VerdictQuant/monitoring"


class MonitoringService:
    def __init__(self, data_root: Path | None = None, workspace: Path | None = None,
                 fund_db: Path | None = None, client=None):
        self.root = (data_root or default_data_root()).resolve()
        paths_file = self.root / "paths.json"
        if paths_file.exists():
            if not paths_file.is_file() or paths_file.stat().st_size > 4096:
                raise ValueError("MONITOR_PATHS_INVALID")
            paths = _local_json(paths_file.read_bytes(), "MONITOR_PATHS_INVALID")
            if (not isinstance(paths, dict)
                    or any(value is not None and not isinstance(value, str)
                           for value in (paths.get("workspace"), paths.get("fund_db")))):
                raise ValueError("MONITOR_PATHS_INVALID")
        else:
            paths = {}
        self.workspace = workspace or Path(paths.get("workspace") or os.environ.get("VERDICTQUANT_FINANCE_WORKSPACE") or self.root)
        self.state_path = self.workspace / "portfolio_state.json"
        database = fund_db or (Path(paths["fund_db"]) if paths.get("fund_db") else None)
        self.policy_path = Path(__file__).with_name("policy.json")
        self.policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        if self.policy["research_only"] is not True or self.policy["account_connections"] is not False or self.policy["live_execution"] is not False:
            raise ValueError("RESEARCH_BOUNDARY_INVALID")
        self.calendars = Calendars()
        self.client = client or PublicClient(self.policy, self.calendars, database)

    def latest(self, now: datetime | None = None) -> dict | None:
        """No directory creation, database connection, network or state mutation."""
        pointer = self.root / "current.json"
        if not pointer.exists():
            return None
        if pointer.stat().st_size > 4096:
            raise ValueError("CURRENT_POINTER_INVALID")
        ref = _local_json(pointer.read_bytes(), "CURRENT_POINTER_INVALID")
        if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
            raise ValueError("CURRENT_POINTER_INVALID")
        run_id = ref.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9TZ-]+-[0-9a-f]{12}", run_id):
            raise ValueError("RUN_ID_INVALID")
        path = (self.root / "runs" / run_id / "bundle.json").resolve()
        if not path.is_relative_to(self.root) or path.stat().st_size > 8_388_608:
            raise ValueError("SNAPSHOT_PATH_OR_SIZE_INVALID")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("SNAPSHOT_HASH_MISMATCH")
        result = _local_json(raw, "SNAPSHOT_SCHEMA_INVALID")
        if (not isinstance(result, dict) or result.get("schema_version") != 1 or result.get("run_id") != run_id
                or result.get("manual_only") is not True or result.get("orders") is not False
                or result.get("real_account_access") is not False or result.get("notifications") is not False
                or result.get("action_proposals") != []):
            raise ValueError("SNAPSHOT_SCHEMA_INVALID")
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("assets"), dict) or not data["assets"] or not isinstance(data.get("errors"), dict):
            raise ValueError("SNAPSHOT_SCHEMA_INVALID")
        for symbol, asset in data["assets"].items():
            if (not isinstance(symbol, str) or not isinstance(asset, dict) or asset.get("symbol") != symbol
                    or not isinstance(asset.get("market"), str)
                    or asset["market"] not in {*self.calendars.data, "FX", "CRYPTO"}
                    or not isinstance(asset.get("date"), str) or not isinstance(asset.get("qualified"), bool)
                    or not isinstance(asset.get("errors"), list)
                    or any(not isinstance(error, str) for error in asset["errors"])
                    or any(asset.get(field) is not None and not isinstance(asset[field], dict)
                           for field in ("source", "ma"))
                    or (asset.get("quote") is not None and not isinstance(asset["quote"], dict))):
                raise ValueError("SNAPSHOT_SCHEMA_INVALID")
        now = now or datetime.now(UTC)
        try:
            for asset in data["assets"].values():
                quote = asset.get("quote")
                if quote:
                    try:
                        quote["fresh"] = -30 <= (now - aware_time(quote["quoted_at"])).total_seconds() <= self.policy["quote_max_age_seconds"]
                    except (KeyError, ValueError, TypeError):
                        quote["fresh"] = False
                try:
                    expired = asset["date"] != self.calendars.latest_completed(now, asset["market"]).isoformat()
                except ValueError:
                    expired = True
                if expired:
                    asset["qualified"] = False
                    asset["errors"] = [*asset.get("errors", []), "SAVED_DATA_NEEDS_REFRESH"]
                    data["errors"][asset["symbol"]] = asset["errors"]
            result["universe"] = self.policy["us_universe"]
            result["core_checks"] = core_review_checks(data, self.policy)
        except (AttributeError, KeyError, TypeError, OverflowError):
            raise ValueError("SNAPSHOT_SCHEMA_INVALID") from None
        result["state"] = public_state_summary(load_state(self.state_path, now))
        return result

    def refresh(self, cancelled=None) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        # SQLite releases its cross-process lock even after a crash. No stale PID
        # deletion or application/account database is involved.
        lock = sqlite3.connect(self.root / "refresh-lock.sqlite3", timeout=0.1)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise RefreshBusy("REFRESH_ALREADY_RUNNING") from exc
            now = datetime.now(UTC)
            data = self.client.collect(now, cancelled)
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            if not data.get("assets"):
                raise ValueError("NO_PUBLIC_DATA_LAST_RESULT_PRESERVED")
            state = load_state(self.state_path, now)
            run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
            result = {"schema_version": 1, "run_id": run_id, "checked_at": now.isoformat(),
                      "manual_only": True, "notifications": False, "real_account_access": False, "orders": False,
                      "policy_sha256": hashlib.sha256(self.policy_path.read_bytes()).hexdigest(),
                      "universe": self.policy["us_universe"],
                      "data": data, "core_checks": core_review_checks(data, self.policy),
                      "state": public_state_summary(state),
                      "research_status": "PUBLIC_METRICS_ONLY",
                      "research_gaps": ["最新公告与经理变更需另行核验", "最新披露持仓与成长广度需补齐", "公司估值与催化剂未完成完整复核"],
                      "action_proposals": []}
            run_dir = self.root / "runs" / run_id
            run_dir.mkdir(parents=True)
            raw = atomic_json(run_dir / "bundle.json", result)
            atomic_json(self.root / "current.json", {"run_id": run_id, "sha256": hashlib.sha256(raw).hexdigest()})
            lock.commit()
            return result
        finally:
            lock.close()
