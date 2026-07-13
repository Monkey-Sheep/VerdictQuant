"""Stable, secret-free lineage for analysis and forecast evidence."""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any


def _project_version() -> str:
    for package in ("verdictquant", "pa-agent"):
        try:
            return version(package)
        except PackageNotFoundError:
            continue
    return "source-checkout"


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_analysis_provenance(record: Any) -> dict[str, Any]:
    """Return model, strategy, and exact-input lineage without any API key."""
    meta = getattr(record, "meta", None)
    provider = getattr(meta, "ai_provider", {}) or {}
    if not isinstance(provider, dict):
        provider = {}
    strategy_files = sorted(str(item) for item in (getattr(record, "strategy_files_used", []) or []))
    input_contract = {
        "stage1_messages": getattr(record, "stage1_messages", []) or [],
        "stage2_messages": getattr(record, "stage2_messages", []) or [],
        "strategy_files_used": strategy_files,
    }
    return {
        "pipeline": "verdictquant-guarded-two-stage",
        "pipeline_version": _project_version(),
        "provider": provider.get("provider"),
        "model": provider.get("model"),
        "decision_stance": getattr(meta, "decision_stance", None),
        "strategy_files_used": strategy_files,
        "analysis_input_sha256": _fingerprint(input_contract),
    }
