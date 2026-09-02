from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tgxm.models import (
    CanonicalSignal,
    EntryKind,
    EntrySemantics,
    EntrySpec,
    EventType,
    IssueCode,
    Side,
)
from tgxm.validation import validate_canonical_signal


UTC_TIME = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)


def valid_buy_signal() -> CanonicalSignal:
    return CanonicalSignal(
        signal_id="-100100000001:42",
        revision=1,
        source_channel_id=-100100000001,
        source_message_id=42,
        reply_to_message_id=None,
        forward_origin=None,
        event_type=EventType.NEW_SIGNAL,
        symbol_alias="GOLD",
        canonical_symbol="GOLD",
        side=Side.BUY,
        entry=EntrySpec(
            kind=EntryKind.SINGLE,
            low=Decimal("4500"),
            high=Decimal("4500"),
            semantics=EntrySemantics.SINGLE_PRICE,
        ),
        stop_loss=Decimal("4490"),
        take_profits=(Decimal("4510"), Decimal("4520")),
        message_time_utc=UTC_TIME,
        expires_at_utc=UTC_TIME + timedelta(minutes=30),
        parser_profile="fixture",
        parser_version="1.0.0",
        profile_version="1.0.0",
        raw_message_hash="a" * 64,
    )


def codes(signal: CanonicalSignal) -> set[IssueCode]:
    return {issue.code for issue in validate_canonical_signal(signal).issues}


def test_valid_buy_signal_passes_static_validation() -> None:
    result = validate_canonical_signal(valid_buy_signal())

    assert result.valid
    assert result.issues == ()


def test_valid_sell_zone_brackets_entire_entry_range() -> None:
    signal = replace(
        valid_buy_signal(),
        side=Side.SELL,
        entry=EntrySpec(
            kind=EntryKind.ZONE,
            low=Decimal("4601"),
            high=Decimal("4605"),
            semantics=EntrySemantics.ZONE_SINGLE_MARKET,
        ),
        stop_loss=Decimal("4618"),
        take_profits=(Decimal("4595"), Decimal("4590")),
    )

    assert validate_canonical_signal(signal).valid


def test_binary_float_or_integer_prices_are_rejected() -> None:
    signal = replace(
        valid_buy_signal(),
        entry=EntrySpec(
            kind=EntryKind.SINGLE,
            low=4500,  # type: ignore[arg-type]
            high=4500,  # type: ignore[arg-type]
            semantics=EntrySemantics.SINGLE_PRICE,
        ),
        stop_loss=4490.0,  # type: ignore[arg-type]
    )

    result = validate_canonical_signal(signal)

    assert not result.valid
    assert IssueCode.INVALID_DECIMAL in {issue.code for issue in result.issues}
    invalid_fields = {
        issue.field
        for issue in result.issues
        if issue.code is IssueCode.INVALID_DECIMAL
    }
    assert invalid_fields == {"entry.low", "entry.high", "stop_loss"}


def test_non_finite_and_non_positive_decimal_prices_are_rejected() -> None:
    signal = replace(
        valid_buy_signal(),
        stop_loss=Decimal("NaN"),
        take_profits=(Decimal("0"), Decimal("Infinity")),
    )

    assert IssueCode.INVALID_DECIMAL in codes(signal)


def test_at_least_one_take_profit_is_required() -> None:
    signal = replace(valid_buy_signal(), take_profits=())

    assert IssueCode.MISSING_TAKE_PROFIT in codes(signal)


def test_single_entry_must_have_equal_bounds_and_single_semantics() -> None:
    signal = replace(
        valid_buy_signal(),
        entry=EntrySpec(
            kind=EntryKind.SINGLE,
            low=Decimal("4500"),
            high=Decimal("4501"),
            semantics=EntrySemantics.UNCONFIRMED,
        ),
    )

    assert IssueCode.INVALID_ENTRY in codes(signal)


def test_zone_requires_distinct_bounds_and_confirmed_semantics() -> None:
    signal = replace(
        valid_buy_signal(),
        entry=EntrySpec(
            kind=EntryKind.ZONE,
            low=Decimal("4500"),
            high=Decimal("4500"),
            semantics=EntrySemantics.UNCONFIRMED,
        ),
    )

    result_codes = codes(signal)
    assert IssueCode.INVALID_ENTRY in result_codes
    assert IssueCode.AMBIGUOUS_ENTRY_SEMANTICS in result_codes


def test_stop_and_targets_must_bracket_full_buy_zone() -> None:
    signal = replace(
        valid_buy_signal(),
        entry=EntrySpec(
            kind=EntryKind.ZONE,
            low=Decimal("4500"),
            high=Decimal("4505"),
            semantics=EntrySemantics.ZONE_SINGLE_MARKET,
        ),
        stop_loss=Decimal("4501"),
        take_profits=(Decimal("4510"),),
    )

    assert IssueCode.INVALID_PRICE_RELATIONSHIP in codes(signal)


def test_target_at_zone_boundary_is_not_profit_side() -> None:
    signal = replace(
        valid_buy_signal(),
        entry=EntrySpec(
            kind=EntryKind.ZONE,
            low=Decimal("4500"),
            high=Decimal("4505"),
            semantics=EntrySemantics.ZONE_SINGLE_MARKET,
        ),
        take_profits=(Decimal("4505"),),
    )

    assert IssueCode.INVALID_PRICE_RELATIONSHIP in codes(signal)


def test_targets_must_be_distinct_and_progress_outward() -> None:
    duplicate = replace(
        valid_buy_signal(),
        take_profits=(Decimal("4510"), Decimal("4510")),
    )
    reversed_order = replace(
        valid_buy_signal(),
        take_profits=(Decimal("4520"), Decimal("4510")),
    )

    assert IssueCode.DUPLICATE_TAKE_PROFIT in codes(duplicate)
    assert IssueCode.UNORDERED_TAKE_PROFITS in codes(duplicate)
    assert IssueCode.UNORDERED_TAKE_PROFITS in codes(reversed_order)


def test_non_signal_event_cannot_carry_canonical_signal() -> None:
    signal = replace(valid_buy_signal(), event_type=EventType.RESULT_UPDATE)

    assert IssueCode.INVALID_EVENT_TYPE in codes(signal)


def test_source_identity_must_be_stable_and_positive_where_required() -> None:
    signal = replace(
        valid_buy_signal(),
        signal_id="",
        revision=0,
        source_channel_id=0,
        source_message_id=0,
    )

    assert IssueCode.INVALID_IDENTITY in codes(signal)


def test_timestamps_must_be_utc_and_expiry_must_be_later() -> None:
    naive = replace(
        valid_buy_signal(),
        message_time_utc=datetime(2026, 8, 27, 1, 0),
    )
    backward_expiry = replace(
        valid_buy_signal(),
        expires_at_utc=UTC_TIME,
    )

    assert IssueCode.INVALID_TIMESTAMP in codes(naive)
    assert IssueCode.INVALID_TIMESTAMP in codes(backward_expiry)


def test_audit_metadata_is_required() -> None:
    signal = replace(
        valid_buy_signal(),
        parser_profile="",
        parser_version="",
        profile_version="",
        raw_message_hash="not-a-hash",
    )

    assert IssueCode.INVALID_IDENTITY in codes(signal)


def test_symbol_names_are_exact_canonical_tokens() -> None:
    signal = replace(valid_buy_signal(), symbol_alias="GOLD EUR", canonical_symbol="")

    assert IssueCode.INVALID_SYMBOL in codes(signal)


def test_side_must_be_typed_buy_or_sell() -> None:
    signal = replace(valid_buy_signal(), side="LONG")  # type: ignore[arg-type]

    assert IssueCode.INVALID_SIDE in codes(signal)

