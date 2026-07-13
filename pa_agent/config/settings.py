"""Pydantic settings models for VerdictQuant."""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pa_agent.security.policy import (
    DEFAULT_PROVIDER_BASE_URL,
    DEFAULT_PROVIDER_MODEL,
    provider_configuration_error,
)
from pa_agent.security.secret_store import (
    SecretStore,
    SecretStoreUnavailable,
    default_secret_store,
)

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal["mt5", "tradingview", "akshare", "eastmoney"]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""

    model_config = ConfigDict(extra="ignore")

    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    api_key_encrypted: str = ""
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "high"
    context_window: int = 2_000_000


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""

    model_config = ConfigDict(extra="ignore")

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    experience_max_entries: int = Field(default=0, ge=0, le=10)
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""

    model_config = ConfigDict(extra="ignore")

    normalization_mode: NormalizationMode = "strict"
    #: Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default.
    stage1_coherence_checks: bool = False
    #: Stage-2 trace / diagnosis cross-checks (not order safety). Off by default.
    stage2_coherence_checks: bool = False
    trace_semantic_checks: bool = False
    strict_bar_by_bar_features: bool = False
    #: Disable Stage 1 truncated JSON tail repair before syntax validation.
    disable_truncation_repair: bool = True
    #: Re-call API with structured feedback when validation fails (format errors).
    retry_enabled: bool = True
    retry_max: int = Field(default=3, ge=0, le=5)
    #: Max retries for category=c semantic errors (subset only).
    retry_max_semantic: int = Field(default=1, ge=0, le=3)
    retry_stage2: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""

    model_config = ConfigDict(extra="ignore")

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 80.0
    last_data_source: DataSourceKind = "tradingview"
    #: A-share K-line adjust for East Money / Baostock (qfq=前复权)
    kline_adjust: Literal["qfq", "hfq", "none"] = "qfq"
    #: TradingView 交易所；空字符串 =（自动）依次探测预设列表
    last_tradingview_exchange: str = ""
    last_symbol: str = "XAUUSD"
    last_timeframe: str = "15m"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    #: 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示）
    alert_on_order_opportunity: bool = True
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=600, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=11, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis
    cancel_keep_analysis_on_retry: bool = False
    #: 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情）
    decision_confidence_threshold: int = Field(default=40, ge=0, le=100)
    #: 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token
    enable_next_bar_prediction: bool = False
    #: 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘）
    structure_flip_cooldown_bars: int = Field(default=3, ge=1, le=50)

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        if v == "yfinance":
            return "tradingview"
        if v == "mt5":
            return "tradingview"
        if v in ("adata", "a_share"):
            return "akshare"
        if v == "eastmoney":
            return "eastmoney"
        if v == "tushare":
            return "akshare"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


_FEISHU_CONFIG_KEYS = (
    "enabled",
    "webhook_url",
    "secret",
    "app_id",
    "app_secret",
    "notify_on_order_only",
)


class FeishuSettings(BaseModel):
    """Feishu bot notification settings (persisted in settings.json)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    #: True = only push when there is an order opportunity.
    notify_on_order_only: bool = True


class TushareSettings(BaseModel):
    """Legacy Tushare settings retained only for config compatibility."""

    model_config = ConfigDict(extra="ignore")

    token: str = ""


class PushPlusSettings(BaseModel):
    """PushPlus notification settings (settings.json only; no GUI)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""


class UpdateSettings(BaseModel):
    """Application update preferences and optional private-repository token."""

    model_config = ConfigDict(extra="ignore")

    auto_check: bool = True
    auto_download: bool = True
    github_token: str = ""


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""

    model_config = ConfigDict(extra="ignore")

    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    pushplus: PushPlusSettings = Field(default_factory=PushPlusSettings)
    tushare: TushareSettings = Field(default_factory=TushareSettings)
    updates: UpdateSettings = Field(default_factory=UpdateSettings)


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

_SECRET_FIELDS: dict[str, tuple[str, str]] = {
    "provider.api_key": ("provider", "api_key"),
    "feishu.webhook_url": ("feishu", "webhook_url"),
    "feishu.secret": ("feishu", "secret"),
    "feishu.app_secret": ("feishu", "app_secret"),
    "pushplus.token": ("pushplus", "token"),
    "updates.github_token": ("updates", "github_token"),
}

_SECRET_JSON_EXCLUDE = {
    "provider": {"api_key", "api_key_encrypted"},
    "feishu": {"webhook_url", "secret", "app_secret"},
    "pushplus": {"token"},
    "tushare": {"token"},
    "updates": {"github_token"},
}


def _secret_store(store: SecretStore | None) -> SecretStore:
    return store if store is not None else default_secret_store()


def _settings_secret_value(settings: "Settings", section: str, field_name: str) -> str:
    return str(getattr(getattr(settings, section), field_name, "") or "")


def _set_settings_secret(settings: "Settings", section: str, field_name: str, value: str) -> None:
    setattr(getattr(settings, section), field_name, value)


def _persist_secrets(settings: "Settings", store: SecretStore) -> None:
    values = {
        name: _settings_secret_value(settings, *path) for name, path in _SECRET_FIELDS.items()
    }
    try:
        for name, value in values.items():
            if value:
                store.set(name, value)
                if not hmac.compare_digest(store.get(name), value):
                    raise SecretStoreUnavailable(f"Secure secret read-back failed for {name!r}")
            else:
                store.delete(name)
                if store.get(name):
                    raise SecretStoreUnavailable(
                        f"Secure secret deletion check failed for {name!r}"
                    )
    except SecretStoreUnavailable:
        if any(values.values()):
            raise
        logger.debug("Secure secret store unavailable; no non-empty secrets to persist")


def _hydrate_secrets(
    settings: "Settings",
    store: SecretStore,
    *,
    skip: set[str] | None = None,
) -> None:
    skip = skip or set()
    try:
        for name, path in _SECRET_FIELDS.items():
            if name in skip:
                continue
            _set_settings_secret(settings, *path, store.get(name))
    except SecretStoreUnavailable as exc:
        logger.warning("Secure secret store unavailable; credentials were not loaded: %s", exc)


def _extract_plaintext_secrets(raw: dict) -> tuple[dict[str, str], bool]:
    extracted: dict[str, str] = {}
    dirty = False
    for name, (section, field_name) in _SECRET_FIELDS.items():
        section_raw = raw.get(section)
        if not isinstance(section_raw, dict) or field_name not in section_raw:
            continue
        value = str(section_raw.pop(field_name) or "")
        if value:
            extracted[name] = value
        dirty = True

    provider = raw.get("provider")
    if isinstance(provider, dict) and "api_key_encrypted" in provider:
        provider.pop("api_key_encrypted", None)
        dirty = True

    tushare = raw.get("tushare")
    if isinstance(tushare, dict) and "token" in tushare:
        tushare.pop("token", None)
        dirty = True
    return extracted, dirty


def _sanitize_provider(raw: dict) -> bool:
    provider = raw.setdefault("provider", {})
    if not isinstance(provider, dict):
        raw["provider"] = {}
        provider = raw["provider"]
    model = str(provider.get("model") or DEFAULT_PROVIDER_MODEL)
    base_url = str(provider.get("base_url") or DEFAULT_PROVIDER_BASE_URL)
    error = provider_configuration_error(model, base_url)
    if not error:
        return False
    logger.warning("Unsafe provider settings reset to hardened defaults: %s", error)
    provider["model"] = DEFAULT_PROVIDER_MODEL
    provider["base_url"] = DEFAULT_PROVIDER_BASE_URL
    provider.pop("api_key", None)
    provider.pop("api_key_encrypted", None)
    return True


def _migrate_legacy_feishu_json(raw: dict, settings_path: Path) -> bool:
    """Merge legacy config/feishu.json into settings.feishu when needed."""
    legacy_path = settings_path.parent / "feishu.json"
    if not legacy_path.exists():
        return False

    feishu = raw.setdefault("feishu", {})
    if (feishu.get("webhook_url") or "").strip():
        return False

    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("legacy feishu.json unreadable (%s); skipping migration", exc)
        return False

    migrated = False
    for key in _FEISHU_CONFIG_KEYS:
        if key not in legacy:
            continue
        value = legacy.get(key)
        if value in (None, ""):
            continue
        if feishu.get(key) in (None, ""):
            feishu[key] = value
            migrated = True
    if migrated:
        logger.info("Migrated Feishu config from %s into settings.json", legacy_path)
    return migrated


def load_settings(
    path: Path | None = None,
    *,
    secret_store: SecretStore | None = None,
) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    store = _secret_store(secret_store)

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path, secret_store=store)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        defaults = Settings()
        _hydrate_secrets(defaults, store)
        return defaults

    if not isinstance(raw, dict):
        logger.warning("settings.json root is not an object; using defaults")
        defaults = Settings()
        _hydrate_secrets(defaults, store)
        return defaults

    # Migrate legacy field names
    general = raw.get("general", {})
    migrated_data_source = False
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    if general.get("last_data_source") in {"mt5", "yfinance"}:
        general["last_data_source"] = "tradingview"
        migrated_data_source = True
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider_reset = _sanitize_provider(raw)
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    migrated_feishu = _migrate_legacy_feishu_json(raw, path)
    plaintext_secrets, migrated_secrets = _extract_plaintext_secrets(raw)
    if provider_reset:
        plaintext_secrets.pop("provider.api_key", None)
    settings = Settings.model_validate(raw)
    _hydrate_secrets(
        settings,
        store,
        skip={"provider.api_key"} if provider_reset else None,
    )
    for name, value in plaintext_secrets.items():
        _set_settings_secret(settings, *_SECRET_FIELDS[name], value)

    dirty = migrated_feishu or migrated_secrets or provider_reset or migrated_data_source
    if settings.pushplus.enabled and not settings.pushplus.token.strip():
        if not (os.environ.get("PUSHPLUS_TOKEN") or "").strip():
            settings.pushplus.enabled = False
            logger.info(
                "PushPlus enabled but token empty — auto-disabled (Feishu notifications unaffected)"
            )
            dirty = True
    if dirty:
        save_settings(settings, path, secret_store=store)
    return settings


def save_settings(
    settings: "Settings",
    path: Path | None = None,
    *,
    secret_store: SecretStore | None = None,
) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.security.policy import ensure_provider_allowed

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_provider_allowed(settings.provider.model, settings.provider.base_url)
    store = _secret_store(secret_store)
    _persist_secrets(settings, store)

    data = settings.model_dump(exclude=_SECRET_JSON_EXCLUDE)

    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
