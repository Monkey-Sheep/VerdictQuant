from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from pa_agent.headless import AnalysisRequest
from pa_agent.quant.maintenance import (
    PaperAccountMaintenanceError,
    migrate_legacy_runtime,
)
from pa_agent.quant.models import WatchItem
from pa_agent.quant.service import QuantPaperService
from pa_agent.quant.signal_adapter import signal_from_analysis
from pa_agent.quant.store import CURRENT_SCHEMA_VERSION, QuantStore


def _analysis() -> dict:
    return {
        "status": "ok",
        "market": "us",
        "symbol": "NVDA",
        "exchange": "NASDAQ",
        "timeframe": "1d",
        "latest_closed_bar_ts_ms": 1_000,
        "latest_close": 100,
        "record_path": "record.json",
        "forecast_path": "forecast.json",
        "signal": {
            "actionable": True,
            "order_direction": "做多",
            "order_type": "限价单",
            "trade_confidence": 75,
            "estimated_win_rate": 58,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 115,
            "invalidation_condition": "close below 95",
        },
    }


def test_signal_adapter_is_deterministic() -> None:
    first = signal_from_analysis(_analysis())
    second = signal_from_analysis(_analysis())
    assert first.signal_id == second.signal_id
    assert first.side == "buy"
    assert first.order_type == "limit"


def test_signal_identity_does_not_depend_on_record_filename() -> None:
    first_analysis = _analysis()
    second_analysis = _analysis()
    second_analysis["record_path"] = "another-run-record.json"

    assert signal_from_analysis(first_analysis).signal_id == signal_from_analysis(
        second_analysis
    ).signal_id


def test_service_records_and_deduplicates_analysis(tmp_path: Path) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    request = AnalysisRequest(market="us", symbol="NVDA", exchange="NASDAQ")

    first = service.analyze_and_queue(request)
    second = service.analyze_and_queue(request)

    assert first["signal_status"] == "queued"
    assert first["order"] is not None
    assert second["signal_status"] == "duplicate"
    status = service.status()
    assert status["paper_only"] is True
    assert status["live_execution"] is False
    assert len(status["signals"]) == 1
    assert len(status["orders"]) == 1


def test_service_records_hold_without_order(tmp_path: Path) -> None:
    result = _analysis()
    result["signal"] = {
        "actionable": False,
        "order_direction": None,
        "order_type": "不下单",
    }
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: result,
    )
    output = service.analyze_and_queue(AnalysisRequest(market="us", symbol="NVDA"))
    assert output["signal_status"] == "hold"
    assert output["order"] is None


def test_duplicate_observed_signal_recovers_atomic_queue(tmp_path: Path) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    signal = signal_from_analysis(_analysis())
    service.store.add_signal(signal, status="observed")

    output = service.analyze_and_queue(AnalysisRequest(market="us", symbol="NVDA"))

    assert output["signal_status"] == "queued_after_recovery"
    assert len(service.store.pending_orders()) == 1


def test_newer_hold_cancels_an_older_pending_entry(tmp_path: Path) -> None:
    analyses = [_analysis(), _analysis()]
    analyses[1]["latest_closed_bar_ts_ms"] = 2_000
    analyses[1]["record_path"] = "newer.json"
    analyses[1]["signal"] = {
        "actionable": False,
        "order_direction": None,
        "order_type": "不下单",
    }
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: analyses.pop(0),
    )
    request = AnalysisRequest(market="us", symbol="NVDA")

    first = service.analyze_and_queue(request)
    second = service.analyze_and_queue(request)

    assert first["signal_status"] == "queued"
    assert second["signal_status"] == "hold"
    assert len(second["cancelled_stale_orders"]) == 1
    assert service.store.pending_orders() == []


def test_analysis_only_does_not_cancel_an_existing_pending_entry(
    tmp_path: Path,
) -> None:
    analyses = [_analysis(), _analysis()]
    analyses[1]["latest_closed_bar_ts_ms"] = 2_000
    analyses[1]["signal"] = {
        "actionable": False,
        "order_direction": None,
        "order_type": "不下单",
    }
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: analyses.pop(0),
    )
    request = AnalysisRequest(market="us", symbol="NVDA")
    service.analyze_and_queue(request)

    observed = service.analyze_and_queue(request, queue=False)

    assert observed["signal_status"] == "hold"
    assert observed["cancelled_stale_orders"] == []
    assert len(service.store.pending_orders()) == 1


def test_doctor_is_local_and_paper_only(tmp_path: Path) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )

    result = service.doctor()

    assert result["status"] == "ok"
    assert result["paper_only"] is True
    assert result["live_execution"] is False
    assert result["real_account_connection"] is False
    assert result["database"]["ok"] is True


def test_watchlist_cycle_is_persistent_and_deduplicated(tmp_path: Path) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    item = WatchItem(market="us", symbol="nvda", exchange="NASDAQ")
    assert service.add_watch(item)["added"] is True
    assert service.add_watch(item)["added"] is False
    service.settle = lambda **_kwargs: {"targets": 0}  # type: ignore[method-assign]

    cycle = service.run_watch_cycle()

    assert cycle["watch_count"] == 1
    assert cycle["analyses"][0]["signal_status"] == "queued"
    reloaded = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    assert reloaded.config.watchlist[0].symbol == "NVDA"
    assert reloaded.remove_watch(market="us", symbol="NVDA")["removed"] == 1


def test_legacy_database_migrates_to_stable_local_identity(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE accounts (
                market TEXT PRIMARY KEY,
                currency TEXT NOT NULL,
                starting_cash REAL NOT NULL,
                cash REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO accounts VALUES('us','USD',100000,98765,123,1000)"
        )

    first = QuantStore(database)
    first_profile = first.profile()
    first_account = first.account("us")
    second = QuantStore(database)

    assert first.schema_version() == CURRENT_SCHEMA_VERSION
    assert second.profile()["profile_id"] == first_profile["profile_id"]
    assert second.account("us")["account_id"] == first_account["account_id"]
    assert second.account("us")["cash"] == 98_765
    assert second.account("us")["realized_pnl"] == 123


def test_complete_v2_database_migrates_execution_evidence_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v2.db"
    profile_id = "cdb30df3-f0dd-4b58-8e56-977cc66c1986"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE accounts (
                market TEXT PRIMARY KEY,
                currency TEXT NOT NULL,
                starting_cash REAL NOT NULL,
                cash REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL,
                account_id TEXT
            );
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY,
                created_at_ms INTEGER NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                base_bar_ts_ms INTEGER NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                requested_price REAL,
                eligible_after_ts_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                wait_bars INTEGER NOT NULL DEFAULT 0,
                fill_ts_ms INTEGER,
                fill_price REAL,
                fee REAL,
                reject_reason TEXT,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE positions (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                avg_price REAL NOT NULL,
                opened_at_ts_ms INTEGER NOT NULL,
                available_after_date TEXT,
                stop_loss REAL,
                take_profit REAL,
                source_signal_id TEXT NOT NULL,
                holding_bars INTEGER NOT NULL DEFAULT 0,
                last_processed_ts_ms INTEGER NOT NULL,
                last_price REAL NOT NULL,
                PRIMARY KEY(market, symbol)
            );
            CREATE TABLE fills (
                fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                fee REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                ts_ms INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at_ms INTEGER NOT NULL
            );
            CREATE TABLE local_profile (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                profile_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO local_profile VALUES(1,?,?,1)",
            (profile_id, "Local Paper Portfolio"),
        )
        conn.execute(
            "INSERT INTO accounts VALUES('us','USD',100000,100000,0,1,?)",
            ("7d627759-f22d-56f0-905a-2321f3138bcd",),
        )
        conn.execute(
            "INSERT INTO schema_migrations VALUES(2,'stable-local-profile',1)"
        )

    store = QuantStore(database)

    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert store.health()["ok"] is True
    with store.connection() as conn:
        assert {row["name"] for row in conn.execute("PRAGMA table_info(orders)")} >= {
            "last_evaluated_ts_ms",
            "filled_quantity",
        }
        assert "entry_fee_basis" in {
            row["name"] for row in conn.execute("PRAGMA table_info(positions)")
        }
        assert "source_signal_id" in {
            row["name"] for row in conn.execute("PRAGMA table_info(fills)")
        }


def test_backup_reset_and_restore_preserve_the_original_account(tmp_path: Path) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    service.analyze_and_queue(
        AnalysisRequest(market="us", symbol="NVDA", exchange="NASDAQ")
    )
    original = service.status()
    original_profile = original["profile"]["profile_id"]
    archive = Path(service.backup()["backup"])

    with pytest.raises(PaperAccountMaintenanceError, match="confirmation mismatch"):
        service.reset(confirmation="RESET wrong-account")

    reset = service.reset(
        confirmation=original["local_account"]["reset_confirmation"]
    )
    reset_profile = reset["profile_id"]
    assert reset_profile != original_profile
    assert service.status()["signals"] == []
    assert Path(reset["safety_backup"]).is_file()

    current = service.status()
    restored = service.restore(
        archive,
        confirmation=current["local_account"]["restore_confirmation"],
    )
    assert restored["profile_id"] == original_profile
    assert service.status()["profile"]["profile_id"] == original_profile
    assert len(service.status()["signals"]) == 1
    assert len(service.status()["orders"]) == 1


def test_restore_rejects_a_tampered_backup_without_changing_account(
    tmp_path: Path,
) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    original_profile = service.status()["profile"]["profile_id"]
    archive = Path(service.backup()["backup"])
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            payload = b"not a sqlite database" if name == "paper.db" else source.read(name)
            target.writestr(name, payload)

    phrase = service.status()["local_account"]["restore_confirmation"]
    with pytest.raises(PaperAccountMaintenanceError, match="hash mismatch"):
        service.restore(tampered, confirmation=phrase)
    assert service.status()["profile"]["profile_id"] == original_profile


def test_restore_accepts_legacy_backup_brand_for_upgrade_compatibility(
    tmp_path: Path,
) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    current_archive = Path(service.backup()["backup"])
    legacy_archive = tmp_path / "legacy-brand.zip"
    with zipfile.ZipFile(current_archive) as source, zipfile.ZipFile(legacy_archive, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(payload)
                manifest["format"] = "pa-agent-quant-paper-backup"
                payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            target.writestr(name, payload)

    result = service.restore(
        legacy_archive,
        confirmation=service.status()["local_account"]["restore_confirmation"],
    )

    assert result["status"] == "restored"


def test_restore_rejects_an_unsafe_compression_ratio(tmp_path: Path) -> None:
    service = QuantPaperService(
        db_path=tmp_path / "paper.db",
        config_path=tmp_path / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    archive = tmp_path / "compressed-bomb.zip"
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as target:
        target.writestr("manifest.json", b"{}")
        target.writestr("paper.db", b"0" * 1_100_000)
        target.writestr("config.json", b"{}")

    phrase = service.status()["local_account"]["restore_confirmation"]
    with pytest.raises(PaperAccountMaintenanceError, match="compression ratio is unsafe"):
        service.restore(archive, confirmation=phrase)


def test_checkout_ledger_is_adopted_once_when_local_target_is_pristine(
    tmp_path: Path,
) -> None:
    legacy = QuantPaperService(
        db_path=tmp_path / "checkout" / "paper.db",
        config_path=tmp_path / "checkout" / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    legacy.analyze_and_queue(AnalysisRequest(market="us", symbol="NVDA"))
    target = QuantPaperService(
        db_path=tmp_path / "local" / "paper.db",
        config_path=tmp_path / "local" / "config.json",
        analysis_runner=lambda _request: _analysis(),
    )
    target_profile = target.status()["profile"]["profile_id"]

    first = migrate_legacy_runtime(
        legacy_db=legacy.store.path,
        legacy_config=legacy.config_path,
        target_db=target.store.path,
        target_config=target.config_path,
    )
    migrated = QuantStore(target.store.path)
    second = migrate_legacy_runtime(
        legacy_db=legacy.store.path,
        legacy_config=legacy.config_path,
        target_db=target.store.path,
        target_config=target.config_path,
    )

    assert first["database_migrated"] is True
    assert Path(first["preserved_target"]).is_file()
    assert migrated.profile()["profile_id"] != target_profile
    assert len(migrated.status()["signals"]) == 1
    assert second["already_checked"] is True
