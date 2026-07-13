from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.config import paths
from pa_agent.security.secret_store import (
    DPAPIFileSecretStore,
    MemorySecretStore,
    ResilientSecretStore,
    SecretStoreUnavailable,
    WindowsCredentialStore,
)


class _CredentialNotFound(Exception):
    winerror = 1168


class _FakeWin32Cred:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

    def CredRead(self, target: str, _credential_type: int, _flags: int) -> dict:
        try:
            return self.values[target]
        except KeyError as exc:
            raise _CredentialNotFound from exc

    def CredWrite(self, credential: dict, _flags: int) -> None:
        self.values[credential["TargetName"]] = credential

    def CredDelete(self, target: str, _credential_type: int, _flags: int) -> None:
        try:
            del self.values[target]
        except KeyError as exc:
            raise _CredentialNotFound from exc


def test_verdictquant_data_home_takes_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current"
    legacy_override = tmp_path / "legacy-override"
    monkeypatch.setenv("VERDICTQUANT_DATA_HOME", str(current))
    monkeypatch.setenv("PA_AGENT_DATA_HOME", str(legacy_override))

    assert paths._user_data_root() == current


def test_default_data_root_copies_legacy_account_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERDICTQUANT_DATA_HOME", raising=False)
    monkeypatch.delenv("PA_AGENT_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths.sys, "platform", "win32")
    legacy = tmp_path / "PAAgentQuant"
    legacy.mkdir()
    (legacy / "paper-marker.txt").write_text("existing-paper-account", encoding="utf-8")

    current = paths._user_data_root()

    assert current == tmp_path / "VerdictQuant"
    assert (current / "paper-marker.txt").read_text(encoding="utf-8") == "existing-paper-account"
    assert (legacy / "paper-marker.txt").is_file()


def test_windows_secret_store_migrates_legacy_target_on_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeWin32Cred()
    fake.values["PA_Agent_Hardened/provider.api_key"] = {
        "CredentialBlob": "legacy-secret".encode("utf-16-le")
    }
    store = WindowsCredentialStore()
    monkeypatch.setattr(store, "_win32cred", lambda: fake)

    assert store.get("provider.api_key") == "legacy-secret"
    assert "VerdictQuant/provider.api_key" in fake.values
    assert fake.values["VerdictQuant/provider.api_key"]["UserName"] == "VerdictQuant"


def test_windows_secret_store_delete_clears_current_and_legacy_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeWin32Cred()
    fake.values["VerdictQuant/provider.api_key"] = {"CredentialBlob": b"current"}
    fake.values["PA_Agent_Hardened/provider.api_key"] = {"CredentialBlob": b"legacy"}
    store = WindowsCredentialStore()
    monkeypatch.setattr(store, "_win32cred", lambda: fake)

    store.delete("provider.api_key")

    assert fake.values == {}


class _FakeDPAPI:
    @staticmethod
    def CryptProtectData(
        value: bytes,
        _description: str,
        _entropy,
        _reserved,
        _prompt,
        _flags: int,
    ) -> bytes:
        return b"encrypted:" + value[::-1]

    @staticmethod
    def CryptUnprotectData(
        value: bytes,
        _entropy,
        _reserved,
        _prompt,
        _flags: int,
    ) -> tuple[str, bytes]:
        assert value.startswith(b"encrypted:")
        return "VerdictQuant", value.removeprefix(b"encrypted:")[::-1]


def test_dpapi_secret_survives_new_store_instance_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.dpapi.json"
    first = DPAPIFileSecretStore(path)
    monkeypatch.setattr(first, "_win32crypt", lambda: _FakeDPAPI)

    first.set("provider.api_key", "sk-restart-test")

    assert "sk-restart-test" not in path.read_text(encoding="utf-8")
    second = DPAPIFileSecretStore(path)
    monkeypatch.setattr(second, "_win32crypt", lambda: _FakeDPAPI)
    assert second.get("provider.api_key") == "sk-restart-test"


def test_resilient_store_recovers_from_fallback_and_repairs_primary() -> None:
    primary = MemorySecretStore()
    fallback = MemorySecretStore({"provider.api_key": "restored-key"})
    store = ResilientSecretStore(primary=primary, fallback=fallback)

    assert store.get("provider.api_key") == "restored-key"
    assert primary.get("provider.api_key") == "restored-key"


def test_resilient_store_mirrors_existing_primary_to_fallback() -> None:
    primary = MemorySecretStore({"provider.api_key": "existing-key"})
    fallback = MemorySecretStore()
    store = ResilientSecretStore(primary=primary, fallback=fallback)

    assert store.get("provider.api_key") == "existing-key"
    assert fallback.get("provider.api_key") == "existing-key"


def test_resilient_store_writes_when_primary_is_unavailable() -> None:
    class _UnavailableStore(MemorySecretStore):
        def set(self, name: str, value: str) -> None:
            del name, value
            raise SecretStoreUnavailable("blocked")

    fallback = MemorySecretStore()
    store = ResilientSecretStore(primary=_UnavailableStore(), fallback=fallback)

    store.set("provider.api_key", "fallback-key")

    assert fallback.get("provider.api_key") == "fallback-key"
