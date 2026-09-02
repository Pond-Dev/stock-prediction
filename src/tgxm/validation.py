"""Static, broker-independent validation for canonical signal candidates."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal

from tgxm.models import (
    CanonicalSignal,
    EntryKind,
    EntrySemantics,
    EventType,
    Issue,
    IssueCode,
    Side,
    ValidationResult,
)


_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9._-]{0,31}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _issue(code: IssueCode, message: str, field: str | None = None) -> Issue:
    return Issue(code=code, message=message, field=field)


def _valid_price(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _is_utc_timestamp(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, datetime):
        return False
    try:
        offset = value.utcoffset()
    except ValueError:
        return False
    return offset == timedelta(0)


def validate_canonical_signal(signal: CanonicalSignal) -> ValidationResult:
    """Validate signal shape and price relationships, accumulating all issues.

    Passing this function is necessary but never sufficient for execution.  It
    intentionally knows nothing about current quotes, signal age, the active
    account, broker symbol constraints, exposure, margin, or risk reservations.
    """

    issues: list[Issue] = []

    if not isinstance(signal.event_type, EventType) or signal.event_type not in {
        EventType.NEW_SIGNAL,
        EventType.SIGNAL_EDIT,
    }:
        issues.append(
            _issue(
                IssueCode.INVALID_EVENT_TYPE,
                "Only a new signal or signal edit can contain a canonical signal.",
                "event_type",
            )
        )

    if (
        not isinstance(signal.signal_id, str)
        or not signal.signal_id.strip()
        or not isinstance(signal.revision, int)
        or isinstance(signal.revision, bool)
        or signal.revision < 1
        or not isinstance(signal.source_channel_id, int)
        or isinstance(signal.source_channel_id, bool)
        or signal.source_channel_id == 0
        or not isinstance(signal.source_message_id, int)
        or isinstance(signal.source_message_id, bool)
        or signal.source_message_id < 1
    ):
        issues.append(
            _issue(
                IssueCode.INVALID_IDENTITY,
                "Signal source identity and revision must be stable and positive.",
                "signal_id",
            )
        )

    if not isinstance(signal.symbol_alias, str) or not _SYMBOL_PATTERN.fullmatch(signal.symbol_alias):
        issues.append(
            _issue(
                IssueCode.INVALID_SYMBOL,
                "Symbol alias is not in the approved canonical character set.",
                "symbol_alias",
            )
        )
    if not isinstance(signal.canonical_symbol, str) or not _SYMBOL_PATTERN.fullmatch(signal.canonical_symbol):
        issues.append(
            _issue(
                IssueCode.INVALID_SYMBOL,
                "Canonical symbol is not in the approved canonical character set.",
                "canonical_symbol",
            )
        )

    side_valid = isinstance(signal.side, Side)
    if not side_valid:
        issues.append(
            _issue(IssueCode.INVALID_SIDE, "Side must be BUY or SELL.", "side")
        )

    prices: tuple[tuple[str, object], ...] = (
        ("entry.low", signal.entry.low),
        ("entry.high", signal.entry.high),
        ("stop_loss", signal.stop_loss),
        *((f"take_profits[{index}]", value) for index, value in enumerate(signal.take_profits)),
    )
    invalid_price_fields: set[str] = set()
    for field, value in prices:
        if not _valid_price(value):
            invalid_price_fields.add(field)
            issues.append(
                _issue(
                    IssueCode.INVALID_DECIMAL,
                    "Price must be a finite, positive Decimal.",
                    field,
                )
            )

    entry_prices_valid = not ({"entry.low", "entry.high"} & invalid_price_fields)
    if entry_prices_valid:
        if signal.entry.low > signal.entry.high:
            issues.append(
                _issue(
                    IssueCode.INVALID_ENTRY,
                    "Entry low cannot exceed entry high.",
                    "entry",
                )
            )
        if signal.entry.kind is EntryKind.SINGLE:
            if signal.entry.low != signal.entry.high:
                issues.append(
                    _issue(
                        IssueCode.INVALID_ENTRY,
                        "A single entry must have identical low and high prices.",
                        "entry",
                    )
                )
            if signal.entry.semantics is not EntrySemantics.SINGLE_PRICE:
                issues.append(
                    _issue(
                        IssueCode.INVALID_ENTRY,
                        "A single entry requires SINGLE_PRICE semantics.",
                        "entry.semantics",
                    )
                )
        elif signal.entry.kind is EntryKind.ZONE:
            if signal.entry.low >= signal.entry.high:
                issues.append(
                    _issue(
                        IssueCode.INVALID_ENTRY,
                        "An entry zone requires two distinct ordered prices.",
                        "entry",
                    )
                )
            if signal.entry.semantics is not EntrySemantics.ZONE_SINGLE_MARKET:
                issues.append(
                    _issue(
                        IssueCode.AMBIGUOUS_ENTRY_SEMANTICS,
                        "A zone is invalid until its one-order semantics are explicit.",
                        "entry.semantics",
                    )
                )
        else:
            issues.append(
                _issue(IssueCode.INVALID_ENTRY, "Unsupported entry kind.", "entry.kind")
            )

    if not signal.take_profits:
        issues.append(
            _issue(
                IssueCode.MISSING_TAKE_PROFIT,
                "At least one numeric Take Profit is required.",
                "take_profits",
            )
        )

    take_profits_valid = bool(signal.take_profits) and not any(
        field.startswith("take_profits[") for field in invalid_price_fields
    )
    if take_profits_valid:
        if len(set(signal.take_profits)) != len(signal.take_profits):
            issues.append(
                _issue(
                    IssueCode.DUPLICATE_TAKE_PROFIT,
                    "Take Profit prices must be distinct.",
                    "take_profits",
                )
            )

        pairs = zip(signal.take_profits, signal.take_profits[1:])
        ordered = side_valid and (
            all(left < right for left, right in pairs)
            if signal.side is Side.BUY
            else all(left > right for left, right in pairs)
        )
        if len(signal.take_profits) > 1 and not ordered:
            issues.append(
                _issue(
                    IssueCode.UNORDERED_TAKE_PROFITS,
                    "Take Profits must progress farther in the signal direction.",
                    "take_profits",
                )
            )

    relationship_inputs_valid = (
        entry_prices_valid
        and "stop_loss" not in invalid_price_fields
        and take_profits_valid
        and side_valid
        and signal.entry.low <= signal.entry.high
    )
    if relationship_inputs_valid:
        if signal.side is Side.BUY:
            relationship_valid = (
                signal.stop_loss < signal.entry.low
                and all(target > signal.entry.high for target in signal.take_profits)
            )
        else:
            relationship_valid = (
                signal.stop_loss > signal.entry.high
                and all(target < signal.entry.low for target in signal.take_profits)
            )
        if not relationship_valid:
            issues.append(
                _issue(
                    IssueCode.INVALID_PRICE_RELATIONSHIP,
                    "Stop Loss and every Take Profit must bracket the full entry range.",
                    "entry",
                )
            )

    if not _is_utc_timestamp(signal.message_time_utc):
        issues.append(
            _issue(
                IssueCode.INVALID_TIMESTAMP,
                "Message timestamp must be timezone-aware UTC.",
                "message_time_utc",
            )
        )
    if not _is_utc_timestamp(signal.expires_at_utc):
        issues.append(
            _issue(
                IssueCode.INVALID_TIMESTAMP,
                "Expiry timestamp must be timezone-aware UTC.",
                "expires_at_utc",
            )
        )
    if (
        signal.message_time_utc is not None
        and signal.expires_at_utc is not None
        and _is_utc_timestamp(signal.message_time_utc)
        and _is_utc_timestamp(signal.expires_at_utc)
        and signal.expires_at_utc <= signal.message_time_utc
    ):
        issues.append(
            _issue(
                IssueCode.INVALID_TIMESTAMP,
                "Signal expiry must be later than its message timestamp.",
                "expires_at_utc",
            )
        )

    if (
        not isinstance(signal.parser_profile, str)
        or not signal.parser_profile.strip()
        or not isinstance(signal.parser_version, str)
        or not signal.parser_version.strip()
        or not isinstance(signal.profile_version, str)
        or not signal.profile_version.strip()
        or not isinstance(signal.raw_message_hash, str)
        or not _SHA256_PATTERN.fullmatch(signal.raw_message_hash)
    ):
        issues.append(
            _issue(
                IssueCode.INVALID_IDENTITY,
                "Parser/profile versions and raw message hash are required for audit.",
                "parser_profile",
            )
        )

    return ValidationResult(issues=tuple(issues))
