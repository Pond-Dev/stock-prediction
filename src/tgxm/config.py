"""Validated, non-secret configuration for the Telegram-to-XM bot.

Only *names* of environment variables are persisted for credentials and
account allowlists.  Secret values are resolved by the integration boundary at
runtime and must never be written by this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


CONFIG_VERSION = 2
CONFIG_PATH = Path("config/settings.local.json")
EXAMPLE_CONFIG_PATH = Path("config/settings.example.json")

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9._-]{0,31}$")
_SEMVER_RE = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")

_PARSERS = frozenset(
    {"compact_gold_v1", "suggested_trade_v1", "narrative_signal_v1"}
)
_RUNTIME_MODES = frozenset({"observe", "shadow", "demo_armed"})
_TWO_LEVEL_SEMANTICS = frozenset({"zone_single_market", "manual_review"})
_ENTRY_MODES = frozenset({"zone_single_market", "manual_review"})
_TP_STRATEGIES = frozenset({"single_tp"})
_TIMEFRAMES = frozenset({"M1", "M5", "M15", "M30", "H1", "H4", "D1"})


class ConfigError(ValueError):
    """Raised when persisted configuration is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: str = "observe"
    require_single_instance: bool = True


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    api_id_env: str = "TGXM_TELEGRAM_API_ID"
    api_hash_env: str = "TGXM_TELEGRAM_API_HASH"
    session_env: str = "TGXM_TELEGRAM_SESSION"
    allowed_admin_ids_env: str = "TGXM_TELEGRAM_ADMIN_IDS"
    alert_chat_id_env: str = "TGXM_TELEGRAM_ALERT_CHAT_ID"
    catchup_overlap_messages: int = 50
    max_future_message_skew_seconds: int = 30


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    adapter: str = "xm_webtrader"
    terminal_path: str = ""
    allowed_demo_accounts_env: str = "TGXM_ALLOWED_DEMO_ACCOUNTS"
    allowed_servers_env: str = "TGXM_ALLOWED_DEMO_SERVERS"
    require_demo: bool = True
    max_tick_age_seconds: int = 5
    webtrader_url: str = "https://mt5.xm.com/?lang=en"
    webtrader_allowed_origins: tuple[str, ...] = (
        "https://mt5.xm.com",
        "https://mt5-1.xm-bz.com",
    )
    webtrader_profile_path: str = "state/webtrader-profile"
    webtrader_browser_channel: str = "chromium"
    webtrader_headless: bool = False
    webtrader_timeout_seconds: int = 20
    webtrader_readback_seconds: int = 15
    webtrader_max_price_drift_points: int = 2
    webtrader_selector_contract: str = "xm-mt5-web-v1"
    webtrader_require_hedging: bool = True


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    peer_id: int | None = None
    parser: str = "compact_gold_v1"
    enabled: bool = False
    trade_enabled: bool = False
    allowed_symbols: tuple[str, ...] = ()
    two_level_semantics: str = "zone_single_market"
    entry_mode: str = "zone_single_market"
    tp_strategy: str = "single_tp"
    tp_index: int = 1
    signal_expiry_minutes: int = 30
    max_spread_points: int = 100
    required_markers: tuple[str, ...] = ()
    ignored_markers: tuple[str, ...] = ()
    reject_open_sl: bool = True
    profile_version: str = "1.0.0"

    @classmethod
    def disabled_default(cls) -> ChannelProfile:
        """Return a safe template for a newly added channel profile."""

        return cls()


@dataclass(frozen=True, slots=True)
class RiskConfig:
    mode: str = "fixed_lot"
    fixed_lot: float = 0.01
    risk_percent: float | None = None
    daily_loss_limit_percent: float | None = None
    hard_lot_cap: float = 0.01
    max_active_per_symbol: int = 1
    max_active_per_channel: int = 1
    max_total_bot_positions: int = 1
    manual_exposure_policy: str = "block"
    same_side_conflict_policy: str = "block"
    opposite_side_conflict_policy: str = "block"
    require_numeric_sl: bool = True


@dataclass(frozen=True, slots=True)
class IndicatorConfig:
    """Settings for the read-only, advisory ``tgxm predict`` command.

    This never feeds the Telegram-to-XM order pipeline; see the
    ``signal-authority`` rule.  It only shapes an EMA/RSI/ATR rule evaluated
    over MT5 candle history that a human reads before deciding anything.
    """

    symbol: str = "GOLD"
    timeframe: str = "M15"
    lookback_bars: int = 300
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    rsi_period: int = 14
    rsi_overbought: int = 70
    rsi_oversold: int = 30
    atr_period: int = 14
    atr_stop_loss_multiplier: float = 1.5
    atr_take_profit_multipliers: tuple[float, ...] = (1.5, 3.0)
    max_bar_age_multiplier: int = 3


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    order_send_retries: int = 0
    reconcile_on_ambiguous_result: bool = True
    verify_broker_side_protection: bool = True
    management_action_policy: str = "notify_only"
    deviation_points: int = 20
    market_poll_seconds: float = 1.0


def _default_channels() -> dict[str, ChannelProfile]:
    return {
        "mr_charlie": ChannelProfile(
            enabled=True,
            allowed_symbols=("GOLD",),
        ),
        "vip_gold": ChannelProfile(
            parser="compact_gold_v1",
            allowed_symbols=("GOLD",),
        ),
        "united_signals": ChannelProfile(
            parser="suggested_trade_v1",
            two_level_semantics="manual_review",
            entry_mode="manual_review",
            required_markers=("SUGGESTED TRADE",),
        ),
        "anabel_signals": ChannelProfile(
            parser="narrative_signal_v1",
            two_level_semantics="manual_review",
            entry_mode="manual_review",
            required_markers=("(signal)",),
            ignored_markers=("(forecast)",),
        ),
    }


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_version: int = CONFIG_VERSION
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    symbol_aliases: dict[str, str] = field(
        default_factory=lambda: {"GOLD": "GOLD", "XAUUSD": "GOLD"}
    )
    channels: dict[str, ChannelProfile] = field(default_factory=_default_channels)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    indicator: IndicatorConfig = field(default_factory=IndicatorConfig)

    @classmethod
    def default(cls) -> AppConfig:
        """Return the accepted, safe Observe-mode defaults from ``CONTEXT.md``."""

        config = cls()
        return validate_config(config)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AppConfig:
        """Build and strictly validate a configuration mapping.

        Unknown keys and implicit type coercions are deliberately rejected so a
        typo cannot silently weaken an operational gate.
        """

        root = _mapping(value, "config")
        _known_keys(
            root,
            {
                "config_version",
                "runtime",
                "telegram",
                "broker",
                "symbol_aliases",
                "channels",
                "risk",
                "execution",
                "indicator",
            },
            "config",
        )
        _required_keys(
            root,
            {
                "config_version",
                "runtime",
                "telegram",
                "broker",
                "symbol_aliases",
                "channels",
                "risk",
                "execution",
                "indicator",
            },
            "config",
        )

        runtime_data = _section(root["runtime"], "runtime", RuntimeConfig)
        telegram_data = _section(root["telegram"], "telegram", TelegramConfig)
        broker_data = _section(root["broker"], "broker", BrokerConfig)
        broker_data["webtrader_allowed_origins"] = _string_tuple(
            broker_data["webtrader_allowed_origins"],
            "broker.webtrader_allowed_origins",
        )
        risk_data = _section(root["risk"], "risk", RiskConfig)
        execution_data = _section(root["execution"], "execution", ExecutionConfig)
        indicator_data = _section(root["indicator"], "indicator", IndicatorConfig)
        indicator_data["atr_take_profit_multipliers"] = _number_tuple(
            indicator_data["atr_take_profit_multipliers"],
            "indicator.atr_take_profit_multipliers",
        )

        aliases_data = _mapping(root["symbol_aliases"], "symbol_aliases")
        aliases: dict[str, str] = {}
        for alias, canonical in aliases_data.items():
            if not isinstance(alias, str):
                raise ConfigError("symbol_aliases keys must be strings")
            aliases[alias] = _string(canonical, f"symbol_aliases.{alias}")

        channels_data = _mapping(root["channels"], "channels")
        channels: dict[str, ChannelProfile] = {}
        for name, raw_profile in channels_data.items():
            if not isinstance(name, str):
                raise ConfigError("channels keys must be strings")
            profile_data = _section(
                raw_profile, f"channels.{name}", ChannelProfile
            )
            profile_data["allowed_symbols"] = _string_tuple(
                profile_data["allowed_symbols"],
                f"channels.{name}.allowed_symbols",
            )
            profile_data["required_markers"] = _string_tuple(
                profile_data["required_markers"],
                f"channels.{name}.required_markers",
            )
            profile_data["ignored_markers"] = _string_tuple(
                profile_data["ignored_markers"],
                f"channels.{name}.ignored_markers",
            )
            channels[name] = ChannelProfile(**profile_data)

        config = cls(
            config_version=_integer(root["config_version"], "config_version"),
            runtime=RuntimeConfig(**runtime_data),
            telegram=TelegramConfig(**telegram_data),
            broker=BrokerConfig(**broker_data),
            symbol_aliases=aliases,
            channels=channels,
            risk=RiskConfig(**risk_data),
            execution=ExecutionConfig(**execution_data),
            indicator=IndicatorConfig(**indicator_data),
        )
        return validate_config(config)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible mapping containing no secret values."""

        return asdict(self)

    def validate(self) -> AppConfig:
        return validate_config(self)


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path} must be an object")
    return dict(value)


def _section(value: Any, path: str, model: type[Any]) -> dict[str, Any]:
    data = _mapping(value, path)
    names = set(model.__dataclass_fields__)
    _known_keys(data, names, path)
    _required_keys(data, names, path)
    return data


def _known_keys(data: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(str(key) for key in set(data) - allowed)
    if unknown:
        raise ConfigError(f"{path} has unknown field(s): {', '.join(unknown)}")


def _required_keys(data: Mapping[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - set(data))
    if missing:
        raise ConfigError(f"{path} is missing field(s): {', '.join(missing)}")


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be a string")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{path} must not be empty")
    return value


def _integer(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{path} must be an integer")
    return value


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{path} must be true or false")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{path} must be finite")
    return result


def _optional_number(value: Any, path: str) -> float | None:
    return None if value is None else _number(value, path)


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigError(f"{path} must be an array of strings")
    result = tuple(_string(item, f"{path}[{index}]") for index, item in enumerate(value))
    return result


def _number_tuple(value: Any, path: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigError(f"{path} must be an array of numbers")
    return tuple(_number(item, f"{path}[{index}]") for index, item in enumerate(value))


def _choice(value: Any, choices: set[str] | frozenset[str], path: str) -> str:
    text = _string(value, path)
    if text not in choices:
        allowed = ", ".join(sorted(choices))
        raise ConfigError(f"{path} must be one of: {allowed}")
    return text


def _env_name(value: Any, path: str) -> str:
    name = _string(value, path)
    if not _ENV_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"{path} must be an uppercase environment-variable name, not a secret value"
        )
    return name


def _validate_string_list(
    values: tuple[str, ...], path: str, *, unique: bool = True
) -> None:
    if unique and len(set(values)) != len(values):
        raise ConfigError(f"{path} must not contain duplicates")


def validate_config(config: AppConfig) -> AppConfig:
    """Validate all fields and cross-field safety invariants.

    Validation is intentionally independent of environment-variable presence;
    integrations resolve those variables at startup and fail closed when they
    are missing.  This keeps validation deterministic and prevents secret
    values from entering error messages.
    """

    if not isinstance(config, AppConfig):
        raise ConfigError("config must be an AppConfig")
    if _integer(config.config_version, "config_version") != CONFIG_VERSION:
        raise ConfigError(
            f"unsupported config_version {config.config_version}; expected {CONFIG_VERSION}"
        )

    mode = _string(config.runtime.mode, "runtime.mode").lower()
    if "live" in mode:
        raise ConfigError("runtime.mode: Live trading is not supported")
    _choice(config.runtime.mode, _RUNTIME_MODES, "runtime.mode")
    if not _boolean(
        config.runtime.require_single_instance, "runtime.require_single_instance"
    ):
        raise ConfigError("runtime.require_single_instance must remain true")

    for name in (
        "api_id_env",
        "api_hash_env",
        "session_env",
        "allowed_admin_ids_env",
        "alert_chat_id_env",
    ):
        _env_name(getattr(config.telegram, name), f"telegram.{name}")
    catchup = _integer(
        config.telegram.catchup_overlap_messages,
        "telegram.catchup_overlap_messages",
    )
    if not 0 <= catchup <= 10_000:
        raise ConfigError("telegram.catchup_overlap_messages must be between 0 and 10000")
    future_skew = _integer(
        config.telegram.max_future_message_skew_seconds,
        "telegram.max_future_message_skew_seconds",
    )
    if not 0 <= future_skew <= 300:
        raise ConfigError(
            "telegram.max_future_message_skew_seconds must be between 0 and 300"
        )

    adapter = _choice(
        config.broker.adapter,
        {"mt5", "xm_webtrader"},
        "broker.adapter",
    )
    _string(config.broker.terminal_path, "broker.terminal_path", allow_empty=True)
    _env_name(
        config.broker.allowed_demo_accounts_env,
        "broker.allowed_demo_accounts_env",
    )
    _env_name(config.broker.allowed_servers_env, "broker.allowed_servers_env")
    if not _boolean(config.broker.require_demo, "broker.require_demo"):
        raise ConfigError("broker.require_demo must remain true; Live accounts are rejected")
    tick_age = _integer(
        config.broker.max_tick_age_seconds,
        "broker.max_tick_age_seconds",
    )
    if not 1 <= tick_age <= 300:
        raise ConfigError("broker.max_tick_age_seconds must be between 1 and 300")
    if config.runtime.mode in {"shadow", "demo_armed"} and not config.broker.terminal_path.strip():
        raise ConfigError(
            f"broker.terminal_path is required in {config.runtime.mode} mode"
        )
    webtrader_url = _string(config.broker.webtrader_url, "broker.webtrader_url")
    parsed_webtrader_url = urlsplit(webtrader_url)
    if (
        parsed_webtrader_url.scheme != "https"
        or not parsed_webtrader_url.hostname
        or parsed_webtrader_url.username is not None
        or parsed_webtrader_url.password is not None
        or parsed_webtrader_url.fragment
    ):
        raise ConfigError(
            "broker.webtrader_url must be an HTTPS URL without credentials or a fragment"
        )
    origins = config.broker.webtrader_allowed_origins
    _validate_string_list(origins, "broker.webtrader_allowed_origins")
    if not origins:
        raise ConfigError("broker.webtrader_allowed_origins must not be empty")
    normalized_origins: set[str] = set()
    for index, origin in enumerate(origins):
        value = _string(origin, f"broker.webtrader_allowed_origins[{index}]")
        parsed_origin = urlsplit(value)
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ConfigError(
                "broker.webtrader_allowed_origins entries must be exact HTTPS origins"
            )
        normalized_origins.add(
            f"{parsed_origin.scheme}://{parsed_origin.netloc.lower()}"
        )
    configured_origin = (
        f"{parsed_webtrader_url.scheme}://{parsed_webtrader_url.netloc.lower()}"
    )
    if configured_origin not in normalized_origins:
        raise ConfigError(
            "broker.webtrader_url origin must be exactly allowlisted"
        )
    profile_path = Path(
        _string(config.broker.webtrader_profile_path, "broker.webtrader_profile_path")
    )
    if (
        profile_path.is_absolute()
        or ".." in profile_path.parts
        or len(profile_path.parts) < 2
        or profile_path.parts[0].lower() != "state"
    ):
        raise ConfigError(
            "broker.webtrader_profile_path must be a relative child of state/"
        )
    _choice(
        config.broker.webtrader_browser_channel,
        {"chromium", "chrome", "msedge"},
        "broker.webtrader_browser_channel",
    )
    _boolean(config.broker.webtrader_headless, "broker.webtrader_headless")
    timeout = _integer(
        config.broker.webtrader_timeout_seconds,
        "broker.webtrader_timeout_seconds",
    )
    if not 5 <= timeout <= 120:
        raise ConfigError(
            "broker.webtrader_timeout_seconds must be between 5 and 120"
        )
    readback = _integer(
        config.broker.webtrader_readback_seconds,
        "broker.webtrader_readback_seconds",
    )
    if not 1 <= readback <= 120:
        raise ConfigError(
            "broker.webtrader_readback_seconds must be between 1 and 120"
        )
    drift = _integer(
        config.broker.webtrader_max_price_drift_points,
        "broker.webtrader_max_price_drift_points",
    )
    if not 0 <= drift <= 100:
        raise ConfigError(
            "broker.webtrader_max_price_drift_points must be between 0 and 100"
        )
    if (
        _string(
            config.broker.webtrader_selector_contract,
            "broker.webtrader_selector_contract",
        )
        != "xm-mt5-web-v1"
    ):
        raise ConfigError(
            "broker.webtrader_selector_contract must be xm-mt5-web-v1"
        )
    if not _boolean(
        config.broker.webtrader_require_hedging,
        "broker.webtrader_require_hedging",
    ):
        raise ConfigError(
            "broker.webtrader_require_hedging must remain true for exact click ownership"
        )
    if adapter == "xm_webtrader" and config.runtime.mode == "demo_armed":
        if config.broker.webtrader_headless:
            raise ConfigError(
                "Demo Armed WebTrader must remain headed for operator visibility"
            )

    if not config.symbol_aliases:
        raise ConfigError("symbol_aliases must contain at least one alias")
    canonical_symbols: set[str] = set()
    for alias, canonical in config.symbol_aliases.items():
        if not isinstance(alias, str) or not _SYMBOL_RE.fullmatch(alias):
            raise ConfigError(
                f"symbol_aliases key {alias!r} must be an uppercase symbol alias"
            )
        canonical_value = _string(canonical, f"symbol_aliases.{alias}")
        if not _SYMBOL_RE.fullmatch(canonical_value):
            raise ConfigError(
                f"symbol_aliases.{alias} must be an uppercase canonical symbol"
            )
        canonical_symbols.add(canonical_value)

    if not config.channels:
        raise ConfigError("channels must contain at least one profile")
    peer_owners: dict[int, str] = {}
    for name, profile in config.channels.items():
        if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"channel profile name {name!r} must match {_PROFILE_NAME_RE.pattern}"
            )
        path = f"channels.{name}"
        if not isinstance(profile, ChannelProfile):
            raise ConfigError(f"{path} must be a ChannelProfile")
        if profile.peer_id is not None:
            peer_id = _integer(profile.peer_id, f"{path}.peer_id")
            if peer_id == 0:
                raise ConfigError(f"{path}.peer_id must not be zero")
            if peer_id in peer_owners:
                raise ConfigError(
                    f"{path}.peer_id duplicates channels.{peer_owners[peer_id]}.peer_id"
                )
            peer_owners[peer_id] = name
        _choice(profile.parser, _PARSERS, f"{path}.parser")
        _boolean(profile.enabled, f"{path}.enabled")
        _boolean(profile.trade_enabled, f"{path}.trade_enabled")
        if profile.trade_enabled and not profile.enabled:
            raise ConfigError(f"{path}.trade_enabled requires enabled=true")
        if profile.trade_enabled and profile.peer_id is None:
            raise ConfigError(f"{path}.trade_enabled requires a numeric peer_id")
        _validate_string_list(profile.allowed_symbols, f"{path}.allowed_symbols")
        for symbol in profile.allowed_symbols:
            if not _SYMBOL_RE.fullmatch(symbol):
                raise ConfigError(f"{path}.allowed_symbols contains invalid symbol {symbol!r}")
            if symbol not in canonical_symbols:
                raise ConfigError(
                    f"{path}.allowed_symbols contains {symbol!r}, which is not a canonical symbol_aliases value"
                )
        if profile.enabled and not profile.allowed_symbols:
            raise ConfigError(f"{path}.allowed_symbols must not be empty when enabled")
        semantics = _choice(
            profile.two_level_semantics,
            _TWO_LEVEL_SEMANTICS,
            f"{path}.two_level_semantics",
        )
        entry_mode = _choice(profile.entry_mode, _ENTRY_MODES, f"{path}.entry_mode")
        if semantics != entry_mode:
            raise ConfigError(
                f"{path}.two_level_semantics and entry_mode must select the same supported policy"
            )
        _choice(profile.tp_strategy, _TP_STRATEGIES, f"{path}.tp_strategy")
        tp_index = _integer(profile.tp_index, f"{path}.tp_index")
        if not 1 <= tp_index <= 20:
            raise ConfigError(f"{path}.tp_index must be between 1 and 20")
        expiry = _integer(
            profile.signal_expiry_minutes, f"{path}.signal_expiry_minutes"
        )
        if not 1 <= expiry <= 24 * 60:
            raise ConfigError(
                f"{path}.signal_expiry_minutes must be between 1 and 1440"
            )
        max_spread = _integer(profile.max_spread_points, f"{path}.max_spread_points")
        if not 1 <= max_spread <= 1_000_000:
            raise ConfigError(f"{path}.max_spread_points must be between 1 and 1000000")
        _validate_string_list(profile.required_markers, f"{path}.required_markers")
        _validate_string_list(profile.ignored_markers, f"{path}.ignored_markers")
        if set(profile.required_markers) & set(profile.ignored_markers):
            raise ConfigError(
                f"{path}.required_markers and ignored_markers must not overlap"
            )
        if not _boolean(profile.reject_open_sl, f"{path}.reject_open_sl"):
            raise ConfigError(
                f"{path}.reject_open_sl must remain true; numeric Stop Loss is mandatory"
            )
        version = _string(profile.profile_version, f"{path}.profile_version")
        if not _SEMVER_RE.fullmatch(version):
            raise ConfigError(f"{path}.profile_version must use MAJOR.MINOR.PATCH")

    _choice(config.risk.mode, {"fixed_lot", "risk_percent"}, "risk.mode")
    fixed_lot = _number(config.risk.fixed_lot, "risk.fixed_lot")
    hard_cap = _number(config.risk.hard_lot_cap, "risk.hard_lot_cap")
    if fixed_lot <= 0:
        raise ConfigError("risk.fixed_lot must be greater than zero")
    if hard_cap <= 0:
        raise ConfigError("risk.hard_lot_cap must be greater than zero")
    if fixed_lot > hard_cap:
        raise ConfigError("risk.fixed_lot must not exceed risk.hard_lot_cap")
    risk_percent = _optional_number(config.risk.risk_percent, "risk.risk_percent")
    daily_limit = _optional_number(
        config.risk.daily_loss_limit_percent,
        "risk.daily_loss_limit_percent",
    )
    if risk_percent is not None and not 0 < risk_percent <= 5:
        raise ConfigError("risk.risk_percent must be greater than 0 and at most 5")
    if daily_limit is not None and not 0 < daily_limit <= 25:
        raise ConfigError(
            "risk.daily_loss_limit_percent must be greater than 0 and at most 25"
        )
    if config.risk.mode == "risk_percent":
        if risk_percent is None or daily_limit is None:
            raise ConfigError(
                "risk_percent mode requires risk_percent and daily_loss_limit_percent"
            )
    for field_name in (
        "max_active_per_symbol",
        "max_active_per_channel",
        "max_total_bot_positions",
    ):
        count = _integer(getattr(config.risk, field_name), f"risk.{field_name}")
        if not 1 <= count <= 100:
            raise ConfigError(f"risk.{field_name} must be between 1 and 100")
    if config.risk.max_active_per_symbol > config.risk.max_total_bot_positions:
        raise ConfigError(
            "risk.max_active_per_symbol must not exceed max_total_bot_positions"
        )
    if config.risk.max_active_per_channel > config.risk.max_total_bot_positions:
        raise ConfigError(
            "risk.max_active_per_channel must not exceed max_total_bot_positions"
        )
    _choice(
        config.risk.manual_exposure_policy,
        {"block", "allow_new"},
        "risk.manual_exposure_policy",
    )
    _choice(
        config.risk.same_side_conflict_policy,
        {"block", "allow_within_limits"},
        "risk.same_side_conflict_policy",
    )
    _choice(
        config.risk.opposite_side_conflict_policy,
        {"block", "allow_hedge"},
        "risk.opposite_side_conflict_policy",
    )
    if not _boolean(config.risk.require_numeric_sl, "risk.require_numeric_sl"):
        raise ConfigError("risk.require_numeric_sl must remain true")

    retries = _integer(
        config.execution.order_send_retries, "execution.order_send_retries"
    )
    if retries != 0:
        raise ConfigError(
            "execution.order_send_retries must remain 0; ambiguous results require reconciliation"
        )
    if not _boolean(
        config.execution.reconcile_on_ambiguous_result,
        "execution.reconcile_on_ambiguous_result",
    ):
        raise ConfigError("execution.reconcile_on_ambiguous_result must remain true")
    if not _boolean(
        config.execution.verify_broker_side_protection,
        "execution.verify_broker_side_protection",
    ):
        raise ConfigError("execution.verify_broker_side_protection must remain true")
    if config.execution.management_action_policy != "notify_only":
        raise ConfigError(
            "execution.management_action_policy must remain notify_only in the first release"
        )
    deviation = _integer(
        config.execution.deviation_points,
        "execution.deviation_points",
    )
    if not 0 <= deviation <= 10_000:
        raise ConfigError("execution.deviation_points must be between 0 and 10000")
    poll_seconds = _number(
        config.execution.market_poll_seconds,
        "execution.market_poll_seconds",
    )
    if not 0.1 <= poll_seconds <= 60:
        raise ConfigError("execution.market_poll_seconds must be between 0.1 and 60")

    if config.runtime.mode == "demo_armed":
        if not any(profile.trade_enabled for profile in config.channels.values()):
            raise ConfigError("demo_armed mode requires at least one trade-enabled channel")

    indicator_symbol = _string(config.indicator.symbol, "indicator.symbol")
    if not _SYMBOL_RE.fullmatch(indicator_symbol):
        raise ConfigError("indicator.symbol must be an uppercase canonical symbol")
    if indicator_symbol not in canonical_symbols:
        raise ConfigError(
            "indicator.symbol must be one of the canonical symbol_aliases values"
        )
    _choice(config.indicator.timeframe, _TIMEFRAMES, "indicator.timeframe")
    lookback_bars = _integer(config.indicator.lookback_bars, "indicator.lookback_bars")
    if not 50 <= lookback_bars <= 5_000:
        raise ConfigError("indicator.lookback_bars must be between 50 and 5000")
    ema_fast_period = _integer(config.indicator.ema_fast_period, "indicator.ema_fast_period")
    ema_slow_period = _integer(config.indicator.ema_slow_period, "indicator.ema_slow_period")
    if not 2 <= ema_fast_period <= 200:
        raise ConfigError("indicator.ema_fast_period must be between 2 and 200")
    if not 2 <= ema_slow_period <= 400:
        raise ConfigError("indicator.ema_slow_period must be between 2 and 400")
    if ema_fast_period >= ema_slow_period:
        raise ConfigError("indicator.ema_fast_period must be less than ema_slow_period")
    rsi_period = _integer(config.indicator.rsi_period, "indicator.rsi_period")
    if not 2 <= rsi_period <= 100:
        raise ConfigError("indicator.rsi_period must be between 2 and 100")
    rsi_overbought = _integer(config.indicator.rsi_overbought, "indicator.rsi_overbought")
    rsi_oversold = _integer(config.indicator.rsi_oversold, "indicator.rsi_oversold")
    if not 50 < rsi_overbought <= 100:
        raise ConfigError("indicator.rsi_overbought must be greater than 50 and at most 100")
    if not 0 <= rsi_oversold < 50:
        raise ConfigError("indicator.rsi_oversold must be at least 0 and less than 50")
    atr_period = _integer(config.indicator.atr_period, "indicator.atr_period")
    if not 2 <= atr_period <= 100:
        raise ConfigError("indicator.atr_period must be between 2 and 100")
    atr_sl_multiplier = _number(
        config.indicator.atr_stop_loss_multiplier, "indicator.atr_stop_loss_multiplier"
    )
    if atr_sl_multiplier <= 0:
        raise ConfigError("indicator.atr_stop_loss_multiplier must be greater than zero")
    tp_multipliers = config.indicator.atr_take_profit_multipliers
    if not tp_multipliers:
        raise ConfigError("indicator.atr_take_profit_multipliers must not be empty")
    if len(tp_multipliers) > 10:
        raise ConfigError("indicator.atr_take_profit_multipliers must have at most 10 entries")
    normalized_multipliers = tuple(
        _number(value, f"indicator.atr_take_profit_multipliers[{index}]")
        for index, value in enumerate(tp_multipliers)
    )
    if any(value <= 0 for value in normalized_multipliers):
        raise ConfigError("indicator.atr_take_profit_multipliers must all be greater than zero")
    if list(normalized_multipliers) != sorted(set(normalized_multipliers)):
        raise ConfigError(
            "indicator.atr_take_profit_multipliers must be strictly ascending with no duplicates"
        )
    max_bar_age_multiplier = _integer(
        config.indicator.max_bar_age_multiplier, "indicator.max_bar_age_multiplier"
    )
    if not 1 <= max_bar_age_multiplier <= 20:
        raise ConfigError("indicator.max_bar_age_multiplier must be between 1 and 20")

    return config


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> None:
    raise ConfigError(f"JSON number {value} is not finite")


def load_config(path: str | os.PathLike[str] = CONFIG_PATH) -> AppConfig:
    """Load and validate one JSON configuration file."""

    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {config_path}") from exc
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite_json,
        )
    except ConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in {config_path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return AppConfig.from_dict(value)


def save_config(
    config: AppConfig, path: str | os.PathLike[str] = CONFIG_PATH
) -> Path:
    """Atomically persist a fully validated JSON configuration."""

    validated = validate_config(config)
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        validated.to_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, config_path)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return config_path


__all__ = [
    "CONFIG_PATH",
    "CONFIG_VERSION",
    "EXAMPLE_CONFIG_PATH",
    "AppConfig",
    "BrokerConfig",
    "ChannelProfile",
    "ConfigError",
    "ExecutionConfig",
    "IndicatorConfig",
    "RiskConfig",
    "RuntimeConfig",
    "TelegramConfig",
    "load_config",
    "save_config",
    "validate_config",
]
