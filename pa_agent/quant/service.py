"""Application service joining PA analysis, risk gates, and paper execution."""
from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from contextlib import suppress
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from pa_agent.headless import AnalysisRequest
from pa_agent.quant.maintenance import PaperAccountMaintenance, migrate_legacy_runtime
from pa_agent.quant.models import PaperConfig, WatchItem
from pa_agent.quant.paper_engine import PaperEngine, PaperRiskError
from pa_agent.quant.signal_adapter import signal_from_analysis
from pa_agent.quant.store import QuantStore


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


class QuantPaperService:
    """Public paper-only API used by the CLI, GUI, and other AI callers."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        config_path: Path | None = None,
        analysis_runner: Callable[[AnalysisRequest], dict[str, Any]] | None = None,
    ) -> None:
        from pa_agent.config.paths import (
            LEGACY_QUANT_DB_PATH,
            LEGACY_QUANT_DIR,
            QUANT_DB_PATH,
            QUANT_DIR,
        )

        env_db = (
            os.environ.get("VERDICTQUANT_QUANT_DB", "").strip()
            or os.environ.get("PA_AGENT_QUANT_DB", "").strip()
        )
        env_config = (
            os.environ.get("VERDICTQUANT_QUANT_CONFIG", "").strip()
            or os.environ.get("PA_AGENT_QUANT_CONFIG", "").strip()
        )
        using_default_db = db_path is None and not env_db
        using_default_config = config_path is None and not env_config
        selected_db = db_path or (Path(env_db) if env_db else QUANT_DB_PATH)
        selected_config = config_path or (
            Path(env_config) if env_config else (QUANT_DIR / "config.json")
        )
        self.legacy_migration: dict[str, Any] = {
            "database_migrated": False,
            "config_migrated": False,
        }
        if using_default_db or using_default_config:
            self.legacy_migration = migrate_legacy_runtime(
                legacy_db=LEGACY_QUANT_DB_PATH,
                legacy_config=LEGACY_QUANT_DIR / "config.json",
                target_db=Path(selected_db),
                target_config=Path(selected_config),
                migrate_database=using_default_db,
                migrate_config=using_default_config,
            )
        self.config_path = selected_config.resolve()
        self.config = self._load_config()
        self.store = QuantStore(selected_db)
        self.engine = PaperEngine(self.store, self.config)
        self.maintenance = PaperAccountMaintenance(self.store, self.config_path)
        if analysis_runner is None:
            from pa_agent.headless import run_analysis

            analysis_runner = run_analysis
        self.analysis_runner = analysis_runner

    def _reload_runtime(self) -> None:
        database = self.store.path
        self.config = self._load_config()
        self.store = QuantStore(database)
        self.engine = PaperEngine(self.store, self.config)
        self.maintenance = PaperAccountMaintenance(self.store, self.config_path)

    def backup(self, output: Path | None = None) -> dict[str, Any]:
        return self.maintenance.backup(output)

    def restore(self, archive: Path, *, confirmation: str) -> dict[str, Any]:
        result = self.maintenance.restore(archive, confirmation=confirmation)
        self._reload_runtime()
        result["account"] = self.status()
        return result

    def reset(self, *, confirmation: str) -> dict[str, Any]:
        result = self.maintenance.reset(self.config, confirmation=confirmation)
        self._reload_runtime()
        result["account"] = self.status()
        return result

    def _load_config(self) -> PaperConfig:
        if not self.config_path.exists():
            config = PaperConfig()
            _atomic_json(self.config_path, config.model_dump(mode="json"))
            return config
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        return PaperConfig.model_validate(payload)

    def _save_config(self) -> None:
        _atomic_json(self.config_path, self.config.model_dump(mode="json"))

    def add_watch(self, item: WatchItem) -> dict[str, Any]:
        normalized = item.model_copy(update={"symbol": item.symbol.strip().upper()})
        key = (normalized.market, normalized.symbol, normalized.timeframe)
        existing = {
            (row.market, row.symbol, row.timeframe) for row in self.config.watchlist
        }
        if key not in existing:
            self.config.watchlist.append(normalized)
            self._save_config()
        return {
            "paper_only": True,
            "added": key not in existing,
            "watchlist": [row.model_dump(mode="json") for row in self.config.watchlist],
        }

    def remove_watch(self, *, market: str, symbol: str) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        before = len(self.config.watchlist)
        self.config.watchlist = [
            row
            for row in self.config.watchlist
            if not (row.market == market and row.symbol == symbol)
        ]
        if len(self.config.watchlist) != before:
            self._save_config()
        return {
            "paper_only": True,
            "removed": before - len(self.config.watchlist),
            "watchlist": [row.model_dump(mode="json") for row in self.config.watchlist],
        }

    def analyze_and_queue(
        self,
        request: AnalysisRequest,
        *,
        queue: bool = True,
    ) -> dict[str, Any]:
        """Analyze any supported stock and optionally queue its guarded signal."""
        if request.market == "crypto":
            raise ValueError("stock quant paper service does not execute crypto")
        analysis = self.analysis_runner(request)
        signal = signal_from_analysis(analysis)
        initial_status = "hold" if signal.side == "hold" else "observed"
        inserted = self.store.add_signal(signal, status=initial_status)
        cancelled_orders: list[str] = []
        if inserted and queue:
            cancelled_orders = self.store.cancel_older_pending_entries(
                market=signal.market,
                symbol=signal.symbol,
                before_base_bar_ts_ms=signal.base_bar_ts_ms,
                reason=f"superseded by newer closed-bar signal {signal.signal_id}",
            )
        result: dict[str, Any] = {
            "paper_only": True,
            "live_execution": False,
            "analysis_status": analysis.get("status"),
            "signal": signal.model_dump(mode="json"),
            "new_signal": inserted,
            "order": None,
            "risk_rejection": None,
            "cancelled_stale_orders": cancelled_orders,
            "analysis_record_path": analysis.get("record_path"),
            "forecast_path": analysis.get("forecast_path"),
        }
        if not inserted:
            existing = self.store.signal(signal.signal_id)
            result["signal_status"] = "duplicate"
            if (
                queue
                and existing is not None
                and existing["status"] == "observed"
                and self.store.order_for_signal(signal.signal_id) is None
            ):
                stored_signal = self.store.signal_payload(existing)
                try:
                    result["order"] = self.engine.queue_signal(stored_signal)
                    result["signal_status"] = "queued_after_recovery"
                except PaperRiskError as exc:
                    reason = str(exc)
                    self.store.update_signal(signal.signal_id, "rejected", reason)
                    result["signal_status"] = "rejected"
                    result["risk_rejection"] = reason
            return result
        if not queue or signal.side == "hold":
            result["signal_status"] = initial_status
            return result
        try:
            result["order"] = self.engine.queue_signal(signal)
            result["signal_status"] = "queued"
        except PaperRiskError as exc:
            reason = str(exc)
            self.store.update_signal(signal.signal_id, "rejected", reason)
            result["signal_status"] = "rejected"
            result["risk_rejection"] = reason
        return result

    def settle(
        self,
        *,
        market: str | None = None,
        symbol: str | None = None,
        source_factory: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch public bars and settle every matching pending paper item."""
        if source_factory is None:
            from pa_agent.data.factory import create_data_source

            source_factory = create_data_source
        targets = self._settlement_targets(market=market, symbol=symbol)
        details: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for target in targets:
            source = None
            try:
                kind = "akshare" if target["market"] == "a-share" else "tradingview"
                source = source_factory(kind)
                if kind == "akshare":
                    fallback = getattr(source, "enable_baostock_fallback", None)
                    if callable(fallback):
                        fallback()
                source.connect()
                set_exchange = getattr(source, "set_exchange", None)
                if callable(set_exchange):
                    set_exchange(target.get("exchange") or "")
                source.subscribe(target["symbol"], target["timeframe"])
                bars = source.latest_snapshot(600)
                details.append(
                    self.engine.process_bars(target["market"], target["symbol"], bars)
                )
            except Exception as exc:
                errors.append(
                    {
                        "market": target["market"],
                        "symbol": target["symbol"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            finally:
                if source is not None:
                    with suppress(Exception):
                        source.disconnect()
        return {
            "paper_only": True,
            "live_execution": False,
            "targets": len(targets),
            "details": details,
            "errors": errors,
            "status": self.status(),
        }

    def run_watch_cycle(self) -> dict[str, Any]:
        """Settle existing positions, then analyze every configured watch item."""
        settlement = self.settle()
        analyses: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item in self.config.watchlist:
            try:
                analyses.append(
                    self.analyze_and_queue(
                        AnalysisRequest(
                            market=item.market,
                            symbol=item.symbol,
                            exchange=item.exchange,
                            timeframe=item.timeframe,
                            bar_count=item.bar_count,
                            predict_next_bar=item.predict_next_bar,
                        )
                    )
                )
            except Exception as exc:
                errors.append(
                    {
                        "market": item.market,
                        "symbol": item.symbol,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "paper_only": True,
            "live_execution": False,
            "watch_count": len(self.config.watchlist),
            "settlement": settlement,
            "analyses": analyses,
            "errors": errors,
            "status": self.status(),
        }

    def _settlement_targets(
        self,
        *,
        market: str | None,
        symbol: str | None,
    ) -> list[dict[str, Any]]:
        keys = {
            (row["market"], row["symbol"])
            for row in self.store.pending_orders(market, symbol)
        }
        keys.update(
            (row["market"], row["symbol"])
            for row in self.store.open_positions(market, symbol)
        )
        targets: list[dict[str, Any]] = []
        with self.store.connection() as conn:
            for target_market, target_symbol in sorted(keys):
                row = conn.execute(
                    "SELECT payload_json FROM signals WHERE market=? AND symbol=? "
                    "ORDER BY created_at_ms DESC LIMIT 1",
                    (target_market, target_symbol),
                ).fetchone()
                if row is None:
                    continue
                payload = json.loads(row["payload_json"])
                targets.append(
                    {
                        "market": target_market,
                        "symbol": target_symbol,
                        "exchange": payload.get("exchange"),
                        "timeframe": payload.get("timeframe") or "1d",
                    }
                )
        return targets

    def status(self) -> dict[str, Any]:
        result = self.store.status()
        profile_id = result["profile"]["profile_id"]
        result.update(
            {
                "config": self.config.model_dump(mode="json"),
                "local_account": {
                    "scope": "one durable portfolio per operating-system user",
                    "profile_id": profile_id,
                    "database": str(self.store.path),
                    "restore_confirmation": f"RESTORE {profile_id}",
                    "reset_confirmation": f"RESET {profile_id}",
                },
                "legacy_migration": self.legacy_migration,
                "execution_reference": self.engine.execution_reference,
                "external_validation": [
                    "VerdictQuant guarded two-stage analysis",
                    "Backtrader reference-strategy reports when installed",
                    "RQAlpha A-share market-rule reports when installed",
                ],
            }
        )
        return result

    def doctor(self) -> dict[str, Any]:
        """Run local, non-network release checks without exposing any secret."""
        database = self.store.health()
        dependency_modules = {
            "pyqt6": "PyQt6",
            "tradingview": "tvDatafeed",
            "akshare": "akshare",
            "baostock": "baostock",
            "cryptography": "cryptography",
        }
        required_dependencies = ["tradingview", "akshare", "cryptography"]
        if os.name == "nt":
            dependency_modules.update(
                {
                    "windows_credential_manager": "win32cred",
                    "windows_dpapi": "win32crypt",
                }
            )
            required_dependencies.extend(
                ["windows_credential_manager", "windows_dpapi"]
            )
        dependencies = {
            name: find_spec(module) is not None
            for name, module in dependency_modules.items()
        }
        blockers: list[str] = []
        if not database["ok"]:
            blockers.append("paper database integrity or foreign-key check failed")
        for dependency in required_dependencies:
            if not dependencies[dependency]:
                blockers.append(f"required runtime dependency is missing: {dependency}")
        validation = self.store.status(signal_limit=1)["validation"]
        return {
            "paper_only": True,
            "live_execution": False,
            "real_account_connection": False,
            "status": "ok" if not blockers else "blocked",
            "blockers": blockers,
            "database": database,
            "dependencies": dependencies,
            "config_valid": True,
            "profile_id": self.store.profile()["profile_id"],
            "evidence_status": validation["evidence_status"],
            "sample_reviewable": validation["sample_reviewable"],
            "live_promotion_allowed": False,
        }
