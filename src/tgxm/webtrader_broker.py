"""Hybrid XM Demo broker: MT5 read/check evidence, WebTrader execution.

The adapter deliberately keeps the irreversible browser click outside the MT5
delegate.  It never calls ``submit_market_order`` on that delegate.  A browser
commit is attempted at most once and is not considered accepted until an exact
MT5 position can be correlated to an identifier returned by WebTrader.

The protocols in this module are structural.  In particular, the production
``webtrader_click`` implementation can provide its own frozen identity, token,
and receipt DTOs without importing this module or inheriting from these types.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import threading
import time
from typing import Any, Protocol, runtime_checkable

from tgxm.broker import (
    AccountNotAllowlisted,
    AccountSnapshot,
    BrokerAdapter,
    BrokerOutcome,
    BrokerResult,
    BrokerSafetyError,
    BrokerUnavailableError,
    DemoAccountPolicy,
    LiveAccountRejected,
    MarketOrderRequest,
    MetaTrader5Broker,
    PendingOrderSnapshot,
    PositionSnapshot,
    StaleTickError,
    SymbolNotAvailable,
    SymbolSnapshot,
    TickSnapshot,
    _validate_request_against_market,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class ReadOnlyBrokerDelegate(Protocol):
    """MT5-shaped evidence boundary used by :class:`WebTraderBroker`.

    ``submit_market_order`` is intentionally absent.  A real
    :class:`~tgxm.broker.MetaTrader5Broker` is structurally compatible for the
    methods below, but this wrapper never reaches its mutation method.
    """

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def discover_account(self) -> AccountSnapshot: ...

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot: ...

    def get_tick(self, exact_symbol: str) -> TickSnapshot: ...

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]: ...

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]: ...

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult: ...


class WebTraderIdentity(Protocol):
    """Minimum browser identity evidence required before a click."""

    login: str
    server: str
    is_demo: bool
    origin: str


class WebTraderReceipt(Protocol):
    """Minimum non-secret receipt evidence returned by a single browser click."""

    order_id: str | None
    deal_id: str | None
    position_id: str | None
    clicked_at_utc: datetime
    origin: str


@runtime_checkable
class WebTraderExecutor(Protocol):
    """Structural WebTrader click boundary.

    ``prepare_order`` may populate and read back a form but must not submit it.
    ``commit_once`` owns the single irreversible click.  The token is opaque to
    this adapter so browser implementations can enforce their own nonce and
    one-shot semantics.
    """

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def inspect_identity(
        self, *, expected_login: str, expected_server: str
    ) -> WebTraderIdentity: ...

    def prepare_order(self, request: MarketOrderRequest) -> object: ...

    def commit_once(
        self,
        token: object,
        *,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> WebTraderReceipt | None: ...


def _validate_read_only_account(
    account: AccountSnapshot, policy: DemoAccountPolicy
) -> None:
    """Validate MT5 identity without requiring its mutation permissions."""

    if not account.is_demo:
        raise LiveAccountRejected(
            "active MT5 account is not positively identified as Demo"
        )
    if account.login not in policy.allowed_demo_accounts:
        raise AccountNotAllowlisted("active MT5 Demo login is not exactly allowlisted")
    if account.server not in policy.allowed_servers:
        raise AccountNotAllowlisted("active MT5 server is not exactly allowlisted")
    if policy.allowed_companies and account.company not in policy.allowed_companies:
        raise AccountNotAllowlisted("active MT5 company is not exactly allowlisted")
    if not account.connected:
        raise BrokerUnavailableError("MT5 terminal is not connected")


def _account_identity(account: AccountSnapshot) -> tuple[str, str, str, bool, str]:
    return (
        account.login,
        account.server,
        account.company,
        account.is_demo,
        account.margin_mode,
    )


def _require_stable_account(
    before: AccountSnapshot, after: AccountSnapshot, operation: str
) -> None:
    if _account_identity(before) != _account_identity(after):
        raise BrokerUnavailableError(
            f"active MT5 Demo account changed during {operation}"
        )


class _MetaTrader5ReadOnlyOracle(MetaTrader5Broker):
    """MT5 implementation detail with relaxed API-mutation permission gates."""

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
        snapshot = AccountSnapshot(
            login=str(getattr(account, "login", "")),
            server=str(getattr(account, "server", "")),
            company=str(getattr(account, "company", "")),
            is_demo=getattr(account, "trade_mode", None) == demo_constant,
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
        _validate_read_only_account(snapshot, self.policy)
        return snapshot

    def _prepare_request(
        self, request: MarketOrderRequest
    ) -> tuple[dict[str, Any], Decimal]:
        """Build an ``order_check`` payload while keeping all market guards.

        The synthetic account is local validation input only.  The real flags
        remain visible on ``discover_account``; only the shared validator's
        MT5-mutation permission checks are bypassed for this read-only oracle.
        """

        mt5 = self._ensure_initialized()
        account = self.discover_account()
        symbol = self.discover_symbol(request.symbol)
        tick = self.get_tick(request.symbol)
        validation_account = replace(
            account,
            trade_allowed=True,
            trade_api_disabled=False,
            trade_expert=True,
        )
        price = _validate_request_against_market(
            request,
            validation_account,
            symbol,
            tick,
            self.policy,
            self._clock(),
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


class MetaTrader5ReadOnlyVerifier:
    """MT5 read/check facade for WebTrader execution.

    Unlike :class:`MetaTrader5Broker`, this object deliberately has no
    ``submit_market_order`` method.  It permits account discovery and
    ``order_check`` when MT5 external/Expert trading is disabled, while still
    requiring exact Demo allowlists and terminal connectivity.  The enclosing
    :class:`WebTraderBroker` adds the mandatory hedging gate.
    """

    def __init__(
        self,
        *,
        policy: DemoAccountPolicy,
        terminal_path: str | None = None,
        mt5_module: Any | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.policy = policy
        self._oracle = _MetaTrader5ReadOnlyOracle(
            policy=policy,
            terminal_path=terminal_path,
            mt5_module=mt5_module,
            clock=clock,
        )

    def __enter__(self) -> MetaTrader5ReadOnlyVerifier:
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.shutdown()

    def initialize(self) -> None:
        self._oracle.initialize()

    def shutdown(self) -> None:
        self._oracle.shutdown()

    def discover_account(self) -> AccountSnapshot:
        return self._oracle.discover_account()

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot:
        return self._oracle.discover_symbol(exact_symbol)

    def get_tick(self, exact_symbol: str) -> TickSnapshot:
        return self._oracle.get_tick(exact_symbol)

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]:
        before = self.discover_account()
        positions = self._oracle.list_open_positions(exact_symbol)
        after = self.discover_account()
        _require_stable_account(before, after, "open-position discovery")
        return positions

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]:
        before = self.discover_account()
        orders = self._oracle.list_pending_orders(exact_symbol)
        after = self.discover_account()
        _require_stable_account(before, after, "pending-order discovery")
        return orders

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        before = self.discover_account()
        result = self._oracle.check_market_order(request)
        after = self.discover_account()
        _require_stable_account(before, after, "order_check")
        return result


@dataclass(frozen=True, slots=True)
class _ReceiptEvidence:
    order_id: str | None
    deal_id: str | None
    position_id: str | None
    clicked_at_utc: datetime
    origin: str

    @property
    def has_position_correlation(self) -> bool:
        # PositionSnapshot has no deal-history linkage.  A deal-only receipt is
        # evidence that a click may have mutated broker state, but it is not
        # sufficient to claim an active position.
        return self.order_id is not None or self.position_id is not None


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


def _broker_id(value: object) -> str | None:
    if value in (None, 0, "", "0"):
        return None
    normalized = str(value).strip()
    return normalized if normalized and normalized != "0" else None


class WebTraderBroker:
    """A fail-closed :class:`BrokerAdapter` for XM WebTrader Demo execution.

    MT5 remains a read/check oracle.  WebTrader is the only mutation path.  The
    adapter serializes submission calls, remembers attempted request identities
    for its process lifetime, performs one browser commit call, and only emits
    ``ACCEPTED`` after bounded exact MT5 read-back.
    """

    def __init__(
        self,
        *,
        read_delegate: ReadOnlyBrokerDelegate,
        executor: WebTraderExecutor,
        policy: DemoAccountPolicy | None = None,
        readback_attempts: int = 5,
        readback_poll_seconds: float = 0.25,
        max_browser_drift_points: Decimal | int | str | None = None,
        require_hedging: bool = True,
        clock: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(require_hedging) is not bool:
            raise TypeError("require_hedging must be true or false")
        if not require_hedging:
            raise ValueError("require_hedging must remain true for WebTrader execution")
        if isinstance(readback_attempts, bool) or not isinstance(readback_attempts, int):
            raise TypeError("readback_attempts must be an integer")
        if readback_attempts < 1 or readback_attempts > 100:
            raise ValueError("readback_attempts must be between 1 and 100")
        if isinstance(readback_poll_seconds, bool):
            raise TypeError("readback_poll_seconds must be numeric")
        try:
            poll_seconds = float(readback_poll_seconds)
        except (TypeError, ValueError) as exc:
            raise TypeError("readback_poll_seconds must be numeric") from exc
        if not 0 <= poll_seconds <= 60:
            raise ValueError("readback_poll_seconds must be between 0 and 60")

        discovered_policy = policy or getattr(read_delegate, "policy", None)
        if not isinstance(discovered_policy, DemoAccountPolicy):
            raise ValueError("an explicit DemoAccountPolicy is required")
        drift = (
            None
            if max_browser_drift_points is None
            else _decimal(max_browser_drift_points, "max_browser_drift_points")
        )
        if drift is not None and drift < 0:
            raise ValueError("max_browser_drift_points cannot be negative")

        self.read_delegate = read_delegate
        self.executor = executor
        self.policy = discovered_policy
        self.readback_attempts = readback_attempts
        self.readback_poll_seconds = poll_seconds
        self.max_browser_drift_points = drift
        self.require_hedging = require_hedging
        self._clock = clock
        self._sleeper = sleeper
        self._lifecycle_lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._initialized = False
        self._attempted_requests: set[tuple[str, str, int]] = set()

    def __enter__(self) -> WebTraderBroker:
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.shutdown()

    def initialize(self) -> None:
        with self._lifecycle_lock:
            if self._initialized:
                return
            self.read_delegate.initialize()
            try:
                self.executor.initialize()
            except Exception:
                try:
                    self.read_delegate.shutdown()
                finally:
                    raise BrokerUnavailableError(
                        "WebTrader executor initialization failed"
                    ) from None
            self._initialized = True

    def shutdown(self) -> None:
        # Waiting for the submission lock is intentional: shutdown must not
        # cancel a possibly dispatched click before its bounded read-back ends.
        with self._execution_lock, self._lifecycle_lock:
            if not self._initialized:
                return
            executor_error: Exception | None = None
            delegate_error: Exception | None = None
            try:
                self.executor.shutdown()
            except Exception as exc:  # pragma: no cover - defensive integration path
                executor_error = exc
            try:
                self.read_delegate.shutdown()
            except Exception as exc:  # pragma: no cover - defensive integration path
                delegate_error = exc
            self._initialized = False
            if executor_error is not None or delegate_error is not None:
                raise BrokerUnavailableError(
                    "hybrid broker shutdown was incomplete"
                ) from None

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _validate_account(self, account: AccountSnapshot) -> AccountSnapshot:
        if not account.is_demo:
            raise LiveAccountRejected(
                "active broker account is not positively identified as Demo"
            )
        if account.login not in self.policy.allowed_demo_accounts:
            raise AccountNotAllowlisted("active Demo login is not exactly allowlisted")
        if account.server not in self.policy.allowed_servers:
            raise AccountNotAllowlisted("active broker server is not exactly allowlisted")
        if (
            self.policy.allowed_companies
            and account.company not in self.policy.allowed_companies
        ):
            raise AccountNotAllowlisted("active broker company is not exactly allowlisted")
        if not account.connected:
            raise BrokerUnavailableError("MT5 read delegate is not connected")
        if self.require_hedging and account.margin_mode != "RETAIL_HEDGING":
            raise BrokerSafetyError(
                "WebTrader execution requires exact RETAIL_HEDGING account mode"
            )
        return account

    def _account_for_request(self, request: MarketOrderRequest) -> AccountSnapshot:
        account = self._validate_account(self.read_delegate.discover_account())
        if account.login != request.account_id:
            raise AccountNotAllowlisted(
                "market-order request does not match the active Demo login"
            )
        return account

    def _inspect_web_identity(self, account: AccountSnapshot) -> WebTraderIdentity:
        identity = self.executor.inspect_identity(
            expected_login=account.login,
            expected_server=account.server,
        )
        if not bool(getattr(identity, "is_demo", False)):
            raise LiveAccountRejected(
                "WebTrader session is not positively identified as Demo"
            )
        if str(getattr(identity, "login", "")) != account.login:
            raise AccountNotAllowlisted(
                "WebTrader login does not exactly match the MT5 Demo login"
            )
        if str(getattr(identity, "server", "")) != account.server:
            raise AccountNotAllowlisted(
                "WebTrader server does not exactly match the MT5 Demo server"
            )
        return identity

    def _fresh_tick(self, symbol: SymbolSnapshot) -> TickSnapshot:
        tick = self.read_delegate.get_tick(symbol.symbol)
        if tick.symbol != symbol.symbol:
            raise SymbolNotAvailable("MT5 tick symbol does not match exactly")
        now = self._clock_utc()
        age = Decimal(str((now - tick.time_utc).total_seconds()))
        if age < Decimal("-1") or age > self.policy.max_tick_age_seconds:
            raise StaleTickError("MT5 read-back tick is outside the freshness policy")
        return tick

    def _clock_utc(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _validated_symbol(self, exact_symbol: str) -> SymbolSnapshot:
        if self.policy.allowed_symbols and exact_symbol not in self.policy.allowed_symbols:
            raise SymbolNotAvailable("broker symbol is not exactly allowlisted")
        symbol = self.read_delegate.discover_symbol(exact_symbol)
        if symbol.symbol != exact_symbol:
            raise SymbolNotAvailable("MT5 symbol identity does not match exactly")
        return symbol

    @staticmethod
    def _validated_check(
        request: MarketOrderRequest, result: BrokerResult
    ) -> BrokerResult:
        if result.stage != "order_check":
            raise BrokerSafetyError("MT5 check result has an unexpected stage")
        if result.request != request:
            raise BrokerSafetyError("MT5 order_check belongs to a different request")
        if result.volume != request.volume:
            raise BrokerSafetyError("MT5 order_check volume does not match the request")
        if result.outcome is BrokerOutcome.CHECK_PASSED and (
            type(result.retcode) is not int or result.retcode != 0
        ):
            raise BrokerSafetyError(
                "MT5 order_check success evidence has a nonzero or missing retcode"
            )
        return result

    @staticmethod
    def _request_key(request: MarketOrderRequest) -> tuple[str, str, int]:
        return (
            request.account_id,
            request.signal_id,
            request.leg_index,
        )

    def _receipt_evidence(
        self,
        receipt: WebTraderReceipt | None,
        *,
        expected_origin: str,
        commit_started_at_utc: datetime,
    ) -> _ReceiptEvidence | None:
        if receipt is None:
            return None
        clicked_at = getattr(receipt, "clicked_at_utc", None)
        if (
            not isinstance(clicked_at, datetime)
            or clicked_at.tzinfo is None
            or clicked_at.utcoffset() is None
        ):
            return None
        clicked_at = clicked_at.astimezone(UTC)
        origin = str(getattr(receipt, "origin", ""))
        now = self._clock_utc()
        if origin != expected_origin:
            return None
        if clicked_at < commit_started_at_utc - timedelta(seconds=5):
            return None
        if clicked_at > now + timedelta(seconds=1):
            return None
        evidence = _ReceiptEvidence(
            order_id=_broker_id(getattr(receipt, "order_id", None)),
            deal_id=_broker_id(getattr(receipt, "deal_id", None)),
            position_id=_broker_id(getattr(receipt, "position_id", None)),
            clicked_at_utc=clicked_at,
            origin=origin,
        )
        if not any((evidence.order_id, evidence.deal_id, evidence.position_id)):
            return None
        return evidence

    @staticmethod
    def _result_evidence(result: BrokerResult) -> _ReceiptEvidence | None:
        raw = result.raw_fields
        clicked_text = raw.get("webtrader_clicked_at_utc")
        if not isinstance(clicked_text, str):
            return None
        try:
            clicked_at = datetime.fromisoformat(clicked_text).astimezone(UTC)
        except (TypeError, ValueError):
            return None
        order_id = _broker_id(raw.get("webtrader_order_id"))
        position_id = _broker_id(raw.get("webtrader_position_id"))
        deal_id = _broker_id(raw.get("webtrader_deal_id")) or _broker_id(
            result.deal_id
        )
        if order_id is None and position_id is None:
            # Existing BrokerResult has no position-id field.  A persisted
            # position-only receipt may therefore occupy its order-id slot.
            order_id = _broker_id(result.order_id)
        evidence = _ReceiptEvidence(
            order_id=order_id,
            deal_id=deal_id,
            position_id=position_id,
            clicked_at_utc=clicked_at,
            origin=str(raw.get("webtrader_origin", "")),
        )
        if not any((evidence.order_id, evidence.deal_id, evidence.position_id)):
            return None
        return evidence

    @staticmethod
    def _matches_request_and_receipt(
        position: PositionSnapshot,
        request: MarketOrderRequest,
        receipt: _ReceiptEvidence,
        *,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> bool:
        position_id = _broker_id(position.position_id)
        identifier = _broker_id(position.identifier)
        has_correlatable_id = (
            receipt.position_id is not None or receipt.order_id is not None
        )
        receipt_position_matches = (
            receipt.position_id is None or receipt.position_id == position_id
        )
        receipt_order_matches = receipt.order_id is None or receipt.order_id in {
            position_id,
            identifier,
        }
        fill_within_drift = (
            abs(position.price_open - expected_quote) <= point * max_drift_points
        )
        fill_in_entry = (
            request.entry_low is None
            or request.entry_high is None
            or request.entry_low <= position.price_open <= request.entry_high
        )
        position_is_fresh = position.time_utc >= receipt.clicked_at_utc - timedelta(
            seconds=5
        )
        return (
            has_correlatable_id
            and receipt_position_matches
            and receipt_order_matches
            and position.account_id == request.account_id
            and position.symbol == request.symbol
            and position.side == request.side
            and position.volume == request.volume
            and position.stop_loss == request.stop_loss
            and position.take_profit == request.take_profit
            and fill_within_drift
            and fill_in_entry
            and position_is_fresh
        )

    def _bounded_read_back(
        self,
        request: MarketOrderRequest,
        receipt: _ReceiptEvidence,
        *,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> tuple[PositionSnapshot | None, bool]:
        """Return ``(position, ambiguous)`` using read-only MT5 polling."""

        if not receipt.has_position_correlation:
            return None, False
        for attempt in range(self.readback_attempts):
            try:
                self._account_for_request(request)
                positions = self.read_delegate.list_open_positions(request.symbol)
                matches = [
                    position
                    for position in positions
                    if self._matches_request_and_receipt(
                        position,
                        request,
                        receipt,
                        expected_quote=expected_quote,
                        point=point,
                        max_drift_points=max_drift_points,
                    )
                ]
            except Exception:
                matches = []
            if len(matches) == 1:
                return matches[0], False
            if len(matches) > 1:
                return None, True
            if attempt + 1 < self.readback_attempts:
                try:
                    self._sleeper(self.readback_poll_seconds)
                except Exception:
                    return None, False
        return None, False

    @staticmethod
    def _receipt_fields(receipt: _ReceiptEvidence | None) -> dict[str, str]:
        if receipt is None:
            return {}
        fields: dict[str, str] = {}
        for attribute, key in (
            ("order_id", "webtrader_order_id"),
            ("deal_id", "webtrader_deal_id"),
            ("position_id", "webtrader_position_id"),
        ):
            value = getattr(receipt, attribute)
            if value is not None:
                fields[key] = value
        fields["webtrader_clicked_at_utc"] = receipt.clicked_at_utc.isoformat()
        fields["webtrader_origin"] = receipt.origin
        return fields

    def _reconcile_required(
        self,
        request: MarketOrderRequest,
        *,
        receipt: _ReceiptEvidence | None = None,
        price: Decimal | None = None,
        reason: str,
    ) -> BrokerResult:
        try:
            fields = self._receipt_fields(receipt)
        except Exception:
            fields = {}
        position_id = fields.get("webtrader_position_id")
        return BrokerResult(
            outcome=BrokerOutcome.RECONCILE_REQUIRED,
            stage="webtrader_commit",
            retcode=None,
            comment=reason,
            request=request,
            price=price,
            volume=request.volume,
            # Persist a position-only receipt through the existing order-id
            # slot so restart reconciliation retains at least one exact ID.
            order_id=fields.get("webtrader_order_id") or position_id,
            deal_id=fields.get("webtrader_deal_id"),
            raw_fields=fields,
        )

    def discover_account(self) -> AccountSnapshot:
        self._ensure_initialized()
        return self._validate_account(self.read_delegate.discover_account())

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot:
        self._ensure_initialized()
        return self._validated_symbol(exact_symbol)

    def get_tick(self, exact_symbol: str) -> TickSnapshot:
        self._ensure_initialized()
        return self.read_delegate.get_tick(exact_symbol)

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]:
        """Return the delegate's raw position set without ownership filtering."""

        self._ensure_initialized()
        return self.read_delegate.list_open_positions(exact_symbol)

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]:
        self._ensure_initialized()
        return self.read_delegate.list_pending_orders(exact_symbol)

    def check_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        self._ensure_initialized()
        return self._validated_check(request, self.read_delegate.check_market_order(request))

    def submit_market_order(self, request: MarketOrderRequest) -> BrokerResult:
        """Prepare, click once, then prove exact MT5 ownership or fail closed."""

        with self._execution_lock:
            if request.magic != 0:
                raise BrokerSafetyError(
                    "WebTrader execution requires magic=0; browser orders cannot claim EA magic"
                )

            request_key = self._request_key(request)
            if request_key in self._attempted_requests:
                return self._reconcile_required(
                    request,
                    reason=(
                        "this WebTrader request was already attempted in the current process; "
                        "no retry is permitted"
                    ),
                )
            self._ensure_initialized()

            account = self._account_for_request(request)
            symbol = self._validated_symbol(request.symbol)
            self._fresh_tick(symbol)
            first_check = self._validated_check(
                request, self.read_delegate.check_market_order(request)
            )
            if first_check.outcome is not BrokerOutcome.CHECK_PASSED:
                return first_check
            self._inspect_web_identity(account)

            # From preparation onward a caller retry is unsafe unless durable
            # reconciliation proves that no browser action occurred.
            self._attempted_requests.add(request_key)
            try:
                token = self.executor.prepare_order(request)
            except Exception:
                return self._reconcile_required(
                    request,
                    price=first_check.price,
                    reason="WebTrader preparation produced no trustworthy receipt",
                )

            try:
                confirmed = self._account_for_request(request)
                if (
                    confirmed.login != account.login
                    or confirmed.server != account.server
                    or confirmed.company != account.company
                ):
                    raise AccountNotAllowlisted(
                        "active Demo account changed before the WebTrader commit"
                    )
                confirmed_symbol = self._validated_symbol(request.symbol)
                last_tick = self._fresh_tick(confirmed_symbol)
                final_check = self._validated_check(
                    request, self.read_delegate.check_market_order(request)
                )
                if final_check.outcome is not BrokerOutcome.CHECK_PASSED:
                    return final_check
                confirmed_identity = self._inspect_web_identity(confirmed)
                expected_quote = (
                    final_check.price
                    if final_check.price is not None
                    else (last_tick.ask if request.side == "BUY" else last_tick.bid)
                )
                max_drift = (
                    self.max_browser_drift_points
                    if self.max_browser_drift_points is not None
                    else Decimal(request.deviation_points)
                )
            except Exception:
                return self._reconcile_required(
                    request,
                    price=first_check.price,
                    reason="pre-commit Demo evidence changed or became unavailable",
                )

            receipt: WebTraderReceipt | None
            commit_started_at = self._clock_utc()
            try:
                # This is the sole mutation call.  Never wrap it in a retry.
                receipt = self.executor.commit_once(
                    token,
                    expected_quote=expected_quote,
                    point=confirmed_symbol.point,
                    max_drift_points=max_drift,
                )
            except Exception:
                receipt = None
            try:
                receipt_evidence = self._receipt_evidence(
                    receipt,
                    expected_origin=str(getattr(confirmed_identity, "origin", "")),
                    commit_started_at_utc=commit_started_at,
                )
            except Exception:
                receipt_evidence = None
            if receipt_evidence is None:
                return self._reconcile_required(
                    request,
                    price=expected_quote,
                    reason=(
                        "WebTrader commit returned no trustworthy exact receipt; "
                        "broker state must be reconciled"
                    ),
                )
            if not receipt_evidence.has_position_correlation:
                return self._reconcile_required(
                    request,
                    receipt=receipt_evidence,
                    price=expected_quote,
                    reason=(
                        "WebTrader receipt contains no position- or "
                        "order-correlatable ID; deal-only evidence requires "
                        "reconciliation"
                    ),
                )

            try:
                position, ambiguous = self._bounded_read_back(
                    request,
                    receipt_evidence,
                    expected_quote=expected_quote,
                    point=confirmed_symbol.point,
                    max_drift_points=max_drift,
                )
            except Exception:
                position, ambiguous = None, False
            if position is None:
                return self._reconcile_required(
                    request,
                    receipt=receipt_evidence,
                    price=expected_quote,
                    reason=(
                        "multiple MT5 positions match the WebTrader receipt; "
                        "reconciliation required"
                        if ambiguous
                        else "WebTrader receipt has no exact protected MT5 position read-back"
                    ),
                )

            try:
                fields = self._receipt_fields(receipt_evidence)
            except Exception:
                return self._reconcile_required(
                    request,
                    price=expected_quote,
                    reason="WebTrader receipt could not be represented safely",
                )
            fields["verified_position_id"] = position.position_id
            return BrokerResult(
                outcome=BrokerOutcome.ACCEPTED,
                stage="webtrader_commit",
                retcode=None,
                comment="WebTrader click verified by exact protected MT5 read-back",
                request=request,
                price=position.price_open,
                volume=position.volume,
                order_id=(
                    fields.get("webtrader_order_id")
                    or fields.get("webtrader_position_id")
                ),
                deal_id=fields.get("webtrader_deal_id"),
                raw_fields=fields,
            )

    def read_back_market_order(
        self, request: MarketOrderRequest, result: BrokerResult
    ) -> PositionSnapshot | None:
        self._ensure_initialized()
        if result.request is not None and result.request != request:
            raise BrokerSafetyError(
                "WebTrader result belongs to a different market-order request"
            )
        receipt_evidence = self._result_evidence(result)
        if receipt_evidence is None or result.price is None:
            return None
        symbol = self._validated_symbol(request.symbol)
        max_drift = (
            self.max_browser_drift_points
            if self.max_browser_drift_points is not None
            else Decimal(request.deviation_points)
        )
        position, ambiguous = self._bounded_read_back(
            request,
            receipt_evidence,
            expected_quote=result.price,
            point=symbol.point,
            max_drift_points=max_drift,
        )
        if ambiguous:
            raise BrokerSafetyError(
                "multiple MT5 positions match one WebTrader browser receipt"
            )
        return position


# A descriptive alias keeps the integration surface clear without duplicating
# behavior.  Both names structurally implement BrokerAdapter.
HybridWebTraderBroker = WebTraderBroker


__all__ = [
    "HybridWebTraderBroker",
    "MetaTrader5ReadOnlyVerifier",
    "ReadOnlyBrokerDelegate",
    "WebTraderBroker",
    "WebTraderExecutor",
    "WebTraderIdentity",
    "WebTraderReceipt",
]
