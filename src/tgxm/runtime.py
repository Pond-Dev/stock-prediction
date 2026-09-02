"""Async runtime wiring for Telegram ingestion and Demo-safe decisions.

The runtime owns resource lifetime and emits only numeric/internal identifiers
plus decision statuses.  Raw Telegram text, credentials, account allowlists,
and broker details are deliberately absent from its output contract.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import inspect
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Protocol

from tgxm.broker import BrokerAdapter, DemoAccountPolicy, MetaTrader5Broker
from tgxm.config import CONFIG_PATH, AppConfig, ConfigError, load_config
from tgxm.engine import DecisionStatus, ProcessDecision, TradingEngine
from tgxm.environment import load_integer_allowlist, load_text_allowlist
from tgxm.models import RawTelegramEvent
from tgxm.reconcile import reconcile_order_intents
from tgxm.store import RawEventRecord, SQLiteStore
from tgxm.telegram_client import (
    TelegramCredentials,
    TelegramMessageEnvelope,
    TelethonEventSource,
)
from tgxm.webtrader_broker import MetaTrader5ReadOnlyVerifier, WebTraderBroker
from tgxm.webtrader_click import PlaywrightWebTraderClicker


DEFAULT_DB_PATH = Path("data/tgxm.sqlite3")
DEFAULT_MAGIC = 26082701
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BotRuntimeError(RuntimeError):
    """Base class for startup/runtime orchestration failures."""


class RuntimeAlreadyRunningError(BotRuntimeError):
    """Raised when another process owns the database-specific runtime lock."""


class DotenvDependencyError(BotRuntimeError):
    """Raised when an explicitly requested env file cannot be parsed safely."""


class StaleTelegramEditError(BotRuntimeError):
    """An older, previously unseen edit arrived after a newer revision."""

    def __init__(self, latest_revision: int) -> None:
        super().__init__("out-of-order Telegram edit was ignored")
        self.latest_revision = latest_revision


class EventSource(Protocol):
    async def run(
        self,
        handler: Callable[[TelegramMessageEnvelope], Any],
    ) -> None: ...


EventSourceFactory = Callable[
    [TelegramCredentials, frozenset[int]],
    EventSource,
]
OutputFn = Callable[[str], None]


class _LazyWebTraderExecutor:
    """Delay browser startup until an operation actually needs WebTrader.

    Shadow/Demo Armed startup and receipt-based reconciliation stay read-only
    and therefore do not open a browser.  The first identity/form operation
    starts the dedicated Playwright worker exactly once.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self._lock = threading.RLock()
        self._started = False

    def initialize(self) -> None:
        return

    def _ensure_started(self) -> None:
        with self._lock:
            if not self._started:
                self._executor.initialize()
                self._started = True

    def shutdown(self) -> None:
        with self._lock:
            if self._started:
                self._executor.shutdown()
                self._started = False

    def inspect_identity(self, *, expected_login: str, expected_server: str) -> Any:
        self._ensure_started()
        return self._executor.inspect_identity(expected_login, expected_server)

    def prepare_order(self, request: Any) -> Any:
        self._ensure_started()
        return self._executor.prepare_order(request)

    def commit_once(
        self,
        token: Any,
        *,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> Any:
        self._ensure_started()
        return self._executor.commit_once(
            token,
            expected_quote,
            point,
            max_drift_points,
        )


def _runtime_broker(
    config: AppConfig,
    policy: DemoAccountPolicy,
) -> tuple[BrokerAdapter, int]:
    """Build the configured broker without granting browser startup authority.

    The WebTrader executor is lazy so Shadow mode, inactive Demo Armed mode,
    and restart reconciliation can inspect MT5 evidence without opening a
    browser. Browser-created orders use ``magic=0`` and rely on the durable
    WebTrader receipt for exact ownership instead of pretending to be EA
    orders.
    """

    if config.broker.adapter == "mt5":
        read_broker = MetaTrader5Broker(
            policy=policy,
            terminal_path=config.broker.terminal_path or None,
        )
        return read_broker, DEFAULT_MAGIC

    read_broker = MetaTrader5ReadOnlyVerifier(
        policy=policy,
        terminal_path=config.broker.terminal_path or None,
    )

    clicker = PlaywrightWebTraderClicker(
        url=config.broker.webtrader_url,
        allowed_origins=config.broker.webtrader_allowed_origins,
        profile_dir=config.broker.webtrader_profile_path,
        headless=config.broker.webtrader_headless,
        browser_channel=config.broker.webtrader_browser_channel,
        action_timeout_seconds=config.broker.webtrader_timeout_seconds,
        receipt_timeout_seconds=config.broker.webtrader_timeout_seconds,
    )
    readback_seconds = float(config.broker.webtrader_readback_seconds)
    readback_attempts = min(
        100,
        max(
            2,
            int(readback_seconds / 0.25) + 1,
        ),
    )
    poll_seconds = readback_seconds / (readback_attempts - 1)
    broker = WebTraderBroker(
        read_delegate=read_broker,
        executor=_LazyWebTraderExecutor(clicker),
        policy=policy,
        readback_attempts=readback_attempts,
        readback_poll_seconds=poll_seconds,
        max_browser_drift_points=Decimal(
            config.broker.webtrader_max_price_drift_points
        ),
        require_hedging=config.broker.webtrader_require_hedging,
    )
    return broker, 0


@dataclass(frozen=True, slots=True)
class RuntimeEventStatus:
    peer_id: int
    message_id: int
    revision: int
    status: str
    raw_event_id: int | None = None
    intent_id: int | None = None

    def to_dict(self) -> dict[str, int | str]:
        result: dict[str, int | str] = {
            "peer_id": self.peer_id,
            "message_id": self.message_id,
            "revision": self.revision,
            "status": self.status,
        }
        if self.raw_event_id is not None:
            result["raw_event_id"] = self.raw_event_id
        if self.intent_id is not None:
            result["intent_id"] = self.intent_id
        return result


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    events: tuple[RuntimeEventStatus, ...]

    @property
    def processed_count(self) -> int:
        return len(self.events)

    @property
    def status_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.status for item in self.events))


@dataclass(slots=True)
class _WaitingCandidate:
    record: RawEventRecord
    last_status: str = DecisionStatus.WAITING_ENTRY.value


_RETRYABLE_WAIT_STATUSES = frozenset(
    {
        DecisionStatus.WAITING_ENTRY,
        DecisionStatus.BROKER_UNAVAILABLE,
        DecisionStatus.STALE_TICK,
        DecisionStatus.SPREAD_BLOCKED,
        DecisionStatus.EXPOSURE_BLOCKED,
    }
)


class SingleInstanceLock:
    """One-byte advisory lock implemented for Windows and POSIX.

    The lock file is intentionally retained after release.  Removing a lock
    file can create two independently lockable inodes during a startup race.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise BotRuntimeError("single-instance lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            elif os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Python's supported desktop targets use nt/posix
                raise BotRuntimeError(
                    "TODO: single-instance locking is unsupported on this platform; "
                    "runtime will not start without a real lock"
                )
        except OSError as exc:
            handle.close()
            raise RuntimeAlreadyRunningError(
                "another TGXM runtime already owns the database lock"
            ) from exc
        except Exception:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - acquire refuses this platform
                raise BotRuntimeError(
                    "TODO: single-instance unlocking is unsupported on this platform"
                )
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


@contextmanager
def _dotenv_overlay(env_file: str | os.PathLike[str] | None) -> Iterator[None]:
    """Temporarily add values parsed by python-dotenv without overriding OS env."""

    if env_file is None:
        yield
        return
    path = Path(env_file)
    if not path.is_file():
        raise BotRuntimeError(f"environment file does not exist: {path}")
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise DotenvDependencyError(
            "env_file requires python-dotenv; install python-dotenv or preload OS environment variables"
        ) from exc
    try:
        parsed = dotenv_values(path)
    except Exception as exc:
        raise BotRuntimeError("python-dotenv could not parse the requested env_file") from exc
    if not isinstance(parsed, Mapping):
        raise BotRuntimeError("python-dotenv returned an invalid env mapping")

    additions: list[str] = []
    try:
        for key, value in parsed.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                raise BotRuntimeError("env_file contains an invalid environment-variable name")
            if value is None or not isinstance(value, str):
                raise BotRuntimeError(f"env_file variable {key} has no value")
            if key not in os.environ:
                os.environ[key] = value
                additions.append(key)
        yield
    finally:
        for key in additions:
            os.environ.pop(key, None)


def _enabled_peer_ids(config: AppConfig) -> frozenset[int]:
    missing = sorted(
        name
        for name, profile in config.channels.items()
        if profile.enabled and profile.peer_id is None
    )
    if missing:
        raise ConfigError(
            "enabled channel profiles require numeric peer_id values: "
            + ", ".join(missing)
        )
    peers = frozenset(
        profile.peer_id
        for profile in config.channels.values()
        if profile.enabled and profile.peer_id is not None
    )
    if not peers:
        raise ConfigError("at least one enabled channel with a numeric peer_id is required")
    return peers


def _demo_policy_from_environment(config: AppConfig) -> DemoAccountPolicy:
    accounts = load_integer_allowlist(config.broker.allowed_demo_accounts_env)
    servers = load_text_allowlist(config.broker.allowed_servers_env)
    return DemoAccountPolicy(
        allowed_demo_accounts=frozenset(str(value) for value in accounts),
        allowed_servers=frozenset(servers),
        allowed_symbols=frozenset(config.symbol_aliases.values()),
        max_tick_age_seconds=config.broker.max_tick_age_seconds,
    )


def _same_envelope_evidence(
    record: RawEventRecord,
    envelope: TelegramMessageEnvelope,
) -> bool:
    metadata = record.metadata
    return (
        record.raw_text == envelope.text
        and bool(metadata.get("is_edit")) == (envelope.event_kind == "edit")
        and record.message_time_utc == envelope.message_time_utc
        and metadata.get("edit_time_utc")
        == (
            envelope.edit_time_utc.astimezone(UTC).isoformat()
            if envelope.edit_time_utc is not None
            else None
        )
        and record.reply_to_message_id == envelope.reply_to_message_id
        and metadata.get("forward_origin") == envelope.forward_origin
    )


def envelope_to_raw_event(
    envelope: TelegramMessageEnvelope,
    store: SQLiteStore,
) -> RawTelegramEvent:
    """Assign a stable revision by comparing durable source evidence.

    The same new/edit envelope reuses its prior revision, making replay
    idempotent.  A materially different edit receives ``max(revision) + 1``.
    The first observed revision is 1, matching ``RawTelegramEvent`` defaults.
    """

    records = [
        record
        for record in store.list_raw_events(chat_id=envelope.peer_id)
        if record.message_id == envelope.message_id
    ]
    matching = [
        record for record in records if _same_envelope_evidence(record, envelope)
    ]
    if matching:
        revision = max(record.revision for record in matching)
    else:
        if envelope.event_kind == "edit":
            if envelope.edit_time_utc is None:
                raise StaleTelegramEditError(
                    max((record.revision for record in records), default=0)
                )
            edit_times = [
                datetime.fromisoformat(str(record.metadata["edit_time_utc"]))
                for record in records
                if record.metadata.get("edit_time_utc")
            ]
            if edit_times and envelope.edit_time_utc <= max(edit_times):
                raise StaleTelegramEditError(
                    max(record.revision for record in records)
                )
            revision = max((record.revision for record in records), default=0) + 1
        else:
            if records:
                # Telegram message IDs are immutable identities. A materially
                # different second "new" envelope is stale/corrupt evidence,
                # never a revision newer than an edit.
                raise StaleTelegramEditError(max(record.revision for record in records))
            revision = 1
    return RawTelegramEvent(
        channel_id=envelope.peer_id,
        message_id=envelope.message_id,
        text=envelope.text,
        revision=revision,
        is_edit=envelope.event_kind == "edit",
        edit_time_utc=envelope.edit_time_utc,
        reply_to_message_id=envelope.reply_to_message_id,
        forward_origin=envelope.forward_origin,
        message_time_utc=envelope.message_time_utc,
    )


async def run_bot(
    config_path: str | os.PathLike[str] = CONFIG_PATH,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    demo_active: bool = False,
    *,
    event_source_factory: EventSourceFactory = TelethonEventSource,
    env_file: str | os.PathLike[str] | None = None,
    output_fn: OutputFn = print,
) -> RuntimeSummary:
    """Run the Telegram-to-XM event loop until the event source disconnects.

    Observe mode constructs no broker and reads no MT5 account/server
    allowlists.  Shadow and Demo Armed always construct an exact Demo policy
    from environment-variable names held in validated configuration.  Setting
    ``demo_active`` is a volatile second gate and is never persisted.
    """

    config = load_config(config_path)
    if demo_active and config.runtime.mode != "demo_armed":
        raise ConfigError("demo_active requires runtime.mode=demo_armed")

    database = str(db_path)
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{database}.lock")
    lock = SingleInstanceLock(lock_path)

    with lock, _dotenv_overlay(env_file), SQLiteStore(database) as store:
        peers = _enabled_peer_ids(config)
        credentials = TelegramCredentials.from_environment(
            api_id_env=config.telegram.api_id_env,
            api_hash_env=config.telegram.api_hash_env,
            session_env=config.telegram.session_env,
        )

        broker: BrokerAdapter | None = None
        order_magic = DEFAULT_MAGIC
        if config.runtime.mode in {"shadow", "demo_armed"}:
            policy = _demo_policy_from_environment(config)
            broker, order_magic = _runtime_broker(config, policy)

        statuses: list[RuntimeEventStatus] = []
        event_lock = asyncio.Lock()
        engine: TradingEngine
        waiting: dict[tuple[int, int], _WaitingCandidate] = {
            (record.chat_id, record.message_id): _WaitingCandidate(record)
            for record in store.list_waiting_raw_events()
        }

        def emit_decision(
            *,
            peer_id: int,
            message_id: int,
            revision: int,
            decision: ProcessDecision,
        ) -> RuntimeEventStatus:
            status = RuntimeEventStatus(
                peer_id=peer_id,
                message_id=message_id,
                revision=revision,
                status=decision.status.value,
                raw_event_id=decision.raw_event_id,
                intent_id=decision.intent_id,
            )
            statuses.append(status)
            output_fn(json.dumps(status.to_dict(), sort_keys=True))
            return status

        def clear_waiting(
            key: tuple[int, int], candidate: _WaitingCandidate
        ) -> None:
            store.clear_waiting_entry(
                candidate.record.chat_id,
                candidate.record.message_id,
                expected_revision=candidate.record.revision,
            )
            current = waiting.get(key)
            if current is candidate:
                waiting.pop(key, None)

        def register_waiting(event: RawTelegramEvent) -> _WaitingCandidate:
            record = store.get_raw_event(
                event.channel_id,
                event.message_id,
                event.revision,
            )
            if record is None:
                raise BotRuntimeError(
                    "WAITING_ENTRY decision has no exact durable raw event"
                )
            store.register_waiting_entry(record)
            candidate = _WaitingCandidate(record)
            waiting[(event.channel_id, event.message_id)] = candidate
            return candidate

        async def handle(envelope: TelegramMessageEnvelope) -> None:
            if envelope.peer_id not in peers:
                return
            async with event_lock:
                try:
                    event = envelope_to_raw_event(envelope, store)
                except StaleTelegramEditError as exc:
                    status = RuntimeEventStatus(
                        peer_id=envelope.peer_id,
                        message_id=envelope.message_id,
                        revision=exc.latest_revision,
                        status="STALE_EDIT_IGNORED",
                    )
                    statuses.append(status)
                    output_fn(json.dumps(status.to_dict(), sort_keys=True))
                    return
                key = (event.channel_id, event.message_id)
                prior = waiting.get(key)
                if prior is not None and event.revision > prior.record.revision:
                    # A newer edit supersedes the old execution authority before
                    # the replacement is parsed. A crash here fails closed.
                    clear_waiting(key, prior)
                decision = engine.process_event(
                    event,
                    observed_at_utc=envelope.observed_at_utc,
                )
                emit_decision(
                    peer_id=envelope.peer_id,
                    message_id=envelope.message_id,
                    revision=event.revision,
                    decision=decision,
                )
                if (
                    config.runtime.mode != "observe"
                    and decision.status is DecisionStatus.WAITING_ENTRY
                ):
                    register_waiting(event)
                elif decision.status is not DecisionStatus.DUPLICATE_EVENT:
                    current = waiting.get(key)
                    if current is not None and event.revision >= current.record.revision:
                        clear_waiting(key, current)

        async def reconstruct_waiting_entries() -> None:
            if config.runtime.mode == "observe":
                return
            async with event_lock:
                for key, candidate in list(waiting.items()):
                    if candidate.record.chat_id not in peers:
                        clear_waiting(key, candidate)
                        continue
                    decision = engine.reevaluate_raw_event(
                        candidate.record,
                        observed_at_utc=datetime.now(UTC),
                    )
                    terminal = decision.status not in _RETRYABLE_WAIT_STATUSES
                    if terminal:
                        # Revoke durable execution authority before any output
                        # callback can fail or terminate the process.
                        clear_waiting(key, candidate)
                    emit_decision(
                        peer_id=candidate.record.chat_id,
                        message_id=candidate.record.message_id,
                        revision=candidate.record.revision,
                        decision=decision,
                    )
                    candidate.last_status = decision.status.value

        async def poll_waiting_entries(stop: asyncio.Event) -> None:
            poll_seconds = float(config.execution.market_poll_seconds)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
                if stop.is_set():
                    return
                async with event_lock:
                    for key, candidate in list(waiting.items()):
                        if waiting.get(key) is not candidate:
                            continue
                        decision = engine.reevaluate_raw_event(
                            candidate.record,
                            observed_at_utc=datetime.now(UTC),
                        )
                        terminal = decision.status not in _RETRYABLE_WAIT_STATUSES
                        if terminal:
                            clear_waiting(key, candidate)
                        if decision.status.value != candidate.last_status:
                            emit_decision(
                                peer_id=candidate.record.chat_id,
                                message_id=candidate.record.message_id,
                                revision=candidate.record.revision,
                                decision=decision,
                            )
                            candidate.last_status = decision.status.value

        try:
            if broker is not None:
                broker.initialize()
                broker.discover_account()
                reconciliation = reconcile_order_intents(
                    store,
                    broker,
                    magic=order_magic,
                )
                output_fn(
                    json.dumps(
                        {
                            "status": "RECONCILIATION",
                            "resolved_count": len(reconciliation.resolved),
                            "unresolved_count": len(reconciliation.unresolved),
                            "error_count": len(reconciliation.errors),
                        },
                        sort_keys=True,
                    )
                )
                if not reconciliation.clean and demo_active:
                    raise BotRuntimeError(
                        "Demo Active startup blocked by unresolved reconciliation state"
                    )
                engine_broker = broker if reconciliation.clean else None
            else:
                engine_broker = None
            engine = TradingEngine(
                config=config,
                store=store,
                broker=engine_broker,
                demo_active=demo_active,
                magic=order_magic,
            )
            await reconstruct_waiting_entries()
            source = event_source_factory(credentials, peers)
            if inspect.isawaitable(source):
                source = await source
            if not hasattr(source, "run"):
                raise BotRuntimeError(
                    "event_source_factory must return an object with async run()"
                )
            result = source.run(handle)
            if not inspect.isawaitable(result):
                raise BotRuntimeError("event source run() must be async")
            if config.runtime.mode == "observe":
                await result
            else:
                stop_polling = asyncio.Event()

                async def source_runner() -> None:
                    try:
                        await result
                    finally:
                        stop_polling.set()

                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(source_runner())
                    tasks.create_task(poll_waiting_entries(stop_polling))
        finally:
            if broker is not None:
                broker.shutdown()

    return RuntimeSummary(events=tuple(statuses))


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_MAGIC",
    "BotRuntimeError",
    "DotenvDependencyError",
    "EventSource",
    "EventSourceFactory",
    "RuntimeAlreadyRunningError",
    "RuntimeEventStatus",
    "RuntimeSummary",
    "SingleInstanceLock",
    "StaleTelegramEditError",
    "envelope_to_raw_event",
    "run_bot",
]
