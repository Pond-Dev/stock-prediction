from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest

from tgxm.broker import (
    AccountSnapshot,
    DemoAccountPolicy,
    FakeBroker,
    SymbolSnapshot,
    TickSnapshot,
)
from tgxm.config import AppConfig, ConfigError, save_config
from tgxm.runtime import (
    BotRuntimeError,
    RuntimeAlreadyRunningError,
    SingleInstanceLock,
    run_bot,
)
from tgxm.store import OrderIntent, SQLiteStore
from tgxm.telegram_client import TelegramMessageEnvelope


PEER_ID = -1008765432109
MESSAGE_TIME = datetime.now(UTC).replace(microsecond=0)
SIGNAL = "GOLD SELL 4601 OR 4605 SL 4618 TP 4595 TP 4590 PRIVATE_MARKER"


class ScriptedEventSource:
    def __init__(self, envelopes: list[TelegramMessageEnvelope]) -> None:
        self.envelopes = envelopes

    async def run(self, handler: Any) -> None:
        for envelope in self.envelopes:
            await handler(envelope)


class CapturingEventSourceFactory:
    def __init__(self, envelopes: list[TelegramMessageEnvelope]) -> None:
        self.envelopes = envelopes
        self.credentials: Any = None
        self.peer_ids: frozenset[int] | None = None
        self.source: ScriptedEventSource | None = None

    def __call__(self, credentials: Any, peer_ids: frozenset[int]) -> ScriptedEventSource:
        self.credentials = credentials
        self.peer_ids = peer_ids
        self.source = ScriptedEventSource(self.envelopes)
        return self.source


class CapturingMT5Broker:
    instances: list["CapturingMT5Broker"] = []

    def __init__(self, *, policy: DemoAccountPolicy, terminal_path: str | None) -> None:
        self.policy = policy
        self.terminal_path = terminal_path
        self.initialized = False
        self.shutdown_called = False
        self.inner = FakeBroker(
            policy=policy,
            account=AccountSnapshot(
                login="123456",
                server="XM-Demo",
                company="XM",
                is_demo=True,
                connected=True,
                trade_allowed=True,
                trade_api_disabled=False,
                margin_mode="RETAIL_HEDGING",
            ),
            symbols={
                "GOLD": SymbolSnapshot(
                    symbol="GOLD",
                    visible=True,
                    trade_mode="FULL",
                    digits=2,
                    point=Decimal("0.01"),
                    tick_size=Decimal("0.01"),
                    tick_value=Decimal("1"),
                    contract_size=Decimal("100"),
                    volume_min=Decimal("0.01"),
                    volume_max=Decimal("50"),
                    volume_step=Decimal("0.01"),
                )
            },
            ticks={
                "GOLD": TickSnapshot(
                    symbol="GOLD",
                    bid=Decimal("4601"),
                    ask=Decimal("4602"),
                    time_utc=MESSAGE_TIME,
                )
            },
        )
        self.instances.append(self)

    def initialize(self) -> None:
        self.initialized = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def discover_account(self):
        return self.inner.discover_account()

    def discover_symbol(self, symbol: str):
        return self.inner.discover_symbol(symbol)

    def get_tick(self, symbol: str):
        return self.inner.get_tick(symbol)

    def list_open_positions(self, exact_symbol: str | None = None):
        return self.inner.list_open_positions(exact_symbol)

    def list_pending_orders(self, exact_symbol: str | None = None):
        return self.inner.list_pending_orders(exact_symbol)

    def read_back_market_order(self, request: Any, result: Any):
        return self.inner.read_back_market_order(request, result)

    def check_market_order(self, request: Any):
        return self.inner.check_market_order(request)

    def submit_market_order(self, request: Any):
        return self.inner.submit_market_order(request)


def set_broker_tick(
    broker: CapturingMT5Broker,
    *,
    bid: str,
    ask: str,
    time_utc: datetime = MESSAGE_TIME,
) -> None:
    broker.inner.ticks["GOLD"] = TickSnapshot(
        symbol="GOLD",
        bid=Decimal(bid),
        ask=Decimal(ask),
        time_utc=time_utc,
    )


def configured(mode: str = "observe") -> AppConfig:
    base = AppConfig.default()
    profile = replace(
        base.channels["mr_charlie"],
        peer_id=PEER_ID,
        trade_enabled=mode in {"shadow", "demo_armed"},
    )
    return replace(
        base,
        runtime=replace(base.runtime, mode=mode),
        broker=replace(
            base.broker,
            adapter="mt5",
            terminal_path=("C:/XM MT5/terminal64.exe" if mode != "observe" else ""),
        ),
        channels={**base.channels, "mr_charlie": profile},
    ).validate()


def envelope(
    *,
    text: str = SIGNAL,
    kind: str = "new",
    observed_offset: int = 0,
    edit_offset: int | None = None,
    message_id: int = 77,
) -> TelegramMessageEnvelope:
    return TelegramMessageEnvelope(
        peer_id=PEER_ID,
        message_id=message_id,
        text=text,
        message_time_utc=MESSAGE_TIME,
        observed_at_utc=MESSAGE_TIME + timedelta(seconds=observed_offset),
        event_kind=kind,
        edit_time_utc=(
            MESSAGE_TIME
            + timedelta(seconds=edit_offset if edit_offset is not None else observed_offset)
            if kind == "edit"
            else None
        ),
    )


def persist_unfinished_intent(db_path: Path) -> None:
    with SQLiteStore(db_path) as store:
        store.create_order_intent(
            OrderIntent(
                account_id="123456",
                signal_id="older-redacted-signal",
                signal_revision=1,
                leg_index=0,
                broker_symbol="GOLD",
                side="SELL",
                volume=Decimal("0.01"),
                stop_loss=Decimal("4618"),
                take_profit=Decimal("4595"),
                entry_price=Decimal("4601"),
                client_reference="tgxm-older-redacted-0",
            )
        )


@pytest.fixture
def telegram_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGXM_TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TGXM_TELEGRAM_API_HASH", "telegram-secret-hash")
    monkeypatch.setenv("TGXM_TELEGRAM_SESSION", "telegram-secret-session")


def test_observe_does_not_read_mt5_allowlists_or_construct_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "observe.sqlite3"
    save_config(configured("observe"), config_path)
    monkeypatch.delenv("TGXM_ALLOWED_DEMO_ACCOUNTS", raising=False)
    monkeypatch.delenv("TGXM_ALLOWED_DEMO_SERVERS", raising=False)

    class ForbiddenBroker:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("Observe mode must not construct an MT5 broker")

    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", ForbiddenBroker)
    factory = CapturingEventSourceFactory([envelope()])
    output: list[str] = []

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=factory,
            output_fn=output.append,
        )
    )

    assert summary.processed_count == 1
    assert summary.events[0].status == "OBSERVED"
    assert factory.peer_ids == frozenset({PEER_ID})
    assert factory.credentials.api_id == 12345
    with SQLiteStore(db_path) as store:
        assert len(store.list_raw_events()) == 1

    payload = json.loads(output[0])
    assert set(payload) <= {
        "peer_id",
        "message_id",
        "revision",
        "status",
        "raw_event_id",
        "intent_id",
    }
    rendered = "\n".join(output)
    assert "PRIVATE_MARKER" not in rendered
    assert "telegram-secret" not in rendered


def test_shadow_builds_exact_demo_policy_from_named_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "shadow.sqlite3"
    save_config(configured("shadow"), config_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456,789012")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo,XM-Backup-Demo")
    CapturingMT5Broker.instances.clear()
    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", CapturingMT5Broker)
    output: list[str] = []

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=output.append,
        )
    )

    assert [item.status for item in summary.events] == ["SHADOW_APPROVED"]
    broker = CapturingMT5Broker.instances[0]
    assert broker.policy.allowed_demo_accounts == frozenset({"123456", "789012"})
    assert broker.policy.allowed_servers == frozenset({"XM-Demo", "XM-Backup-Demo"})
    assert broker.policy.allowed_symbols == frozenset({"GOLD"})
    assert broker.policy.max_tick_age_seconds == Decimal("5")
    assert broker.terminal_path == "C:/XM MT5/terminal64.exe"
    assert broker.initialized is True
    assert broker.shutdown_called is True
    assert "123456" not in "\n".join(output)
    assert "XM-Demo" not in "\n".join(output)


def test_webtrader_shadow_uses_hybrid_broker_without_starting_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    base = configured("shadow")
    save_config(
        replace(base, broker=replace(base.broker, adapter="xm_webtrader")),
        config_path,
    )
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()
    monkeypatch.setattr(
        "tgxm.runtime.MetaTrader5ReadOnlyVerifier", CapturingMT5Broker
    )

    class BrowserMustStayLazy:
        instances: list["BrowserMustStayLazy"] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.initialize_calls = 0
            self.shutdown_calls = 0
            self.instances.append(self)

        def initialize(self) -> None:
            self.initialize_calls += 1
            raise AssertionError("Shadow mode must not start the WebTrader browser")

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    monkeypatch.setattr(
        "tgxm.runtime.PlaywrightWebTraderClicker", BrowserMustStayLazy
    )

    summary = asyncio.run(
        run_bot(
            config_path,
            tmp_path / "web-shadow.sqlite3",
            event_source_factory=CapturingEventSourceFactory([]),
            output_fn=lambda _: None,
        )
    )

    assert summary.processed_count == 0
    assert len(BrowserMustStayLazy.instances) == 1
    browser = BrowserMustStayLazy.instances[0]
    assert browser.initialize_calls == 0
    assert browser.shutdown_calls == 0
    assert browser.kwargs["url"] == base.broker.webtrader_url
    assert CapturingMT5Broker.instances[-1].initialized is True
    assert CapturingMT5Broker.instances[-1].shutdown_called is True


def test_demo_submission_requires_volatile_active_gate_and_uses_mt5_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    save_config(configured("demo_armed"), config_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()
    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", CapturingMT5Broker)

    inactive = asyncio.run(
        run_bot(
            config_path,
            tmp_path / "inactive.sqlite3",
            demo_active=False,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=lambda _: None,
        )
    )
    inactive_broker = CapturingMT5Broker.instances[-1]
    assert inactive.events[0].status == "DEMO_NOT_ACTIVE"
    assert inactive_broker.inner.sent_requests == []

    active = asyncio.run(
        run_bot(
            config_path,
            tmp_path / "active.sqlite3",
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=lambda _: None,
        )
    )
    active_broker = CapturingMT5Broker.instances[-1]
    assert active.events[0].status == "OPEN"
    assert len(active_broker.inner.sent_requests) == 1


def test_repeated_identical_edit_reuses_revision_and_deduplicates(
    tmp_path: Path,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "edits.sqlite3"
    save_config(configured("observe"), config_path)
    edited = "GOLD SELL 4601 OR 4605 SL 4618 TP 4590"
    factory = CapturingEventSourceFactory(
        [
            envelope(observed_offset=1),
            envelope(text=edited, kind="edit", observed_offset=2),
            envelope(text=edited, kind="edit", observed_offset=30, edit_offset=2),
        ]
    )

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=factory,
            output_fn=lambda _: None,
        )
    )

    assert [item.revision for item in summary.events] == [1, 2, 2]
    assert [item.status for item in summary.events] == [
        "OBSERVED",
        "OBSERVED",
        "DUPLICATE_EVENT",
    ]
    with SQLiteStore(db_path) as store:
        records = store.list_raw_events(chat_id=PEER_ID)
        assert [record.revision for record in records] == [1, 2]
        assert len(records) == 2

    replay = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=CapturingEventSourceFactory(
                [envelope(text=edited, kind="edit", observed_offset=60, edit_offset=2)]
            ),
            output_fn=lambda _: None,
        )
    )
    assert replay.events[0].revision == 2
    assert replay.events[0].status == "DUPLICATE_EVENT"


def test_out_of_order_unseen_edit_is_ignored_without_revising_durable_state(
    tmp_path: Path,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "out-of-order.sqlite3"
    save_config(configured("observe"), config_path)
    source = CapturingEventSourceFactory(
        [
            envelope(observed_offset=0),
            envelope(text=SIGNAL + " A", kind="edit", observed_offset=2, edit_offset=2),
            envelope(text=SIGNAL + " B", kind="edit", observed_offset=3, edit_offset=3),
            envelope(text=SIGNAL + " OLD", kind="edit", observed_offset=4, edit_offset=1),
        ]
    )

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=source,
            output_fn=lambda _: None,
        )
    )

    assert [item.revision for item in summary.events] == [1, 2, 3, 3]
    assert summary.events[-1].status == "STALE_EDIT_IGNORED"
    with SQLiteStore(db_path) as store:
        assert [item.revision for item in store.list_raw_events()] == [1, 2, 3]


def test_non_identical_late_new_event_cannot_supersede_an_edit(
    tmp_path: Path,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "late-new.sqlite3"
    save_config(configured("observe"), config_path)
    edited = envelope(
        text=SIGNAL + " EDITED",
        kind="edit",
        observed_offset=2,
        edit_offset=2,
    )
    late_new = envelope(text=SIGNAL + " LATE NEW", observed_offset=3)

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=CapturingEventSourceFactory(
                [envelope(), edited, late_new]
            ),
            output_fn=lambda _: None,
        )
    )

    assert [item.revision for item in summary.events] == [1, 2, 2]
    assert summary.events[-1].status == "STALE_EDIT_IGNORED"
    with SQLiteStore(db_path) as store:
        assert [item.revision for item in store.list_raw_events()] == [1, 2]


def test_shadow_reconciliation_unresolved_hard_locks_new_entries_but_keeps_observing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "shadow-unresolved.sqlite3"
    save_config(configured("shadow"), config_path)
    persist_unfinished_intent(db_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()
    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", CapturingMT5Broker)
    output: list[str] = []

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=output.append,
        )
    )

    assert summary.events[0].status == "HARD_LOCK"
    broker = CapturingMT5Broker.instances[0]
    assert broker.inner.checked_requests == []
    assert broker.inner.sent_requests == []
    reconciliation = json.loads(output[0])
    assert reconciliation == {
        "error_count": 0,
        "resolved_count": 0,
        "status": "RECONCILIATION",
        "unresolved_count": 1,
    }
    assert "older-redacted-signal" not in "\n".join(output)
    assert "123456" not in "\n".join(output)


def test_demo_active_unresolved_reconciliation_stops_before_event_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "demo-unresolved.sqlite3"
    save_config(configured("demo_armed"), config_path)
    persist_unfinished_intent(db_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()
    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", CapturingMT5Broker)
    output: list[str] = []
    factory = CapturingEventSourceFactory([envelope()])

    with pytest.raises(BotRuntimeError, match="blocked by unresolved reconciliation"):
        asyncio.run(
            run_bot(
                config_path,
                db_path,
                demo_active=True,
                event_source_factory=factory,
                output_fn=output.append,
            )
        )

    assert output and json.loads(output[0])["unresolved_count"] == 1
    assert factory.source is None
    with SQLiteStore(db_path) as store:
        assert store.list_raw_events() == []
    assert CapturingMT5Broker.instances[0].shutdown_called is True


def test_runtime_filters_non_allowlisted_envelope_defense_in_depth(
    tmp_path: Path,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    save_config(configured("observe"), config_path)
    unexpected = TelegramMessageEnvelope(
        peer_id=-100999,
        message_id=1,
        text=SIGNAL,
        message_time_utc=MESSAGE_TIME,
        observed_at_utc=MESSAGE_TIME,
    )

    summary = asyncio.run(
        run_bot(
            config_path,
            tmp_path / "filtered.sqlite3",
            event_source_factory=CapturingEventSourceFactory([unexpected]),
            output_fn=lambda _: None,
        )
    )

    assert summary.events == ()


def test_demo_waiting_entry_opens_once_when_poll_observes_price_enter_zone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "polling.sqlite3"
    config = configured("demo_armed")
    config = replace(
        config,
        execution=replace(config.execution, market_poll_seconds=0.1),
    ).validate()
    save_config(config, config_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()

    def broker_factory(
        *, policy: DemoAccountPolicy, terminal_path: str | None
    ) -> CapturingMT5Broker:
        broker = CapturingMT5Broker(policy=policy, terminal_path=terminal_path)
        set_broker_tick(broker, bid="4610", ask="4611")
        return broker

    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", broker_factory)

    class MovingPriceSource:
        async def run(self, handler: Any) -> None:
            await handler(envelope())
            await asyncio.sleep(0.03)
            set_broker_tick(
                CapturingMT5Broker.instances[-1],
                bid="4603",
                ask="4604",
                time_utc=datetime.now(UTC),
            )
            # Two poll periods prove that a terminal candidate is removed and
            # cannot submit a second time without another Telegram event.
            await asyncio.sleep(0.25)

    output: list[str] = []
    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=lambda _credentials, _peers: MovingPriceSource(),
            output_fn=output.append,
        )
    )

    assert [item.status for item in summary.events] == ["WAITING_ENTRY", "OPEN"]
    broker = CapturingMT5Broker.instances[-1]
    assert len(broker.inner.sent_requests) == 1
    with SQLiteStore(db_path) as store:
        assert store.list_waiting_entries() == []
        assert len(store.list_order_intents()) == 1
    rendered = "\n".join(output)
    assert "PRIVATE_MARKER" not in rendered
    assert "telegram-secret" not in rendered


def test_waiting_entry_is_reconstructed_from_registry_and_not_rearmed_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "waiting-restart.sqlite3"
    save_config(configured("demo_armed"), config_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()
    prices = iter(
        [
            ("4610", "4611"),
            ("4603", "4604"),
            ("4603", "4604"),
        ]
    )

    def broker_factory(
        *, policy: DemoAccountPolicy, terminal_path: str | None
    ) -> CapturingMT5Broker:
        broker = CapturingMT5Broker(policy=policy, terminal_path=terminal_path)
        bid, ask = next(prices)
        set_broker_tick(
            broker,
            bid=bid,
            ask=ask,
            time_utc=(
                MESSAGE_TIME
                if len(CapturingMT5Broker.instances) == 1
                else datetime.now(UTC)
            ),
        )
        return broker

    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", broker_factory)

    first = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=lambda _: None,
        )
    )
    assert [item.status for item in first.events] == ["WAITING_ENTRY"]
    assert CapturingMT5Broker.instances[0].inner.sent_requests == []
    with SQLiteStore(db_path) as store:
        waiting = store.list_waiting_entries()
        assert [(item.message_id, item.revision) for item in waiting] == [(77, 1)]

    second = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([]),
            output_fn=lambda _: None,
        )
    )
    assert [item.status for item in second.events] == ["OPEN"]
    assert len(CapturingMT5Broker.instances[1].inner.sent_requests) == 1
    with SQLiteStore(db_path) as store:
        assert store.list_waiting_entries() == []

    third = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([]),
            output_fn=lambda _: None,
        )
    )
    assert third.events == ()
    assert CapturingMT5Broker.instances[2].inner.sent_requests == []


def test_terminal_reconstruction_revokes_waiting_before_output_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "terminal-output-failure.sqlite3"
    save_config(configured("demo_armed"), config_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()
    prices = iter(
        [
            ("4610", "4611"),  # initial WAITING_ENTRY
            ("4594", "4595"),  # terminal MISSED on restart
            ("4603", "4604"),  # would be eligible if stale authority survived
        ]
    )

    def broker_factory(
        *, policy: DemoAccountPolicy, terminal_path: str | None
    ) -> CapturingMT5Broker:
        broker = CapturingMT5Broker(policy=policy, terminal_path=terminal_path)
        bid, ask = next(prices)
        set_broker_tick(
            broker,
            bid=bid,
            ask=ask,
            time_utc=(
                MESSAGE_TIME
                if len(CapturingMT5Broker.instances) == 1
                else datetime.now(UTC)
            ),
        )
        return broker

    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", broker_factory)
    first = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=lambda _: None,
        )
    )
    assert [item.status for item in first.events] == ["WAITING_ENTRY"]

    def fail_on_missed(payload: str) -> None:
        if json.loads(payload).get("status") == "MISSED":
            raise RuntimeError("simulated output sink failure")

    with pytest.raises(RuntimeError, match="output sink failure"):
        asyncio.run(
            run_bot(
                config_path,
                db_path,
                demo_active=True,
                event_source_factory=CapturingEventSourceFactory([]),
                output_fn=fail_on_missed,
            )
        )
    with SQLiteStore(db_path) as store:
        assert store.list_waiting_entries() == []

    restarted = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([]),
            output_fn=lambda _: None,
        )
    )
    assert restarted.events == ()
    assert CapturingMT5Broker.instances[2].inner.sent_requests == []


def test_newer_non_actionable_edit_clears_waiting_authority_permanently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    telegram_environment: None,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "waiting-edit.sqlite3"
    save_config(configured("demo_armed"), config_path)
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_ACCOUNTS", "123456")
    monkeypatch.setenv("TGXM_ALLOWED_DEMO_SERVERS", "XM-Demo")
    CapturingMT5Broker.instances.clear()

    def broker_factory(
        *, policy: DemoAccountPolicy, terminal_path: str | None
    ) -> CapturingMT5Broker:
        broker = CapturingMT5Broker(policy=policy, terminal_path=terminal_path)
        set_broker_tick(broker, bid="4610", ask="4611")
        return broker

    monkeypatch.setattr("tgxm.runtime.MetaTrader5Broker", broker_factory)
    edited = envelope(
        text="GOLD SELL TP2 HIT PROFIT DONE",
        kind="edit",
        observed_offset=1,
        edit_offset=1,
    )

    first = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([envelope(), edited]),
            output_fn=lambda _: None,
        )
    )
    assert [item.status for item in first.events] == [
        "WAITING_ENTRY",
        "NON_ACTIONABLE",
    ]
    assert [item.revision for item in first.events] == [1, 2]
    with SQLiteStore(db_path) as store:
        assert store.list_waiting_entries() == []

    restarted = asyncio.run(
        run_bot(
            config_path,
            db_path,
            demo_active=True,
            event_source_factory=CapturingEventSourceFactory([]),
            output_fn=lambda _: None,
        )
    )
    assert restarted.events == ()
    assert all(
        broker.inner.sent_requests == [] for broker in CapturingMT5Broker.instances
    )


def test_explicit_env_file_uses_python_dotenv_and_restores_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "settings.local.json"
    db_path = tmp_path / "dotenv.sqlite3"
    env_path = tmp_path / ".env"
    env_path.write_text("fixture content parsed by injected python-dotenv", encoding="utf-8")
    save_config(configured("observe"), config_path)
    for name in (
        "TGXM_TELEGRAM_API_ID",
        "TGXM_TELEGRAM_API_HASH",
        "TGXM_TELEGRAM_SESSION",
    ):
        monkeypatch.delenv(name, raising=False)

    fake_dotenv = ModuleType("dotenv")
    fake_dotenv.dotenv_values = lambda _: {
        "TGXM_TELEGRAM_API_ID": "12345",
        "TGXM_TELEGRAM_API_HASH": "dotenv-secret-hash",
        "TGXM_TELEGRAM_SESSION": "dotenv-secret-session",
    }
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    output: list[str] = []

    summary = asyncio.run(
        run_bot(
            config_path,
            db_path,
            env_file=env_path,
            event_source_factory=CapturingEventSourceFactory([envelope()]),
            output_fn=output.append,
        )
    )

    assert summary.events[0].status == "OBSERVED"
    assert "dotenv-secret" not in "\n".join(output)
    assert "TGXM_TELEGRAM_API_ID" not in os.environ
    assert "TGXM_TELEGRAM_API_HASH" not in os.environ
    assert "TGXM_TELEGRAM_SESSION" not in os.environ


def test_single_instance_lock_fails_closed_and_can_be_reacquired(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeAlreadyRunningError, match="already owns"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
    assert path.exists()


def test_enabled_profile_without_peer_id_fails_before_credentials_are_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "settings.local.json"
    save_config(AppConfig.default(), config_path)
    monkeypatch.delenv("TGXM_TELEGRAM_API_ID", raising=False)

    with pytest.raises(ConfigError, match="require numeric peer_id"):
        asyncio.run(
            run_bot(
                config_path,
                tmp_path / "missing-peer.sqlite3",
                event_source_factory=CapturingEventSourceFactory([]),
                output_fn=lambda _: None,
            )
        )
