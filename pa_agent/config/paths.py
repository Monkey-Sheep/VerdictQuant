"""Centralised path constants for VerdictQuant.

Import this module everywhere instead of hard-coding paths. Quant paper state is
always user-local so source checkouts and packaged releases share one durable
paper portfolio instead of silently creating a ledger per checkout.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _platform_data_root(product_dir: str) -> Path:
    """Return the conventional per-user data directory for this platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / product_dir
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / product_dir
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / product_dir.lower()


def _user_data_root() -> Path:
    override = (
        os.environ.get("VERDICTQUANT_DATA_HOME", "").strip()
        or os.environ.get("PA_AGENT_DATA_HOME", "").strip()
    )
    if override:
        return Path(override).expanduser()

    current = _platform_data_root("VerdictQuant")
    legacy = _platform_data_root("PAAgentQuant")
    if current.exists() or not legacy.exists():
        return current

    # Preserve existing local paper accounts across the public product rename.
    # This is copy-only: the legacy directory is never modified or deleted.
    try:
        shutil.copytree(legacy, current)
    except OSError:
        return legacy
    return current


USER_DATA_ROOT: Path = _user_data_root()


# Source runs keep state in the checkout. Frozen releases keep bundled assets
# read-only and write each user's secrets, ledgers, and logs under LocalAppData.
if getattr(sys, "frozen", False):
    PROJECT_ROOT: Path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    RUNTIME_ROOT: Path = USER_DATA_ROOT
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    RUNTIME_ROOT = PROJECT_ROOT

# ── Prompt engineering assets (read-only at runtime) ─────────────────────────
PROMPT_DIR: Path = PROJECT_ROOT / "prompt_engineering"

# Alias kept for backward compat with design doc
PA_AGENT_DIR: Path = PROJECT_ROOT

# ── Runtime write directories ─────────────────────────────────────────────────
RECORDS_PENDING_DIR: Path = RUNTIME_ROOT / "records" / "pending"
FORECASTS_DIR: Path = RUNTIME_ROOT / "records" / "forecasts"
FORECASTS_PENDING_DIR: Path = FORECASTS_DIR / "pending"
FORECASTS_EVALUATED_DIR: Path = FORECASTS_DIR / "evaluated"
# The quant ledger is deliberately outside the source checkout in every mode.
# Legacy PA_AGENT_QUANT_HOME remains a compatibility escape hatch.
QUANT_HOME: Path = Path(
    os.environ.get("VERDICTQUANT_QUANT_HOME", "").strip()
    or os.environ.get("PA_AGENT_QUANT_HOME", "").strip()
    or str(USER_DATA_ROOT)
).expanduser()
QUANT_DIR: Path = QUANT_HOME / "records" / "quant"
QUANT_DB_PATH: Path = QUANT_DIR / "paper.db"
LEGACY_QUANT_DIR: Path = PROJECT_ROOT / "records" / "quant"
LEGACY_QUANT_DB_PATH: Path = LEGACY_QUANT_DIR / "paper.db"
EXPERIENCE_DIR: Path = RUNTIME_ROOT / "experience"
CONFIG_DIR: Path = RUNTIME_ROOT / "config"
LOGS_DIR: Path = RUNTIME_ROOT / "logs"

# ── Individual file paths ─────────────────────────────────────────────────────
FEISHU_JSON_LEGACY_PATH: Path = CONFIG_DIR / "feishu.json"
SETTINGS_JSON_PATH: Path = CONFIG_DIR / "settings.json"
LOG_FILE_PATH: Path = LOGS_DIR / "pa_agent.log"
CRASH_LOG_PATH: Path = LOGS_DIR / "crash.log"
