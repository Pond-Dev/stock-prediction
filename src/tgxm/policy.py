"""Deterministic gates between static parsing and broker execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from tgxm.models import CanonicalSignal, Side


class PolicyError(ValueError):
    """Raised when operational policy is incomplete or unsafe."""


class EntryState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    WAITING = "WAITING"
    MISSED = "MISSED"


@dataclass(frozen=True, slots=True)
class EntryDecision:
    state: EntryState
    quote: Decimal
    reason: str


class RiskSettings(Protocol):
    mode: str
    fixed_lot: Decimal
    hard_lot_cap: Decimal


class ChannelSettings(Protocol):
    signal_expiry_minutes: int
    tp_index: int


def decimal_value(value: object, *, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyError(f"{field} must be a decimal number") from exc
    if not result.is_finite():
        raise PolicyError(f"{field} must be finite")
    return result


def fixed_volume(risk: RiskSettings) -> Decimal:
    """Return fixed volume without ever rounding or increasing it."""

    if str(risk.mode).lower() != "fixed_lot":
        raise PolicyError("only fixed_lot risk mode is implemented in the Demo MVP")
    volume = decimal_value(risk.fixed_lot, field="risk.fixed_lot")
    cap = decimal_value(risk.hard_lot_cap, field="risk.hard_lot_cap")
    if volume <= 0 or cap <= 0:
        raise PolicyError("fixed volume and hard cap must be positive")
    if volume > cap:
        raise PolicyError("fixed volume exceeds the configured hard lot cap")
    return volume


def selected_take_profit(
    signal: CanonicalSignal,
    profile: ChannelSettings,
) -> Decimal:
    """Select one TP by one-based index while retaining all TPs on the signal."""

    index = int(profile.tp_index)
    if index < 1 or index > len(signal.take_profits):
        raise PolicyError(
            f"tp_index {index} is outside the parsed TP range 1..{len(signal.take_profits)}"
        )
    return signal.take_profits[index - 1]


def signal_expiry(
    signal: CanonicalSignal,
    profile: ChannelSettings,
) -> datetime | None:
    if signal.expires_at_utc is not None:
        return signal.expires_at_utc.astimezone(UTC)
    if signal.message_time_utc is None:
        return None
    minutes = int(profile.signal_expiry_minutes)
    if minutes <= 0:
        raise PolicyError("signal_expiry_minutes must be positive")
    return signal.message_time_utc.astimezone(UTC) + timedelta(minutes=minutes)


def signal_is_expired(
    signal: CanonicalSignal,
    profile: ChannelSettings,
    *,
    now_utc: datetime | None = None,
) -> bool:
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        raise PolicyError("now_utc must be timezone-aware")
    expires_at = signal_expiry(signal, profile)
    # Missing source time is unsafe for live/shadow execution, but Observe mode
    # can still retain and classify the candidate.  Callers gate it explicitly.
    return expires_at is not None and now.astimezone(UTC) >= expires_at


def entry_decision(
    signal: CanonicalSignal,
    *,
    bid: Decimal,
    ask: Decimal,
) -> EntryDecision:
    """Evaluate a market quote against a single entry/zone conservatively."""

    bid_value = decimal_value(bid, field="tick.bid")
    ask_value = decimal_value(ask, field="tick.ask")
    if bid_value <= 0 or ask_value <= 0 or ask_value < bid_value:
        raise PolicyError("tick bid/ask is invalid")

    quote = ask_value if signal.side is Side.BUY else bid_value
    low = signal.entry.low
    high = signal.entry.high
    if low <= quote <= high:
        return EntryDecision(EntryState.ELIGIBLE, quote, "quote is inside entry range")

    first_target = signal.take_profits[0]
    if signal.side is Side.BUY:
        if quote <= signal.stop_loss:
            return EntryDecision(EntryState.MISSED, quote, "quote crossed stop loss")
        if quote >= first_target:
            return EntryDecision(EntryState.MISSED, quote, "quote reached/passed TP1")
    else:
        if quote >= signal.stop_loss:
            return EntryDecision(EntryState.MISSED, quote, "quote crossed stop loss")
        if quote <= first_target:
            return EntryDecision(EntryState.MISSED, quote, "quote reached/passed TP1")

    return EntryDecision(EntryState.WAITING, quote, "quote is outside entry range")
