"""Typed domain objects shared by the Telegram signal parsing pipeline.

The objects in this module deliberately stop at *static* signal validity.  A
``CanonicalSignal`` is still untrusted input and is not authority to contact a
broker.  Account, market, duplicate, conflict, age, and risk gates belong to
later pipeline stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class EventType(StrEnum):
    """Deterministic classifications defined by ``CONTEXT.md``."""

    NEW_SIGNAL = "NEW_SIGNAL"
    INCOMPLETE_SIGNAL = "INCOMPLETE_SIGNAL"
    SIGNAL_EDIT = "SIGNAL_EDIT"
    RESULT_UPDATE = "RESULT_UPDATE"
    CLOSE_REQUEST = "CLOSE_REQUEST"
    MODIFY_REQUEST = "MODIFY_REQUEST"
    FORECAST = "FORECAST"
    ADVERTISEMENT = "ADVERTISEMENT"
    UNKNOWN = "UNKNOWN"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class EntryKind(StrEnum):
    SINGLE = "SINGLE"
    ZONE = "ZONE"


class EntrySemantics(StrEnum):
    """Supported meanings of parsed entry levels.

    ``UNCONFIRMED`` is representable for audit/import purposes, but canonical
    validation rejects it.  This prevents a two-level signal from becoming an
    order merely because a parser happened to find two prices.
    """

    SINGLE_PRICE = "SINGLE_PRICE"
    ZONE_SINGLE_MARKET = "ZONE_SINGLE_MARKET"
    UNCONFIRMED = "UNCONFIRMED"


class IssueCode(StrEnum):
    EMPTY_MESSAGE = "EMPTY_MESSAGE"
    UNKNOWN_PARSER = "UNKNOWN_PARSER"
    REQUIRED_MARKER_MISSING = "REQUIRED_MARKER_MISSING"
    CONFLICTING_MARKERS = "CONFLICTING_MARKERS"
    IGNORED_MARKER = "IGNORED_MARKER"
    MISSING_SYMBOL = "MISSING_SYMBOL"
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    AMBIGUOUS_SIDE = "AMBIGUOUS_SIDE"
    MISSING_SIDE = "MISSING_SIDE"
    MISSING_ENTRY = "MISSING_ENTRY"
    TOO_MANY_ENTRY_LEVELS = "TOO_MANY_ENTRY_LEVELS"
    AMBIGUOUS_ENTRY_SEMANTICS = "AMBIGUOUS_ENTRY_SEMANTICS"
    UNSUPPORTED_ENTRY_SEMANTICS = "UNSUPPORTED_ENTRY_SEMANTICS"
    MISSING_NUMERIC_STOP_LOSS = "MISSING_NUMERIC_STOP_LOSS"
    MISSING_TAKE_PROFIT = "MISSING_TAKE_PROFIT"
    CONFLICTING_DIRECTION_BIAS = "CONFLICTING_DIRECTION_BIAS"
    INVALID_EVENT_TYPE = "INVALID_EVENT_TYPE"
    INVALID_IDENTITY = "INVALID_IDENTITY"
    INVALID_SYMBOL = "INVALID_SYMBOL"
    INVALID_SIDE = "INVALID_SIDE"
    INVALID_DECIMAL = "INVALID_DECIMAL"
    INVALID_ENTRY = "INVALID_ENTRY"
    INVALID_PRICE_RELATIONSHIP = "INVALID_PRICE_RELATIONSHIP"
    DUPLICATE_TAKE_PROFIT = "DUPLICATE_TAKE_PROFIT"
    UNORDERED_TAKE_PROFITS = "UNORDERED_TAKE_PROFITS"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"


@dataclass(frozen=True, slots=True)
class RawTelegramEvent:
    """One raw Telegram observation supplied to the deterministic parser.

    ``text`` is intentionally retained separately from normalized parser text.
    Callers must not log it because a real channel message may be private.
    """

    channel_id: int
    message_id: int
    text: str
    revision: int = 1
    is_edit: bool = False
    edit_time_utc: datetime | None = None
    reply_to_message_id: int | None = None
    forward_origin: str | None = None
    message_time_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class EntrySpec:
    kind: EntryKind
    low: Decimal
    high: Decimal
    semantics: EntrySemantics


@dataclass(frozen=True, slots=True)
class CanonicalSignal:
    """A complete, typed signal candidate that passed static validation."""

    signal_id: str
    revision: int
    source_channel_id: int
    source_message_id: int
    reply_to_message_id: int | None
    forward_origin: str | None
    event_type: EventType
    symbol_alias: str
    canonical_symbol: str
    side: Side
    entry: EntrySpec
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]
    message_time_utc: datetime | None
    expires_at_utc: datetime | None
    parser_profile: str
    parser_version: str
    profile_version: str
    raw_message_hash: str


@dataclass(frozen=True, slots=True)
class Issue:
    code: IssueCode
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Classification plus an optional statically valid signal candidate."""

    event_type: EventType
    signal: CanonicalSignal | None
    issues: tuple[Issue, ...]
    normalized_text: str

    @property
    def has_signal(self) -> bool:
        return self.signal is not None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    issues: tuple[Issue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues
