"""Demo-only broker boundary with a deterministic offline implementation.

`MetaTrader5` is imported lazily so parser, persistence, and unit-test paths do
not require the Windows-only package.  The adapter never retries `order_send`.
An ambiguous result is returned as ``RECONCILE_REQUIRED`` and must be resolved
against broker state by the execution coordinator before any later action.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import importlib
import math
from types import MappingProxyType
from typing import Any, Callable, Deque, Iterable, Mapping, Protocol, runtime_checkable


UTC = timezone.utc


class BrokerError(RuntimeError):
    """Base class for broker-boundary failures."""


class BrokerUnavailableError(BrokerError):
    """The terminal/package cannot provide a trustworthy broker state."""


class BrokerSafetyError(BrokerError):
    """A fail-closed precondition rejected the request."""


class LiveAccountRejected(BrokerSafetyError):
    """The active account is not positively identified as Demo."""


class AccountNotAllowlisted(BrokerSafetyError):
    """The exact login or server is not allowlisted."""


class SymbolNotAvailable(BrokerSafetyError):
    """The requested exact broker symbol cannot be used."""


class StaleTickError(BrokerSafetyError):
    """The latest broker tick is absent, invalid, future-dated, or stale."""


class BrokerOutcome(str, Enum):
    CHECK_PASSED = "CHECK_PASSED"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


@dataclass(frozen=True, slots=True)
class DemoAccountPolicy:
    allowed_demo_accounts: frozenset[str]
    allowed_servers: frozenset[str]
    allowed_companies: frozenset[str] = frozenset()
    allowed_symbols: frozenset[str] = frozenset()
    max_tick_age_seconds: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_demo_accounts",
            frozenset(str(item) for item in self.allowed_demo_accounts),
        )
        object.__setattr__(
            self, "allowed_servers", frozenset(str(item) for item in self.allowed_servers)
        )
        object.__setattr__(
            self,
            "allowed_companies",
            frozenset(str(item) for item in self.allowed_companies),
        )
        object.__setattr__(
            self, "allowed_symbols", frozenset(str(item) for item in self.allowed_symbols)
        )
        age = _decimal(self.max_tick_age_seconds, "max_tick_age_seconds")
        object.__setattr__(self, "max_tick_age_seconds", age)
        if not self.allowed_demo_accounts:
            raise ValueError("allowed_demo_accounts must be non-empty")
        if not self.allowed_servers:
            raise ValueError("allowed_servers must be non-empty")
        if age <= 0:
            raise ValueError("max_tick_age_seconds must be positive")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    login: str
    server: str
    company: str
    is_demo: bool
    connected: bool
    trade_allowed: bool
    trade_api_disabled: bool
    trade_expert: bool = True
    currency: str = ""
    margin_mode: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SymbolSnapshot:
    symbol: str
    visible: bool
    trade_mode: str
    digits: int
    point: Decimal
    tick_size: Decimal
    tick_value: Decimal
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int = 0
    freeze_level_points: int = 0
    filling_flags: int = 0
    execution_mode: int = 0
    market_order_allowed: bool = True
    stop_loss_allowed: bool = True
    take_profit_allowed: bool = True

    def __post_init__(self) -> None:
        for name in (
            "point",
            "tick_size",
            "tick_value",
            "contract_size",
            "volume_min",
            "volume_max",
            "volume_step",
        ):
            value = _decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if self.volume_min <= 0 or self.volume_max < self.volume_min:
            raise ValueError("invalid symbol volume bounds")
        if self.volume_step <= 0:
            raise ValueError("volume_step must be positive")
        if self.point <= 0 or self.tick_size <= 0:
            raise ValueError("point and tick_size must be positive")


@dataclass(frozen=True, slots=True)
class TickSnapshot:
    symbol: str
    bid: Decimal
    ask: Decimal
    time_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "bid", _decimal(self.bid, "bid"))
        object.__setattr__(self, "ask", _decimal(self.ask, "ask"))
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("tick prices must be positive and ask >= bid")
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise ValueError("tick time must be timezone-aware")
        object.__setattr__(self, "time_utc", self.time_utc.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Read-only broker position evidence, including manual positions."""

    account_id: str
    position_id: str
    symbol: str
    side: str
    volume: Decimal
    price_open: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    magic: int
    comment: str
    time_utc: datetime
    identifier: str | None = None

    def __post_init__(self) -> None:
        for name in ("account_id", "position_id", "symbol"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "side", _side_text(self.side))
        object.__setattr__(self, "volume", _decimal_from_broker(self.volume, "volume"))
        object.__setattr__(
            self, "price_open", _decimal_from_broker(self.price_open, "price_open")
        )
        if self.volume <= 0 or self.price_open <= 0:
            raise ValueError("position volume and price_open must be positive")
        for name in ("stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is not None:
                normalized = _decimal_from_broker(value, name)
                object.__setattr__(self, name, normalized if normalized > 0 else None)
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise ValueError("position time must be timezone-aware")
        object.__setattr__(self, "time_utc", self.time_utc.astimezone(UTC))

    @property
    def has_numeric_stop_loss(self) -> bool:
        return self.stop_loss is not None and self.stop_loss > 0


@dataclass(frozen=True, slots=True)
class PendingOrderSnapshot:
    """Read-only evidence for an active broker order that has not fully filled."""

    account_id: str
    order_id: str
    symbol: str
    side: str
    volume: Decimal
    magic: int
    comment: str
    time_utc: datetime
    client_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("account_id", "order_id", "symbol"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "side", _side_text(self.side))
        object.__setattr__(self, "volume", _decimal_from_broker(self.volume, "volume"))
        if self.volume <= 0:
            raise ValueError("pending-order volume must be positive")
        if self.magic < 0:
            raise ValueError("pending-order magic must be non-negative")
        comment = "" if self.comment is None else str(self.comment)
        object.__setattr__(self, "comment", comment)
        reference = comment if self.client_reference is None else str(self.client_reference)
        object.__setattr__(self, "client_reference", reference)
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise ValueError("pending-order time must be timezone-aware")
        object.__setattr__(self, "time_utc", self.time_utc.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class MarketOrderRequest:
    account_id: str
    signal_id: str
    leg_index: int
    symbol: str
    side: str
    volume: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    client_reference: str
    magic: int
    deviation_points: int = 0
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    max_spread_points: Decimal | None = None
    expires_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("account_id", "signal_id", "symbol", "client_reference"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        normalized_side = _side_text(self.side)
        object.__setattr__(self, "side", normalized_side)
        object.__setattr__(self, "volume", _decimal(self.volume, "volume"))
        object.__setattr__(self, "stop_loss", _decimal(self.stop_loss, "stop_loss"))
        if self.take_profit is not None:
            object.__setattr__(
                self, "take_profit", _decimal(self.take_profit, "take_profit")
            )
        for name in ("entry_low", "entry_high", "max_spread_points"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
        if self.leg_index < 0:
            raise ValueError("leg_index must be non-negative")
        if self.volume <= 0:
            raise ValueError("volume must be positive")
        if self.stop_loss <= 0:
            raise ValueError("a positive numeric stop_loss is required")
        if self.take_profit is not None and self.take_profit <= 0:
            raise ValueError("take_profit must be positive")
        if self.magic < 0:
            raise ValueError("magic must be non-negative")
        if self.deviation_points < 0:
            raise ValueError("deviation_points must be non-negative")
        if (self.entry_low is None) != (self.entry_high is None):
            raise ValueError("entry_low and entry_high must be configured together")
        if self.entry_low is not None and self.entry_high is not None:
            if self.entry_low <= 0 or self.entry_high < self.entry_low:
                raise ValueError("entry bounds must be positive and ordered")
        if self.max_spread_points is not None and self.max_spread_points <= 0:
            raise ValueError("max_spread_points must be positive")
        if self.expires_at_utc is not None:
            if (
                self.expires_at_utc.tzinfo is None
                or self.expires_at_utc.utcoffset() is None
            ):
                raise ValueError("expires_at_utc must be timezone-aware")
            object.__setattr__(
                self, "expires_at_utc", self.expires_at_utc.astimezone(UTC)
            )


@dataclass(frozen=True, slots=True)
class BrokerResult:
    outcome: BrokerOutcome
    stage: str
    retcode: int | None
    comment: str
    request: MarketOrderRequest | None = None
    price: Decimal | None = None
    volume: Decimal | None = None
    order_id: str | None = None
    deal_id: str | None = None
    raw_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.price is not None:
            object.__setattr__(self, "price", _decimal_from_broker(self.price, "price"))
        if self.volume is not None:
            object.__setattr__(self, "volume", _decimal_from_broker(self.volume, "volume"))
        object.__setattr__(self, "raw_fields", MappingProxyType(dict(self.raw_fields)))

    @property
    def requires_reconciliation(self) -> bool:
        return self.outcome is BrokerOutcome.RECONCILE_REQUIRED

    @property
    def accepted(self) -> bool:
        return self.outcome in {BrokerOutcome.ACCEPTED, BrokerOutcome.PARTIAL}


@runtime_checkable
class BrokerAdapter(Protocol):
    """Minimal broker boundary used by the execution coordinator."""

    def discover_account(self) -> AccountSnapshot: ...

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot: ...

    def get_tick(self, exact_symbol: str) -> TickSnapshot: ...

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]: ...

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]: ...

    def read_back_market_order(
        self, request: MarketOrderRequest, result: BrokerResult
    ) -> PositionSnapshot | None: ...

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult: ...

    def submit_market_order(self, request: MarketOrderRequest) -> BrokerResult: ...


@runtime_checkable
class PositionManager(Protocol):
    """Mutations on an *already open* position the bot has proved it owns.

    Deliberately separate from :class:`BrokerAdapter`: an adapter that can only
    submit (the WebTrader click surface) must not gain these by implementing
    the execution protocol, and a caller that only submits cannot reach them.
    """

    def modify_position_protection(
        self,
        position: PositionSnapshot,
        *,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        expected_magic: int,
        expected_client_reference: str,
    ) -> PositionSnapshot: ...

    def close_position(
        self,
        position: PositionSnapshot,
        *,
        expected_magic: int,
        expected_client_reference: str,
        deviation_points: int = 0,
    ) -> BrokerResult: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, int, or decimal text, not float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _decimal_from_broker(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BrokerUnavailableError(f"broker returned invalid {name}") from exc
    if not result.is_finite():
        raise BrokerUnavailableError(f"broker returned non-finite {name}")
    return result


def _side_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    side = str(raw).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    return side


#: MT5 reports tick, bar, and position timestamps in *broker server* time and
#: exposes no offset field, so the offset has to be measured against a trusted
#: UTC clock.  Server clocks sit on whole or half hours in practice; 15 minutes
#: is the finest grid worth trusting and keeps the rounding residual meaningful
#: as a freshness check.
#: ``ENUM_SYMBOL_ORDER_MODE`` bits as documented for MQL5.  The MetaTrader5
#: Python package returns a symbol's ``order_mode`` bitmask but does not export
#: the names of its bits, so these are the fallback when the module lacks them;
#: a module that does define them still wins.
SYMBOL_ORDER_MARKET_FLAG = 1
SYMBOL_ORDER_SL_FLAG = 16
SYMBOL_ORDER_TP_FLAG = 32

SERVER_OFFSET_GRANULARITY_MINUTES = 15

#: Wider than any real broker offset (UTC-12..UTC+14), so a measurement taken
#: against a frozen feed - a closed market, a disconnected terminal - lands
#: outside it and fails closed instead of inventing a plausible clock.
MAX_SERVER_OFFSET_MINUTES = 14 * 60


def detect_server_utc_offset_minutes(
    server_epoch_seconds: float,
    now_utc: datetime,
    *,
    max_residual_seconds: Decimal,
) -> int:
    """Measure the broker server clock's offset from UTC using a fresh quote.

    ``server_epoch_seconds`` is an MT5 timestamp: seconds since the epoch as
    *counted on the server's clock*.  The difference from real UTC is the
    offset plus however stale the quote is; rounding to
    :data:`SERVER_OFFSET_GRANULARITY_MINUTES` separates the two, and the
    leftover residual is exactly the quote's age.

    Fails closed rather than guessing: a residual beyond the policy's tick-age
    limit means the newest quote is not fresh (a closed market or a stalled
    feed), and an implausible offset means the measurement is not a clock
    offset at all.
    """

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    limit = _decimal(max_residual_seconds, "max_residual_seconds")
    if limit <= 0:
        raise ValueError("max_residual_seconds must be positive")
    difference_seconds = float(server_epoch_seconds) - now_utc.timestamp()
    granularity_seconds = SERVER_OFFSET_GRANULARITY_MINUTES * 60
    steps = math.floor(difference_seconds / granularity_seconds + 0.5)
    offset_minutes = int(steps * SERVER_OFFSET_GRANULARITY_MINUTES)
    if abs(offset_minutes) > MAX_SERVER_OFFSET_MINUTES:
        raise BrokerSafetyError(
            "broker server time is not a plausible clock offset from UTC "
            f"({offset_minutes} minutes); the quote feed is probably stalled"
        )
    residual_seconds = Decimal(
        str(difference_seconds - steps * granularity_seconds)
    )
    if abs(residual_seconds) > limit:
        raise StaleTickError(
            "cannot measure the broker server clock: the newest quote is "
            f"{residual_seconds} seconds off the {offset_minutes}-minute grid, "
            f"limit is {limit}"
        )
    return offset_minutes


def _validate_account(account: AccountSnapshot, policy: DemoAccountPolicy) -> None:
    if not account.is_demo:
        raise LiveAccountRejected("active broker account is not positively identified as Demo")
    if account.login not in policy.allowed_demo_accounts:
        raise AccountNotAllowlisted("active Demo login is not exactly allowlisted")
    if account.server not in policy.allowed_servers:
        raise AccountNotAllowlisted("active broker server is not exactly allowlisted")
    if policy.allowed_companies and account.company not in policy.allowed_companies:
        raise AccountNotAllowlisted("active broker company is not exactly allowlisted")
    if not account.connected:
        raise BrokerUnavailableError("terminal is not connected")
    if not account.trade_allowed or account.trade_api_disabled:
        raise BrokerSafetyError("external trading is not enabled")
    if not account.trade_expert:
        raise BrokerSafetyError("automated Expert trading is not allowed by the account")


def _validate_symbol_allowed(symbol: str, policy: DemoAccountPolicy) -> None:
    if policy.allowed_symbols and symbol not in policy.allowed_symbols:
        raise SymbolNotAvailable("broker symbol is not exactly allowlisted")


def _validate_request_against_market(
    request: MarketOrderRequest,
    account: AccountSnapshot,
    symbol: SymbolSnapshot,
    tick: TickSnapshot,
    policy: DemoAccountPolicy,
    now: datetime,
) -> Decimal:
    _validate_account(account, policy)
    if request.account_id != account.login:
        raise AccountNotAllowlisted("intent account does not match the active Demo login")
    _validate_symbol_allowed(request.symbol, policy)
    if symbol.symbol != request.symbol or tick.symbol != request.symbol:
        raise SymbolNotAvailable("runtime symbol identity does not match the request exactly")
    if not symbol.visible:
        raise SymbolNotAvailable("broker symbol is not visible after discovery")
    mode = symbol.trade_mode.upper()
    if mode in {"DISABLED", "CLOSE_ONLY", "UNKNOWN"}:
        raise SymbolNotAvailable(f"symbol trade mode does not permit entry: {mode}")
    if request.side == "BUY" and mode == "SHORT_ONLY":
        raise SymbolNotAvailable("symbol only permits SELL entries")
    if request.side == "SELL" and mode == "LONG_ONLY":
        raise SymbolNotAvailable("symbol only permits BUY entries")
    if not symbol.market_order_allowed:
        raise SymbolNotAvailable("symbol does not allow market orders")
    if not symbol.stop_loss_allowed:
        raise SymbolNotAvailable("symbol does not allow broker-side Stop Loss")
    if request.take_profit is not None and not symbol.take_profit_allowed:
        raise SymbolNotAvailable("symbol does not allow broker-side Take Profit")
    now_utc = now.astimezone(UTC)
    age = Decimal(str((now_utc - tick.time_utc).total_seconds()))
    if age < Decimal("-1") or age > policy.max_tick_age_seconds:
        raise StaleTickError(f"broker tick age is outside policy: {age} seconds")
    if request.volume < symbol.volume_min or request.volume > symbol.volume_max:
        raise BrokerSafetyError("volume is outside broker min/max")
    units = request.volume / symbol.volume_step
    if units != units.to_integral_value():
        raise BrokerSafetyError("volume is not aligned to broker volume_step")
    price = tick.ask if request.side == "BUY" else tick.bid
    if (
        request.entry_low is not None
        and request.entry_high is not None
        and not request.entry_low <= price <= request.entry_high
    ):
        raise BrokerSafetyError("fresh executable quote is outside the approved entry range")
    if request.max_spread_points is not None:
        spread_points = (tick.ask - tick.bid) / symbol.point
        if spread_points > request.max_spread_points:
            raise BrokerSafetyError("fresh broker spread exceeds the request limit")
    if request.expires_at_utc is not None and now_utc >= request.expires_at_utc:
        raise BrokerSafetyError("signal expired before broker submission")
    if request.side == "BUY":
        if not request.stop_loss < price:
            raise BrokerSafetyError("BUY requires stop_loss below current Ask")
        if request.take_profit is not None and not price < request.take_profit:
            raise BrokerSafetyError("BUY requires take_profit above current Ask")
    else:
        if not price < request.stop_loss:
            raise BrokerSafetyError("SELL requires stop_loss above current Bid")
        if request.take_profit is not None and not request.take_profit < price:
            raise BrokerSafetyError("SELL requires take_profit below current Bid")
    minimum_distance = symbol.point * Decimal(symbol.stops_level_points)
    if minimum_distance > 0:
        protection_quote = tick.bid if request.side == "BUY" else tick.ask
        if abs(protection_quote - request.stop_loss) < minimum_distance:
            raise BrokerSafetyError("stop_loss violates broker stops level")
        if (
            request.take_profit is not None
            and abs(protection_quote - request.take_profit) < minimum_distance
        ):
            raise BrokerSafetyError("take_profit violates broker stops level")
    return price


def _verify_position_ownership(
    positions: Iterable[PositionSnapshot],
    position: PositionSnapshot,
    *,
    expected_magic: int,
    expected_client_reference: str,
) -> PositionSnapshot:
    """Re-prove that one live broker position is the bot's own before touching it.

    Identity is the broker ticket plus every immutable field of the position
    the caller read, plus the bot's ``magic`` and client reference.  A manual
    position, or a position that changed between the read and the mutation, is
    never managed: per the ``idempotency-and-reconciliation`` rule ownership
    has to be proved exactly, not inferred from shape.
    """

    matches = [
        candidate
        for candidate in positions
        if candidate.position_id == position.position_id
    ]
    if not matches:
        raise BrokerSafetyError(
            "open position is no longer present; it must be reconciled, not managed"
        )
    if len(matches) != 1:
        raise BrokerSafetyError("broker returned duplicate positions for one ticket")
    live = matches[0]
    if live.magic != expected_magic or live.comment != expected_client_reference:
        raise BrokerSafetyError("open position is not owned by this bot")
    if (
        live.account_id != position.account_id
        or live.symbol != position.symbol
        or live.side != position.side
        or live.volume != position.volume
        or live.price_open != position.price_open
    ):
        raise BrokerSafetyError("open position changed between read and management")
    return live


def _validate_protection_against_market(
    side: str,
    stop_loss: Decimal,
    take_profit: Decimal | None,
    symbol: SymbolSnapshot,
    tick: TickSnapshot,
) -> None:
    """Reject protective prices the broker would refuse or that remove cover."""

    if stop_loss <= 0:
        raise BrokerSafetyError("a positive numeric stop_loss is required")
    if take_profit is not None and take_profit <= 0:
        raise BrokerSafetyError("take_profit must be positive")
    if not symbol.stop_loss_allowed:
        raise SymbolNotAvailable("symbol does not allow broker-side Stop Loss")
    if take_profit is not None and not symbol.take_profit_allowed:
        raise SymbolNotAvailable("symbol does not allow broker-side Take Profit")
    close_quote = tick.bid if side == "BUY" else tick.ask
    if side == "BUY":
        if not stop_loss < close_quote:
            raise BrokerSafetyError("BUY stop_loss must stay below the current Bid")
        if take_profit is not None and not close_quote < take_profit:
            raise BrokerSafetyError("BUY take_profit must stay above the current Bid")
    else:
        if not close_quote < stop_loss:
            raise BrokerSafetyError("SELL stop_loss must stay above the current Ask")
        if take_profit is not None and not take_profit < close_quote:
            raise BrokerSafetyError("SELL take_profit must stay below the current Ask")
    minimum_distance = symbol.point * Decimal(symbol.stops_level_points)
    if minimum_distance > 0:
        if abs(close_quote - stop_loss) < minimum_distance:
            raise BrokerSafetyError("stop_loss violates broker stops level")
        if take_profit is not None and abs(close_quote - take_profit) < minimum_distance:
            raise BrokerSafetyError("take_profit violates broker stops level")


def _read_back_from_positions(
    positions: Iterable[PositionSnapshot],
    request: MarketOrderRequest,
    result: BrokerResult,
) -> PositionSnapshot | None:
    if result.request is not None and result.request != request:
        raise BrokerSafetyError("broker result belongs to a different market-order request")
    expected_volume = result.volume or request.volume
    owned = [
        position
        for position in positions
        if position.account_id == request.account_id
        and position.symbol == request.symbol
        and position.side == request.side
        and position.magic == request.magic
        and position.comment == request.client_reference
    ]
    if result.order_id is not None:
        identifier_matches = [
            position
            for position in owned
            if result.order_id in {position.position_id, position.identifier}
        ]
        if not identifier_matches:
            return None
        owned = identifier_matches
    if not owned:
        return None
    if len(owned) != 1:
        raise BrokerSafetyError("multiple open positions match one durable order intent")
    position = owned[0]
    if position.volume != expected_volume:
        raise BrokerSafetyError("broker read-back volume does not match the accepted result")
    if position.stop_loss != request.stop_loss:
        raise BrokerSafetyError("broker-side stop_loss is missing or does not match the intent")
    if request.take_profit is not None and position.take_profit != request.take_profit:
        raise BrokerSafetyError("broker-side take_profit does not match the intent")
    return position


class FakeBroker:
    """Deterministic in-memory broker; it never performs external I/O."""

    def __init__(
        self,
        *,
        policy: DemoAccountPolicy,
        account: AccountSnapshot,
        symbols: Mapping[str, SymbolSnapshot],
        ticks: Mapping[str, TickSnapshot],
        positions: Iterable[PositionSnapshot] = (),
        pending_orders: Iterable[PendingOrderSnapshot] = (),
        check_results: Iterable[BrokerResult] = (),
        send_results: Iterable[BrokerResult] = (),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.policy = policy
        self.account = account
        self.symbols = dict(symbols)
        self.ticks = dict(ticks)
        self.positions: list[PositionSnapshot] = list(positions)
        self.pending_orders: list[PendingOrderSnapshot] = list(pending_orders)
        self._check_results: Deque[BrokerResult] = deque(check_results)
        self._send_results: Deque[BrokerResult] = deque(send_results)
        self._clock = clock
        self.checked_requests: list[MarketOrderRequest] = []
        self.sent_requests: list[MarketOrderRequest] = []
        self.protection_changes: list[tuple[str, Decimal, Decimal | None]] = []
        self.closed_positions: list[str] = []
        self._close_results: Deque[BrokerResult] = deque()
        self._ticket_sequence = 1000

    def queue_check_result(self, result: BrokerResult) -> None:
        self._check_results.append(result)

    def queue_send_result(self, result: BrokerResult) -> None:
        self._send_results.append(result)

    def queue_close_result(self, result: BrokerResult) -> None:
        self._close_results.append(result)

    def discover_account(self) -> AccountSnapshot:
        _validate_account(self.account, self.policy)
        return self.account

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot:
        _validate_symbol_allowed(exact_symbol, self.policy)
        symbol = self.symbols.get(exact_symbol)
        if symbol is None or symbol.symbol != exact_symbol:
            raise SymbolNotAvailable(f"exact broker symbol not found: {exact_symbol}")
        return symbol

    def get_tick(self, exact_symbol: str) -> TickSnapshot:
        tick = self.ticks.get(exact_symbol)
        if tick is None or tick.symbol != exact_symbol:
            raise StaleTickError(f"no exact tick available for {exact_symbol}")
        return tick

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]:
        self.discover_account()
        if exact_symbol is not None:
            self.discover_symbol(exact_symbol)
            return tuple(
                position for position in self.positions if position.symbol == exact_symbol
            )
        return tuple(self.positions)

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]:
        self.discover_account()
        if exact_symbol is not None:
            self.discover_symbol(exact_symbol)
            return tuple(
                order for order in self.pending_orders if order.symbol == exact_symbol
            )
        return tuple(self.pending_orders)

    def read_back_market_order(
        self, request: MarketOrderRequest, result: BrokerResult
    ) -> PositionSnapshot | None:
        return _read_back_from_positions(
            self.list_open_positions(request.symbol), request, result
        )

    def modify_position_protection(
        self,
        position: PositionSnapshot,
        *,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        expected_magic: int,
        expected_client_reference: str,
    ) -> PositionSnapshot:
        self.discover_account()
        symbol = self.discover_symbol(position.symbol)
        tick = self.get_tick(position.symbol)
        live = _verify_position_ownership(
            self.list_open_positions(position.symbol),
            position,
            expected_magic=expected_magic,
            expected_client_reference=expected_client_reference,
        )
        new_stop = _decimal(stop_loss, "stop_loss")
        new_target = None if take_profit is None else _decimal(take_profit, "take_profit")
        _validate_protection_against_market(live.side, new_stop, new_target, symbol, tick)
        updated = replace(live, stop_loss=new_stop, take_profit=new_target)
        self.positions = [
            updated if item.position_id == live.position_id else item
            for item in self.positions
        ]
        self.protection_changes.append((live.position_id, new_stop, new_target))
        return updated

    def close_position(
        self,
        position: PositionSnapshot,
        *,
        expected_magic: int,
        expected_client_reference: str,
        deviation_points: int = 0,
    ) -> BrokerResult:
        if deviation_points < 0:
            raise ValueError("deviation_points must be non-negative")
        self.discover_account()
        self.discover_symbol(position.symbol)
        tick = self.get_tick(position.symbol)
        live = _verify_position_ownership(
            self.list_open_positions(position.symbol),
            position,
            expected_magic=expected_magic,
            expected_client_reference=expected_client_reference,
        )
        price = tick.bid if live.side == "BUY" else tick.ask
        if self._close_results:
            scripted = self._close_results.popleft()
            if scripted.outcome is BrokerOutcome.ACCEPTED:
                self._remove_position(live.position_id)
            return replace(
                scripted,
                price=scripted.price if scripted.price is not None else price,
                volume=scripted.volume if scripted.volume is not None else live.volume,
            )
        self._remove_position(live.position_id)
        return BrokerResult(
            outcome=BrokerOutcome.ACCEPTED,
            stage="close_position",
            retcode=10009,
            comment="fake position closed",
            price=price,
            volume=live.volume,
            raw_fields={"position_id": live.position_id},
        )

    def _remove_position(self, position_id: str) -> None:
        self.positions = [
            item for item in self.positions if item.position_id != position_id
        ]
        self.closed_positions.append(position_id)

    def _preflight(self, request: MarketOrderRequest) -> Decimal:
        account = self.discover_account()
        symbol = self.discover_symbol(request.symbol)
        tick = self.get_tick(request.symbol)
        return _validate_request_against_market(
            request, account, symbol, tick, self.policy, self._clock()
        )

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        price = self._preflight(request)
        self.checked_requests.append(request)
        if self._check_results:
            scripted = self._check_results.popleft()
            return replace(
                scripted,
                request=scripted.request or request,
                price=scripted.price if scripted.price is not None else price,
            )
        return BrokerResult(
            outcome=BrokerOutcome.CHECK_PASSED,
            stage="order_check",
            retcode=0,
            comment="fake order check passed",
            request=request,
            price=price,
            volume=request.volume,
        )

    def submit_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        check = self.check_market_order(request)
        if check.outcome is not BrokerOutcome.CHECK_PASSED:
            return check
        self.sent_requests.append(request)
        if self._send_results:
            scripted = self._send_results.popleft()
            result = replace(
                scripted,
                request=scripted.request or request,
                price=scripted.price if scripted.price is not None else check.price,
                volume=scripted.volume if scripted.volume is not None else request.volume,
            )
        else:
            self._ticket_sequence += 1
            ticket = str(self._ticket_sequence)
            result = BrokerResult(
                outcome=BrokerOutcome.ACCEPTED,
                stage="order_send",
                retcode=10009,
                comment="fake order accepted",
                request=request,
                price=check.price,
                volume=request.volume,
                order_id=ticket,
                deal_id=ticket,
            )
        if result.accepted:
            position_id = result.order_id
            if position_id is None:
                self._ticket_sequence += 1
                position_id = str(self._ticket_sequence)
            self.positions.append(
                PositionSnapshot(
                    account_id=request.account_id,
                    position_id=position_id,
                    identifier=result.order_id,
                    symbol=request.symbol,
                    side=request.side,
                    volume=result.volume or request.volume,
                    price_open=result.price or check.price or Decimal("0"),
                    stop_loss=request.stop_loss,
                    take_profit=request.take_profit,
                    magic=request.magic,
                    comment=request.client_reference,
                    time_utc=self._clock(),
                )
            )
        return result


class MetaTrader5Broker:
    """Lazy, exact-allowlisted XM/MT5 Demo adapter.

    This class intentionally exposes no retry loop.  ``submit_market_order``
    performs one fresh preflight, one ``order_check``, and at most one
    ``order_send`` call.
    """

    def __init__(
        self,
        *,
        policy: DemoAccountPolicy,
        terminal_path: str | None = None,
        mt5_module: Any | None = None,
        clock: Callable[[], datetime] = _utc_now,
        server_utc_offset_minutes: int | None = 0,
    ) -> None:
        self.policy = policy
        self.terminal_path = terminal_path
        self._mt5 = mt5_module
        self._clock = clock
        self._initialized = False
        if server_utc_offset_minutes is None:
            self._offset_minutes: int | None = None
            self._offset_source = "unresolved"
        else:
            offset = int(server_utc_offset_minutes)
            if abs(offset) > MAX_SERVER_OFFSET_MINUTES:
                raise ValueError("server_utc_offset_minutes is not a plausible offset")
            self._offset_minutes = offset
            self._offset_source = "configured"

    @property
    def server_utc_offset_minutes(self) -> int | None:
        """Resolved broker-server offset from UTC, or ``None`` until measured."""

        return self._offset_minutes

    @property
    def server_offset_source(self) -> str:
        return self._offset_source

    def resolve_server_utc_offset(self, exact_symbol: str) -> int:
        """Measure and cache the server clock offset from one fresh quote.

        Measured once per session on purpose.  A later clock drift or a frozen
        feed then shows up as a growing tick age and fails the freshness gate,
        which is what that gate is for; re-measuring on every call would keep
        re-centring the window on a stalled feed and hide exactly that.
        """

        if self._offset_minutes is not None:
            return self._offset_minutes
        self.get_tick(exact_symbol)
        assert self._offset_minutes is not None
        return self._offset_minutes

    def _to_utc(self, server_epoch_seconds: float) -> datetime:
        """Convert an MT5 server-clock timestamp to true UTC.

        Before the offset is resolved this is the raw server reading.  Only
        :meth:`get_tick` feeds a time-based gate, and it always resolves the
        offset first; position and order timestamps are evidence fields.
        """

        offset_seconds = (self._offset_minutes or 0) * 60
        return datetime.fromtimestamp(server_epoch_seconds - offset_seconds, tz=UTC)

    def __enter__(self) -> MetaTrader5Broker:
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.shutdown()

    def initialize(self) -> None:
        if self._initialized:
            return
        if self._mt5 is None:
            try:
                self._mt5 = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise BrokerUnavailableError(
                    "MetaTrader5 is optional and is not installed"
                ) from exc
        try:
            ok = (
                self._mt5.initialize(self.terminal_path)
                if self.terminal_path
                else self._mt5.initialize()
            )
        except Exception as exc:
            raise BrokerUnavailableError("MetaTrader5 initialize failed") from exc
        if not ok:
            raise BrokerUnavailableError(
                f"MetaTrader5 initialize returned false: {self._last_error()}"
            )
        self._initialized = True

    def shutdown(self) -> None:
        if self._initialized and self._mt5 is not None:
            try:
                self._mt5.shutdown()
            finally:
                self._initialized = False

    def _ensure_initialized(self) -> Any:
        if not self._initialized:
            self.initialize()
        assert self._mt5 is not None
        return self._mt5

    def _last_error(self) -> str:
        if self._mt5 is None:
            return "MetaTrader5 module unavailable"
        try:
            return str(self._mt5.last_error())
        except Exception:
            return "MetaTrader5 last_error unavailable"

    def discover_account(self) -> AccountSnapshot:
        mt5 = self._ensure_initialized()
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is None or account is None:
            raise BrokerUnavailableError(
                f"MT5 account/terminal information unavailable: {self._last_error()}"
            )
        demo_constant = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        if demo_constant is None:
            raise BrokerUnavailableError("MT5 Demo trade-mode constant is unavailable")
        trade_mode = getattr(account, "trade_mode", None)
        snapshot = AccountSnapshot(
            login=str(getattr(account, "login", "")),
            server=str(getattr(account, "server", "")),
            company=str(getattr(account, "company", "")),
            is_demo=trade_mode == demo_constant,
            connected=bool(getattr(terminal, "connected", False)),
            trade_allowed=bool(getattr(account, "trade_allowed", False))
            and bool(getattr(terminal, "trade_allowed", False)),
            trade_api_disabled=bool(getattr(terminal, "tradeapi_disabled", True)),
            trade_expert=bool(getattr(account, "trade_expert", False)),
            currency=str(getattr(account, "currency", "")),
            margin_mode=self._margin_mode_text(
                int(getattr(account, "margin_mode", -1))
            ),
        )
        _validate_account(snapshot, self.policy)
        return snapshot

    def _margin_mode_text(self, value: int) -> str:
        mt5 = self._ensure_initialized()
        modes = {
            getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", object()): (
                "RETAIL_NETTING"
            ),
            getattr(mt5, "ACCOUNT_MARGIN_MODE_EXCHANGE", object()): "EXCHANGE",
            getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", object()): (
                "RETAIL_HEDGING"
            ),
        }
        return modes.get(value, "UNKNOWN")

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot:
        mt5 = self._ensure_initialized()
        _validate_symbol_allowed(exact_symbol, self.policy)
        info = mt5.symbol_info(exact_symbol)
        if info is None:
            raise SymbolNotAvailable(f"exact MT5 symbol not found: {exact_symbol}")
        if str(getattr(info, "name", exact_symbol)) != exact_symbol:
            raise SymbolNotAvailable("MT5 returned a non-exact symbol identity")
        if not bool(getattr(info, "visible", False)):
            if not mt5.symbol_select(exact_symbol, True):
                raise SymbolNotAvailable(
                    f"cannot select exact MT5 symbol {exact_symbol}: {self._last_error()}"
                )
            info = mt5.symbol_info(exact_symbol)
            if info is None or not bool(getattr(info, "visible", False)):
                raise SymbolNotAvailable("symbol is still unavailable after exact selection")
        trade_mode = self._trade_mode_text(int(getattr(info, "trade_mode", -1)))
        order_mode = int(getattr(info, "order_mode", 0))
        market_flag = getattr(mt5, "SYMBOL_ORDER_MARKET", SYMBOL_ORDER_MARKET_FLAG)
        sl_flag = getattr(mt5, "SYMBOL_ORDER_SL", SYMBOL_ORDER_SL_FLAG)
        tp_flag = getattr(mt5, "SYMBOL_ORDER_TP", SYMBOL_ORDER_TP_FLAG)
        if not order_mode:
            raise BrokerUnavailableError("MT5 symbol order-mode bitmask is unavailable")
        return SymbolSnapshot(
            symbol=exact_symbol,
            visible=True,
            trade_mode=trade_mode,
            digits=int(getattr(info, "digits", 0)),
            point=_decimal_from_broker(getattr(info, "point", 0), "point"),
            tick_size=_decimal_from_broker(
                getattr(info, "trade_tick_size", 0), "trade_tick_size"
            ),
            tick_value=_decimal_from_broker(
                getattr(info, "trade_tick_value", 0), "trade_tick_value"
            ),
            contract_size=_decimal_from_broker(
                getattr(info, "trade_contract_size", 0), "trade_contract_size"
            ),
            volume_min=_decimal_from_broker(
                getattr(info, "volume_min", 0), "volume_min"
            ),
            volume_max=_decimal_from_broker(
                getattr(info, "volume_max", 0), "volume_max"
            ),
            volume_step=_decimal_from_broker(
                getattr(info, "volume_step", 0), "volume_step"
            ),
            stops_level_points=int(getattr(info, "trade_stops_level", 0)),
            freeze_level_points=int(getattr(info, "trade_freeze_level", 0)),
            filling_flags=int(getattr(info, "filling_mode", 0)),
            execution_mode=int(getattr(info, "trade_exemode", 0)),
            market_order_allowed=bool(order_mode & int(market_flag)),
            stop_loss_allowed=bool(order_mode & int(sl_flag)),
            take_profit_allowed=bool(order_mode & int(tp_flag)),
        )

    def get_tick(self, exact_symbol: str) -> TickSnapshot:
        mt5 = self._ensure_initialized()
        tick = mt5.symbol_info_tick(exact_symbol)
        if tick is None:
            raise StaleTickError(
                f"MT5 tick unavailable for {exact_symbol}: {self._last_error()}"
            )
        time_msc = int(getattr(tick, "time_msc", 0) or 0)
        if time_msc:
            server_epoch = time_msc / 1000
        else:
            seconds = int(getattr(tick, "time", 0) or 0)
            if not seconds:
                raise StaleTickError("MT5 tick has no timestamp")
            server_epoch = float(seconds)
        if self._offset_minutes is None:
            self._offset_minutes = detect_server_utc_offset_minutes(
                server_epoch,
                self._clock(),
                max_residual_seconds=self.policy.max_tick_age_seconds,
            )
            self._offset_source = "detected"
        tick_time = self._to_utc(server_epoch)
        return TickSnapshot(
            symbol=exact_symbol,
            bid=_decimal_from_broker(getattr(tick, "bid", 0), "bid"),
            ask=_decimal_from_broker(getattr(tick, "ask", 0), "ask"),
            time_utc=tick_time,
        )

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]:
        mt5 = self._ensure_initialized()
        account = self.discover_account()
        if exact_symbol is not None:
            self.discover_symbol(exact_symbol)
            raw_positions = mt5.positions_get(symbol=exact_symbol)
        else:
            raw_positions = mt5.positions_get()
        if raw_positions is None:
            raise BrokerUnavailableError(
                f"MT5 open-position query failed: {self._last_error()}"
            )
        buy_type = getattr(mt5, "POSITION_TYPE_BUY", None)
        sell_type = getattr(mt5, "POSITION_TYPE_SELL", None)
        if buy_type is None or sell_type is None:
            raise BrokerUnavailableError("MT5 position-side constants are unavailable")
        snapshots: list[PositionSnapshot] = []
        for raw in raw_positions:
            raw_type = getattr(raw, "type", None)
            if raw_type == buy_type:
                side = "BUY"
            elif raw_type == sell_type:
                side = "SELL"
            else:
                raise BrokerUnavailableError("MT5 returned an unknown open-position side")
            time_msc = int(getattr(raw, "time_msc", 0) or 0)
            if time_msc:
                opened_at = self._to_utc(time_msc / 1000)
            else:
                seconds = int(getattr(raw, "time", 0) or 0)
                if not seconds:
                    raise BrokerUnavailableError("MT5 position has no open timestamp")
                opened_at = self._to_utc(float(seconds))
            snapshots.append(
                PositionSnapshot(
                    account_id=account.login,
                    position_id=str(getattr(raw, "ticket", "")),
                    identifier=(
                        None
                        if getattr(raw, "identifier", None) in (None, 0)
                        else str(getattr(raw, "identifier"))
                    ),
                    symbol=str(getattr(raw, "symbol", "")),
                    side=side,
                    volume=_decimal_from_broker(
                        getattr(raw, "volume", 0), "position volume"
                    ),
                    price_open=_decimal_from_broker(
                        getattr(raw, "price_open", 0), "position price_open"
                    ),
                    stop_loss=_decimal_from_broker(
                        getattr(raw, "sl", 0), "position stop_loss"
                    ),
                    take_profit=_decimal_from_broker(
                        getattr(raw, "tp", 0), "position take_profit"
                    ),
                    magic=int(getattr(raw, "magic", -1)),
                    comment=str(getattr(raw, "comment", "")),
                    time_utc=opened_at,
                )
            )
        return tuple(snapshots)

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]:
        mt5 = self._ensure_initialized()
        account = self.discover_account()
        if exact_symbol is not None:
            self.discover_symbol(exact_symbol)
        try:
            raw_orders = (
                mt5.orders_get(symbol=exact_symbol)
                if exact_symbol is not None
                else mt5.orders_get()
            )
        except Exception:
            raise BrokerUnavailableError("MT5 pending-order query failed") from None
        if raw_orders is None:
            raise BrokerUnavailableError("MT5 pending-order query failed")

        buy_types = {
            getattr(mt5, name)
            for name in (
                "ORDER_TYPE_BUY",
                "ORDER_TYPE_BUY_LIMIT",
                "ORDER_TYPE_BUY_STOP",
                "ORDER_TYPE_BUY_STOP_LIMIT",
            )
            if hasattr(mt5, name)
        }
        sell_types = {
            getattr(mt5, name)
            for name in (
                "ORDER_TYPE_SELL",
                "ORDER_TYPE_SELL_LIMIT",
                "ORDER_TYPE_SELL_STOP",
                "ORDER_TYPE_SELL_STOP_LIMIT",
            )
            if hasattr(mt5, name)
        }
        snapshots: list[PendingOrderSnapshot] = []
        try:
            for raw in raw_orders:
                raw_type = getattr(raw, "type", None)
                if raw_type in buy_types:
                    side = "BUY"
                elif raw_type in sell_types:
                    side = "SELL"
                else:
                    raise ValueError("unknown pending-order side")
                time_msc = int(getattr(raw, "time_setup_msc", 0) or 0)
                if time_msc:
                    created_at = self._to_utc(time_msc / 1000)
                else:
                    seconds = int(getattr(raw, "time_setup", 0) or 0)
                    if not seconds:
                        raise ValueError("missing pending-order timestamp")
                    created_at = self._to_utc(float(seconds))
                volume_value = getattr(raw, "volume_current", None)
                if volume_value is None:
                    volume_value = getattr(raw, "volume_initial", None)
                comment = str(getattr(raw, "comment", ""))
                snapshot = PendingOrderSnapshot(
                    account_id=account.login,
                    order_id=str(getattr(raw, "ticket", "")),
                    symbol=str(getattr(raw, "symbol", "")),
                    side=side,
                    volume=_decimal_from_broker(volume_value, "pending-order volume"),
                    magic=int(getattr(raw, "magic", -1)),
                    comment=comment,
                    client_reference=comment,
                    time_utc=created_at,
                )
                if exact_symbol is not None and snapshot.symbol != exact_symbol:
                    raise ValueError("pending-order symbol mismatch")
                snapshots.append(snapshot)
        except Exception:
            raise BrokerUnavailableError(
                "MT5 returned malformed pending-order state"
            ) from None

        confirmed_account = self.discover_account()
        if (
            confirmed_account.login != account.login
            or confirmed_account.server != account.server
            or confirmed_account.company != account.company
        ):
            raise BrokerUnavailableError(
                "active Demo account changed during pending-order query"
            )
        return tuple(snapshots)

    def read_back_market_order(
        self, request: MarketOrderRequest, result: BrokerResult
    ) -> PositionSnapshot | None:
        return _read_back_from_positions(
            self.list_open_positions(request.symbol), request, result
        )

    def _trade_mode_text(self, value: int) -> str:
        mt5 = self._ensure_initialized()
        modes = {
            getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", object()): "DISABLED",
            getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", object()): "LONG_ONLY",
            getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", object()): "SHORT_ONLY",
            getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", object()): "CLOSE_ONLY",
            getattr(mt5, "SYMBOL_TRADE_MODE_FULL", object()): "FULL",
        }
        return modes.get(value, "UNKNOWN")

    def _filling_type(self, symbol: SymbolSnapshot) -> int:
        mt5 = self._ensure_initialized()
        flags = symbol.filling_flags
        symbol_fok = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
        symbol_ioc = getattr(mt5, "SYMBOL_FILLING_IOC", 2)
        if flags & symbol_fok:
            return int(getattr(mt5, "ORDER_FILLING_FOK"))
        if flags & symbol_ioc:
            return int(getattr(mt5, "ORDER_FILLING_IOC"))
        market_execution = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", None)
        if market_execution is None or symbol.execution_mode != market_execution:
            return int(getattr(mt5, "ORDER_FILLING_RETURN"))
        raise BrokerSafetyError("no supported filling mode discovered for market order")

    def _prepare_request(
        self, request: MarketOrderRequest
    ) -> tuple[dict[str, Any], Decimal]:
        mt5 = self._ensure_initialized()
        account = self.discover_account()
        symbol = self.discover_symbol(request.symbol)
        tick = self.get_tick(request.symbol)
        price = _validate_request_against_market(
            request, account, symbol, tick, self.policy, self._clock()
        )
        side_type = (
            getattr(mt5, "ORDER_TYPE_BUY")
            if request.side == "BUY"
            else getattr(mt5, "ORDER_TYPE_SELL")
        )
        payload: dict[str, Any] = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL"),
            "symbol": request.symbol,
            "volume": float(request.volume),
            "type": side_type,
            "sl": float(request.stop_loss),
            "deviation": request.deviation_points,
            "magic": request.magic,
            "comment": request.client_reference,
            "type_time": getattr(mt5, "ORDER_TIME_GTC"),
            "type_filling": self._filling_type(symbol),
        }
        market_execution = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", None)
        if market_execution is None:
            raise BrokerUnavailableError("MT5 market-execution constant is unavailable")
        if symbol.execution_mode == market_execution:
            if request.entry_low is not None or request.entry_high is not None:
                raise BrokerSafetyError(
                    "strict entry bounds cannot be guaranteed with Market Execution"
                )
        else:
            payload["price"] = float(price)
        if request.take_profit is not None:
            payload["tp"] = float(request.take_profit)
        return payload, price

    def _check_payload(
        self,
        payload: Mapping[str, Any],
        request: MarketOrderRequest,
        price: Decimal,
    ) -> BrokerResult:
        mt5 = self._ensure_initialized()
        try:
            result = mt5.order_check(dict(payload))
        except Exception as exc:
            return BrokerResult(
                outcome=BrokerOutcome.REJECTED,
                stage="order_check",
                retcode=None,
                comment=f"order_check raised {type(exc).__name__}; no order was submitted",
                request=request,
                price=price,
                volume=request.volume,
                raw_fields={"last_error": self._last_error()},
            )
        if result is None:
            return BrokerResult(
                outcome=BrokerOutcome.REJECTED,
                stage="order_check",
                retcode=None,
                comment="order_check returned no result; no order was submitted",
                request=request,
                price=price,
                volume=request.volume,
                raw_fields={"last_error": self._last_error()},
            )
        retcode = int(getattr(result, "retcode", -1))
        outcome = BrokerOutcome.CHECK_PASSED if retcode == 0 else BrokerOutcome.REJECTED
        return self._result_from_mt5(
            result,
            outcome=outcome,
            stage="order_check",
            request=request,
            fallback_price=price,
        )

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        payload, price = self._prepare_request(request)
        return self._check_payload(payload, request, price)

    def submit_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        payload, price = self._prepare_request(request)
        check = self._check_payload(payload, request, price)
        if check.outcome is not BrokerOutcome.CHECK_PASSED:
            return check
        # order_check can take long enough for a market quote to leave the
        # approved zone or spread/expiry window. Rebuild from a fresh tick and
        # re-run every local safety constraint immediately before order_send.
        payload, price = self._prepare_request(request)
        mt5 = self._ensure_initialized()
        # The terminal account can be switched by the operator while this
        # process is running. Re-read and exact-match it immediately before the
        # irreversible call; preflight evidence alone is not sufficient.
        account = self.discover_account()
        if account.login != request.account_id:
            raise AccountNotAllowlisted(
                "active Demo login changed after order_check; order was not submitted"
            )
        try:
            result = mt5.order_send(dict(payload))
        except Exception as exc:
            return BrokerResult(
                outcome=BrokerOutcome.RECONCILE_REQUIRED,
                stage="order_send",
                retcode=None,
                comment=(
                    f"order_send raised {type(exc).__name__}; outcome is ambiguous "
                    "and must be reconciled"
                ),
                request=request,
                price=price,
                volume=request.volume,
                raw_fields={"last_error": self._last_error()},
            )
        if result is None:
            return BrokerResult(
                outcome=BrokerOutcome.RECONCILE_REQUIRED,
                stage="order_send",
                retcode=None,
                comment="order_send returned no result; broker state must be reconciled",
                request=request,
                price=price,
                volume=request.volume,
                raw_fields={"last_error": self._last_error()},
            )
        retcode = int(getattr(result, "retcode", -1))
        outcome = self._classify_send_retcode(retcode)
        return self._result_from_mt5(
            result,
            outcome=outcome,
            stage="order_send",
            request=request,
            fallback_price=price,
        )

    def modify_position_protection(
        self,
        position: PositionSnapshot,
        *,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        expected_magic: int,
        expected_client_reference: str,
    ) -> PositionSnapshot:
        """Replace the broker-side protection of one owned position.

        Only ever relocates protection the bot already placed; it cannot change
        volume or open anything.  The new Stop Loss must still be a valid
        protective price at the current quote, so this can never be used to
        remove cover.  Success requires exact read-back.
        """

        mt5 = self._ensure_initialized()
        account = self.discover_account()
        if account.login != position.account_id:
            raise AccountNotAllowlisted(
                "position account does not match the active Demo login"
            )
        symbol = self.discover_symbol(position.symbol)
        tick = self.get_tick(position.symbol)
        live = _verify_position_ownership(
            self.list_open_positions(position.symbol),
            position,
            expected_magic=expected_magic,
            expected_client_reference=expected_client_reference,
        )
        new_stop = _decimal(stop_loss, "stop_loss")
        new_target = None if take_profit is None else _decimal(take_profit, "take_profit")
        _validate_protection_against_market(live.side, new_stop, new_target, symbol, tick)
        payload: dict[str, Any] = {
            "action": getattr(mt5, "TRADE_ACTION_SLTP"),
            "symbol": live.symbol,
            "position": int(live.position_id),
            "sl": float(new_stop),
            "tp": 0.0 if new_target is None else float(new_target),
        }
        try:
            mt5.order_send(dict(payload))
        except Exception:
            # A protection change cannot create exposure, so an ambiguous send
            # is resolved by read-back below rather than by a retry.
            pass
        applied = _verify_position_ownership(
            self.list_open_positions(live.symbol),
            live,
            expected_magic=expected_magic,
            expected_client_reference=expected_client_reference,
        )
        tolerance = symbol.point / Decimal(2)
        if applied.stop_loss is None or abs(applied.stop_loss - new_stop) > tolerance:
            raise BrokerSafetyError(
                "broker did not apply the requested stop_loss; position is unchanged"
            )
        if new_target is not None and (
            applied.take_profit is None
            or abs(applied.take_profit - new_target) > tolerance
        ):
            raise BrokerSafetyError("broker did not preserve the requested take_profit")
        return applied

    def close_position(
        self,
        position: PositionSnapshot,
        *,
        expected_magic: int,
        expected_client_reference: str,
        deviation_points: int = 0,
    ) -> BrokerResult:
        """Close one owned position at market with a single, unrepeated send.

        The result is only ``ACCEPTED`` when the ticket is gone from broker
        state afterwards.  A position still open after an accepted-looking
        result is ``RECONCILE_REQUIRED``, never a second attempt.
        """

        if deviation_points < 0:
            raise ValueError("deviation_points must be non-negative")
        mt5 = self._ensure_initialized()
        account = self.discover_account()
        if account.login != position.account_id:
            raise AccountNotAllowlisted(
                "position account does not match the active Demo login"
            )
        symbol = self.discover_symbol(position.symbol)
        tick = self.get_tick(position.symbol)
        live = _verify_position_ownership(
            self.list_open_positions(position.symbol),
            position,
            expected_magic=expected_magic,
            expected_client_reference=expected_client_reference,
        )
        closing_side = "SELL" if live.side == "BUY" else "BUY"
        price = tick.bid if live.side == "BUY" else tick.ask
        payload: dict[str, Any] = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL"),
            "symbol": live.symbol,
            "position": int(live.position_id),
            "volume": float(live.volume),
            "type": (
                getattr(mt5, "ORDER_TYPE_SELL")
                if closing_side == "SELL"
                else getattr(mt5, "ORDER_TYPE_BUY")
            ),
            "price": float(price),
            "deviation": int(deviation_points),
            "magic": expected_magic,
            "comment": expected_client_reference,
            "type_time": getattr(mt5, "ORDER_TIME_GTC"),
            "type_filling": self._filling_type(symbol),
        }
        send_error: str | None = None
        try:
            result = mt5.order_send(dict(payload))
        except Exception as exc:
            result = None
            send_error = f"close order_send raised {type(exc).__name__}"
        still_open = [
            candidate
            for candidate in self.list_open_positions(live.symbol)
            if candidate.position_id == live.position_id
        ]
        if result is None:
            return BrokerResult(
                outcome=(
                    BrokerOutcome.ACCEPTED
                    if not still_open
                    else BrokerOutcome.RECONCILE_REQUIRED
                ),
                stage="close_position",
                retcode=None,
                comment=send_error or "close order_send returned no result",
                price=price,
                volume=live.volume,
                raw_fields={"position_id": live.position_id},
            )
        retcode = int(getattr(result, "retcode", -1))
        outcome = self._classify_send_retcode(retcode)
        if still_open:
            # Anything short of the ticket disappearing leaves ownership open;
            # a clean rejection that left it open is simply a rejection.
            outcome = (
                BrokerOutcome.REJECTED
                if outcome is BrokerOutcome.REJECTED
                else BrokerOutcome.RECONCILE_REQUIRED
            )
        elif outcome is not BrokerOutcome.REJECTED:
            outcome = BrokerOutcome.ACCEPTED
        return BrokerResult(
            outcome=outcome,
            stage="close_position",
            retcode=retcode,
            comment=str(getattr(result, "comment", "")),
            price=(
                price
                if getattr(result, "price", None) in (None, 0, 0.0)
                else _decimal_from_broker(getattr(result, "price"), "close price")
            ),
            volume=live.volume,
            order_id=(
                None
                if getattr(result, "order", None) in (None, 0)
                else str(getattr(result, "order"))
            ),
            deal_id=(
                None
                if getattr(result, "deal", None) in (None, 0)
                else str(getattr(result, "deal"))
            ),
            raw_fields={"position_id": live.position_id},
        )

    def _classify_send_retcode(self, retcode: int) -> BrokerOutcome:
        mt5 = self._ensure_initialized()
        accepted = {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        }
        partial = {int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010))}
        ambiguous = {
            int(getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012)),
            int(getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031)),
        }
        rejected = {
            int(getattr(mt5, name))
            for name in (
                "TRADE_RETCODE_REQUOTE",
                "TRADE_RETCODE_REJECT",
                "TRADE_RETCODE_CANCEL",
                "TRADE_RETCODE_INVALID",
                "TRADE_RETCODE_INVALID_VOLUME",
                "TRADE_RETCODE_INVALID_PRICE",
                "TRADE_RETCODE_INVALID_STOPS",
                "TRADE_RETCODE_TRADE_DISABLED",
                "TRADE_RETCODE_MARKET_CLOSED",
                "TRADE_RETCODE_NO_MONEY",
                "TRADE_RETCODE_PRICE_CHANGED",
                "TRADE_RETCODE_PRICE_OFF",
                "TRADE_RETCODE_INVALID_EXPIRATION",
                "TRADE_RETCODE_TOO_MANY_REQUESTS",
                "TRADE_RETCODE_NO_CHANGES",
                "TRADE_RETCODE_SERVER_DISABLES_AT",
                "TRADE_RETCODE_CLIENT_DISABLES_AT",
                "TRADE_RETCODE_LIMIT_VOLUME",
                "TRADE_RETCODE_INVALID_ORDER",
                "TRADE_RETCODE_INVALID_FILL",
            )
            if hasattr(mt5, name)
        }
        if retcode in accepted:
            return BrokerOutcome.ACCEPTED
        if retcode in partial:
            return BrokerOutcome.PARTIAL
        if retcode in ambiguous:
            return BrokerOutcome.RECONCILE_REQUIRED
        if retcode in rejected:
            return BrokerOutcome.REJECTED
        # Unknown post-send results are not proof that no broker action occurred.
        return BrokerOutcome.RECONCILE_REQUIRED

    @staticmethod
    def _result_from_mt5(
        result: Any,
        *,
        outcome: BrokerOutcome,
        stage: str,
        request: MarketOrderRequest,
        fallback_price: Decimal,
    ) -> BrokerResult:
        price_value = getattr(result, "price", None)
        volume_value = getattr(result, "volume", None)
        return BrokerResult(
            outcome=outcome,
            stage=stage,
            retcode=int(getattr(result, "retcode", -1)),
            comment=str(getattr(result, "comment", "")),
            request=request,
            price=(
                fallback_price
                if price_value in (None, 0, 0.0)
                else _decimal_from_broker(price_value, "result price")
            ),
            volume=(
                request.volume
                if volume_value in (None, 0, 0.0)
                else _decimal_from_broker(volume_value, "result volume")
            ),
            order_id=(
                None
                if getattr(result, "order", None) in (None, 0)
                else str(getattr(result, "order"))
            ),
            deal_id=(
                None
                if getattr(result, "deal", None) in (None, 0)
                else str(getattr(result, "deal"))
            ),
        )


__all__ = [
    "MAX_SERVER_OFFSET_MINUTES",
    "SERVER_OFFSET_GRANULARITY_MINUTES",
    "AccountNotAllowlisted",
    "AccountSnapshot",
    "BrokerAdapter",
    "BrokerError",
    "BrokerOutcome",
    "BrokerResult",
    "BrokerSafetyError",
    "BrokerUnavailableError",
    "DemoAccountPolicy",
    "FakeBroker",
    "LiveAccountRejected",
    "MarketOrderRequest",
    "MetaTrader5Broker",
    "PendingOrderSnapshot",
    "PositionManager",
    "PositionSnapshot",
    "StaleTickError",
    "SymbolNotAvailable",
    "SymbolSnapshot",
    "TickSnapshot",
    "detect_server_utc_offset_minutes",
]
