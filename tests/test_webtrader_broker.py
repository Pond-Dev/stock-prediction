from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tgxm.broker import (
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
    SymbolSnapshot,
    TickSnapshot,
)
from tgxm.webtrader_broker import (
    MetaTrader5ReadOnlyVerifier,
    ReadOnlyBrokerDelegate,
    WebTraderBroker,
    WebTraderExecutor,
)


NOW = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)


def policy() -> DemoAccountPolicy:
    return DemoAccountPolicy(
        allowed_demo_accounts=frozenset({"10001"}),
        allowed_servers=frozenset({"XM-Demo"}),
        allowed_companies=frozenset({"XM"}),
        allowed_symbols=frozenset({"GOLD"}),
        max_tick_age_seconds=Decimal("5"),
    )


def account(**changes: Any) -> AccountSnapshot:
    values: dict[str, Any] = {
        "login": "10001",
        "server": "XM-Demo",
        "company": "XM",
        "is_demo": True,
        "connected": True,
        "trade_allowed": True,
        "trade_api_disabled": False,
        "trade_expert": True,
        "currency": "USD",
        "margin_mode": "RETAIL_HEDGING",
    }
    values.update(changes)
    return AccountSnapshot(**values)


def symbol() -> SymbolSnapshot:
    return SymbolSnapshot(
        symbol="GOLD",
        visible=True,
        trade_mode="FULL",
        digits=2,
        point=Decimal("0.01"),
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )


def tick() -> TickSnapshot:
    return TickSnapshot(
        symbol="GOLD",
        bid=Decimal("4604"),
        ask=Decimal("4605"),
        time_utc=NOW,
    )


def request(**changes: Any) -> MarketOrderRequest:
    values: dict[str, Any] = {
        "account_id": "10001",
        "signal_id": "signal-1",
        "leg_index": 0,
        "symbol": "GOLD",
        "side": "SELL",
        "volume": Decimal("0.01"),
        "stop_loss": Decimal("4618"),
        "take_profit": Decimal("4595"),
        "client_reference": "tgxm-web-1",
        "magic": 0,
        "deviation_points": 20,
    }
    values.update(changes)
    return MarketOrderRequest(**values)


def matching_position(
    *,
    position_id: str = "position-7001",
    identifier: str | None = "order-7001",
    magic: int = 0,
    comment: str = "",
    **changes: Any,
) -> PositionSnapshot:
    values: dict[str, Any] = {
        "account_id": "10001",
        "position_id": position_id,
        "identifier": identifier,
        "symbol": "GOLD",
        "side": "SELL",
        "volume": Decimal("0.01"),
        "price_open": Decimal("4604"),
        "stop_loss": Decimal("4618"),
        "take_profit": Decimal("4595"),
        "magic": magic,
        "comment": comment,
        "time_utc": NOW,
    }
    values.update(changes)
    return PositionSnapshot(**values)


class FakeReadDelegate:
    def __init__(
        self,
        *,
        accounts: tuple[AccountSnapshot, ...] = (account(),),
        positions_by_read: tuple[tuple[PositionSnapshot, ...], ...] = (
            (matching_position(),),
        ),
        checks: tuple[BrokerResult, ...] = (),
    ) -> None:
        self.policy = policy()
        self.accounts = list(accounts)
        self.positions_by_read = list(positions_by_read)
        self.checks = list(checks)
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.account_calls = 0
        self.symbol_calls: list[str] = []
        self.tick_calls: list[str] = []
        self.position_calls: list[str | None] = []
        self.pending_calls: list[str | None] = []
        self.check_calls: list[MarketOrderRequest] = []
        self.submit_calls: list[MarketOrderRequest] = []
        self.pending: tuple[PendingOrderSnapshot, ...] = ()

    def initialize(self) -> None:
        self.initialize_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def discover_account(self) -> AccountSnapshot:
        index = min(self.account_calls, len(self.accounts) - 1)
        self.account_calls += 1
        return self.accounts[index]

    def discover_symbol(self, exact_symbol: str) -> SymbolSnapshot:
        self.symbol_calls.append(exact_symbol)
        return symbol()

    def get_tick(self, exact_symbol: str) -> TickSnapshot:
        self.tick_calls.append(exact_symbol)
        return tick()

    def list_open_positions(
        self, exact_symbol: str | None = None
    ) -> tuple[PositionSnapshot, ...]:
        self.position_calls.append(exact_symbol)
        index = min(len(self.position_calls) - 1, len(self.positions_by_read) - 1)
        return self.positions_by_read[index]

    def list_pending_orders(
        self, exact_symbol: str | None = None
    ) -> tuple[PendingOrderSnapshot, ...]:
        self.pending_calls.append(exact_symbol)
        return self.pending

    def check_market_order(self, value: MarketOrderRequest) -> BrokerResult:
        self.check_calls.append(value)
        if self.checks:
            index = min(len(self.check_calls) - 1, len(self.checks) - 1)
            return replace(self.checks[index], request=value)
        return BrokerResult(
            outcome=BrokerOutcome.CHECK_PASSED,
            stage="order_check",
            retcode=0,
            comment="passed",
            request=value,
            price=Decimal("4604"),
            volume=value.volume,
        )

    def submit_market_order(self, value: MarketOrderRequest) -> BrokerResult:
        self.submit_calls.append(value)
        raise AssertionError("hybrid wrapper must never call delegate order_send")


@dataclass(frozen=True, slots=True)
class Identity:
    login: str = "10001"
    server: str = "XM-Demo"
    is_demo: bool = True
    origin: str = "https://my.xm.com"


@dataclass(frozen=True, slots=True)
class Receipt:
    order_id: str | None = "order-7001"
    deal_id: str | None = None
    position_id: str | None = None
    clicked_at_utc: datetime = NOW
    origin: str = "https://my.xm.com"


class FakeExecutor:
    def __init__(
        self,
        *,
        identities: tuple[Identity, ...] = (Identity(),),
        receipt: object | None = Receipt(),
        prepare_error: Exception | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.identities = list(identities)
        self.receipt = receipt
        self.prepare_error = prepare_error
        self.commit_error = commit_error
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.identity_calls: list[tuple[str, str]] = []
        self.prepared: list[MarketOrderRequest] = []
        self.commits: list[tuple[object, Decimal, Decimal, Decimal]] = []
        self.token = object()

    def initialize(self) -> None:
        self.initialize_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def inspect_identity(
        self, *, expected_login: str, expected_server: str
    ) -> Identity:
        self.identity_calls.append((expected_login, expected_server))
        index = min(len(self.identity_calls) - 1, len(self.identities) - 1)
        return self.identities[index]

    def prepare_order(self, value: MarketOrderRequest) -> object:
        self.prepared.append(value)
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.token

    def commit_once(
        self,
        token: object,
        *,
        expected_quote: Decimal,
        point: Decimal,
        max_drift_points: Decimal,
    ) -> object | None:
        self.commits.append((token, expected_quote, point, max_drift_points))
        if self.commit_error is not None:
            raise self.commit_error
        return self.receipt


class DisabledMutationMT5:
    """Small MT5 stub whose Python mutation permissions are all disabled."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_MARGIN_MODE_EXCHANGE = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_LONGONLY = 1
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_CLOSEONLY = 3
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_TRADE_EXECUTION_INSTANT = 1
    SYMBOL_TRADE_EXECUTION_MARKET = 2
    SYMBOL_ORDER_MARKET = 1
    SYMBOL_ORDER_SL = 16
    SYMBOL_ORDER_TP = 32
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0

    def __init__(self) -> None:
        self.connected = True
        self.account_login = 10001
        self.account_server = "XM-Demo"
        self.account_company = "XM"
        self.account_trade_mode = self.ACCOUNT_TRADE_MODE_DEMO
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.order_check_calls: list[dict[str, object]] = []
        self.order_send_calls: list[dict[str, object]] = []

    def initialize(self, path: str | None = None) -> bool:
        self.initialize_calls += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            connected=self.connected,
            trade_allowed=False,
            tradeapi_disabled=True,
        )

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            login=self.account_login,
            server=self.account_server,
            company=self.account_company,
            trade_mode=self.account_trade_mode,
            trade_allowed=False,
            trade_expert=False,
            currency="USD",
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        )

    def symbol_info(self, name: str) -> SimpleNamespace | None:
        if name != "GOLD":
            return None
        return SimpleNamespace(
            name="GOLD",
            visible=True,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_stops_level=0,
            trade_freeze_level=0,
            filling_mode=self.SYMBOL_FILLING_FOK,
            trade_exemode=self.SYMBOL_TRADE_EXECUTION_INSTANT,
            order_mode=(
                self.SYMBOL_ORDER_MARKET
                | self.SYMBOL_ORDER_SL
                | self.SYMBOL_ORDER_TP
            ),
        )

    def symbol_info_tick(self, name: str) -> SimpleNamespace | None:
        if name != "GOLD":
            return None
        return SimpleNamespace(
            bid=4604.0,
            ask=4605.0,
            time_msc=int(NOW.timestamp() * 1000),
        )

    def order_check(self, payload: dict[str, object]) -> SimpleNamespace:
        self.order_check_calls.append(payload)
        return SimpleNamespace(
            retcode=0,
            comment="check passed",
            price=payload.get("price", 4604.0),
            volume=payload["volume"],
            order=0,
            deal=0,
        )

    def order_send(self, payload: dict[str, object]) -> SimpleNamespace:
        self.order_send_calls.append(payload)
        raise AssertionError("read-only verifier must never call order_send")

    def positions_get(
        self, *, symbol: str | None = None
    ) -> tuple[SimpleNamespace, ...]:
        position = SimpleNamespace(
            ticket="position-7001",
            identifier="order-7001",
            symbol="GOLD",
            type=self.POSITION_TYPE_SELL,
            volume=0.01,
            price_open=4604.0,
            sl=4618.0,
            tp=4595.0,
            magic=0,
            comment="browser order",
            time_msc=int(NOW.timestamp() * 1000),
        )
        if symbol is not None and symbol != "GOLD":
            return ()
        return (position,)


def hybrid(
    delegate: FakeReadDelegate | None = None,
    executor: FakeExecutor | None = None,
    **changes: Any,
) -> WebTraderBroker:
    return WebTraderBroker(
        read_delegate=delegate or FakeReadDelegate(),
        executor=executor or FakeExecutor(),
        policy=policy(),
        readback_attempts=changes.pop("readback_attempts", 2),
        readback_poll_seconds=changes.pop("readback_poll_seconds", 0),
        clock=lambda: NOW,
        sleeper=changes.pop("sleeper", lambda _: None),
        **changes,
    )


def test_mt5_read_only_verifier_ignores_only_python_mutation_permissions() -> None:
    module = DisabledMutationMT5()
    strict = MetaTrader5Broker(
        policy=policy(), mt5_module=module, clock=lambda: NOW
    )

    with pytest.raises(BrokerSafetyError, match="external trading"):
        strict.discover_account()
    strict.shutdown()

    verifier = MetaTrader5ReadOnlyVerifier(
        policy=policy(), mt5_module=module, clock=lambda: NOW
    )
    discovered = verifier.discover_account()
    checked = verifier.check_market_order(request())

    assert isinstance(verifier, ReadOnlyBrokerDelegate)
    assert not hasattr(verifier, "submit_market_order")
    assert discovered.login == "10001"
    assert discovered.is_demo is True
    assert discovered.connected is True
    assert discovered.trade_allowed is False
    assert discovered.trade_api_disabled is True
    assert discovered.trade_expert is False
    assert discovered.margin_mode == "RETAIL_HEDGING"
    assert checked.outcome is BrokerOutcome.CHECK_PASSED
    assert checked.retcode == 0
    assert checked.request == request()
    assert checked.volume == request().volume
    assert len(module.order_check_calls) == 1
    assert module.order_send_calls == []
    verifier.shutdown()


def test_webtrader_can_use_read_only_verifier_without_mt5_send_permission() -> None:
    module = DisabledMutationMT5()
    verifier = MetaTrader5ReadOnlyVerifier(
        policy=policy(), mt5_module=module, clock=lambda: NOW
    )
    executor = FakeExecutor()
    broker = WebTraderBroker(
        read_delegate=verifier,
        executor=executor,
        policy=policy(),
        readback_attempts=1,
        readback_poll_seconds=0,
        require_hedging=True,
        clock=lambda: NOW,
    )

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert len(module.order_check_calls) == 2
    assert module.order_send_calls == []
    assert len(executor.commits) == 1
    broker.shutdown()


@pytest.mark.parametrize(
    "changes, error_type",
    [
        (
            {"account_trade_mode": DisabledMutationMT5.ACCOUNT_TRADE_MODE_REAL},
            LiveAccountRejected,
        ),
        ({"account_login": 99999}, BrokerSafetyError),
        ({"account_server": "XM-Live"}, BrokerSafetyError),
        ({"account_company": "Other"}, BrokerSafetyError),
        ({"connected": False}, BrokerUnavailableError),
    ],
)
def test_mt5_read_only_verifier_keeps_identity_and_connectivity_gates(
    changes: dict[str, object], error_type: type[Exception]
) -> None:
    module = DisabledMutationMT5()
    for name, value in changes.items():
        setattr(module, name, value)
    verifier = MetaTrader5ReadOnlyVerifier(
        policy=policy(), mt5_module=module, clock=lambda: NOW
    )

    with pytest.raises(error_type):
        verifier.discover_account()

    assert module.order_check_calls == []
    assert module.order_send_calls == []
    verifier.shutdown()


def test_protocol_lifecycle_and_raw_read_delegation() -> None:
    manual = matching_position(
        position_id="manual-1", identifier=None, magic=991, comment="manual"
    )
    delegate = FakeReadDelegate(positions_by_read=((manual,),))
    executor = FakeExecutor()
    broker = hybrid(delegate, executor)

    assert isinstance(broker, BrokerAdapter)
    assert isinstance(executor, WebTraderExecutor)
    broker.initialize()
    broker.initialize()
    assert broker.discover_account() == account()
    assert broker.discover_symbol("GOLD") == symbol()
    assert broker.get_tick("GOLD") == tick()
    assert broker.check_market_order(request()).outcome is BrokerOutcome.CHECK_PASSED
    assert broker.list_open_positions("GOLD") == (manual,)
    assert broker.list_pending_orders("GOLD") == ()
    broker.shutdown()
    broker.shutdown()

    assert delegate.initialize_calls == 1
    assert delegate.shutdown_calls == 1
    assert executor.initialize_calls == 1
    assert executor.shutdown_calls == 1
    assert delegate.position_calls == ["GOLD"]
    assert delegate.pending_calls == ["GOLD"]
    assert delegate.check_calls == [request()]
    assert delegate.submit_calls == []


def test_submit_uses_two_fresh_checks_one_click_and_zero_delegate_sends() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor()
    broker = hybrid(delegate, executor)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert result.order_id == "order-7001"
    assert len(delegate.check_calls) == 2
    assert delegate.tick_calls == ["GOLD", "GOLD"]
    assert delegate.submit_calls == []
    assert executor.prepared == [request()]
    assert executor.identity_calls == [("10001", "XM-Demo"), ("10001", "XM-Demo")]
    assert len(executor.commits) == 1
    token, quote, point, drift = executor.commits[0]
    assert token is executor.token
    assert quote == Decimal("4604")
    assert point == Decimal("0.01")
    assert drift == Decimal("20")
    # The matching position has an empty comment and no EA ownership claim.
    assert broker.read_back_market_order(request(), result) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": "order_send"},
        {"retcode": 10016},
        {"retcode": None},
        {"request": None},
        {"request": request(signal_id="different-signal")},
        {"volume": None},
        {"volume": Decimal("0.02")},
    ],
)
def test_inconsistent_successful_order_check_never_reaches_browser(
    changes: dict[str, Any],
) -> None:
    exact = BrokerResult(
        outcome=BrokerOutcome.CHECK_PASSED,
        stage="order_check",
        retcode=0,
        comment="passed",
        request=request(),
        price=Decimal("4604"),
        volume=Decimal("0.01"),
    )

    class InconsistentCheckDelegate(FakeReadDelegate):
        def check_market_order(self, value: MarketOrderRequest) -> BrokerResult:
            self.check_calls.append(value)
            return replace(exact, **changes)

    delegate = InconsistentCheckDelegate()
    executor = FakeExecutor()

    with pytest.raises(BrokerSafetyError):
        hybrid(delegate, executor).submit_market_order(request())

    assert executor.prepared == []
    assert executor.commits == []
    assert delegate.submit_calls == []


def test_position_only_receipt_is_preserved_and_matches_position_ticket() -> None:
    delegate = FakeReadDelegate(
        positions_by_read=((matching_position(identifier=None),),)
    )
    executor = FakeExecutor(
        receipt=Receipt(order_id=None, position_id="position-7001")
    )
    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert result.order_id == "position-7001"
    assert result.raw_fields["webtrader_position_id"] == "position-7001"
    assert delegate.submit_calls == []
    assert len(executor.commits) == 1


def test_nonzero_magic_is_rejected_before_prepare_check_or_click() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor()
    broker = hybrid(delegate, executor)

    with pytest.raises(BrokerSafetyError, match="magic=0"):
        broker.submit_market_order(request(magic=26082701))

    assert delegate.check_calls == []
    assert delegate.submit_calls == []
    assert delegate.initialize_calls == 0
    assert executor.initialize_calls == 0
    assert executor.prepared == []
    assert executor.commits == []


@pytest.mark.parametrize(
    "unsafe_account",
    [
        account(is_demo=False),
        account(login="99999"),
        account(server="XM-Live"),
        account(company="Other"),
        account(margin_mode="RETAIL_NETTING"),
        account(margin_mode="UNKNOWN"),
    ],
)
def test_exact_demo_allowlist_is_checked_before_browser_use(
    unsafe_account: AccountSnapshot,
) -> None:
    delegate = FakeReadDelegate(accounts=(unsafe_account,))
    executor = FakeExecutor()

    with pytest.raises((LiveAccountRejected, BrokerSafetyError)):
        hybrid(delegate, executor).submit_market_order(request())

    assert delegate.check_calls == []
    assert delegate.submit_calls == []
    assert executor.prepared == []
    assert executor.commits == []


def test_hedging_gate_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="must remain true"):
        hybrid(require_hedging=False)


def test_webtrader_identity_is_rechecked_and_switch_blocks_commit() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(
        identities=(Identity(), Identity(login="99999")),
    )
    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(delegate.check_calls) == 2
    assert executor.prepared == [request()]
    assert executor.commits == []
    assert delegate.submit_calls == []


def test_mt5_account_is_rechecked_and_switch_blocks_commit() -> None:
    delegate = FakeReadDelegate(
        accounts=(account(), account(login="99999")),
    )
    executor = FakeExecutor()
    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(delegate.check_calls) == 1
    assert executor.prepared == [request()]
    assert executor.commits == []
    assert delegate.submit_calls == []


def test_failing_final_check_after_prepare_never_clicks() -> None:
    passed = BrokerResult(
        outcome=BrokerOutcome.CHECK_PASSED,
        stage="order_check",
        retcode=0,
        comment="passed",
        price=Decimal("4604"),
        volume=Decimal("0.01"),
    )
    rejected = replace(
        passed,
        outcome=BrokerOutcome.REJECTED,
        retcode=10016,
        comment="invalid stops",
    )
    delegate = FakeReadDelegate(checks=(passed, rejected))
    executor = FakeExecutor()

    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.REJECTED
    assert len(delegate.check_calls) == 2
    assert executor.prepared == [request()]
    assert executor.commits == []
    assert delegate.submit_calls == []


def test_inconsistent_successful_final_check_after_prepare_never_clicks() -> None:
    passed = BrokerResult(
        outcome=BrokerOutcome.CHECK_PASSED,
        stage="order_check",
        retcode=0,
        comment="passed",
        price=Decimal("4604"),
        volume=Decimal("0.01"),
    )
    impossible_success = replace(passed, retcode=10016)
    delegate = FakeReadDelegate(checks=(passed, impossible_success))
    executor = FakeExecutor()

    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(delegate.check_calls) == 2
    assert executor.prepared == [request()]
    assert executor.commits == []
    assert delegate.submit_calls == []


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        Receipt(order_id=None, deal_id=None, position_id=None),
    ],
)
def test_missing_browser_receipt_is_ambiguous_and_never_retried(
    receipt: Receipt | None,
) -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(receipt=receipt)
    broker = hybrid(delegate, executor)

    first = broker.submit_market_order(request())
    second = broker.submit_market_order(request())

    assert first.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert second.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert delegate.submit_calls == []


def test_same_durable_intent_with_changed_reference_cannot_click_again() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor()
    broker = hybrid(delegate, executor)

    first = broker.submit_market_order(request(client_reference="first-reference"))
    second = broker.submit_market_order(request(client_reference="changed-reference"))

    assert first.outcome is BrokerOutcome.ACCEPTED
    assert second.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert delegate.submit_calls == []


def test_commit_exception_is_ambiguous_and_not_retried() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(commit_error=TimeoutError("browser timeout"))
    broker = hybrid(delegate, executor)

    first = broker.submit_market_order(request())
    second = broker.submit_market_order(request())

    assert first.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert second.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert "browser timeout" not in first.comment
    assert delegate.submit_calls == []


def test_receipt_origin_mismatch_is_ambiguous_after_one_click() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(
        receipt=Receipt(origin="https://unexpected.example"),
    )
    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert delegate.position_calls == []
    assert delegate.submit_calls == []


def test_malformed_receipt_property_after_click_is_reconcile_required() -> None:
    class MalformedReceipt:
        order_id = "order-7001"
        deal_id = None
        position_id = None
        clicked_at_utc = NOW

        @property
        def origin(self) -> str:
            raise RuntimeError("private browser state")

    delegate = FakeReadDelegate()
    executor = FakeExecutor(receipt=MalformedReceipt())

    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert "private browser state" not in result.comment
    assert delegate.submit_calls == []


def test_stale_receipt_or_position_cannot_claim_a_previous_trade() -> None:
    stale_receipt = Receipt(clicked_at_utc=NOW - timedelta(seconds=10))
    first = hybrid(
        FakeReadDelegate(), FakeExecutor(receipt=stale_receipt)
    ).submit_market_order(request())

    stale_position = matching_position(time_utc=NOW - timedelta(seconds=10))
    second = hybrid(
        FakeReadDelegate(positions_by_read=((stale_position,),)),
        FakeExecutor(),
        readback_attempts=1,
    ).submit_market_order(request())

    assert first.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert second.outcome is BrokerOutcome.RECONCILE_REQUIRED


def test_prepare_failure_is_fail_closed_and_never_clicks() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(prepare_error=RuntimeError("private browser detail"))
    broker = hybrid(delegate, executor)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert executor.prepared == [request()]
    assert executor.commits == []
    assert "private browser detail" not in result.comment
    assert delegate.submit_calls == []


def test_bounded_readback_can_observe_delayed_exact_position() -> None:
    sleeps: list[float] = []
    delegate = FakeReadDelegate(
        positions_by_read=((), (), (matching_position(),)),
    )
    executor = FakeExecutor()
    broker = hybrid(
        delegate,
        executor,
        readback_attempts=3,
        readback_poll_seconds=0.01,
        sleeper=sleeps.append,
    )

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert len(executor.commits) == 1
    assert delegate.position_calls == ["GOLD", "GOLD", "GOLD"]
    assert sleeps == [0.01, 0.01]
    assert delegate.submit_calls == []


def test_zero_or_ambiguous_exact_readback_requires_reconciliation() -> None:
    duplicate_identifier = matching_position(
        position_id="position-7002", identifier="order-7001"
    )
    for positions in (
        (),
        (matching_position(stop_loss=Decimal("0")),),
        (matching_position(), duplicate_identifier),
    ):
        delegate = FakeReadDelegate(positions_by_read=(positions,))
        executor = FakeExecutor()

        result = hybrid(delegate, executor, readback_attempts=1).submit_market_order(
            request()
        )

        assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
        assert len(executor.commits) == 1
        assert delegate.submit_calls == []


def test_deal_only_or_cross_typed_receipt_cannot_claim_a_position() -> None:
    deal_only = FakeExecutor(
        receipt=Receipt(order_id=None, deal_id="order-7001"),
    )
    first = hybrid(
        FakeReadDelegate(), deal_only, readback_attempts=1
    ).submit_market_order(request())

    cross_typed = FakeExecutor(
        receipt=Receipt(order_id=None, position_id="order-7001"),
    )
    second = hybrid(
        FakeReadDelegate(), cross_typed, readback_attempts=1
    ).submit_market_order(request())

    assert first.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert second.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert "deal-only evidence" in first.comment
    assert "multiple MT5 positions" not in first.comment
    assert len(deal_only.commits) == 1
    assert len(cross_typed.commits) == 1


@pytest.mark.parametrize(
    "receipt",
    [
        Receipt(order_id="order-7001", position_id="wrong-position"),
        Receipt(order_id="wrong-order", position_id="position-7001"),
    ],
)
def test_every_provided_correlatable_receipt_id_must_match(
    receipt: Receipt,
) -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(receipt=receipt)

    result = hybrid(delegate, executor, readback_attempts=1).submit_market_order(
        request()
    )

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert delegate.submit_calls == []


def test_matching_order_and_position_receipt_ids_can_claim_one_position() -> None:
    delegate = FakeReadDelegate()
    executor = FakeExecutor(
        receipt=Receipt(order_id="order-7001", position_id="position-7001")
    )

    result = hybrid(delegate, executor).submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert len(executor.commits) == 1
    assert delegate.submit_calls == []


@pytest.mark.parametrize(
    "mismatch",
    [
        {"account_id": "other-account"},
        {"symbol": "EURUSD"},
        {"side": "BUY"},
        {"volume": Decimal("0.02")},
        {"stop_loss": Decimal("4617")},
        {"take_profit": Decimal("4594")},
        {"price_open": Decimal("9999")},
        {"time_utc": NOW - timedelta(seconds=10)},
        {"identifier": "different-order"},
    ],
)
def test_every_exact_ownership_field_is_required(mismatch: dict[str, Any]) -> None:
    values = dict(mismatch)
    identifier = values.pop("identifier", "order-7001")
    mismatched = matching_position(identifier=identifier, **values)
    delegate = FakeReadDelegate(positions_by_read=((mismatched,),))
    executor = FakeExecutor()

    result = hybrid(delegate, executor, readback_attempts=1).submit_market_order(
        request()
    )

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(executor.commits) == 1
    assert delegate.submit_calls == []


def test_readback_requires_receipt_id_and_all_trade_fields_but_not_magic_comment() -> None:
    exact = matching_position(magic=987654, comment="not-the-client-reference")
    delegate = FakeReadDelegate(positions_by_read=((exact,),))
    broker = hybrid(delegate, FakeExecutor())
    matching_result = BrokerResult(
        outcome=BrokerOutcome.ACCEPTED,
        stage="webtrader_commit",
        retcode=None,
        comment="verified",
        request=request(),
        price=Decimal("4604"),
        order_id="order-7001",
        volume=Decimal("0.01"),
        raw_fields={
            "webtrader_order_id": "order-7001",
            "webtrader_clicked_at_utc": NOW.isoformat(),
            "webtrader_origin": "https://my.xm.com",
        },
    )

    assert broker.read_back_market_order(request(), matching_result) == exact
    assert broker.read_back_market_order(
        request(),
        replace(
            matching_result,
            order_id="wrong",
            raw_fields={
                "webtrader_order_id": "wrong",
                "webtrader_clicked_at_utc": NOW.isoformat(),
                "webtrader_origin": "https://my.xm.com",
            },
        ),
    ) is None
    assert broker.read_back_market_order(
        request(),
        replace(
            matching_result,
            order_id=None,
            raw_fields={
                "webtrader_clicked_at_utc": NOW.isoformat(),
                "webtrader_origin": "https://my.xm.com",
            },
        ),
    ) is None
