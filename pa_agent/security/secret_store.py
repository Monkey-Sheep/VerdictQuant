"""OS-backed secret persistence for VerdictQuant."""
from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class SecretStoreUnavailable(RuntimeError):
    """Raised when secure secret persistence is unavailable."""


class SecretStore(Protocol):
    def get(self, name: str) -> str:
        """Return a secret or an empty string when it does not exist."""

    def set(self, name: str, value: str) -> None:
        """Persist a secret value."""

    def delete(self, name: str) -> None:
        """Delete a secret if it exists."""


class WindowsCredentialStore:
    """Store secrets in the current user's Windows Credential Manager."""

    _TARGET_PREFIX = "VerdictQuant/"
    _LEGACY_TARGET_PREFIX = "PA_Agent_Hardened/"

    @staticmethod
    def _win32cred():
        try:
            import win32cred
        except ImportError as exc:
            raise SecretStoreUnavailable(
                "pywin32 is required for Windows Credential Manager support"
            ) from exc
        return win32cred

    def _target(self, name: str) -> str:
        return f"{self._TARGET_PREFIX}{name}"

    def _legacy_target(self, name: str) -> str:
        return f"{self._LEGACY_TARGET_PREFIX}{name}"

    @staticmethod
    def _decode_blob(credential: dict) -> str:
        blob = credential.get("CredentialBlob", b"")
        if isinstance(blob, bytes):
            try:
                return blob.decode("utf-16-le").rstrip("\x00")
            except UnicodeDecodeError:
                return blob.decode("utf-8")
        return str(blob or "")

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        return getattr(exc, "winerror", None) == 1168

    def get(self, name: str) -> str:
        win32cred = self._win32cred()
        target = self._target(name)
        try:
            credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
            return self._decode_blob(credential)
        except Exception as exc:  # pywin32 exposes platform-specific errors
            if not self._is_not_found(exc):
                raise SecretStoreUnavailable(f"Unable to read credential {name!r}") from exc

        try:
            legacy = win32cred.CredRead(
                self._legacy_target(name), win32cred.CRED_TYPE_GENERIC, 0
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return ""
            raise SecretStoreUnavailable(f"Unable to read credential {name!r}") from exc

        value = self._decode_blob(legacy)
        if value:
            with suppress(SecretStoreUnavailable):
                self.set(name, value)
        return value

    def set(self, name: str, value: str) -> None:
        win32cred = self._win32cred()
        try:
            win32cred.CredWrite(
                {
                    "Type": win32cred.CRED_TYPE_GENERIC,
                    "TargetName": self._target(name),
                    "UserName": "VerdictQuant",
                    "CredentialBlob": value,
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                },
                0,
            )
        except Exception as exc:
            raise SecretStoreUnavailable(f"Unable to write credential {name!r}") from exc

    def delete(self, name: str) -> None:
        win32cred = self._win32cred()
        for target in (self._target(name), self._legacy_target(name)):
            try:
                win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise SecretStoreUnavailable(
                        f"Unable to delete credential {name!r}"
                    ) from exc


class DPAPIFileSecretStore:
    """Store DPAPI-encrypted values in a user-local JSON container.

    The file only contains ciphertext bound to the current Windows user. It is
    a recovery layer for machines where Credential Manager writes are blocked
    or become unavailable after packaging.
    """

    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path

    @staticmethod
    def _win32crypt():
        try:
            import win32crypt
        except ImportError as exc:
            raise SecretStoreUnavailable("pywin32 DPAPI support is unavailable") from exc
        return win32crypt

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreUnavailable("Unable to read encrypted secret backup") from exc
        if not isinstance(raw, dict) or raw.get("version") != self._FORMAT_VERSION:
            raise SecretStoreUnavailable("Encrypted secret backup has an invalid format")
        secrets = raw.get("secrets")
        if not isinstance(secrets, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in secrets.items()
        ):
            raise SecretStoreUnavailable("Encrypted secret backup has invalid entries")
        return dict(secrets)

    def _write(self, secrets: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        payload = {
            "version": self._FORMAT_VERSION,
            "protection": "windows-dpapi-current-user",
            "secrets": secrets,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise SecretStoreUnavailable("Unable to write encrypted secret backup") from exc

    def get(self, name: str) -> str:
        encoded = self._load().get(name, "")
        if not encoded:
            return ""
        try:
            encrypted = base64.b64decode(encoded, validate=True)
            _description, plaintext = self._win32crypt().CryptUnprotectData(
                encrypted, None, None, None, 0
            )
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise SecretStoreUnavailable(
                f"Unable to decrypt secret {name!r} for the current Windows user"
            ) from exc

    def set(self, name: str, value: str) -> None:
        try:
            encrypted = self._win32crypt().CryptProtectData(
                value.encode("utf-8"), "VerdictQuant", None, None, None, 0
            )
        except Exception as exc:
            raise SecretStoreUnavailable(f"Unable to encrypt secret {name!r}") from exc
        secrets = self._load()
        secrets[name] = base64.b64encode(encrypted).decode("ascii")
        self._write(secrets)

    def delete(self, name: str) -> None:
        secrets = self._load()
        if name not in secrets:
            return
        secrets.pop(name, None)
        self._write(secrets)


@dataclass
class ResilientSecretStore:
    """Mirror secrets to two secure stores and recover either copy."""

    primary: SecretStore
    fallback: SecretStore

    def get(self, name: str) -> str:
        primary_error: SecretStoreUnavailable | None = None
        try:
            value = self.primary.get(name)
            if value:
                try:
                    if self.fallback.get(name) != value:
                        self.fallback.set(name, value)
                except SecretStoreUnavailable:
                    logger.warning("Encrypted backup repair failed for %s", name)
                return value
        except SecretStoreUnavailable as exc:
            primary_error = exc

        try:
            value = self.fallback.get(name)
        except SecretStoreUnavailable as fallback_error:
            if primary_error is not None:
                raise SecretStoreUnavailable(
                    f"Both secure stores failed while reading {name!r}"
                ) from fallback_error
            raise

        if value:
            try:
                self.primary.set(name, value)
            except SecretStoreUnavailable:
                logger.warning("Credential Manager repair failed for %s", name)
        return value

    def set(self, name: str, value: str) -> None:
        errors: list[SecretStoreUnavailable] = []
        for store in (self.primary, self.fallback):
            try:
                store.set(name, value)
            except SecretStoreUnavailable as exc:
                errors.append(exc)
        if len(errors) == 2:
            raise SecretStoreUnavailable(
                f"Both secure stores failed while writing {name!r}"
            ) from errors[-1]
        if errors:
            logger.warning("One secure store failed while writing %s", name)

    def delete(self, name: str) -> None:
        errors: list[SecretStoreUnavailable] = []
        for store in (self.primary, self.fallback):
            try:
                store.delete(name)
            except SecretStoreUnavailable as exc:
                errors.append(exc)
        if errors:
            raise SecretStoreUnavailable(
                f"A secure store failed while deleting {name!r}"
            ) from errors[-1]


@dataclass
class MemorySecretStore:
    """In-memory implementation for tests and isolated validation."""

    values: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str:
        return self.values.get(name, "")

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def default_secret_store() -> SecretStore:
    """Return VerdictQuant's platform secret store."""
    from pa_agent.config.paths import USER_DATA_ROOT

    return ResilientSecretStore(
        primary=WindowsCredentialStore(),
        fallback=DPAPIFileSecretStore(
            USER_DATA_ROOT / "config" / "secrets.dpapi.json"
        ),
    )
