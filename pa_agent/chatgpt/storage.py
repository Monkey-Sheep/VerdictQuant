"""Non-secret account metadata and serialized, encrypted credential generations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from pa_agent.chatgpt.oauth import ChatGPTError, check_cancelled
from pa_agent.security.secret_store import (
    DPAPIFileSecretStore,
    ResilientSecretStore,
    SecretStoreUnavailable,
    WindowsCredentialStore,
)


class SessionStore:
    def __init__(self, data_root: Path | None = None, secret_store=None):
        if data_root is None:
            from pa_agent.config.paths import USER_DATA_ROOT

            data_root = USER_DATA_ROOT
        self.root = Path(data_root).expanduser().resolve() / "chatgpt"
        self.secrets = (
            secret_store
            if secret_store is not None
            else ResilientSecretStore(
                primary=WindowsCredentialStore(),
                fallback=DPAPIFileSecretStore(self.root / "credentials.dpapi.json"),
            )
        )
        self.db_path = self.root / "accounts.sqlite3"

    @contextmanager
    def locked(self, cancelled=None):
        """BEGIN IMMEDIATE serializes refresh/read/write across processes."""
        db = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(self.db_path, timeout=0.1)
            deadline = time.monotonic() + 35
            while True:
                check_cancelled(cancelled)
                try:
                    db.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts (profile_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('host_id', ?)",
                ("urn:uuid:" + str(uuid.uuid4()),),
            )
            db.execute("INSERT OR IGNORE INTO settings VALUES ('active', '')")
            if os.name != "nt":
                self.db_path.chmod(0o600)
            yield db
            db.commit()
        except (sqlite3.Error, OSError, SecretStoreUnavailable):
            if db is not None:
                db.rollback()
            raise ChatGPTError(
                "ChatGPT 本机会话暂不可用，请稍后重试。", "storage_unavailable"
            ) from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def setting(db, key):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def set_setting(db, key, value):
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    @staticmethod
    def account(db, profile_id):
        row = db.execute(
            "SELECT metadata FROM accounts WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise ValueError("Invalid account")
            return value
        except (ValueError, TypeError):
            raise ChatGPTError("ChatGPT 本机账户记录损坏。", "storage_unavailable") from None

    @staticmethod
    def put(db, account):
        db.execute(
            "INSERT OR REPLACE INTO accounts VALUES (?, ?)",
            (account["profile_id"], json.dumps(account, ensure_ascii=True)),
        )

    def snapshot(self):
        if not self.db_path.exists():
            return "", []
        try:
            with closing(
                sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as db:
                active = self.setting(db, "active")
                accounts = [
                    self.account(db, row[0])
                    for row in db.execute("SELECT profile_id FROM accounts ORDER BY rowid")
                ]
                return active, accounts
        except sqlite3.Error:
            raise ChatGPTError("无法读取 ChatGPT 本机账户。", "storage_unavailable") from None

    def credentials(self, account):
        reference = account.get("credential_ref", "")
        if not reference:
            return {}
        try:
            raw = self.secrets.get(reference)
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                raise ValueError("Invalid credential record")
            return value
        except (SecretStoreUnavailable, ValueError, TypeError):
            raise ChatGPTError(
                "无法读取 ChatGPT 安全凭据，请重新登录。", "storage_unavailable"
            ) from None

    def replace_credentials(self, account, credentials):
        """Publish a unique immutable secret, then commit its pointer with metadata.

        A stale primary store cannot overshadow a newly rotated fallback token:
        the new generation never reuses the old credential name. The SQLite
        transaction publishes the pointer only after the secure write succeeds.
        """
        previous = account.get("credential_ref", "")
        namespace = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        reference = f"chatgpt/{namespace}/{account['profile_id']}/{uuid.uuid4().hex}"
        self.secrets.set(reference, json.dumps(credentials, ensure_ascii=True))
        account["credential_ref"] = reference
        return previous

    def delete_secret(self, reference):
        if reference:
            with suppress(SecretStoreUnavailable, ChatGPTError), self.locked():
                self.secrets.delete(reference)
