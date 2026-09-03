from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from tgxm.config import (
    AppConfig,
    ConfigError,
    load_config,
    save_config,
    validate_config,
)


def _changed(config: AppConfig, *path_and_value: object) -> AppConfig:
    """Return config rebuilt after changing one nested mapping path."""

    *segments, value = path_and_value
    data = config.to_dict()
    target = data
    for segment in segments[:-1]:
        target = target[str(segment)]
    target[str(segments[-1])] = value
    return AppConfig.from_dict(data)


def test_recommended_defaults_are_safe_and_editable() -> None:
    config = AppConfig.default()

    assert config.runtime.mode == "observe"
    assert config.runtime.require_single_instance is True
    assert config.broker.require_demo is True
    assert config.broker.max_tick_age_seconds == 5
    assert config.telegram.max_future_message_skew_seconds == 30
    assert config.risk.mode == "fixed_lot"
    assert config.risk.fixed_lot == 0.01
    assert config.risk.hard_lot_cap == 0.01
    assert config.risk.require_numeric_sl is True
    assert config.risk.manual_exposure_policy == "block"
    assert config.risk.opposite_side_conflict_policy == "block"
    assert config.execution.management_action_policy == "notify_only"
    assert config.execution.order_send_retries == 0
    assert config.execution.deviation_points == 20
    assert config.execution.market_poll_seconds == 1.0

    mr_charlie = config.channels["mr_charlie"]
    assert mr_charlie.enabled is True
    assert mr_charlie.trade_enabled is False
    assert mr_charlie.two_level_semantics == "zone_single_market"
    assert mr_charlie.entry_mode == "zone_single_market"
    assert mr_charlie.tp_strategy == "single_tp"
    assert mr_charlie.tp_index == 1
    assert mr_charlie.signal_expiry_minutes == 30
    assert mr_charlie.max_spread_points == 100
    assert config.symbol_aliases == {"GOLD": "GOLD", "XAUUSD": "GOLD"}


def test_default_round_trips_through_mapping_and_json(tmp_path: Path) -> None:
    config = AppConfig.default()
    assert AppConfig.from_dict(config.to_dict()) == config

    path = tmp_path / "config" / "settings.local.json"
    returned = save_config(config, path)

    assert returned == path
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert load_config(path) == config


def test_example_file_is_the_valid_recommended_default() -> None:
    example = Path("config/settings.example.json")
    assert load_config(example) == AppConfig.default()

    raw = json.loads(example.read_text(encoding="utf-8"))
    assert "password" not in json.dumps(raw).lower()
    assert "allowed_demo_accounts" not in raw["broker"]
    assert raw["broker"]["allowed_demo_accounts_env"] == "TGXM_ALLOWED_DEMO_ACCOUNTS"
    assert raw["telegram"]["api_hash_env"] == "TGXM_TELEGRAM_API_HASH"


def test_webtrader_defaults_are_demo_safe_and_use_an_ignored_profile() -> None:
    config = AppConfig.default()

    assert config.broker.adapter == "xm_webtrader"
    assert config.broker.webtrader_url.startswith("https://")
    assert config.broker.webtrader_profile_path.startswith("state/")
    assert config.broker.webtrader_require_hedging is True
    assert config.broker.webtrader_headless is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("webtrader_url", "http://mt5.xm.com/", "must be an HTTPS URL"),
        ("webtrader_profile_path", "../browser-profile", "child of state"),
        ("webtrader_timeout_seconds", 4, "between 5 and 120"),
        ("webtrader_readback_seconds", 0, "between 1 and 120"),
        ("webtrader_max_price_drift_points", 101, "between 0 and 100"),
        ("webtrader_require_hedging", False, "must remain true"),
    ],
)
def test_webtrader_safety_configuration_fails_closed(
    field: str, value: object, message: str
) -> None:
    data = AppConfig.default().to_dict()
    data["broker"][field] = value

    with pytest.raises(ConfigError, match=message):
        AppConfig.from_dict(data)


def test_webtrader_url_origin_must_be_exactly_allowlisted() -> None:
    data = AppConfig.default().to_dict()
    data["broker"]["webtrader_url"] = "https://lookalike.example/"

    with pytest.raises(ConfigError, match="origin must be exactly allowlisted"):
        AppConfig.from_dict(data)


@pytest.mark.parametrize("mode", ["live", "LIVE", "live_active", "demo_live"])
def test_every_live_mode_is_rejected(mode: str) -> None:
    config = replace(AppConfig.default(), runtime=replace(AppConfig.default().runtime, mode=mode))

    with pytest.raises(ConfigError, match="Live trading is not supported"):
        validate_config(config)


@pytest.mark.parametrize(
    ("section", "field", "unsafe", "message"),
    [
        ("runtime", "require_single_instance", False, "must remain true"),
        ("broker", "require_demo", False, "Live accounts are rejected"),
        ("risk", "require_numeric_sl", False, "must remain true"),
        ("execution", "order_send_retries", 1, "must remain 0"),
        (
            "execution",
            "reconcile_on_ambiguous_result",
            False,
            "must remain true",
        ),
        (
            "execution",
            "verify_broker_side_protection",
            False,
            "must remain true",
        ),
        (
            "execution",
            "management_action_policy",
            "close_now",
            "must remain notify_only",
        ),
    ],
)
def test_hard_safety_invariants_cannot_be_disabled(
    section: str, field: str, unsafe: object, message: str
) -> None:
    data = AppConfig.default().to_dict()
    data[section][field] = unsafe

    with pytest.raises(ConfigError, match=message):
        AppConfig.from_dict(data)


@pytest.mark.parametrize(
    ("section", "field", "secret"),
    [
        ("telegram", "api_id_env", "12345678"),
        ("telegram", "api_hash_env", "abc123-secret-value"),
        ("telegram", "session_env", "1AAStringSessionSecret"),
        ("broker", "allowed_demo_accounts_env", "12345678,87654321"),
    ],
)
def test_secret_fields_accept_only_environment_variable_names(
    section: str, field: str, secret: str
) -> None:
    data = AppConfig.default().to_dict()
    data[section][field] = secret

    with pytest.raises(ConfigError, match="environment-variable name"):
        AppConfig.from_dict(data)


def test_unknown_and_missing_fields_are_rejected() -> None:
    unknown = AppConfig.default().to_dict()
    unknown["risk"]["martingale"] = True
    with pytest.raises(ConfigError, match="unknown field.*martingale"):
        AppConfig.from_dict(unknown)

    missing = AppConfig.default().to_dict()
    del missing["channels"]["mr_charlie"]["reject_open_sl"]
    with pytest.raises(ConfigError, match="missing field.*reject_open_sl"):
        AppConfig.from_dict(missing)


def test_implicit_bool_to_integer_coercion_is_rejected() -> None:
    data = AppConfig.default().to_dict()
    data["channels"]["mr_charlie"]["signal_expiry_minutes"] = True

    with pytest.raises(ConfigError, match="must be an integer"):
        AppConfig.from_dict(data)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("telegram", "max_future_message_skew_seconds", 301, "between 0 and 300"),
        ("broker", "max_tick_age_seconds", 0, "between 1 and 300"),
        ("execution", "deviation_points", -1, "between 0 and 10000"),
        ("execution", "market_poll_seconds", 0.09, "between 0.1 and 60"),
    ],
)
def test_runtime_safety_tolerances_are_strictly_bounded(
    section: str, field: str, value: int | float, message: str
) -> None:
    data = AppConfig.default().to_dict()
    data[section][field] = value

    with pytest.raises(ConfigError, match=message):
        AppConfig.from_dict(data)


def test_channel_spread_limit_is_required_and_bounded() -> None:
    data = AppConfig.default().to_dict()
    data["channels"]["mr_charlie"]["max_spread_points"] = 0

    with pytest.raises(ConfigError, match="max_spread_points must be between"):
        AppConfig.from_dict(data)


def test_channel_cross_field_validation_is_fail_closed() -> None:
    no_peer = AppConfig.default().to_dict()
    no_peer["channels"]["mr_charlie"]["trade_enabled"] = True
    with pytest.raises(ConfigError, match="requires a numeric peer_id"):
        AppConfig.from_dict(no_peer)

    unknown_symbol = AppConfig.default().to_dict()
    unknown_symbol["channels"]["mr_charlie"]["allowed_symbols"] = ["BTCUSD"]
    with pytest.raises(ConfigError, match="not a canonical symbol_aliases value"):
        AppConfig.from_dict(unknown_symbol)

    duplicate_peer = AppConfig.default().to_dict()
    duplicate_peer["channels"]["mr_charlie"]["peer_id"] = -100123
    duplicate_peer["channels"]["vip_gold"]["peer_id"] = -100123
    with pytest.raises(ConfigError, match="duplicates"):
        AppConfig.from_dict(duplicate_peer)


def test_risk_percent_requires_explicit_percentage_and_daily_limit() -> None:
    data = AppConfig.default().to_dict()
    data["risk"]["mode"] = "risk_percent"

    with pytest.raises(ConfigError, match="requires risk_percent"):
        AppConfig.from_dict(data)

    data["risk"]["risk_percent"] = 0.5
    data["risk"]["daily_loss_limit_percent"] = 2.0
    config = AppConfig.from_dict(data)
    assert config.risk.risk_percent == 0.5
    assert config.risk.daily_loss_limit_percent == 2.0


def test_shadow_and_demo_armed_require_explicit_prerequisites() -> None:
    shadow = AppConfig.default().to_dict()
    shadow["runtime"]["mode"] = "shadow"
    with pytest.raises(ConfigError, match="terminal_path is required"):
        AppConfig.from_dict(shadow)

    demo = AppConfig.default().to_dict()
    demo["runtime"]["mode"] = "demo_armed"
    demo["broker"]["terminal_path"] = "C:/XM MT5/terminal64.exe"
    with pytest.raises(ConfigError, match="trade-enabled channel"):
        AppConfig.from_dict(demo)

    demo["channels"]["mr_charlie"]["peer_id"] = -1001234567890
    demo["channels"]["mr_charlie"]["trade_enabled"] = True
    assert AppConfig.from_dict(demo).runtime.mode == "demo_armed"


def test_invalid_save_does_not_overwrite_last_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.json"
    valid = AppConfig.default()
    save_config(valid, path)
    before = path.read_bytes()

    invalid = replace(valid, broker=replace(valid.broker, require_demo=False))
    with pytest.raises(ConfigError, match="Live accounts are rejected"):
        save_config(invalid, path)

    assert path.read_bytes() == before
    assert load_config(path) == valid


def test_load_rejects_duplicate_keys_and_non_finite_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"config_version": 1, "config_version": 1}', encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key"):
        load_config(duplicate)

    non_finite = tmp_path / "nan.json"
    text = json.dumps(AppConfig.default().to_dict()).replace('"fixed_lot": 0.01', '"fixed_lot": NaN')
    non_finite.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="not finite"):
        load_config(non_finite)


def test_load_missing_file_has_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "missing.json")


def test_autotrade_defaults_are_off_and_editable() -> None:
    config = AppConfig.default()

    assert config.autotrade.enabled is False
    assert config.autotrade.trade_enabled is False
    assert config.autotrade.timeframe == "M1"
    assert config.autotrade.higher_timeframe == "auto"
    assert config.autotrade.require_higher_timeframe_agreement is True
    assert config.autotrade.move_stop_to_breakeven_after_tp1 is True
    assert config.autotrade.close_on_opposite_crossover is True
    assert config.broker.server_utc_offset_minutes is None


def test_server_utc_offset_must_sit_on_a_plausible_grid() -> None:
    config = AppConfig.default()

    assert _changed(config, "broker", "server_utc_offset_minutes", 180)
    with pytest.raises(ConfigError, match="server_utc_offset_minutes"):
        _changed(config, "broker", "server_utc_offset_minutes", 7)
    with pytest.raises(ConfigError, match="server_utc_offset_minutes"):
        _changed(config, "broker", "server_utc_offset_minutes", 2000)


def test_autotrade_section_rejects_unknown_fields() -> None:
    config = AppConfig.default()
    data = config.to_dict()
    data["autotrade"]["martingale"] = True
    with pytest.raises(ConfigError, match="unknown field"):
        AppConfig.from_dict(data)


def test_autotrade_open_positions_cannot_exceed_the_risk_cap() -> None:
    config = AppConfig.default()
    with pytest.raises(ConfigError, match="max_total_bot_positions"):
        _changed(config, "autotrade", "max_open_positions", 3)
