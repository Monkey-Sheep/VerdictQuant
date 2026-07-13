"""Unit tests for hardened settings persistence."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pa_agent.config.settings import Settings, load_settings, save_settings
from pa_agent.security.secret_store import MemorySecretStore, SecretStoreUnavailable


def test_defaults(tmp_path):
    path = tmp_path / "settings.json"
    store = MemorySecretStore()

    settings = load_settings(path, secret_store=store)

    assert settings.provider.model == "deepseek-v4-flash"
    assert settings.provider.base_url == "https://api.deepseek.com"
    assert settings.provider.thinking is True
    assert settings.provider.reasoning_effort == "high"
    assert settings.provider.context_window == 2_000_000
    assert settings.general.analysis_bar_count == 100
    assert path.exists()


def test_round_trip_keeps_secret_out_of_json(tmp_path):
    path = tmp_path / "settings.json"
    store = MemorySecretStore()
    original = Settings()
    original.provider.api_key = "sk-test-1234"
    original.updates.github_token = "github_pat_read_only"
    original.general.last_symbol = "BTCUSDT"

    save_settings(original, path, secret_store=store)
    loaded = load_settings(path, secret_store=store)

    assert loaded.provider.api_key == "sk-test-1234"
    assert loaded.updates.github_token == "github_pat_read_only"
    assert loaded.general.last_symbol == "XAUUSD"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in data["provider"]
    assert "github_token" not in data["updates"]
    assert "sk-test-1234" not in path.read_text(encoding="utf-8")
    assert "github_pat_read_only" not in path.read_text(encoding="utf-8")


def test_save_rejects_secret_store_that_cannot_read_back(tmp_path):
    class _BrokenStore(MemorySecretStore):
        def get(self, name: str) -> str:
            del name
            return ""

    settings = Settings()
    settings.provider.api_key = "must-survive-restart"

    with pytest.raises(SecretStoreUnavailable, match="read-back failed"):
        save_settings(
            settings,
            tmp_path / "settings.json",
            secret_store=_BrokenStore(),
        )


def test_all_supported_secrets_use_secure_store(tmp_path):
    path = tmp_path / "settings.json"
    store = MemorySecretStore()
    settings = Settings()
    settings.provider.api_key = "provider-secret"
    settings.feishu.webhook_url = "https://example.com/secret-hook"
    settings.feishu.secret = "feishu-secret"
    settings.feishu.app_secret = "app-secret"
    settings.pushplus.token = "pushplus-secret"
    settings.updates.github_token = "github-update-secret"

    save_settings(settings, path, secret_store=store)

    raw = path.read_text(encoding="utf-8")
    for secret in (
        "provider-secret",
        "secret-hook",
        "feishu-secret",
        "app-secret",
        "pushplus-secret",
        "github-update-secret",
    ):
        assert secret not in raw
    assert store.values["provider.api_key"] == "provider-secret"
    assert store.values["feishu.webhook_url"].endswith("secret-hook")
    assert store.values["pushplus.token"] == "pushplus-secret"
    assert store.values["updates.github_token"] == "github-update-secret"


def test_corrupt_json_returns_defaults_and_hydrates_secret(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = MemorySecretStore({"provider.api_key": "stored-key"})

    settings = load_settings(path, secret_store=store)

    assert settings.provider.model == "deepseek-v4-flash"
    assert settings.provider.api_key == "stored-key"


def test_missing_api_key_leaves_api_key_blank(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(Settings().model_dump()), encoding="utf-8")

    settings = load_settings(path, secret_store=MemorySecretStore())

    assert settings.provider.api_key == ""
    sanitized = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in sanitized["provider"]


def test_pushplus_auto_disabled_when_enabled_without_token(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"pushplus": {"enabled": true}}', encoding="utf-8")

    with patch.dict("os.environ", {}, clear=True):
        loaded = load_settings(path, secret_store=MemorySecretStore())

    assert loaded.pushplus.enabled is False
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pushplus"]["enabled"] is False


def test_migrate_legacy_plaintext_secrets(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "provider": {
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "legacy-provider-key",
                },
                "pushplus": {"enabled": False, "token": "legacy-push-token"},
            }
        ),
        encoding="utf-8",
    )
    store = MemorySecretStore()

    loaded = load_settings(path, secret_store=store)

    assert loaded.provider.api_key == "legacy-provider-key"
    assert loaded.pushplus.token == "legacy-push-token"
    raw = path.read_text(encoding="utf-8")
    assert "legacy-provider-key" not in raw
    assert "legacy-push-token" not in raw


def test_migrate_legacy_feishu_json(tmp_path):
    path = tmp_path / "settings.json"
    legacy = tmp_path / "feishu.json"
    store = MemorySecretStore()
    save_settings(Settings(), path, secret_store=store)
    legacy.write_text(
        json.dumps(
            {
                "enabled": True,
                "webhook_url": "https://example.com/legacy-hook",
                "secret": "legacy-secret",
                "app_id": "cli_legacy",
                "app_secret": "legacy-app-secret",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_settings(path, secret_store=store)

    assert loaded.feishu.webhook_url.endswith("legacy-hook")
    assert loaded.feishu.secret == "legacy-secret"
    assert loaded.feishu.app_id == "cli_legacy"
    raw = path.read_text(encoding="utf-8")
    assert "legacy-hook" not in raw
    assert "legacy-secret" not in raw


def test_legacy_tushare_config_migrates_to_akshare_and_drops_token(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "general": {"last_data_source": "tushare"},
                "tushare": {"token": "legacy-tushare-token"},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_settings(path, secret_store=MemorySecretStore())

    assert loaded.general.last_data_source == "akshare"
    assert loaded.tushare.token == ""
    assert "legacy-tushare-token" not in path.read_text(encoding="utf-8")


def test_legacy_mt5_default_migrates_to_anonymous_tradingview(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "general": {
                    "last_data_source": "mt5",
                    "last_symbol": "XAUUSDm",
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = load_settings(path, secret_store=MemorySecretStore())

    assert loaded.general.last_data_source == "tradingview"
    assert loaded.general.last_symbol == "XAUUSD"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["general"]["last_data_source"] == "tradingview"


def test_unsafe_legacy_provider_is_reset_and_credential_removed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "provider": {
                    "model": "openclaw_wb",
                    "base_url": "https://copilot.tencent.com/v2",
                    "api_key": "connector-token",
                }
            }
        ),
        encoding="utf-8",
    )
    store = MemorySecretStore({"provider.api_key": "previous-token"})

    loaded = load_settings(path, secret_store=store)

    assert loaded.provider.model == "deepseek-v4-flash"
    assert loaded.provider.base_url == "https://api.deepseek.com"
    assert loaded.provider.api_key == ""
    assert "provider.api_key" not in store.values
