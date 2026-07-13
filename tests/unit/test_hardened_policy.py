"""Security policy regression tests."""
from __future__ import annotations

import pytest

from pa_agent.security.policy import (
    ensure_provider_allowed,
    provider_configuration_error,
)


@pytest.mark.parametrize("model", ["openclaw", "openclaw/main", "openclaw_cs", "openclaw_wb"])
def test_disabled_connector_aliases(model: str) -> None:
    assert provider_configuration_error(model, "http://127.0.0.1:51187/v1")


def test_workbuddy_host_is_disabled_even_with_normal_model() -> None:
    assert provider_configuration_error(
        "some-model", "https://copilot.tencent.com/v2"
    )


def test_workbuddy_subdomain_is_disabled() -> None:
    assert provider_configuration_error(
        "some-model", "https://api.copilot.tencent.com/v2"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.deepseek.com",
        "https://provider.example/v1",
        "http://localhost:8080/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
    ],
)
def test_allowed_transports(base_url: str) -> None:
    ensure_provider_allowed("model-name", base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "ftp://provider.example/v1",
        "https://user:secret@provider.example/v1",
        "https://provider.example/v1?token=secret",
        "https://provider.example:invalid/v1",
        "https://provider.example\\@evil.example/v1",
        "https://provider.example/v1 with-space",
    ],
)
def test_unsafe_urls_are_rejected(base_url: str) -> None:
    with pytest.raises(ValueError):
        ensure_provider_allowed("model-name", base_url)
