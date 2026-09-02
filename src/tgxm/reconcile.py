"""Read-only broker reconciliation for durable, uncertain order intents.

This module may read broker account/position state and update SQLite intent
status.  It never calls ``order_check``, ``order_send``, or any retry path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tgxm.broker import (
    BrokerAdapter,
    BrokerOutcome,
    BrokerResult,
    MarketOrderRequest,
)
from tgxm.store import IntentStatus, OrderIntentRecord, SQLiteStore, StoreError


class UnresolvedReason(str, Enum):
    INTENT_NOT_SUBMITTED = "INTENT_NOT_SUBMITTED"
    NO_MATCHING_POSITION = "NO_MATCHING_POSITION"
    ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    BROKER_EVIDENCE_INVALID = "BROKER_EVIDENCE_INVALID"
    STORE_TRANSITION_FAILED = "STORE_TRANSITION_FAILED"


@dataclass(frozen=True, slots=True)
class ResolvedIntent:
    intent_id: int
    previous_status: IntentStatus
    final_status: IntentStatus
    broker_position_id: str


@dataclass(frozen=True, slots=True)
class UnresolvedIntent:
    intent_id: int
    previous_status: IntentStatus
    final_status: IntentStatus
    reason: UnresolvedReason


@dataclass(frozen=True, slots=True)
class ReconciliationError:
    intent_id: int | None
    code: UnresolvedReason
    message: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    resolved: tuple[ResolvedIntent, ...]
    unresolved: tuple[UnresolvedIntent, ...]
    errors: tuple[ReconciliationError, ...]

    @property
    def clean(self) -> bool:
        return not self.unresolved and not self.errors


def _market_request(intent: OrderIntentRecord, magic: int) -> MarketOrderRequest:
    return MarketOrderRequest(
        account_id=intent.account_id,
        signal_id=intent.signal_id,
        leg_index=intent.leg_index,
        symbol=intent.broker_symbol,
        side=intent.side,
        volume=intent.volume,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit,
        client_reference=intent.client_reference,
        magic=magic,
    )


def _read_back_result(
    intent: OrderIntentRecord, request: MarketOrderRequest
) -> BrokerResult:
    """Build non-authoritative context for a broker position read-back."""

    return BrokerResult(
        outcome=BrokerOutcome.RECONCILE_REQUIRED,
        stage="reconcile_read_back",
        retcode=None,
        comment="durable intent requires broker read-back",
        request=request,
        price=intent.entry_price,
        volume=intent.volume,
        order_id=intent.broker_order_id,
        deal_id=intent.broker_deal_id,
    )


def _move_submitting_to_reconcile_required(
    store: SQLiteStore,
    intent: OrderIntentRecord,
    *,
    reason: UnresolvedReason,
) -> tuple[IntentStatus, ReconciliationError | None]:
    if intent.status is not IntentStatus.SUBMITTING:
        return intent.status, None
    try:
        updated = store.transition_order_intent(
            intent.id,
            IntentStatus.RECONCILE_REQUIRED,
            expected_status=IntentStatus.SUBMITTING,
            error_code=reason.value,
            error_message="broker evidence did not prove an exact protected position",
            detail={"reconciliation_reason": reason.value},
        )
    except StoreError:
        return (
            intent.status,
            ReconciliationError(
                intent_id=intent.id,
                code=UnresolvedReason.STORE_TRANSITION_FAILED,
                message="could not durably mark the uncertain intent for reconciliation",
            ),
        )
    return updated.status, None


def reconcile_order_intents(
    store: SQLiteStore,
    broker: BrokerAdapter,
    *,
    magic: int,
) -> ReconciliationReport:
    """Reconcile unfinished execution intents without any broker mutation.

    ``INTENT_PERSISTED`` means submission was never durably started, so it is
    reported unresolved without querying position ownership.  ``SUBMITTING``
    and ``RECONCILE_REQUIRED`` are read back using exact account and position
    evidence.  Only an exact protected match can transition to ``OPEN``.
    """

    if isinstance(magic, bool) or not isinstance(magic, int) or magic < 0:
        raise ValueError("magic must be a non-negative integer")

    intents = store.list_order_intents(
        statuses=(
            IntentStatus.INTENT_PERSISTED,
            IntentStatus.SUBMITTING,
            IntentStatus.RECONCILE_REQUIRED,
        )
    )
    resolved: list[ResolvedIntent] = []
    unresolved: list[UnresolvedIntent] = []
    errors: list[ReconciliationError] = []

    actionable = [
        intent
        for intent in intents
        if intent.status
        in {IntentStatus.SUBMITTING, IntentStatus.RECONCILE_REQUIRED}
    ]
    active_login: str | None = None
    account_error = False
    if actionable:
        try:
            active_login = broker.discover_account().login
        except Exception:
            # Adapter failures are converted to a static, non-sensitive report.
            account_error = True
            errors.append(
                ReconciliationError(
                    intent_id=None,
                    code=UnresolvedReason.ACCOUNT_UNAVAILABLE,
                    message="active Demo broker account could not be verified",
                )
            )

    for intent in intents:
        previous = intent.status
        if previous is IntentStatus.INTENT_PERSISTED:
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=previous,
                    reason=UnresolvedReason.INTENT_NOT_SUBMITTED,
                )
            )
            continue

        if account_error:
            final, transition_error = _move_submitting_to_reconcile_required(
                store,
                intent,
                reason=UnresolvedReason.ACCOUNT_UNAVAILABLE,
            )
            if transition_error is not None:
                errors.append(transition_error)
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=final,
                    reason=UnresolvedReason.ACCOUNT_UNAVAILABLE,
                )
            )
            continue

        if active_login != intent.account_id:
            final, transition_error = _move_submitting_to_reconcile_required(
                store,
                intent,
                reason=UnresolvedReason.ACCOUNT_MISMATCH,
            )
            if transition_error is not None:
                errors.append(transition_error)
            errors.append(
                ReconciliationError(
                    intent_id=intent.id,
                    code=UnresolvedReason.ACCOUNT_MISMATCH,
                    message="durable intent account does not match the active Demo account",
                )
            )
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=final,
                    reason=UnresolvedReason.ACCOUNT_MISMATCH,
                )
            )
            continue

        request = _market_request(intent, magic)
        evidence = _read_back_result(intent, request)
        try:
            position = broker.read_back_market_order(request, evidence)
        except Exception:
            final, transition_error = _move_submitting_to_reconcile_required(
                store,
                intent,
                reason=UnresolvedReason.BROKER_EVIDENCE_INVALID,
            )
            if transition_error is not None:
                errors.append(transition_error)
            errors.append(
                ReconciliationError(
                    intent_id=intent.id,
                    code=UnresolvedReason.BROKER_EVIDENCE_INVALID,
                    message="broker position evidence was missing, ambiguous, or inconsistent",
                )
            )
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=final,
                    reason=UnresolvedReason.BROKER_EVIDENCE_INVALID,
                )
            )
            continue

        if position is None:
            final, transition_error = _move_submitting_to_reconcile_required(
                store,
                intent,
                reason=UnresolvedReason.NO_MATCHING_POSITION,
            )
            if transition_error is not None:
                errors.append(transition_error)
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=final,
                    reason=UnresolvedReason.NO_MATCHING_POSITION,
                )
            )
            continue

        if not position.has_numeric_stop_loss:
            # Defensive even though the adapter's exact read-back also checks SL.
            final, transition_error = _move_submitting_to_reconcile_required(
                store,
                intent,
                reason=UnresolvedReason.BROKER_EVIDENCE_INVALID,
            )
            if transition_error is not None:
                errors.append(transition_error)
            errors.append(
                ReconciliationError(
                    intent_id=intent.id,
                    code=UnresolvedReason.BROKER_EVIDENCE_INVALID,
                    message="broker position does not prove numeric Stop Loss protection",
                )
            )
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=final,
                    reason=UnresolvedReason.BROKER_EVIDENCE_INVALID,
                )
            )
            continue

        try:
            opened = store.transition_order_intent(
                intent.id,
                IntentStatus.OPEN,
                expected_status=previous,
                broker_order_id=intent.broker_order_id,
                broker_deal_id=intent.broker_deal_id,
                broker_position_id=position.position_id,
                detail={"broker_read_back_verified": True},
            )
        except StoreError:
            errors.append(
                ReconciliationError(
                    intent_id=intent.id,
                    code=UnresolvedReason.STORE_TRANSITION_FAILED,
                    message="exact broker evidence could not be committed to durable state",
                )
            )
            unresolved.append(
                UnresolvedIntent(
                    intent_id=intent.id,
                    previous_status=previous,
                    final_status=previous,
                    reason=UnresolvedReason.STORE_TRANSITION_FAILED,
                )
            )
            continue
        resolved.append(
            ResolvedIntent(
                intent_id=intent.id,
                previous_status=previous,
                final_status=opened.status,
                broker_position_id=position.position_id,
            )
        )

    return ReconciliationReport(
        resolved=tuple(resolved),
        unresolved=tuple(unresolved),
        errors=tuple(errors),
    )


__all__ = [
    "ReconciliationError",
    "ReconciliationReport",
    "ResolvedIntent",
    "UnresolvedIntent",
    "UnresolvedReason",
    "reconcile_order_intents",
]
