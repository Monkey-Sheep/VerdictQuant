"""Construct the correct AI client for the configured provider route."""

from __future__ import annotations

import logging

from pa_agent.config.settings import AIProviderSettings
from pa_agent.security.policy import ensure_provider_allowed


def create_ai_client(
    settings: AIProviderSettings,
    logger_: logging.Logger | None = None,
) -> object:
    """Return an OpenAI-compatible client after enforcing hardened policy."""
    log = logger_ or logging.getLogger(__name__)
    ensure_provider_allowed(settings.model, settings.base_url)

    from pa_agent.ai.deepseek_client import DeepSeekClient

    log.info(
        "AI client route: OpenAI-compatible (model=%s base_url=%s)",
        settings.model,
        settings.base_url or "(empty)",
    )
    return DeepSeekClient(settings=settings, logger_=log)
