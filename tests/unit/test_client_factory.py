"""Tests for hardened AI client routing."""
from __future__ import annotations

import pytest

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.config.settings import AIProviderSettings


@pytest.mark.parametrize("model", ["openclaw", "openclaw_cs", "openclaw_wb/auto"])
def test_agent_connector_routes_are_rejected(model: str) -> None:
    settings = AIProviderSettings(
        model=model,
        base_url="http://127.0.0.1:19000/v1",
        api_key="connector-token",
    )

    with pytest.raises(ValueError, match="disabled"):
        create_ai_client(settings)


def test_regular_https_provider_uses_deepseek_client() -> None:
    settings = AIProviderSettings(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key="test",
    )

    client = create_ai_client(settings)

    assert isinstance(client, DeepSeekClient)
