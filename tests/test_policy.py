from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tgxm.models import (
    CanonicalSignal,
    EntryKind,
    EntrySemantics,
    EntrySpec,
    EventType,
    Side,
)
from tgxm.policy import (
    EntryState,
    PolicyError,
    entry_decision,
    fixed_volume,
    selected_take_profit,
    signal_is_expired,
)


def make_signal(side=Side.BUY):
    if side is Side.BUY:
        entry = EntrySpec(
            EntryKind.ZONE,
            Decimal("4600"),
            Decimal("4605"),
            EntrySemantics.ZONE_SINGLE_MARKET,
        )
        stop, targets = Decimal("4590"), (Decimal("4610"), Decimal("4620"))
    else:
        entry = EntrySpec(
            EntryKind.ZONE,
            Decimal("4600"),
            Decimal("4605"),
            EntrySemantics.ZONE_SINGLE_MARKET,
        )
        stop, targets = Decimal("4615"), (Decimal("4590"), Decimal("4580"))
    return CanonicalSignal(
        signal_id="-100:10",
        revision=1,
        source_channel_id=-100,
        source_message_id=10,
        reply_to_message_id=None,
        forward_origin=None,
        event_type=EventType.NEW_SIGNAL,
        symbol_alias="GOLD",
        canonical_symbol="GOLD",
        side=side,
        entry=entry,
        stop_loss=stop,
        take_profits=targets,
        message_time_utc=datetime(2026, 8, 27, 0, 0, tzinfo=UTC),
        expires_at_utc=None,
        parser_profile="compact_gold_v1",
        parser_version="1.0.0",
        profile_version="1",
        raw_message_hash="hash",
    )


def test_fixed_volume_never_exceeds_hard_cap():
    safe = SimpleNamespace(
        mode="fixed_lot", fixed_lot=Decimal("0.01"), hard_lot_cap=Decimal("0.01")
    )
    assert fixed_volume(safe) == Decimal("0.01")

    unsafe = SimpleNamespace(
        mode="fixed_lot", fixed_lot=Decimal("0.02"), hard_lot_cap=Decimal("0.01")
    )
    with pytest.raises(PolicyError, match="exceeds"):
        fixed_volume(unsafe)


def test_tp_selection_is_one_based_and_fail_closed():
    signal = make_signal()
    assert selected_take_profit(signal, SimpleNamespace(tp_index=1)) == Decimal("4610")
    with pytest.raises(PolicyError, match="outside"):
        selected_take_profit(signal, SimpleNamespace(tp_index=3))


def test_signal_expiry_uses_utc_message_age():
    signal = make_signal()
    profile = SimpleNamespace(signal_expiry_minutes=30)
    assert not signal_is_expired(
        signal,
        profile,
        now_utc=datetime(2026, 8, 27, 0, 29, 59, tzinfo=UTC),
    )
    assert signal_is_expired(
        signal,
        profile,
        now_utc=datetime(2026, 8, 27, 0, 30, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("side", "bid", "ask", "state"),
    [
        (Side.BUY, "4600", "4601", EntryState.ELIGIBLE),
        (Side.BUY, "4611", "4612", EntryState.MISSED),
        (Side.BUY, "4595", "4596", EntryState.WAITING),
        (Side.SELL, "4604", "4605", EntryState.ELIGIBLE),
        (Side.SELL, "4589", "4590", EntryState.MISSED),
        (Side.SELL, "4608", "4609", EntryState.WAITING),
    ],
)
def test_entry_decision_uses_executable_side_quote(side, bid, ask, state):
    assert (
        entry_decision(
            make_signal(side), bid=Decimal(bid), ask=Decimal(ask)
        ).state
        is state
    )
