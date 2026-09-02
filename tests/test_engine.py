from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tgxm.broker import (
    AccountSnapshot,
    BrokerOutcome,
    BrokerResult,
    DemoAccountPolicy,
    FakeBroker,
    PendingOrderSnapshot,
    PositionSnapshot,
    SymbolSnapshot,
    TickSnapshot,
)
from tgxm.config import AppConfig
from tgxm.engine import DecisionStatus, TradingEngine
from tgxm.models import RawTelegramEvent
from tgxm.store import IntentStatus, OrderIntent, SQLiteStore


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)


def configured(mode="observe", *, trade_enabled=False):
    base = AppConfig.default()
    profile = replace(
        base.channels["mr_charlie"],
        peer_id=-100123,
        trade_enabled=trade_enabled,
    )
    terminal = "C:/Program Files/XM MT5/terminal64.exe" if mode != "observe" else ""
    return replace(
        base,
        runtime=replace(base.runtime, mode=mode),
        broker=replace(base.broker, terminal_path=terminal),
        channels={**base.channels, "mr_charlie": profile},
    ).validate()


def broker(
    *, bid="4601", ask="4602", send_results=(), positions=(), pending_orders=()
):
    return FakeBroker(
        policy=DemoAccountPolicy(
            allowed_demo_accounts=frozenset({"123456"}),
            allowed_servers=frozenset({"XM-Demo"}),
            allowed_symbols=frozenset({"GOLD"}),
        ),
        account=AccountSnapshot(
            login="123456",
            server="XM-Demo",
            company="XM",
            is_demo=True,
            connected=True,
            trade_allowed=True,
            trade_api_disabled=False,
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
                bid=Decimal(bid),
                ask=Decimal(ask),
                time_utc=NOW,
            )
        },
        positions=positions,
        pending_orders=pending_orders,
        send_results=send_results,
        clock=lambda: NOW,
    )


def event(
    message_id=10,
    text="GOLD SELL 4601 OR 4605 SL 4618 TP 4595 TP 4590",
    message_time=NOW,
):
    return RawTelegramEvent(
        channel_id=-100123,
        message_id=message_id,
        text=text,
        message_time_utc=message_time,
    )


def durable_owned_position(
    store,
    *,
    position_id,
    source_channel_id,
    symbol="GOLD",
    side="SELL",
    volume=Decimal("0.01"),
    price_open=Decimal("4601"),
    stop_loss=Decimal("4618"),
    take_profit=Decimal("4595"),
):
    client_reference = f"tgxm-owned-{position_id}"
    created = store.create_order_intent(
        OrderIntent(
            account_id="123456",
            signal_id=f"owned-signal-{position_id}",
            signal_revision=1,
            leg_index=0,
            broker_symbol=symbol,
            side=side,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_reference=client_reference,
            request_metadata={"source_channel_id": source_channel_id},
        )
    ).record
    store.transition_order_intent(
        created.id,
        IntentStatus.SUBMITTING,
        expected_status=IntentStatus.INTENT_PERSISTED,
    )
    store.transition_order_intent(
        created.id,
        IntentStatus.OPEN,
        expected_status=IntentStatus.SUBMITTING,
        broker_order_id=position_id,
        broker_deal_id=position_id,
        broker_position_id=position_id,
    )
    return PositionSnapshot(
        account_id="123456",
        position_id=position_id,
        symbol=symbol,
        side=side,
        volume=volume,
        price_open=price_open,
        stop_loss=stop_loss,
        take_profit=take_profit,
        magic=26082701,
        comment=client_reference,
        time_utc=NOW,
    )


def test_observe_records_candidate_without_touching_broker():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured(), store=store, broker=fake, clock=lambda: NOW
        ).process_event(event())

        assert decision.status is DecisionStatus.OBSERVED
        assert len(store.list_raw_events()) == 1
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_unknown_peer_is_rejected_without_retaining_private_text():
    private = RawTelegramEvent(
        channel_id=-100999,
        message_id=1,
        text="PRIVATE_DM_CONTENT",
        message_time_utc=NOW,
    )
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured(), store=store, clock=lambda: NOW
        ).process_event(private)

        assert decision.status is DecisionStatus.UNKNOWN_CHANNEL
        assert decision.raw_event_id is None
        assert store.list_raw_events() == []


def test_result_update_is_audited_but_never_submitted():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("shadow", trade_enabled=True),
            store=store,
            broker=fake,
            clock=lambda: NOW,
        ).process_event(event(text="GOLD SELL TP2 HIT PROFIT DONE"))

        assert decision.status is DecisionStatus.NON_ACTIONABLE
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_shadow_approves_inside_zone_without_creating_intent():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("shadow", trade_enabled=True),
            store=store,
            broker=fake,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.SHADOW_APPROVED
        assert store.list_order_intents() == []
        assert not fake.sent_requests


def test_duplicate_raw_revision_never_reaches_broker_twice():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        engine = TradingEngine(
            config=configured("shadow", trade_enabled=True),
            store=store,
            broker=fake,
            clock=lambda: NOW,
        )
        assert engine.process_event(event()).status is DecisionStatus.SHADOW_APPROVED
        assert engine.process_event(event()).status is DecisionStatus.DUPLICATE_EVENT
        assert not fake.sent_requests


def test_demo_armed_still_needs_volatile_active_gate():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=False,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.DEMO_NOT_ACTIVE
        assert store.list_order_intents() == []
        assert not fake.sent_requests


def test_demo_submit_is_durable_only_after_protected_readback():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.OPEN
        assert len(fake.sent_requests) == 1
        intent = store.list_order_intents()[0]
        assert intent.status is IntentStatus.OPEN
        assert intent.broker_position_id is not None
        assert intent.volume == Decimal("0.01")
        assert intent.stop_loss == Decimal("4618")
        assert intent.take_profit == Decimal("4595")


def test_ambiguous_send_never_retries_and_requires_reconciliation():
    scripted = BrokerResult(
        outcome=BrokerOutcome.RECONCILE_REQUIRED,
        stage="order_send",
        retcode=10012,
        comment="timeout",
    )
    fake = broker(send_results=(scripted,))
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.RECONCILE_REQUIRED
        assert len(fake.sent_requests) == 1
        assert store.list_order_intents()[0].status is IntentStatus.RECONCILE_REQUIRED


def test_manual_same_symbol_position_blocks_before_order_check():
    manual = PositionSnapshot(
        account_id="123456",
        position_id="manual-1",
        symbol="GOLD",
        side="BUY",
        volume=Decimal("0.01"),
        price_open=Decimal("4600"),
        stop_loss=Decimal("4590"),
        take_profit=Decimal("4610"),
        magic=0,
        comment="manual",
        time_utc=NOW,
    )
    fake = broker(positions=(manual,))
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_pending_same_symbol_order_blocks_before_order_check():
    pending = PendingOrderSnapshot(
        account_id="123456",
        order_id="pending-1",
        symbol="GOLD",
        side="SELL",
        volume=Decimal("0.01"),
        magic=0,
        comment="manual",
        time_utc=NOW,
    )
    fake = broker(pending_orders=(pending,))
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert not fake.checked_requests
        assert not fake.sent_requests
        assert store.list_order_intents() == []


def test_accepted_without_readback_is_locked_and_not_marked_open():
    class NoReadbackBroker(FakeBroker):
        def read_back_market_order(self, request, result):
            return None

    base = broker()
    fake = NoReadbackBroker(
        policy=base.policy,
        account=base.account,
        symbols=base.symbols,
        ticks=base.ticks,
        clock=lambda: NOW,
    )
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event())

        assert decision.status is DecisionStatus.SUBMITTED_UNVERIFIED
        assert store.list_order_intents()[0].status is IntentStatus.RECONCILE_REQUIRED


def test_unresolved_intent_hard_locks_all_new_entries():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        store.create_order_intent(
            OrderIntent(
                account_id="123456",
                signal_id="old-signal",
                signal_revision=1,
                leg_index=0,
                broker_symbol="GOLD",
                side="SELL",
                volume=Decimal("0.01"),
                stop_loss=Decimal("4618"),
                take_profit=Decimal("4595"),
                client_reference="tgxm-old",
            )
        )
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=99))

        assert decision.status is DecisionStatus.HARD_LOCK
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_future_message_and_wide_spread_fail_before_order_check():
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        fake = broker()
        future = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(
            event(message_id=201, message_time=NOW + timedelta(seconds=31))
        )
        assert future.status is DecisionStatus.FUTURE_TIMESTAMP
        assert not fake.checked_requests

    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        fake = broker(ask="4602.01")
        wide = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=202))
        assert wide.status is DecisionStatus.SPREAD_BLOCKED
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_shadow_rejects_stale_and_future_broker_ticks():
    for message_id, tick_time in (
        (210, NOW - timedelta(seconds=6)),
        (211, NOW + timedelta(seconds=2)),
    ):
        base = broker()
        base.ticks["GOLD"] = TickSnapshot(
            symbol="GOLD",
            bid=Decimal("4601"),
            ask=Decimal("4602"),
            time_utc=tick_time,
        )
        with SQLiteStore(":memory:", clock=lambda: NOW) as store:
            decision = TradingEngine(
                config=configured("shadow", trade_enabled=True),
                store=store,
                broker=base,
                clock=lambda: NOW,
            ).process_event(event(message_id=message_id))
            assert decision.status is DecisionStatus.STALE_TICK
            assert not base.sent_requests


def test_configured_deviation_is_passed_to_broker_request():
    fake = broker()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=203))
        assert decision.status is DecisionStatus.OPEN
        assert fake.sent_requests[0].deviation_points == 20


def test_existing_unprotected_bot_position_hard_locks_new_orders():
    unprotected = PositionSnapshot(
        account_id="123456",
        position_id="bot-unsafe",
        symbol="GOLD",
        side="SELL",
        volume=Decimal("0.01"),
        price_open=Decimal("4601"),
        stop_loss=None,
        take_profit=Decimal("4595"),
        magic=26082701,
        comment="tgxm-existing",
        time_utc=NOW,
    )
    fake = broker(positions=(unprotected,))
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        created = store.create_order_intent(
            OrderIntent(
                account_id="123456",
                signal_id="existing-owned",
                signal_revision=1,
                leg_index=0,
                broker_symbol="GOLD",
                side="SELL",
                volume=Decimal("0.01"),
                stop_loss=Decimal("4618"),
                take_profit=Decimal("4595"),
                client_reference="tgxm-existing",
                request_metadata={"source_channel_id": -100123},
            )
        ).record
        store.transition_order_intent(
            created.id,
            IntentStatus.SUBMITTING,
            expected_status=IntentStatus.INTENT_PERSISTED,
        )
        store.transition_order_intent(
            created.id,
            IntentStatus.OPEN,
            expected_status=IntentStatus.SUBMITTING,
            broker_position_id="bot-unsafe",
        )
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=204))

        assert decision.status is DecisionStatus.HARD_LOCK
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_magic_comment_without_durable_intent_is_unowned_manual_exposure():
    spoofed = PositionSnapshot(
        account_id="123456",
        position_id="spoofed",
        symbol="GOLD",
        side="SELL",
        volume=Decimal("0.01"),
        price_open=Decimal("4601"),
        stop_loss=Decimal("4618"),
        take_profit=Decimal("4595"),
        magic=26082701,
        comment="tgxm-spoofed",
        time_utc=NOW,
    )
    fake = broker(positions=(spoofed,))
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=206))
        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert not fake.sent_requests


def test_webtrader_receipt_backed_position_is_owned_without_magic_or_comment():
    config = configured("demo_armed", trade_enabled=True)
    config = replace(
        config,
        risk=replace(config.risk, max_total_bot_positions=2),
    ).validate()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        created = store.create_order_intent(
            OrderIntent(
                account_id="123456",
                signal_id="webtrader-owned",
                signal_revision=1,
                leg_index=0,
                broker_symbol="GOLD",
                side="SELL",
                volume=Decimal("0.01"),
                stop_loss=Decimal("4618"),
                take_profit=Decimal("4595"),
                client_reference="tgxm-browser-receipt",
                request_metadata={
                    "source_channel_id": -100999,
                    "execution_adapter": "xm_webtrader",
                },
            )
        ).record
        store.transition_order_intent(
            created.id,
            IntentStatus.SUBMITTING,
            expected_status=IntentStatus.INTENT_PERSISTED,
        )
        store.transition_order_intent(
            created.id,
            IntentStatus.OPEN,
            expected_status=IntentStatus.SUBMITTING,
            broker_order_id="7812345",
            broker_position_id="7812345",
        )
        web_position = PositionSnapshot(
            account_id="123456",
            position_id="7812345",
            symbol="GOLD",
            side="SELL",
            volume=Decimal("0.01"),
            price_open=Decimal("4601"),
            stop_loss=Decimal("4618"),
            take_profit=Decimal("4595"),
            magic=0,
            comment="",
            time_utc=NOW,
        )
        fake = broker(positions=(web_position,))

        decision = TradingEngine(
            config=config,
            store=store,
            broker=fake,
            demo_active=True,
            magic=0,
            clock=lambda: NOW,
        ).process_event(event(message_id=207))

        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert decision.reason == "same-side bot exposure is blocked by policy"
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_same_channel_owned_position_at_cap_blocks_new_signal():
    config = configured("demo_armed", trade_enabled=True)
    config = replace(
        config,
        risk=replace(config.risk, max_total_bot_positions=2),
    ).validate()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        existing = durable_owned_position(
            store,
            position_id="same-channel-1",
            source_channel_id=-100123,
            symbol="EURUSD",
            price_open=Decimal("1.1600"),
            stop_loss=Decimal("1.1700"),
            take_profit=Decimal("1.1500"),
        )
        fake = broker(positions=(existing,))
        decision = TradingEngine(
            config=config,
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=301))

        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert decision.reason == "maximum bot positions for this channel has been reached"
        assert len(store.list_order_intents()) == 1
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_other_channel_position_does_not_consume_current_channel_cap():
    config = configured("demo_armed", trade_enabled=True)
    config = replace(
        config,
        risk=replace(config.risk, max_total_bot_positions=2),
    ).validate()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        existing = durable_owned_position(
            store,
            position_id="other-channel-room",
            source_channel_id=-100999,
            symbol="EURUSD",
            price_open=Decimal("1.1600"),
            stop_loss=Decimal("1.1700"),
            take_profit=Decimal("1.1500"),
        )
        fake = broker(positions=(existing,))
        decision = TradingEngine(
            config=config,
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=302))

        assert decision.status is DecisionStatus.OPEN
        assert len(store.list_order_intents()) == 2
        assert len(fake.sent_requests) == 1


def test_other_channel_owned_position_still_counts_toward_symbol_cap():
    config = configured("demo_armed", trade_enabled=True)
    config = replace(
        config,
        risk=replace(
            config.risk,
            max_total_bot_positions=2,
            same_side_conflict_policy="allow_within_limits",
        ),
    ).validate()
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        existing = durable_owned_position(
            store,
            position_id="other-channel-symbol",
            source_channel_id=-100999,
        )
        fake = broker(positions=(existing,))
        decision = TradingEngine(
            config=config,
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=303))

        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert decision.reason == "maximum bot positions for this symbol has been reached"
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_other_channel_owned_position_still_counts_toward_total_cap():
    config = configured("demo_armed", trade_enabled=True)
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        existing = durable_owned_position(
            store,
            position_id="other-channel-total",
            source_channel_id=-100999,
            symbol="EURUSD",
            price_open=Decimal("1.1600"),
            stop_loss=Decimal("1.1700"),
            take_profit=Decimal("1.1500"),
        )
        fake = broker(positions=(existing,))
        decision = TradingEngine(
            config=config,
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=304))

        assert decision.status is DecisionStatus.EXPOSURE_BLOCKED
        assert decision.reason == "maximum total bot positions has been reached"
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_mismatched_durable_position_identity_remains_hard_locked():
    config = configured("demo_armed", trade_enabled=True)
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        exact = durable_owned_position(
            store,
            position_id="identity-mismatch",
            source_channel_id=-100123,
        )
        mismatched = replace(exact, comment="tgxm-similar-but-not-exact")
        fake = broker(positions=(mismatched,))
        decision = TradingEngine(
            config=config,
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=305))

        assert decision.status is DecisionStatus.HARD_LOCK
        assert decision.reason == (
            "durable open intent is missing exact broker-position evidence"
        )
        assert not fake.checked_requests
        assert not fake.sent_requests


def test_fresh_submit_tick_cannot_escape_the_approved_entry_zone():
    class MoveAfterFirstCheckBroker(FakeBroker):
        def check_market_order(self, request):
            result = super().check_market_order(request)
            if len(self.checked_requests) == 1:
                self.ticks["GOLD"] = TickSnapshot(
                    symbol="GOLD",
                    bid=Decimal("4608"),
                    ask=Decimal("4608.50"),
                    time_utc=NOW,
                )
            return result

    base = broker()
    fake = MoveAfterFirstCheckBroker(
        policy=base.policy,
        account=base.account,
        symbols=base.symbols,
        ticks=base.ticks,
        clock=lambda: NOW,
    )
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        decision = TradingEngine(
            config=configured("demo_armed", trade_enabled=True),
            store=store,
            broker=fake,
            demo_active=True,
            clock=lambda: NOW,
        ).process_event(event(message_id=205))

        assert decision.status is DecisionStatus.RECONCILE_REQUIRED
        assert fake.sent_requests == []
        assert store.list_order_intents()[0].status is IntentStatus.RECONCILE_REQUIRED
