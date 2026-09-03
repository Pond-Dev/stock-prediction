"""Command-line control plane for the Telegram-to-XM Demo bot."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import json
import os
import platform
import shutil
import struct
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from tgxm.autotrader import AutoTradeDecision, AutoTradeStatus, AutoTrader
from tgxm.broker import (
    BrokerError,
    BrokerSafetyError,
    DemoAccountPolicy,
    MetaTrader5Broker,
)
from tgxm.config import CONFIG_PATH, AppConfig, ConfigError, load_config, save_config
from tgxm.dashboard import DashboardError, run_dashboard
from tgxm.engine import TradingEngine
from tgxm.environment import EnvironmentError as RuntimeEnvironmentError
from tgxm.environment import (
    load_environment_file,
    load_integer_allowlist,
    load_text_allowlist,
)
from tgxm.indicator import predict as compute_prediction
from tgxm.indicator_feed import MetaTrader5CandleSource
from tgxm.menu import run_config_menu
from tgxm.models import RawTelegramEvent
from tgxm.parsers import parse_event
from tgxm.runtime import BotRuntimeError
from tgxm.store import SQLiteStore
from tgxm.telegram_client import (
    TelegramAuthenticationError,
    TelegramConfigurationError,
    TelegramCredentials,
    TelegramDependencyError,
    authorize_telegram_session,
    list_telegram_dialogs,
)
from tgxm.webtrader_broker import MetaTrader5ReadOnlyVerifier
from tgxm.webtrader_click import PlaywrightWebTraderClicker, WebTraderError


def _json_default(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (datetime, Decimal, Path)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_json(value: object) -> None:
    print(
        json.dumps(
            value,
            default=_json_default,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _parse_utc(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "time must be ISO-8601, for example 2026-08-27T03:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("time must include a UTC offset")
    return parsed.astimezone(UTC)


def _load_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    return Path(args.file).read_text(encoding="utf-8")


def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config)


def _env_file(args: argparse.Namespace) -> str | None:
    if args.env_file is not None:
        return args.env_file
    default = Path(".env")
    return str(default) if default.is_file() else None


def command_init_config(args: argparse.Namespace) -> int:
    path = _config_path(args)
    if path.exists() and not args.force:
        raise ConfigError(f"configuration already exists: {path}; use --force to replace it")
    config = AppConfig.default()
    save_config(config, path)
    print(f"created safe Observe-mode configuration: {path.resolve()}")
    return 0


def command_menu(args: argparse.Namespace) -> int:
    config = run_config_menu(_config_path(args))
    print(
        f"saved valid configuration version {config.config_version} "
        f"in {Path(args.config).resolve()}"
    )
    return 0


def command_show_config(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    _print_json(config.to_dict())
    return 0


def command_validate_config(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    config.validate()
    print(
        f"valid config: mode={config.runtime.mode}, "
        f"channels={len(config.channels)}, version={config.config_version}"
    )
    return 0


def command_parse(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    try:
        profile = config.channels[args.channel]
    except KeyError as exc:
        available = ", ".join(sorted(config.channels)) or "(none)"
        raise ConfigError(
            f"unknown channel profile {args.channel!r}; available: {available}"
        ) from exc
    peer_id = args.peer_id if args.peer_id is not None else profile.peer_id
    if peer_id is None:
        raise ConfigError(
            f"channel {args.channel!r} has no peer_id; pass --peer-id for an offline parse"
        )
    event = RawTelegramEvent(
        channel_id=int(peer_id),
        message_id=args.message_id,
        text=_load_text(args),
        revision=args.revision,
        is_edit=args.edit,
        message_time_utc=_parse_utc(args.message_time),
    )
    result = parse_event(
        event,
        profile=profile,
        profile_name=args.channel,
        symbol_aliases=config.symbol_aliases,
    )
    payload = dataclasses.asdict(result)
    if not args.include_normalized_text:
        payload.pop("normalized_text", None)
    _print_json(payload)
    return 0


def _dependency_state(module: str) -> str:
    return "installed" if importlib.util.find_spec(module) is not None else "not installed"


def _environment_presence(variable_name: str) -> str:
    return "set" if os.getenv(variable_name) else "missing"


def _browser_channel_available(channel: str) -> bool:
    if _dependency_state("playwright") != "installed":
        return False
    if channel == "chromium":
        try:
            from playwright.sync_api import sync_playwright

            manager = sync_playwright().start()
            try:
                return Path(manager.chromium.executable_path).is_file()
            finally:
                manager.stop()
        except Exception:
            return False

    executable_names = {
        "chrome": ("chrome", "chrome.exe"),
        "msedge": ("msedge", "msedge.exe"),
    }
    names = executable_names.get(channel, ())
    if any(shutil.which(name) for name in names):
        return True
    roots = tuple(
        Path(value)
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        if (value := os.getenv(name))
    )
    relative_paths = {
        "chrome": Path("Google/Chrome/Application/chrome.exe"),
        "msedge": Path("Microsoft/Edge/Application/msedge.exe"),
    }
    relative = relative_paths.get(channel)
    return relative is not None and any((root / relative).is_file() for root in roots)


def command_doctor(args: argparse.Namespace) -> int:
    load_environment_file(_env_file(args))
    config = load_config(_config_path(args))
    config.validate()
    secret_state = {
        config.telegram.api_id_env: _environment_presence(config.telegram.api_id_env),
        config.telegram.api_hash_env: _environment_presence(
            config.telegram.api_hash_env
        ),
        config.telegram.session_env: _environment_presence(
            config.telegram.session_env
        ),
        config.broker.allowed_demo_accounts_env: _environment_presence(
            config.broker.allowed_demo_accounts_env
        ),
        config.broker.allowed_servers_env: _environment_presence(
            config.broker.allowed_servers_env
        ),
    }
    enabled_profiles = {
        name: profile
        for name, profile in config.channels.items()
        if profile.enabled
    }
    missing_peer_profiles = sorted(
        name for name, profile in enabled_profiles.items() if profile.peer_id is None
    )
    terminal_path = Path(config.broker.terminal_path) if config.broker.terminal_path else None
    telegram_ready = (
        not missing_peer_profiles
        and all(
            secret_state[name] == "set"
            for name in (
                config.telegram.api_id_env,
                config.telegram.api_hash_env,
                config.telegram.session_env,
            )
        )
    )
    broker_ready = (
        _dependency_state("MetaTrader5") == "installed"
        and terminal_path is not None
        and terminal_path.is_file()
        and secret_state[config.broker.allowed_demo_accounts_env] == "set"
        and secret_state[config.broker.allowed_servers_env] == "set"
    )
    webtrader_required = config.broker.adapter == "xm_webtrader"
    browser_available = _browser_channel_available(
        config.broker.webtrader_browser_channel
    )
    webtrader_profile = Path(config.broker.webtrader_profile_path)
    webtrader_ready = (
        _dependency_state("playwright") == "installed"
        and browser_available
        and webtrader_profile.is_dir()
    )
    execution_ready = broker_ready and (
        not webtrader_required or webtrader_ready
    )
    report = {
        "python": {
            "version": platform.python_version(),
            "bits": struct.calcsize("P") * 8,
            "platform": platform.platform(),
        },
        "config": {
            "path": str(Path(args.config).resolve()),
            "mode": config.runtime.mode,
            "valid": True,
            "enabled_channel_count": len(enabled_profiles),
            "profiles_missing_peer_id": missing_peer_profiles,
        },
        "optional_dependencies": {
            "telethon": _dependency_state("telethon"),
            "MetaTrader5": _dependency_state("MetaTrader5"),
            "playwright": _dependency_state("playwright"),
        },
        "secret_environment": secret_state,
        "broker_terminal": {
            "configured": terminal_path is not None,
            "exists": terminal_path.is_file() if terminal_path is not None else False,
        },
        "webtrader": {
            "required": webtrader_required,
            "browser_channel": config.broker.webtrader_browser_channel,
            "browser_available": browser_available,
            "profile_initialized": webtrader_profile.is_dir(),
            "selector_contract": config.broker.webtrader_selector_contract,
        },
        "readiness": {
            "observe": telegram_ready,
            "shadow": telegram_ready and broker_ready,
            "demo_armed": (
                telegram_ready
                and execution_ready
                and any(profile.trade_enabled for profile in enabled_profiles.values())
            ),
        },
    }
    _print_json(report)
    return 0


def _strict_replay_event(value: Any, line_number: int) -> RawTelegramEvent:
    if not isinstance(value, dict):
        raise ValueError(f"replay line {line_number} must be a JSON object")
    allowed = {
        "peer_id",
        "message_id",
        "text",
        "revision",
        "is_edit",
        "reply_to_message_id",
        "forward_origin",
        "message_time_utc",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"replay line {line_number} has unknown fields: {', '.join(unknown)}"
        )
    required = {"peer_id", "message_id", "text", "message_time_utc"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(
            f"replay line {line_number} is missing fields: {', '.join(missing)}"
        )
    if type(value["peer_id"]) is not int or type(value["message_id"]) is not int:
        raise ValueError(f"replay line {line_number} IDs must be integers")
    if value["peer_id"] == 0 or value["message_id"] < 1:
        raise ValueError(f"replay line {line_number} IDs are outside the valid range")
    if not isinstance(value["text"], str):
        raise ValueError(f"replay line {line_number} text must be a string")
    revision = value.get("revision", 1)
    if type(revision) is not int or revision < 1:
        raise ValueError(f"replay line {line_number} revision must be a positive integer")
    is_edit = value.get("is_edit", False)
    if type(is_edit) is not bool:
        raise ValueError(f"replay line {line_number} is_edit must be true or false")
    reply_to = value.get("reply_to_message_id")
    if reply_to is not None and (type(reply_to) is not int or reply_to < 1):
        raise ValueError(
            f"replay line {line_number} reply_to_message_id must be a positive integer or null"
        )
    forward_origin = value.get("forward_origin")
    if forward_origin is not None and not isinstance(forward_origin, str):
        raise ValueError(
            f"replay line {line_number} forward_origin must be a string or null"
        )
    if not isinstance(value["message_time_utc"], str):
        raise ValueError(
            f"replay line {line_number} message_time_utc must be an ISO-8601 string"
        )
    return RawTelegramEvent(
        channel_id=value["peer_id"],
        message_id=value["message_id"],
        text=value["text"],
        revision=revision,
        is_edit=is_edit,
        reply_to_message_id=reply_to,
        forward_origin=forward_origin,
        message_time_utc=_parse_utc(value["message_time_utc"]),
    )


def command_replay(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    if config.runtime.mode != "observe":
        raise ConfigError(
            "offline replay requires runtime.mode=observe; it never connects to MT5"
        )
    source = Path(args.file)
    events: list[RawTelegramEvent] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON on replay line {line_number}: {exc.msg}"
                ) from exc
            events.append(_strict_replay_event(value, line_number))

    decisions: dict[str, int] = {}
    with SQLiteStore(Path(args.db)) as store:
        engine = TradingEngine(config=config, store=store)
        for event in events:
            decision = engine.process_event(event)
            decisions[decision.status.value] = decisions.get(decision.status.value, 0) + 1
    _print_json(
        {
            "database": str(Path(args.db).resolve()),
            "processed": len(events),
            "decision_counts": decisions,
            "raw_text_printed": False,
        }
    )
    return 0


def command_db_status(args: argparse.Namespace) -> int:
    with SQLiteStore(Path(args.db)) as store:
        intents = store.list_order_intents()
        _print_json(
            {
                "database": str(Path(args.db).resolve()),
                "raw_event_count": len(store.list_raw_events()),
                "intent_count": len(intents),
                "intent_status_counts": {
                    status: sum(1 for intent in intents if intent.status.value == status)
                    for status in sorted({intent.status.value for intent in intents})
                },
                "raw_text_printed": False,
            }
        )
    return 0


def command_predict(args: argparse.Namespace) -> int:
    """Print an advisory Buy/Sell/SL/TP suggestion from MT5 history.

    This is read-only: it never persists an Order Intent and never calls a
    broker adapter's mutation methods.  It is not a Canonical Signal and is
    never consumed by the Telegram-to-XM order pipeline.
    """

    load_environment_file(_env_file(args))
    config = load_config(_config_path(args))
    settings = config.indicator
    if not config.broker.terminal_path.strip():
        raise ConfigError(
            "predict requires broker.terminal_path to point at a local MT5 terminal"
        )
    policy = _webtrader_demo_policy(config)
    source = MetaTrader5CandleSource(policy=policy, terminal_path=config.broker.terminal_path)
    source.initialize()
    try:
        candles = source.fetch_candles(settings.symbol, settings.timeframe, settings.lookback_bars)
    finally:
        source.shutdown()
    prediction = compute_prediction(candles, settings)
    _print_json(
        {
            "advisory_only": True,
            "connected_to_order_execution": False,
            "prediction": dataclasses.asdict(prediction),
        }
    )
    return 0


def command_dashboard(args: argparse.Namespace) -> int:
    run_dashboard(
        config_path=_config_path(args),
        db_path=Path(args.db),
        port=args.port,
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    from tgxm.runtime import run_bot

    asyncio.run(
        run_bot(
            config_path=_config_path(args),
            db_path=Path(args.db),
            demo_active=args.activate_demo,
            env_file=_env_file(args),
        )
    )
    return 0


def _telegram_credentials(config: AppConfig, env_file: str | None) -> TelegramCredentials:
    load_environment_file(env_file)
    return TelegramCredentials.from_environment(
        api_id_env=config.telegram.api_id_env,
        api_hash_env=config.telegram.api_hash_env,
        session_env=config.telegram.session_env,
    )


def command_telegram_login(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    user_id = asyncio.run(
        authorize_telegram_session(_telegram_credentials(config, _env_file(args)))
    )
    print(f"Telegram session authorized for user ID {user_id}; session file is gitignored")
    return 0


def command_telegram_dialogs(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    dialogs = asyncio.run(
        list_telegram_dialogs(_telegram_credentials(config, _env_file(args)))
    )
    _print_json(
        {
            "dialogs": dialogs,
            "notice": "dialog titles are printed locally only and are not persisted",
        }
    )
    return 0


def _webtrader_demo_policy(config: AppConfig) -> DemoAccountPolicy:
    accounts = load_integer_allowlist(config.broker.allowed_demo_accounts_env)
    servers = load_text_allowlist(config.broker.allowed_servers_env)
    return DemoAccountPolicy(
        allowed_demo_accounts=frozenset(str(value) for value in accounts),
        allowed_servers=frozenset(servers),
        allowed_symbols=frozenset(config.symbol_aliases.values()),
        max_tick_age_seconds=config.broker.max_tick_age_seconds,
    )


def _autotrade_demo_policy(config: AppConfig) -> DemoAccountPolicy:
    """Demo allowlist narrowed to the one symbol the strategy may touch."""

    accounts = load_integer_allowlist(config.broker.allowed_demo_accounts_env)
    servers = load_text_allowlist(config.broker.allowed_servers_env)
    return DemoAccountPolicy(
        allowed_demo_accounts=frozenset(str(value) for value in accounts),
        allowed_servers=frozenset(servers),
        allowed_symbols=frozenset({config.autotrade.broker_symbol}),
        max_tick_age_seconds=config.broker.max_tick_age_seconds,
    )


def _autotrade_report_key(decision: AutoTradeDecision) -> tuple[Any, ...]:
    """What makes one cycle worth printing again."""

    return (
        decision.status.value,
        decision.reason,
        decision.bar_time_utc,
        tuple(item.action.value + item.position_id for item in decision.management),
    )


def command_autotrade(args: argparse.Namespace) -> int:
    """Run the local indicator strategy against the MT5 Demo terminal.

    Reads no Telegram content.  Submission still requires
    ``autotrade.enabled``, ``autotrade.trade_enabled``, the volatile
    ``--activate-demo`` flag, and every broker-side Demo gate.
    """

    from tgxm.runtime import RuntimeAlreadyRunningError, SingleInstanceLock

    load_environment_file(_env_file(args))
    config = load_config(_config_path(args))
    if not config.autotrade.enabled:
        raise ConfigError(
            "autotrade is disabled; set autotrade.enabled in the configuration menu"
        )
    if args.activate_demo and not config.autotrade.trade_enabled:
        raise ConfigError("--activate-demo requires autotrade.trade_enabled")
    if not config.broker.terminal_path.strip():
        raise ConfigError("autotrade requires broker.terminal_path")

    symbol = config.autotrade.broker_symbol
    policy = _autotrade_demo_policy(config)
    broker = MetaTrader5Broker(
        policy=policy,
        terminal_path=config.broker.terminal_path,
        server_utc_offset_minutes=config.broker.server_utc_offset_minutes,
    )
    lock = SingleInstanceLock(Path(args.db).with_suffix(".autotrade.lock"))
    if config.runtime.require_single_instance:
        try:
            lock.acquire()
        except RuntimeAlreadyRunningError as exc:
            raise BotRuntimeError(str(exc)) from exc
    source: MetaTrader5CandleSource | None = None
    try:
        broker.initialize()
        try:
            account = broker.discover_account()
        except BrokerSafetyError as exc:
            if "external trading is not enabled" in str(exc):
                raise BrokerSafetyError(
                    "external trading is not enabled: turn on the Algo Trading "
                    "button in the MT5 window (Tools > Options > Expert Advisors "
                    "> Allow algorithmic trading)"
                ) from exc
            raise
        offset = broker.resolve_server_utc_offset(symbol)
        source = MetaTrader5CandleSource(
            policy=policy,
            terminal_path=config.broker.terminal_path,
            server_utc_offset_minutes=offset,
        )
        source.initialize()
        with SQLiteStore(Path(args.db)) as store:
            trader = AutoTrader(
                config=config,
                store=store,
                broker=broker,
                candle_source=source,
                position_manager=broker,
                demo_active=args.activate_demo,
            )
            _print_json(
                {
                    "started": True,
                    "symbol": symbol,
                    "timeframe": config.autotrade.timeframe,
                    "higher_timeframe": trader.higher_timeframe,
                    "server_utc_offset_minutes": offset,
                    "server_offset_source": broker.server_offset_source,
                    "account_is_demo": account.is_demo,
                    "account_margin_mode": account.margin_mode,
                    "trade_enabled": config.autotrade.trade_enabled,
                    "demo_active": bool(args.activate_demo),
                    "fixed_lot": str(config.risk.fixed_lot),
                    "once": bool(args.once),
                }
            )
            previous: tuple[Any, ...] | None = None
            while True:
                decision = trader.run_cycle()
                key = _autotrade_report_key(decision)
                if key != previous:
                    _print_json(decision.to_dict())
                    previous = key
                if args.once:
                    break
                if decision.status is AutoTradeStatus.DISABLED:
                    break
                time.sleep(float(config.autotrade.poll_seconds))
    except KeyboardInterrupt:
        _print_json({"stopped": "keyboard interrupt", "orders_are_never_retried": True})
    finally:
        if source is not None:
            source.shutdown()
        broker.shutdown()
        if config.runtime.require_single_instance:
            lock.release()
    return 0


def command_webtrader_login(args: argparse.Namespace) -> int:
    """Open the dedicated headed profile and verify a manual Demo login."""

    load_environment_file(_env_file(args))
    config = load_config(_config_path(args))
    if config.broker.adapter != "xm_webtrader":
        raise ConfigError(
            "webtrader-login requires broker.adapter=xm_webtrader"
        )
    if config.broker.webtrader_headless:
        raise ConfigError(
            "webtrader-login requires a headed browser for manual operator login"
        )

    policy = _webtrader_demo_policy(config)
    mt5 = MetaTrader5ReadOnlyVerifier(
        policy=policy,
        terminal_path=config.broker.terminal_path or None,
    )
    clicker: PlaywrightWebTraderClicker | None = None
    try:
        mt5.initialize()
        account = mt5.discover_account()
        if (
            config.broker.webtrader_require_hedging
            and account.margin_mode != "RETAIL_HEDGING"
        ):
            raise BrokerSafetyError(
                "WebTrader execution requires exact RETAIL_HEDGING account mode"
            )
        clicker = PlaywrightWebTraderClicker(
            url=config.broker.webtrader_url,
            allowed_origins=config.broker.webtrader_allowed_origins,
            profile_dir=config.broker.webtrader_profile_path,
            headless=False,
            browser_channel=config.broker.webtrader_browser_channel,
            action_timeout_seconds=config.broker.webtrader_timeout_seconds,
            receipt_timeout_seconds=config.broker.webtrader_timeout_seconds,
        )
        clicker.initialize()
        try:
            input(
                "Sign in manually in the opened XM WebTrader window, then press "
                "Enter here to verify the Demo identity: "
            )
        except (EOFError, KeyboardInterrupt) as exc:
            raise WebTraderError("manual WebTrader login was cancelled") from exc

        identity = clicker.inspect_identity(
            expected_login=account.login,
            expected_server=account.server,
        )
        if (
            not identity.is_demo
            or identity.login != account.login
            or identity.server != account.server
        ):
            raise WebTraderError(
                "WebTrader identity did not exactly match the MT5 Demo evidence"
            )
        _print_json(
            {
                "account_details_printed": False,
                "credentials_handled": False,
                "demo": True,
                "matched_mt5_identity": True,
                "status": "WEBTRADER_DEMO_VERIFIED",
            }
        )
        return 0
    finally:
        try:
            if clicker is not None:
                clicker.shutdown()
        finally:
            mt5.shutdown()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgxm",
        description=(
            "Parse allowlisted Telegram signals and click XM WebTrader Demo safely"
        ),
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help=f"configuration JSON path (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv file for runtime secrets (auto-loads .env when it exists)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-config", help="write safe recommended defaults")
    init.add_argument(
        "--force",
        action="store_true",
        help="replace an existing configuration file",
    )
    init.set_defaults(func=command_init_config)

    menu = commands.add_parser("menu", help="open the interactive configuration menu")
    menu.set_defaults(func=command_menu)

    show = commands.add_parser("show-config", help="print validated non-secret configuration")
    show.set_defaults(func=command_show_config)

    validate = commands.add_parser("validate-config", help="validate configuration and exit")
    validate.set_defaults(func=command_validate_config)

    parse = commands.add_parser("parse", help="parse one message without contacting Telegram/XM")
    parse.add_argument("--channel", required=True, help="configured channel profile name")
    content = parse.add_mutually_exclusive_group(required=True)
    content.add_argument("--text", help="message text")
    content.add_argument("--file", help="UTF-8 text file containing one message")
    parse.add_argument("--peer-id", type=int, help="offline override when profile has no peer ID")
    parse.add_argument("--message-id", type=int, default=1)
    parse.add_argument("--revision", type=int, default=1)
    parse.add_argument("--edit", action="store_true")
    parse.add_argument("--message-time", help="ISO-8601 timestamp with UTC offset")
    parse.add_argument(
        "--include-normalized-text",
        action="store_true",
        help="explicitly include private normalized message text in local output",
    )
    parse.set_defaults(func=command_parse)

    doctor = commands.add_parser(
        "doctor", help="check local configuration, optional packages, and secret presence"
    )
    doctor.set_defaults(func=command_doctor)

    replay = commands.add_parser(
        "replay", help="replay private JSONL evidence in offline Observe mode"
    )
    replay.add_argument("--file", required=True, help="UTF-8 JSONL input")
    replay.add_argument(
        "--db", default="state/tgxm.sqlite3", help="ignored local SQLite database path"
    )
    replay.set_defaults(func=command_replay)

    status = commands.add_parser(
        "db-status", help="show local counts without printing private messages"
    )
    status.add_argument(
        "--db", default="state/tgxm.sqlite3", help="ignored local SQLite database path"
    )
    status.set_defaults(func=command_db_status)

    predict = commands.add_parser(
        "predict",
        help=(
            "print an advisory Buy/Sell/SL/TP suggestion from MT5 history; "
            "read-only, never trades"
        ),
    )
    predict.set_defaults(func=command_predict)

    dashboard = commands.add_parser(
        "dashboard",
        help="open the read-only local status dashboard on 127.0.0.1",
    )
    dashboard.add_argument(
        "--db", default="state/tgxm.sqlite3", help="ignored local SQLite database path"
    )
    dashboard.add_argument(
        "--port", type=int, default=8765, help="loopback dashboard port (default: 8765)"
    )
    dashboard.set_defaults(func=command_dashboard)

    autotrade = commands.add_parser(
        "autotrade",
        help="run the local EMA/RSI/ATR strategy against the MT5 Demo terminal",
    )
    autotrade.add_argument(
        "--db", default="state/tgxm.sqlite3", help="ignored local SQLite database path"
    )
    autotrade.add_argument(
        "--once", action="store_true", help="evaluate one cycle and exit"
    )
    autotrade.add_argument(
        "--activate-demo",
        action="store_true",
        help=(
            "volatile authorization to submit and manage Demo orders; requires "
            "autotrade.trade_enabled and exact account/server allowlists"
        ),
    )
    autotrade.set_defaults(func=command_autotrade)

    run = commands.add_parser(
        "run", help="stream configured Telegram rooms (Observe/Shadow/Demo Armed)"
    )
    run.add_argument(
        "--db", default="state/tgxm.sqlite3", help="ignored local SQLite database path"
    )
    run.add_argument(
        "--activate-demo",
        action="store_true",
        help=(
            "volatile authorization to click XM WebTrader Demo orders; requires "
            "mode=demo_armed and exact account/server allowlists"
        ),
    )
    run.set_defaults(func=command_run)

    login = commands.add_parser(
        "telegram-login", help="interactively create/authorize the ignored Telethon session"
    )
    login.set_defaults(func=command_telegram_login)

    dialogs = commands.add_parser(
        "telegram-dialogs", help="list local numeric peer IDs for channel configuration"
    )
    dialogs.set_defaults(func=command_telegram_dialogs)

    webtrader_login = commands.add_parser(
        "webtrader-login",
        help="open a headed XM WebTrader profile and verify a manual Demo login",
    )
    webtrader_login.set_defaults(func=command_webtrader_login)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (
        BotRuntimeError,
        BrokerError,
        ConfigError,
        DashboardError,
        OSError,
        RuntimeEnvironmentError,
        TelegramAuthenticationError,
        TelegramConfigurationError,
        TelegramDependencyError,
        WebTraderError,
        argparse.ArgumentTypeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
