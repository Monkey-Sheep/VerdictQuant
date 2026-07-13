"""Safe lifecycle operations for the single local paper portfolio."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pa_agent.quant.models import PaperConfig
from pa_agent.quant.store import CURRENT_SCHEMA_VERSION, QuantStore

BACKUP_FORMAT = "verdictquant-paper-backup"
_LEGACY_BACKUP_FORMATS = {"pa-agent-quant-paper-backup"}
BACKUP_FORMAT_VERSION = 1
_BACKUP_FILES = {"manifest.json", "paper.db", "config.json"}
_MAX_BACKUP_FILE_SIZE = {
    "manifest.json": 1_000_000,
    "paper.db": 128_000_000,
    "config.json": 5_000_000,
}
_MAX_COMPRESSION_RATIO = 200


class PaperAccountMaintenanceError(RuntimeError):
    """Raised when a local account maintenance operation is unsafe."""


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_conn, closing(
        sqlite3.connect(destination)
    ) as target_conn:
        source_conn.backup(target_conn)


def _fold_wal(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise PaperAccountMaintenanceError("paper database integrity check failed")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")


def _activity_score(path: Path) -> int:
    if not path.exists():
        return -1
    try:
        with closing(sqlite3.connect(path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            score = 0
            for table in ("signals", "orders", "positions", "fills"):
                if table in tables:
                    score += int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return score
    except sqlite3.DatabaseError:
        return -1


def migrate_legacy_runtime(
    *,
    legacy_db: Path,
    legacy_config: Path,
    target_db: Path,
    target_config: Path,
    migrate_database: bool = True,
    migrate_config: bool = True,
) -> dict[str, Any]:
    """Adopt a checkout-local ledger only when it is safer than the target."""
    result: dict[str, Any] = {"database_migrated": False, "config_migrated": False}
    marker = target_db.parent / ".legacy-runtime-migration-v1.json"
    if marker.exists():
        result["already_checked"] = True
        return result
    database_eligible = (
        migrate_database
        and legacy_db.resolve() != target_db.resolve()
        and legacy_db.exists()
    )
    source_score = _activity_score(legacy_db) if database_eligible else -1
    target_score = _activity_score(target_db) if database_eligible else -1
    should_migrate = database_eligible and source_score >= 0 and (
        target_score < 0 or source_score > target_score
    )
    if should_migrate:
        target_db.parent.mkdir(parents=True, exist_ok=True)
        if target_db.exists():
            preserved = target_db.with_name(
                f"{target_db.stem}.pre-legacy-migration-{_timestamp()}{target_db.suffix}"
            )
            _sqlite_snapshot(target_db, preserved)
            result["preserved_target"] = str(preserved)
        with tempfile.TemporaryDirectory(dir=target_db.parent) as temp_dir:
            snapshot = Path(temp_dir) / "paper.db"
            _sqlite_snapshot(legacy_db, snapshot)
            _fold_wal(snapshot)
            os.replace(snapshot, target_db)
        result["database_migrated"] = True

    if (
        migrate_config
        and legacy_config.exists()
        and (should_migrate or not target_config.exists())
    ):
        payload = json.loads(legacy_config.read_text(encoding="utf-8"))
        PaperConfig.model_validate(payload)
        _atomic_write(
            target_config,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        result["config_migrated"] = True
    marker_payload = {
        **result,
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "legacy_database": str(legacy_db.resolve()),
        "target_database": str(target_db.resolve()),
    }
    _atomic_write(
        marker,
        (json.dumps(marker_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return result


class PaperAccountMaintenance:
    """Backup, restore, and reset one local paper portfolio."""

    def __init__(self, store: QuantStore, config_path: Path) -> None:
        self.store = store
        self.config_path = config_path.resolve()

    def confirmation_phrases(self) -> dict[str, str]:
        profile_id = self.store.profile()["profile_id"]
        return {
            "restore": f"RESTORE {profile_id}",
            "reset": f"RESET {profile_id}",
        }

    def backup(self, output: Path | None = None, *, reason: str = "manual") -> dict[str, Any]:
        profile = self.store.profile()
        destination = (
            output.expanduser().resolve()
            if output is not None
            else self.store.path.parent
            / "backups"
            / f"paper-{profile['profile_id'][:8]}-{_timestamp()}.zip"
        )
        if destination.suffix.lower() != ".zip":
            raise PaperAccountMaintenanceError("paper backup output must end with .zip")
        destination.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir:
            temp_root = Path(temp_dir)
            database = temp_root / "paper.db"
            _sqlite_snapshot(self.store.path, database)
            _fold_wal(database)
            config_bytes = self.config_path.read_bytes()
            PaperConfig.model_validate_json(config_bytes)
            if database.stat().st_size > _MAX_BACKUP_FILE_SIZE["paper.db"]:
                raise PaperAccountMaintenanceError(
                    "paper database is too large for the portable backup format"
                )
            if len(config_bytes) > _MAX_BACKUP_FILE_SIZE["config.json"]:
                raise PaperAccountMaintenanceError("paper config is too large to back up")
            database_bytes = database.read_bytes()
            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "paper_only": True,
                "created_at_utc": datetime.now(UTC).isoformat(),
                "reason": reason,
                "profile_id": profile["profile_id"],
                "schema_version": self.store.schema_version(),
                "files": {
                    "paper.db": _sha256(database_bytes),
                    "config.json": _sha256(config_bytes),
                },
            }
            manifest_bytes = (
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                archive.writestr("paper.db", database_bytes)
                archive.writestr("config.json", config_bytes)
            os.replace(temporary, destination)

        return {
            "paper_only": True,
            "status": "backed_up",
            "profile_id": profile["profile_id"],
            "schema_version": self.store.schema_version(),
            "backup": str(destination),
        }

    def restore(self, archive_path: Path, *, confirmation: str) -> dict[str, Any]:
        phrases = self.confirmation_phrases()
        if confirmation != phrases["restore"]:
            raise PaperAccountMaintenanceError(
                f"restore confirmation mismatch; expected: {phrases['restore']}"
            )
        archive_path = archive_path.expanduser().resolve()
        if not archive_path.is_file():
            raise PaperAccountMaintenanceError(f"paper backup does not exist: {archive_path}")

        current_profile = self.store.profile()["profile_id"]
        with tempfile.TemporaryDirectory(dir=self.store.path.parent) as temp_dir:
            temp_root = Path(temp_dir)
            manifest, config_bytes = self._validate_archive(archive_path, temp_root)
            restored_database = temp_root / "paper.db"
            restored_store = QuantStore(restored_database)
            restored_profile = restored_store.profile()["profile_id"]
            _fold_wal(restored_database)
            if restored_profile != manifest["profile_id"]:
                raise PaperAccountMaintenanceError(
                    "backup manifest profile does not match the paper database"
                )
            safety_backup = self.backup(reason="pre-restore")
            previous_config = self.config_path.read_bytes()
            try:
                _atomic_write(self.config_path, config_bytes)
                self._replace_database(restored_database)
            except Exception:
                _atomic_write(self.config_path, previous_config)
                raise

        return {
            "paper_only": True,
            "status": "restored",
            "previous_profile_id": current_profile,
            "profile_id": restored_profile,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "safety_backup": safety_backup["backup"],
        }

    def reset(self, config: PaperConfig, *, confirmation: str) -> dict[str, Any]:
        phrases = self.confirmation_phrases()
        if confirmation != phrases["reset"]:
            raise PaperAccountMaintenanceError(
                f"reset confirmation mismatch; expected: {phrases['reset']}"
            )
        previous_profile = self.store.profile()["profile_id"]
        safety_backup = self.backup(reason="pre-reset")
        with tempfile.TemporaryDirectory(dir=self.store.path.parent) as temp_dir:
            replacement = Path(temp_dir) / "paper.db"
            replacement_store = QuantStore(replacement)
            replacement_store.initialize_accounts(config)
            new_profile = replacement_store.profile()["profile_id"]
            _fold_wal(replacement)
            self._replace_database(replacement)
        return {
            "paper_only": True,
            "status": "reset",
            "previous_profile_id": previous_profile,
            "profile_id": new_profile,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "safety_backup": safety_backup["backup"],
        }

    def _validate_archive(
        self, archive_path: Path, temp_root: Path
    ) -> tuple[dict[str, Any], bytes]:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or set(names) != _BACKUP_FILES:
                    raise PaperAccountMaintenanceError(
                        "paper backup must contain only manifest.json, paper.db, and config.json"
                    )
                for info in archive.infolist():
                    limit = _MAX_BACKUP_FILE_SIZE[info.filename]
                    if info.file_size > limit:
                        raise PaperAccountMaintenanceError(
                            f"paper backup member is too large: {info.filename}"
                        )
                    if info.flag_bits & 0x1:
                        raise PaperAccountMaintenanceError(
                            f"encrypted paper backup members are not supported: {info.filename}"
                        )
                    if (
                        info.file_size > 1_000_000
                        and info.file_size / max(1, info.compress_size) > _MAX_COMPRESSION_RATIO
                    ):
                        raise PaperAccountMaintenanceError(
                            f"paper backup compression ratio is unsafe: {info.filename}"
                        )
                manifest_bytes = archive.read("manifest.json")
                database_bytes = archive.read("paper.db")
                config_bytes = archive.read("config.json")
        except zipfile.BadZipFile as exc:
            raise PaperAccountMaintenanceError("paper backup is not a valid zip archive") from exc

        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaperAccountMaintenanceError("paper backup manifest is invalid") from exc
        if manifest.get("format") not in {BACKUP_FORMAT, *_LEGACY_BACKUP_FORMATS}:
            raise PaperAccountMaintenanceError("unsupported paper backup format")
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise PaperAccountMaintenanceError("unsupported paper backup version")
        if manifest.get("paper_only") is not True:
            raise PaperAccountMaintenanceError("backup is not marked paper-only")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise PaperAccountMaintenanceError("paper backup hashes are missing")
        if files.get("paper.db") != _sha256(database_bytes):
            raise PaperAccountMaintenanceError("paper database hash mismatch")
        if files.get("config.json") != _sha256(config_bytes):
            raise PaperAccountMaintenanceError("paper config hash mismatch")
        try:
            PaperConfig.model_validate_json(config_bytes)
        except Exception as exc:
            raise PaperAccountMaintenanceError("paper backup config is invalid") from exc

        database = temp_root / "paper.db"
        database.write_bytes(database_bytes)
        try:
            store = QuantStore(database)
            profile = store.profile()
            _fold_wal(database)
        except (RuntimeError, sqlite3.DatabaseError) as exc:
            raise PaperAccountMaintenanceError("paper backup database is invalid") from exc
        if not isinstance(manifest.get("profile_id"), str) or not profile["profile_id"]:
            raise PaperAccountMaintenanceError("paper backup profile identity is invalid")
        return manifest, config_bytes

    def _replace_database(self, replacement: Path) -> None:
        _fold_wal(self.store.path)
        for suffix in ("-wal", "-shm"):
            self.store.path.with_name(self.store.path.name + suffix).unlink(missing_ok=True)
        os.replace(replacement, self.store.path)
