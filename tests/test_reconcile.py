from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tgxm.broker import (
    AccountSnapshot,
    BrokerResult,
    DemoAccountPolicy,
    FakeBroker,
    MarketOrderRequest,
    PositionSnapshot,
    SymbolSnapshot,
    TickSnapshot,
)
from tgxm.reconcile import UnresolvedReason, reconcile_order_intents
from tgxm.store import IntentStatus, OrderIntent, SQLiteStore


UTC = timezone.utc
NOW = datetime(2026, 8, 27, 3, 0, 0, tzinfo=UTC)
MAGIC = 26082701


def make_intent(
    *, account_id: str = "10001", signal_id: str = "sig-redacted-77"
) -> OrderIntent:
    return OrderIntent(
        account_id=account_id,
        signal_id=signal_id,
        signal_revision=0,
        leg_index=0,
        broker_symbol="GOLD",
        side="SELL",
        volume=Decimal("0.01"),
        stop_loss=Decimal("4618.00"),
        take_profit=Decimal("4595.00"),
        entry_price=Decimal("4601.00"),
        expected_risk=Decimal("17.00"),
        client_reference="tgxm-deadbeef-0",
        request_metadata={"raw_text": "SENSITIVE FIXTURE MUST NOT ENTER REPORT"},
    )


def persist_with_status(
    store: SQLiteStore,
    status: IntentStatus,
    *,
    account_id: str = "10001",
    broker_order_id: str | None = None,
) -> int:
    intent_id = store.create_order_intent(make_intent(account_id=account_id)).record.id
    if status is IntentStatus.INTENT_PERSISTED:
        return intent_id
    store.transition_order_intent(intent_id, IntentStatus.SUBMITTING)
    if status is IntentStatus.SUBMITTING:
        return intent_id
    if status is IntentStatus.RECONCILE_REQUIRED:
        store.transition_order_intent(
            intent_id,
            IntentStatus.RECONCILE_REQUIRED,
            broker_order_id=broker_order_id,
            error_code="TIMEOUT",
            error_message="submission outcome unknown",
        )
        return intent_id
    raise AssertionError(f"unsupported fixture status: {status}")


def exact_position(**changes: object) -> PositionSnapshot:
    values: dict[str, object] = {
        "account_id": "10001",
        "position_id": "7001",
        "identifier": "7001",
        "symbol": "GOLD",
        "side": "SELL",
        "volume": Decimal("0.01"),
        "price_open": Decimal("4601.00"),
        "stop_loss": Decimal("4618.00"),
        "take_profit": Decimal("4595.00"),
        "magic": MAGIC,
        "comment": "tgxm-deadbeef-0",
        "time_utc": NOW,
    }
    values.update(changes)
    return PositionSnapshot(**values)  # type: ignore[arg-type]


class ReadOnlyGuardBroker(FakeBroker):
    """Fails the test if reconciliation touches a broker mutation path."""

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        raise AssertionError("reconciliation must not call order_check")

    def submit_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        raise AssertionError("reconciliation must not call order_send")


def make_broker(
    *,
    positions: tuple[PositionSnapshot, ...] = (),
    account_login: str = "10001",
    is_demo: bool = True,
) -> ReadOnlyGuardBroker:
    account = AccountSnapshot(
        login=account_login,
        server="XM-Demo 1",
        company="XM Test",
        is_demo=is_demo,
        connected=True,
        trade_allowed=True,
        trade_api_disabled=False,
        currency="USD",
        margin_mode="HEDGING",
    )
    symbol = SymbolSnapshot(
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
    return ReadOnlyGuardBroker(
        policy=DemoAccountPolicy(
            allowed_demo_accounts=frozenset({account_login}),
            allowed_servers=frozenset({"XM-Demo 1"}),
            allowed_companies=frozenset({"XM Test"}),
            allowed_symbols=frozenset({"GOLD"}),
        ),
        account=account,
        symbols={"GOLD": symbol},
        ticks={
            "GOLD": TickSnapshot(
                symbol="GOLD",
                bid=Decimal("4601.00"),
                ask=Decimal("4601.59"),
                time_utc=NOW,
            )
        },
        positions=positions,
        clock=lambda: NOW,
    )


def test_submitting_intent_opens_only_after_exact_protected_read_back() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(store, IntentStatus.SUBMITTING)
        broker = make_broker(positions=(exact_position(),))

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert len(report.resolved) == 1
        assert report.resolved[0].intent_id == intent_id
        assert report.resolved[0].previous_status is IntentStatus.SUBMITTING
        assert report.resolved[0].final_status is IntentStatus.OPEN
        assert report.resolved[0].broker_position_id == "7001"
        assert report.unresolved == ()
        assert report.errors == ()
        assert report.clean is True
        assert store.get_order_intent(intent_id).status is IntentStatus.OPEN  # type: ignore[union-attr]
        assert broker.checked_requests == []
        assert broker.sent_requests == []


def test_reconcile_required_intent_can_resolve_by_stored_broker_identity() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(
            store,
            IntentStatus.RECONCILE_REQUIRED,
            broker_order_id="7001",
        )
        broker = make_broker(positions=(exact_position(),))

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert [item.intent_id for item in report.resolved] == [intent_id]
        assert store.get_order_intent(intent_id).status is IntentStatus.OPEN  # type: ignore[union-attr]
        assert broker.checked_requests == []
        assert broker.sent_requests == []


def test_submitting_without_position_moves_to_reconcile_required_without_retry() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(store, IntentStatus.SUBMITTING)
        broker = make_broker()

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert report.resolved == ()
        assert len(report.unresolved) == 1
        assert report.unresolved[0].reason is UnresolvedReason.NO_MATCHING_POSITION
        assert report.unresolved[0].final_status is IntentStatus.RECONCILE_REQUIRED
        assert report.errors == ()
        assert store.get_order_intent(intent_id).status is IntentStatus.RECONCILE_REQUIRED  # type: ignore[union-attr]
        assert broker.checked_requests == []
        assert broker.sent_requests == []


def test_mismatched_or_ambiguous_evidence_never_marks_open() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(store, IntentStatus.SUBMITTING)
        broker = make_broker(positions=(exact_position(stop_loss=None),))

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert report.resolved == ()
        assert report.unresolved[0].reason is UnresolvedReason.BROKER_EVIDENCE_INVALID
        assert report.errors[0].code is UnresolvedReason.BROKER_EVIDENCE_INVALID
        assert store.get_order_intent(intent_id).status is IntentStatus.RECONCILE_REQUIRED  # type: ignore[union-attr]
        assert broker.sent_requests == []

    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(store, IntentStatus.SUBMITTING)
        broker = make_broker(
            positions=(
                exact_position(position_id="7001", identifier="7001"),
                exact_position(position_id="7002", identifier="7002"),
            )
        )
        report = reconcile_order_intents(store, broker, magic=MAGIC)
        assert report.resolved == ()
        assert report.errors[0].code is UnresolvedReason.BROKER_EVIDENCE_INVALID
        assert store.get_order_intent(intent_id).status is IntentStatus.RECONCILE_REQUIRED  # type: ignore[union-attr]
        assert broker.sent_requests == []


def test_intent_persisted_remains_unresolved_and_never_infers_submission() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(store, IntentStatus.INTENT_PERSISTED)
        # Even matching broker evidence must not make an unsubmitted intent OPEN.
        broker = make_broker(positions=(exact_position(),))

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert report.resolved == ()
        assert report.unresolved[0].reason is UnresolvedReason.INTENT_NOT_SUBMITTED
        assert report.unresolved[0].final_status is IntentStatus.INTENT_PERSISTED
        assert store.get_order_intent(intent_id).status is IntentStatus.INTENT_PERSISTED  # type: ignore[union-attr]
        assert broker.checked_requests == []
        assert broker.sent_requests == []


def test_exact_account_mismatch_is_error_and_submitting_becomes_uncertain() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(
            store, IntentStatus.SUBMITTING, account_id="different-demo-login"
        )
        broker = make_broker()

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert report.resolved == ()
        assert report.unresolved[0].reason is UnresolvedReason.ACCOUNT_MISMATCH
        assert report.errors[0].code is UnresolvedReason.ACCOUNT_MISMATCH
        assert store.get_order_intent(intent_id).status is IntentStatus.RECONCILE_REQUIRED  # type: ignore[union-attr]
        assert broker.sent_requests == []


def test_unavailable_account_is_structured_and_never_retries() -> None:
    with SQLiteStore(":memory:") as store:
        submitting_id = persist_with_status(store, IntentStatus.SUBMITTING)
        persisted_id = store.create_order_intent(
            make_intent(signal_id="different-signal")
        ).record.id
        broker = make_broker(is_demo=False)

        report = reconcile_order_intents(store, broker, magic=MAGIC)

        assert report.errors[0].code is UnresolvedReason.ACCOUNT_UNAVAILABLE
        by_id = {item.intent_id: item for item in report.unresolved}
        assert by_id[submitting_id].final_status is IntentStatus.RECONCILE_REQUIRED
        assert by_id[persisted_id].final_status is IntentStatus.INTENT_PERSISTED
        assert broker.checked_requests == []
        assert broker.sent_requests == []


def test_report_never_contains_raw_message_or_intent_metadata() -> None:
    with SQLiteStore(":memory:") as store:
        persist_with_status(store, IntentStatus.INTENT_PERSISTED)
        report = reconcile_order_intents(store, make_broker(), magic=MAGIC)

        rendered = repr(report)
        assert "SENSITIVE FIXTURE" not in rendered
        assert "raw_text" not in rendered


def test_invalid_magic_fails_before_any_state_change() -> None:
    with SQLiteStore(":memory:") as store:
        intent_id = persist_with_status(store, IntentStatus.SUBMITTING)
        broker = make_broker(positions=(exact_position(),))

        with pytest.raises(ValueError, match="magic"):
            reconcile_order_intents(store, broker, magic=-1)

        assert store.get_order_intent(intent_id).status is IntentStatus.SUBMITTING  # type: ignore[union-attr]
        assert broker.checked_requests == []
        assert broker.sent_requests == []
