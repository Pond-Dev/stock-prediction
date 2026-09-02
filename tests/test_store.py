from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from threading import Barrier, Thread

import pytest

from tgxm.store import (
    ConcurrentTransitionError,
    IntentStatus,
    InvalidIntentTransitionError,
    OrderIntent,
    PersistenceConflictError,
    RawEvent,
    SQLiteStore,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 27, 1, 2, 3, tzinfo=UTC)


def raw_event(**changes: object) -> RawEvent:
    values: dict[str, object] = {
        "chat_id": -100123456789,
        "message_id": 77,
        "revision": 0,
        "event_type": "NEW_MESSAGE",
        "observed_at_utc": NOW,
        "message_time_utc": NOW - timedelta(seconds=1),
        "raw_text": "GOLD SELL 4601 OR 4605 SL 4618 TP 4595",
        "reply_to_message_id": None,
        "forward_chat_id": None,
        "forward_message_id": None,
        "metadata": {"fixture": "redacted", "sequence": 1},
    }
    values.update(changes)
    return RawEvent(**values)  # type: ignore[arg-type]


def order_intent(**changes: object) -> OrderIntent:
    values: dict[str, object] = {
        "account_id": "DEMO-10001",
        "signal_id": "sig-redacted-77",
        "signal_revision": 0,
        "leg_index": 0,
        "broker_symbol": "GOLD",
        "side": "SELL",
        "volume": Decimal("0.01"),
        "stop_loss": Decimal("4618.00"),
        "take_profit": Decimal("4595.00"),
        "entry_price": Decimal("4605.00"),
        "expected_risk": Decimal("13.00"),
        "client_reference": "tgxm-deadbeef-0",
        "request_metadata": {"parser_version": "fixture-v1"},
    }
    values.update(changes)
    return OrderIntent(**values)  # type: ignore[arg-type]


def test_raw_events_are_append_only_and_duplicate_ingest_is_idempotent(tmp_path) -> None:
    path = tmp_path / "bot.sqlite3"
    with SQLiteStore(path) as store:
        first = store.append_raw_event(raw_event())
        replay = store.append_raw_event(
            raw_event(observed_at_utc=NOW + timedelta(minutes=5))
        )

        assert first.created is True
        assert replay.created is False
        assert replay.record.id == first.record.id
        assert len(store.list_raw_events()) == 1

        with pytest.raises(PersistenceConflictError):
            store.append_raw_event(raw_event(raw_text="different evidence"))

    direct = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            direct.execute("UPDATE raw_events SET raw_text = 'changed' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            direct.execute("DELETE FROM raw_events WHERE id = 1")
    finally:
        direct.close()


def test_message_revisions_have_distinct_append_only_rows() -> None:
    with SQLiteStore(":memory:") as store:
        first = store.append_raw_event(raw_event())
        edited = store.append_raw_event(
            raw_event(
                revision=1,
                event_type="MESSAGE_EDITED",
                raw_text="GOLD SELL 4601 OR 4605 SL 4618 TP 4590",
            )
        )

        assert first.record.id != edited.record.id
        assert [item.revision for item in store.list_raw_events()] == [0, 1]


def test_waiting_registry_tracks_only_latest_exact_raw_revision() -> None:
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        first = store.append_raw_event(raw_event(revision=1)).record
        registered = store.register_waiting_entry(first)
        replay = store.register_waiting_entry(first)

        assert registered == replay
        assert registered.raw_event_id == first.id
        assert store.list_waiting_raw_events() == [first]

        edited = store.append_raw_event(
            raw_event(
                revision=2,
                event_type="MESSAGE_EDITED",
                raw_text="GOLD SELL 4602 OR 4605 SL 4618 TP 4595",
            )
        ).record
        assert store.list_waiting_entries() == []
        with pytest.raises(PersistenceConflictError, match="older raw revision"):
            store.register_waiting_entry(first)

        latest = store.register_waiting_entry(edited)
        assert latest.revision == 2
        assert latest.raw_event_id == edited.id
        assert store.list_waiting_raw_events() == [edited]

        with pytest.raises(PersistenceConflictError, match="older raw revision"):
            store.register_waiting_entry(first)

        assert store.clear_waiting_entry(
            edited.chat_id,
            edited.message_id,
            expected_revision=1,
        ) is False
        assert store.list_waiting_entries() == [latest]
        assert store.clear_waiting_entry(
            edited.chat_id,
            edited.message_id,
            expected_revision=2,
        ) is True
        assert store.list_waiting_entries() == []


def test_waiting_registry_survives_restart_without_copying_raw_text(tmp_path) -> None:
    path = tmp_path / "waiting-restart.sqlite3"
    with SQLiteStore(path, clock=lambda: NOW) as store:
        record = store.append_raw_event(raw_event(revision=4)).record
        store.register_waiting_entry(record)

    with SQLiteStore(path) as reopened:
        waiting = reopened.list_waiting_entries()
        assert len(waiting) == 1
        assert waiting[0].revision == 4
        assert reopened.list_waiting_raw_events()[0].id == record.id

    direct = sqlite3.connect(path)
    try:
        columns = {
            row[1] for row in direct.execute("PRAGMA table_info(waiting_entries)")
        }
        assert "raw_text" not in columns
        stored = direct.execute("SELECT * FROM waiting_entries").fetchone()
        assert stored is not None
        assert "GOLD SELL" not in repr(stored)
    finally:
        direct.close()


def test_order_intent_unique_key_is_idempotent_but_conflicts_fail_closed() -> None:
    with SQLiteStore(":memory:", clock=lambda: NOW) as store:
        first = store.create_order_intent(order_intent())
        replay = store.create_order_intent(order_intent())

        assert first.created is True
        assert replay.created is False
        assert replay.record.id == first.record.id
        assert replay.record.volume == Decimal("0.01")
        assert replay.record.stop_loss == Decimal("4618.00")

        with pytest.raises(PersistenceConflictError):
            store.create_order_intent(order_intent(volume=Decimal("0.02")))
        with pytest.raises(PersistenceConflictError):
            store.create_order_intent(order_intent(signal_revision=1))


def test_order_intent_rejects_float_and_missing_protection() -> None:
    with pytest.raises(TypeError, match="not float"):
        order_intent(volume=0.01)
    with pytest.raises(ValueError, match="stop_loss"):
        order_intent(stop_loss=Decimal("0"))
    with pytest.raises(ValueError, match="take_profit"):
        order_intent(take_profit=Decimal("0"))


def test_status_transitions_are_checked_and_audited() -> None:
    times = iter(
        [
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
        ]
    )
    with SQLiteStore(":memory:", clock=lambda: next(times)) as store:
        created = store.create_order_intent(order_intent()).record
        submitting = store.transition_order_intent(
            created.id,
            IntentStatus.SUBMITTING,
            expected_status=IntentStatus.INTENT_PERSISTED,
        )
        opened = store.transition_order_intent(
            created.id,
            IntentStatus.OPEN,
            expected_status=IntentStatus.SUBMITTING,
            broker_order_id=9001,
            broker_deal_id=9002,
            broker_position_id=9003,
            detail={"read_back_verified": True},
        )

        assert submitting.version == 1
        assert opened.version == 2
        assert opened.broker_position_id == "9003"
        events = store.list_intent_transitions(created.id)
        assert [item.to_status for item in events] == [
            IntentStatus.INTENT_PERSISTED,
            IntentStatus.SUBMITTING,
            IntentStatus.OPEN,
        ]
        assert events[-1].detail["read_back_verified"] is True

        # Replaying the exact transition is an idempotent no-op.
        replay = store.transition_order_intent(
            created.id,
            IntentStatus.OPEN,
            broker_order_id=9001,
            broker_deal_id=9002,
            broker_position_id=9003,
        )
        assert replay.version == opened.version
        assert len(store.list_intent_transitions(created.id)) == 3

        with pytest.raises(PersistenceConflictError):
            store.transition_order_intent(
                created.id, IntentStatus.OPEN, broker_position_id=9999
            )
        with pytest.raises(InvalidIntentTransitionError):
            store.transition_order_intent(created.id, IntentStatus.BROKER_REJECTED)


def test_ambiguous_submission_can_only_move_through_reconciliation() -> None:
    with SQLiteStore(":memory:") as store:
        created = store.create_order_intent(order_intent()).record
        store.transition_order_intent(created.id, IntentStatus.SUBMITTING)
        uncertain = store.transition_order_intent(
            created.id,
            IntentStatus.RECONCILE_REQUIRED,
            error_code="TIMEOUT",
            error_message="broker outcome unknown",
        )

        assert uncertain in store.unfinished_intents()
        with pytest.raises(InvalidIntentTransitionError):
            store.transition_order_intent(
                created.id, IntentStatus.SUBMITTING
            )  # never blind-resubmit

        reconciled = store.transition_order_intent(
            created.id,
            IntentStatus.OPEN,
            broker_order_id="broker-1",
            broker_position_id="position-1",
        )
        assert reconciled.status is IntentStatus.OPEN


def test_expected_status_is_compare_and_set_guard() -> None:
    with SQLiteStore(":memory:") as store:
        created = store.create_order_intent(order_intent()).record
        store.transition_order_intent(created.id, IntentStatus.SUBMITTING)
        with pytest.raises(ConcurrentTransitionError):
            store.transition_order_intent(
                created.id,
                IntentStatus.RECONCILE_REQUIRED,
                expected_status=IntentStatus.INTENT_PERSISTED,
            )


def test_later_failure_can_replace_last_error_but_not_broker_identity() -> None:
    with SQLiteStore(":memory:") as store:
        created = store.create_order_intent(order_intent()).record
        store.transition_order_intent(created.id, IntentStatus.SUBMITTING)
        store.transition_order_intent(
            created.id,
            IntentStatus.RECONCILE_REQUIRED,
            broker_order_id="broker-1",
            error_code="TIMEOUT",
            error_message="unknown outcome",
        )
        failed = store.transition_order_intent(
            created.id,
            IntentStatus.SAFE_FAILED,
            broker_order_id="broker-1",
            error_code="AMBIGUOUS_MATCH",
            error_message="more than one broker record matched",
        )
        assert failed.last_error_code == "AMBIGUOUS_MATCH"

    with SQLiteStore(":memory:") as store:
        created = store.create_order_intent(order_intent()).record
        store.transition_order_intent(created.id, IntentStatus.SUBMITTING)
        store.transition_order_intent(
            created.id,
            IntentStatus.RECONCILE_REQUIRED,
            broker_order_id="broker-1",
        )
        with pytest.raises(PersistenceConflictError, match="broker_order_id"):
            store.transition_order_intent(
                created.id,
                IntentStatus.SAFE_FAILED,
                broker_order_id="different-order",
            )


def test_store_reopens_with_intent_and_transition_history(tmp_path) -> None:
    path = tmp_path / "restart.sqlite3"
    with SQLiteStore(path, clock=lambda: NOW) as store:
        intent_id = store.create_order_intent(order_intent()).record.id
        store.transition_order_intent(intent_id, IntentStatus.SUBMITTING)
        store.transition_order_intent(intent_id, IntentStatus.RECONCILE_REQUIRED)

    with SQLiteStore(path) as reopened:
        record = reopened.get_order_intent(intent_id)
        assert record is not None
        assert record.status is IntentStatus.RECONCILE_REQUIRED
        assert [event.to_status for event in reopened.list_intent_transitions(intent_id)] == [
            IntentStatus.INTENT_PERSISTED,
            IntentStatus.SUBMITTING,
            IntentStatus.RECONCILE_REQUIRED,
        ]


def test_concurrent_duplicate_intent_creation_inserts_once(tmp_path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    store = SQLiteStore(path)
    barrier = Barrier(3)
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            outcomes.append(store.create_order_intent(order_intent()).created)
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [Thread(target=worker), Thread(target=worker)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    try:
        assert errors == []
        assert sorted(outcomes) == [False, True]
        assert len(store.list_order_intents()) == 1
    finally:
        store.close()
