"""Fail-closed orchestration from one Telegram event to a broker decision."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable

from tgxm.broker import (
    BrokerAdapter,
    BrokerError,
    BrokerOutcome,
    BrokerResult,
    MarketOrderRequest,
)
from tgxm.config import AppConfig, ChannelProfile
from tgxm.models import CanonicalSignal, EventType, ParseResult, RawTelegramEvent
from tgxm.parsers import parse_event
from tgxm.policy import (
    EntryState,
    PolicyError,
    entry_decision,
    fixed_volume,
    selected_take_profit,
    signal_is_expired,
)
from tgxm.store import (
    IntentStatus,
    OrderIntent,
    RawEvent,
    RawEventRecord,
    SQLiteStore,
)


class DecisionStatus(StrEnum):
    UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    NON_ACTIONABLE = "NON_ACTIONABLE"
    OBSERVED = "OBSERVED"
    TRADE_DISABLED = "TRADE_DISABLED"
    MISSING_MESSAGE_TIME = "MISSING_MESSAGE_TIME"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    EXPIRED = "EXPIRED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    STALE_TICK = "STALE_TICK"
    SPREAD_BLOCKED = "SPREAD_BLOCKED"
    HARD_LOCK = "HARD_LOCK"
    EXPOSURE_BLOCKED = "EXPOSURE_BLOCKED"
    WAITING_ENTRY = "WAITING_ENTRY"
    MISSED = "MISSED"
    SHADOW_APPROVED = "SHADOW_APPROVED"
    DEMO_NOT_ACTIVE = "DEMO_NOT_ACTIVE"
    INTENT_EXISTS = "INTENT_EXISTS"
    BROKER_REJECTED = "BROKER_REJECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    SUBMITTED_UNVERIFIED = "SUBMITTED_UNVERIFIED"
    OPEN = "OPEN"
    PARTIAL_OPEN = "PARTIAL_OPEN"


@dataclass(frozen=True, slots=True)
class ProcessDecision:
    status: DecisionStatus
    reason: str
    profile_name: str | None
    event_type: EventType | None
    raw_event_id: int | None = None
    signal: CanonicalSignal | None = None
    parse_result: ParseResult | None = None
    intent_id: int | None = None
    broker_result: BrokerResult | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _client_reference(account_id: str, signal_id: str, leg_index: int) -> str:
    material = f"{account_id}|{signal_id}|{leg_index}".encode("utf-8")
    return "tgxm-" + hashlib.sha256(material).hexdigest()[:20]


def _profile_for_peer(
    config: AppConfig,
    peer_id: int,
) -> tuple[str, ChannelProfile] | None:
    matches = [
        (name, profile)
        for name, profile in config.channels.items()
        if profile.enabled and profile.peer_id == peer_id
    ]
    if len(matches) != 1:
        return None
    return matches[0]


class TradingEngine:
    """Processes events synchronously; caller controls streaming and retries.

    ``demo_active`` is a volatile runtime gate.  Persisting ``demo_armed`` in
    configuration alone is intentionally insufficient to submit an order.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        broker: BrokerAdapter | None = None,
        demo_active: bool = False,
        magic: int = 26082701,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config.validate()
        self.store = store
        self.broker = broker
        self.demo_active = bool(demo_active)
        self.magic = int(magic)
        self.clock = clock
        if self.demo_active and self.config.runtime.mode != "demo_armed":
            raise ValueError("demo_active requires runtime.mode=demo_armed")
        if self.magic < 0:
            raise ValueError("magic must be non-negative")

    def process_event(
        self,
        event: RawTelegramEvent,
        *,
        observed_at_utc: datetime | None = None,
        _allow_existing_revision: bool = False,
    ) -> ProcessDecision:
        now = observed_at_utc or self.clock()
        if now.tzinfo is None:
            raise ValueError("observed_at_utc must be timezone-aware")
        now = now.astimezone(UTC)

        routed = _profile_for_peer(self.config, event.channel_id)
        profile_name = routed[0] if routed else None
        if routed is None:
            # The live source is already filtered, but defense in depth must
            # not turn an unexpected private chat into retained raw evidence.
            return ProcessDecision(
                status=DecisionStatus.UNKNOWN_CHANNEL,
                reason="peer ID is not mapped to one enabled channel profile",
                profile_name=None,
                event_type=None,
                raw_event_id=None,
            )
        persisted = self.store.append_raw_event(
            RawEvent(
                chat_id=event.channel_id,
                message_id=event.message_id,
                revision=event.revision,
                event_type="RAW_RECEIVED",
                observed_at_utc=now,
                message_time_utc=event.message_time_utc,
                raw_text=event.text,
                reply_to_message_id=event.reply_to_message_id,
                metadata={
                    "profile_name": profile_name,
                    "is_edit": event.is_edit,
                    "edit_time_utc": (
                        event.edit_time_utc.astimezone(UTC).isoformat()
                        if event.edit_time_utc is not None
                        else None
                    ),
                    "forward_origin": event.forward_origin,
                },
            )
        )
        raw_id = persisted.record.id
        if not persisted.created and not _allow_existing_revision:
            return ProcessDecision(
                status=DecisionStatus.DUPLICATE_EVENT,
                reason="the same Telegram event revision is already durable",
                profile_name=profile_name,
                event_type=None,
                raw_event_id=raw_id,
            )
        profile_name, profile = routed
        parsed = parse_event(
            event,
            profile=profile,
            profile_name=profile_name,
            symbol_aliases=self.config.symbol_aliases,
        )
        if parsed.signal is None:
            return ProcessDecision(
                status=DecisionStatus.NON_ACTIONABLE,
                reason=f"classified as {parsed.event_type.value}; no canonical signal",
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                parse_result=parsed,
            )

        signal = parsed.signal
        if self.config.runtime.mode == "observe":
            return ProcessDecision(
                status=DecisionStatus.OBSERVED,
                reason="Observe mode records a valid candidate without reading MT5",
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                signal=signal,
                parse_result=parsed,
            )
        if not profile.trade_enabled:
            return ProcessDecision(
                status=DecisionStatus.TRADE_DISABLED,
                reason="channel profile is not trade-enabled",
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                signal=signal,
                parse_result=parsed,
            )
        ambiguous_intents = tuple(
            intent
            for intent in self.store.unfinished_intents()
            if intent.status
            in {
                IntentStatus.INTENT_PERSISTED,
                IntentStatus.SUBMITTING,
                IntentStatus.RECONCILE_REQUIRED,
            }
        )
        if ambiguous_intents:
            return ProcessDecision(
                status=DecisionStatus.HARD_LOCK,
                reason=(
                    "unresolved durable order intent exists; reconcile before new entries"
                ),
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                signal=signal,
                parse_result=parsed,
            )
        if signal.message_time_utc is None:
            return ProcessDecision(
                status=DecisionStatus.MISSING_MESSAGE_TIME,
                reason="Shadow/Demo decisions require a trusted UTC message timestamp",
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                signal=signal,
                parse_result=parsed,
            )
        if signal.message_time_utc > now + timedelta(
            seconds=self.config.telegram.max_future_message_skew_seconds
        ):
            return ProcessDecision(
                status=DecisionStatus.FUTURE_TIMESTAMP,
                reason="message timestamp is too far in the future",
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                signal=signal,
                parse_result=parsed,
            )
        try:
            if signal_is_expired(signal, profile, now_utc=now):
                return ProcessDecision(
                    status=DecisionStatus.EXPIRED,
                    reason="signal is older than the channel expiry policy",
                    profile_name=profile_name,
                    event_type=parsed.event_type,
                    raw_event_id=raw_id,
                    signal=signal,
                    parse_result=parsed,
                )
        except PolicyError as exc:
            return self._blocked(
                DecisionStatus.BROKER_UNAVAILABLE,
                str(exc),
                profile_name,
                parsed,
                raw_id,
            )
        if self.broker is None:
            return self._blocked(
                DecisionStatus.BROKER_UNAVAILABLE,
                "Shadow/Demo mode requires a broker adapter",
                profile_name,
                parsed,
                raw_id,
            )

        try:
            account = self.broker.discover_account()
            symbol = self.broker.discover_symbol(signal.canonical_symbol)
            tick = self.broker.get_tick(symbol.symbol)
            entry = entry_decision(signal, bid=tick.bid, ask=tick.ask)
        except (BrokerError, PolicyError, ValueError) as exc:
            return self._blocked(
                DecisionStatus.BROKER_UNAVAILABLE,
                str(exc),
                profile_name,
                parsed,
                raw_id,
            )
        spread_points = (tick.ask - tick.bid) / symbol.point
        if spread_points > profile.max_spread_points:
            return self._blocked(
                DecisionStatus.SPREAD_BLOCKED,
                (
                    f"spread {spread_points} points exceeds channel limit "
                    f"{profile.max_spread_points}"
                ),
                profile_name,
                parsed,
                raw_id,
            )
        tick_age_seconds = (now - tick.time_utc).total_seconds()
        if (
            tick_age_seconds < -1
            or tick_age_seconds > self.config.broker.max_tick_age_seconds
        ):
            return self._blocked(
                DecisionStatus.STALE_TICK,
                "broker tick timestamp is outside the configured freshness window",
                profile_name,
                parsed,
                raw_id,
            )

        try:
            all_positions = self.broker.list_open_positions()
            pending_same_symbol = self.broker.list_pending_orders(symbol.symbol)
        except (BrokerError, ValueError) as exc:
            return self._blocked(
                DecisionStatus.BROKER_UNAVAILABLE,
                f"broker exposure could not be verified: {exc}",
                profile_name,
                parsed,
                raw_id,
            )
        if pending_same_symbol:
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "an active pending order exists on the same broker symbol",
                profile_name,
                parsed,
                raw_id,
            )
        active_intents = self.store.list_order_intents(
            statuses=(IntentStatus.OPEN, IntentStatus.PARTIAL_OPEN)
        )
        owned: list[tuple[object, object]] = []
        matched_intent_ids: set[int] = set()
        for position in all_positions:
            matches = [
                intent
                for intent in active_intents
                if intent.account_id == account.login
                and intent.broker_position_id == position.position_id
                and intent.broker_symbol == position.symbol
                and intent.side == position.side
                and intent.volume == position.volume
                and (
                    (
                        intent.client_reference == position.comment
                        and position.magic == self.magic
                    )
                    or (
                        self.config.broker.adapter == "xm_webtrader"
                        and intent.request_metadata.get("execution_adapter")
                        == "xm_webtrader"
                        and position.magic == 0
                    )
                )
            ]
            if len(matches) > 1:
                return self._blocked(
                    DecisionStatus.HARD_LOCK,
                    "multiple durable intents claim the same broker position",
                    profile_name,
                    parsed,
                    raw_id,
                )
            if matches:
                owned.append((position, matches[0]))
                matched_intent_ids.add(matches[0].id)
        if any(
            intent.account_id == account.login and intent.id not in matched_intent_ids
            for intent in active_intents
        ):
            return self._blocked(
                DecisionStatus.HARD_LOCK,
                "durable open intent is missing exact broker-position evidence",
                profile_name,
                parsed,
                raw_id,
            )
        if any(
            not position.has_numeric_stop_loss
            or position.stop_loss != intent.stop_loss
            or position.take_profit != intent.take_profit
            for position, intent in owned
        ):
            return self._blocked(
                DecisionStatus.HARD_LOCK,
                "an existing bot-owned position no longer matches broker-side protection",
                profile_name,
                parsed,
                raw_id,
            )
        bot_positions = tuple(position for position, _ in owned)
        manual_same_symbol = tuple(
            position
            for position in all_positions
            if position.symbol == symbol.symbol and position not in bot_positions
        )
        if (
            manual_same_symbol
            and self.config.risk.manual_exposure_policy == "block"
        ):
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "manual/unowned exposure exists on the same broker symbol",
                profile_name,
                parsed,
                raw_id,
            )
        bot_same_symbol = tuple(
            position for position in bot_positions if position.symbol == symbol.symbol
        )
        channel_position_count = sum(
            1
            for _, intent in owned
            if intent.request_metadata.get("source_channel_id")
            == signal.source_channel_id
        )
        same_side = tuple(
            position for position in bot_same_symbol if position.side == signal.side.value
        )
        opposite_side = tuple(
            position for position in bot_same_symbol if position.side != signal.side.value
        )
        if same_side and self.config.risk.same_side_conflict_policy == "block":
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "same-side bot exposure is blocked by policy",
                profile_name,
                parsed,
                raw_id,
            )
        if opposite_side and self.config.risk.opposite_side_conflict_policy == "block":
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "opposite-side bot exposure is blocked by policy",
                profile_name,
                parsed,
                raw_id,
            )
        if len(bot_positions) >= self.config.risk.max_total_bot_positions:
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "maximum total bot positions has been reached",
                profile_name,
                parsed,
                raw_id,
            )
        if len(bot_same_symbol) >= self.config.risk.max_active_per_symbol:
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "maximum bot positions for this symbol has been reached",
                profile_name,
                parsed,
                raw_id,
            )
        if channel_position_count >= self.config.risk.max_active_per_channel:
            return self._blocked(
                DecisionStatus.EXPOSURE_BLOCKED,
                "maximum bot positions for this channel has been reached",
                profile_name,
                parsed,
                raw_id,
            )

        if entry.state is EntryState.WAITING:
            return self._blocked(
                DecisionStatus.WAITING_ENTRY,
                entry.reason,
                profile_name,
                parsed,
                raw_id,
            )
        if entry.state is EntryState.MISSED:
            return self._blocked(
                DecisionStatus.MISSED,
                entry.reason,
                profile_name,
                parsed,
                raw_id,
            )
        if self.config.runtime.mode == "shadow":
            return self._blocked(
                DecisionStatus.SHADOW_APPROVED,
                "all static/account/symbol/tick/entry gates passed; no order submitted",
                profile_name,
                parsed,
                raw_id,
            )
        if not self.demo_active:
            return self._blocked(
                DecisionStatus.DEMO_NOT_ACTIVE,
                "configuration is Demo Armed but the volatile Demo Active gate is off",
                profile_name,
                parsed,
                raw_id,
            )

        try:
            volume = fixed_volume(self.config.risk)
            take_profit = selected_take_profit(signal, profile)
        except PolicyError as exc:
            return self._blocked(
                DecisionStatus.BROKER_REJECTED,
                str(exc),
                profile_name,
                parsed,
                raw_id,
            )
        reference = _client_reference(account.login, signal.signal_id, 0)
        request = MarketOrderRequest(
            account_id=account.login,
            signal_id=signal.signal_id,
            leg_index=0,
            symbol=symbol.symbol,
            side=signal.side.value,
            volume=volume,
            stop_loss=signal.stop_loss,
            take_profit=take_profit,
            client_reference=reference,
            magic=self.magic,
            deviation_points=self.config.execution.deviation_points,
            entry_low=signal.entry.low,
            entry_high=signal.entry.high,
            max_spread_points=profile.max_spread_points,
            expires_at_utc=signal.expires_at_utc,
        )
        append = self.store.create_order_intent(
            OrderIntent(
                account_id=account.login,
                signal_id=signal.signal_id,
                signal_revision=signal.revision,
                leg_index=0,
                broker_symbol=symbol.symbol,
                side=signal.side.value,
                volume=volume,
                stop_loss=signal.stop_loss,
                take_profit=take_profit,
                entry_price=entry.quote,
                expected_risk=None,
                client_reference=reference,
                request_metadata={
                    "source_channel_id": signal.source_channel_id,
                    "source_message_id": signal.source_message_id,
                    "parser_profile": signal.parser_profile,
                    "parser_version": signal.parser_version,
                    "profile_version": signal.profile_version,
                    "raw_message_hash": signal.raw_message_hash,
                    "execution_adapter": self.config.broker.adapter,
                    "entry_low": str(signal.entry.low),
                    "entry_high": str(signal.entry.high),
                    "max_spread_points": profile.max_spread_points,
                    "expires_at_utc": (
                        signal.expires_at_utc.isoformat()
                        if signal.expires_at_utc is not None
                        else None
                    ),
                },
            )
        )
        intent = append.record
        if not append.created:
            return ProcessDecision(
                status=DecisionStatus.INTENT_EXISTS,
                reason=f"durable intent already exists in state {intent.status.value}",
                profile_name=profile_name,
                event_type=parsed.event_type,
                raw_event_id=raw_id,
                signal=signal,
                parse_result=parsed,
                intent_id=intent.id,
            )

        try:
            check = self.broker.check_market_order(request)
        except (BrokerError, ValueError) as exc:
            updated = self.store.transition_order_intent(
                intent.id,
                IntentStatus.BROKER_REJECTED,
                expected_status=IntentStatus.INTENT_PERSISTED,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            return self._broker_decision(
                DecisionStatus.BROKER_REJECTED,
                str(exc),
                profile_name,
                parsed,
                raw_id,
                updated.id,
                None,
            )
        if check.outcome is not BrokerOutcome.CHECK_PASSED:
            updated = self.store.transition_order_intent(
                intent.id,
                IntentStatus.BROKER_REJECTED,
                expected_status=IntentStatus.INTENT_PERSISTED,
                error_code=check.retcode,
                error_message=check.comment,
            )
            return self._broker_decision(
                DecisionStatus.BROKER_REJECTED,
                "broker order_check did not pass",
                profile_name,
                parsed,
                raw_id,
                updated.id,
                check,
            )

        self.store.transition_order_intent(
            intent.id,
            IntentStatus.SUBMITTING,
            expected_status=IntentStatus.INTENT_PERSISTED,
            detail={"check_retcode": check.retcode},
        )
        try:
            sent = self.broker.submit_market_order(request)
        except Exception as exc:
            # Once SUBMITTING is durable, any exception is ambiguous.  The
            # coordinator never retries it from this path.
            updated = self.store.transition_order_intent(
                intent.id,
                IntentStatus.RECONCILE_REQUIRED,
                expected_status=IntentStatus.SUBMITTING,
                error_code=type(exc).__name__,
                error_message="broker submission raised; reconciliation required",
            )
            return self._broker_decision(
                DecisionStatus.RECONCILE_REQUIRED,
                "broker submission raised after SUBMITTING; no retry is permitted",
                profile_name,
                parsed,
                raw_id,
                updated.id,
                None,
            )

        position = None
        readback_error: str | None = None
        if sent.outcome in {BrokerOutcome.ACCEPTED, BrokerOutcome.PARTIAL}:
            try:
                position = self.broker.read_back_market_order(request, sent)
            except (BrokerError, ValueError) as exc:
                readback_error = str(exc)

        if sent.outcome is BrokerOutcome.REJECTED:
            status = IntentStatus.BROKER_REJECTED
            decision_status = DecisionStatus.BROKER_REJECTED
        elif position is not None:
            status = (
                IntentStatus.PARTIAL_OPEN
                if sent.outcome is BrokerOutcome.PARTIAL
                else IntentStatus.OPEN
            )
            decision_status = (
                DecisionStatus.PARTIAL_OPEN
                if status is IntentStatus.PARTIAL_OPEN
                else DecisionStatus.OPEN
            )
        else:
            # ACCEPTED/PARTIAL without exact protected read-back remains
            # ambiguous and is never retried from this path.
            status = IntentStatus.RECONCILE_REQUIRED
            decision_status = (
                DecisionStatus.RECONCILE_REQUIRED
                if sent.outcome is BrokerOutcome.RECONCILE_REQUIRED
                else DecisionStatus.SUBMITTED_UNVERIFIED
            )
        updated = self.store.transition_order_intent(
            intent.id,
            status,
            expected_status=IntentStatus.SUBMITTING,
            broker_order_id=sent.order_id,
            broker_deal_id=sent.deal_id,
            broker_position_id=position.position_id if position is not None else None,
            error_code=sent.retcode if status is not IntentStatus.OPEN else None,
            error_message=(
                sent.comment
                if status is IntentStatus.BROKER_REJECTED
                else (
                    None
                    if status in {IntentStatus.OPEN, IntentStatus.PARTIAL_OPEN}
                    else readback_error or "broker-side protection read-back is required"
                )
            ),
            detail={"broker_outcome": sent.outcome.value},
        )
        return self._broker_decision(
            decision_status,
            (
                "broker rejected the request"
                if status is IntentStatus.BROKER_REJECTED
                else (
                    "protected broker position verified by exact read-back"
                    if status in {IntentStatus.OPEN, IntentStatus.PARTIAL_OPEN}
                    else "submission is locked pending broker order/position read-back"
                )
            ),
            profile_name,
            parsed,
            raw_id,
            updated.id,
            sent,
        )

    def reevaluate_raw_event(
        self,
        record: RawEventRecord,
        *,
        observed_at_utc: datetime | None = None,
    ) -> ProcessDecision:
        """Re-evaluate durable evidence against a fresh market quote.

        This is the only path used by the waiting-entry monitor. It never adds
        a second raw row, and the durable OrderIntent key still guarantees at
        most one submission for the signal leg.
        """

        if record.raw_text is None:
            raise ValueError("durable raw event has no text to re-evaluate")
        edit_value = record.metadata.get("edit_time_utc")
        edit_time = (
            datetime.fromisoformat(str(edit_value)) if edit_value is not None else None
        )
        event = RawTelegramEvent(
            channel_id=record.chat_id,
            message_id=record.message_id,
            text=record.raw_text,
            revision=record.revision,
            is_edit=bool(record.metadata.get("is_edit")),
            edit_time_utc=edit_time,
            reply_to_message_id=record.reply_to_message_id,
            forward_origin=(
                str(record.metadata["forward_origin"])
                if record.metadata.get("forward_origin") is not None
                else None
            ),
            message_time_utc=record.message_time_utc,
        )
        return self.process_event(
            event,
            observed_at_utc=observed_at_utc,
            _allow_existing_revision=True,
        )

    @staticmethod
    def _blocked(
        status: DecisionStatus,
        reason: str,
        profile_name: str,
        parsed: ParseResult,
        raw_id: int,
    ) -> ProcessDecision:
        return ProcessDecision(
            status=status,
            reason=reason,
            profile_name=profile_name,
            event_type=parsed.event_type,
            raw_event_id=raw_id,
            signal=parsed.signal,
            parse_result=parsed,
        )

    @staticmethod
    def _broker_decision(
        status: DecisionStatus,
        reason: str,
        profile_name: str,
        parsed: ParseResult,
        raw_id: int,
        intent_id: int,
        broker_result: BrokerResult | None,
    ) -> ProcessDecision:
        return ProcessDecision(
            status=status,
            reason=reason,
            profile_name=profile_name,
            event_type=parsed.event_type,
            raw_event_id=raw_id,
            signal=parsed.signal,
            parse_result=parsed,
            intent_id=intent_id,
            broker_result=broker_result,
        )
