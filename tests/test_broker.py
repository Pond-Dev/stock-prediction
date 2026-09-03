from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tgxm.broker import (
    detect_server_utc_offset_minutes,
    AccountNotAllowlisted,
    AccountSnapshot,
    BrokerAdapter,
    BrokerOutcome,
    BrokerResult,
    BrokerSafetyError,
    BrokerUnavailableError,
    DemoAccountPolicy,
    FakeBroker,
    LiveAccountRejected,
    MarketOrderRequest,
    MetaTrader5Broker,
    PendingOrderSnapshot,
    PositionSnapshot,
    StaleTickError,
    SymbolSnapshot,
    TickSnapshot,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 27, 2, 0, 0, tzinfo=UTC)


def policy(**changes: object) -> DemoAccountPolicy:
    values: dict[str, object] = {
        "allowed_demo_accounts": frozenset({"10001"}),
        "allowed_servers": frozenset({"XM-Demo 1"}),
        "allowed_companies": frozenset({"XM Test"}),
        "allowed_symbols": frozenset({"GOLD"}),
        "max_tick_age_seconds": Decimal("5"),
    }
    values.update(changes)
    return DemoAccountPolicy(**values)  # type: ignore[arg-type]


def account(**changes: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "login": "10001",
        "server": "XM-Demo 1",
        "company": "XM Test",
        "is_demo": True,
        "connected": True,
        "trade_allowed": True,
        "trade_api_disabled": False,
        "currency": "USD",
        "margin_mode": "HEDGING",
    }
    values.update(changes)
    return AccountSnapshot(**values)  # type: ignore[arg-type]


def symbol(**changes: object) -> SymbolSnapshot:
    values: dict[str, object] = {
        "symbol": "GOLD",
        "visible": True,
        "trade_mode": "FULL",
        "digits": 2,
        "point": Decimal("0.01"),
        "tick_size": Decimal("0.01"),
        "tick_value": Decimal("1"),
        "contract_size": Decimal("100"),
        "volume_min": Decimal("0.01"),
        "volume_max": Decimal("50"),
        "volume_step": Decimal("0.01"),
        "stops_level_points": 10,
        "freeze_level_points": 0,
        "filling_flags": 1,
        "execution_mode": 2,
    }
    values.update(changes)
    return SymbolSnapshot(**values)  # type: ignore[arg-type]


def tick(**changes: object) -> TickSnapshot:
    values: dict[str, object] = {
        "symbol": "GOLD",
        "bid": Decimal("4601.00"),
        "ask": Decimal("4601.59"),
        "time_utc": NOW,
    }
    values.update(changes)
    return TickSnapshot(**values)  # type: ignore[arg-type]


def request(**changes: object) -> MarketOrderRequest:
    values: dict[str, object] = {
        "account_id": "10001",
        "signal_id": "sig-redacted-77",
        "leg_index": 0,
        "symbol": "GOLD",
        "side": "SELL",
        "volume": Decimal("0.01"),
        "stop_loss": Decimal("4618.00"),
        "take_profit": Decimal("4595.00"),
        "client_reference": "tgxm-deadbeef-0",
        "magic": 17001,
        "deviation_points": 10,
    }
    values.update(changes)
    return MarketOrderRequest(**values)  # type: ignore[arg-type]


def position(**changes: object) -> PositionSnapshot:
    values: dict[str, object] = {
        "account_id": "10001",
        "position_id": "7001",
        "identifier": "7001",
        "symbol": "GOLD",
        "side": "SELL",
        "volume": Decimal("0.01"),
        "price_open": Decimal("4601.00"),
        "stop_loss": Decimal("4618.00"),
        "take_profit": Decimal("4595.00"),
        "magic": 17001,
        "comment": "tgxm-deadbeef-0",
        "time_utc": NOW,
    }
    values.update(changes)
    return PositionSnapshot(**values)  # type: ignore[arg-type]


def pending_order(**changes: object) -> PendingOrderSnapshot:
    values: dict[str, object] = {
        "account_id": "10001",
        "order_id": "8001",
        "symbol": "GOLD",
        "side": "BUY",
        "volume": Decimal("0.02"),
        "magic": 0,
        "comment": "manual-pending",
        "time_utc": NOW,
    }
    values.update(changes)
    return PendingOrderSnapshot(**values)  # type: ignore[arg-type]


def fake_broker(**changes: object) -> FakeBroker:
    values: dict[str, object] = {
        "policy": policy(),
        "account": account(),
        "symbols": {"GOLD": symbol()},
        "ticks": {"GOLD": tick()},
        "clock": lambda: NOW,
    }
    values.update(changes)
    return FakeBroker(**values)  # type: ignore[arg-type]


def test_fake_broker_is_protocol_compatible_and_sends_once() -> None:
    broker = fake_broker()
    assert isinstance(broker, BrokerAdapter)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert result.order_id == "1001"
    assert len(broker.checked_requests) == 1
    assert len(broker.sent_requests) == 1
    read_back = broker.read_back_market_order(request(), result)
    assert read_back is not None
    assert read_back.position_id == "1001"
    assert read_back.has_numeric_stop_loss is True


def test_fake_lists_manual_exposure_and_read_back_fails_on_protection_mismatch() -> None:
    manual = position(
        position_id="manual-1",
        identifier="manual-1",
        magic=0,
        comment="manual",
    )
    broker = fake_broker(positions=[manual])
    assert broker.list_open_positions("GOLD") == (manual,)

    accepted = BrokerResult(
        outcome=BrokerOutcome.ACCEPTED,
        stage="order_send",
        retcode=10009,
        comment="accepted",
        request=request(),
        price=Decimal("4601"),
        volume=Decimal("0.01"),
        order_id="7001",
    )
    broker.positions.append(position(stop_loss=None))
    with pytest.raises(BrokerSafetyError, match="stop_loss"):
        broker.read_back_market_order(request(), accepted)


def test_fake_lists_pending_orders_without_mutating_them() -> None:
    manual = pending_order()
    bot = pending_order(
        order_id="8002",
        side="SELL",
        volume=Decimal("0.03"),
        magic=17001,
        comment="tgxm-pending-2",
    )
    broker = fake_broker(pending_orders=[manual, bot])

    assert broker.list_pending_orders() == (manual, bot)
    assert broker.list_pending_orders("GOLD") == (manual, bot)
    assert manual.client_reference == "manual-pending"
    assert broker.pending_orders == [manual, bot]


def test_live_or_non_allowlisted_account_is_rejected_before_send() -> None:
    live = fake_broker(account=account(is_demo=False))
    with pytest.raises(LiveAccountRejected):
        live.submit_market_order(request())
    assert live.sent_requests == []

    wrong_server = fake_broker(account=account(server="XM-Demo Other"))
    with pytest.raises(AccountNotAllowlisted):
        wrong_server.submit_market_order(request())
    assert wrong_server.sent_requests == []

    mismatched_intent = fake_broker()
    with pytest.raises(AccountNotAllowlisted, match="intent account"):
        mismatched_intent.submit_market_order(request(account_id="10002"))
    assert mismatched_intent.sent_requests == []


def test_stale_tick_bad_volume_and_bad_protection_fail_closed() -> None:
    stale = fake_broker(
        ticks={"GOLD": tick(time_utc=NOW - timedelta(seconds=6))}
    )
    with pytest.raises(StaleTickError):
        stale.submit_market_order(request())

    bad_volume = fake_broker()
    with pytest.raises(BrokerSafetyError, match="volume_step"):
        bad_volume.submit_market_order(request(volume=Decimal("0.015")))

    bad_sl = fake_broker()
    with pytest.raises(BrokerSafetyError, match="SELL requires stop_loss"):
        bad_sl.submit_market_order(request(stop_loss=Decimal("4599")))

    bad_tp = fake_broker()
    with pytest.raises(BrokerSafetyError, match="SELL requires take_profit"):
        bad_tp.submit_market_order(request(take_profit=Decimal("4602")))

    assert stale.sent_requests == []
    assert bad_volume.sent_requests == []
    assert bad_sl.sent_requests == []
    assert bad_tp.sent_requests == []


def test_fresh_broker_quote_rechecks_entry_spread_and_expiry_constraints() -> None:
    constrained = request(
        entry_low=Decimal("4601"),
        entry_high=Decimal("4605"),
        max_spread_points=Decimal("100"),
        expires_at_utc=NOW + timedelta(seconds=30),
    )
    outside = fake_broker(ticks={"GOLD": tick(bid=Decimal("4608"), ask=Decimal("4608.5"))})
    with pytest.raises(BrokerSafetyError, match="entry range"):
        outside.submit_market_order(constrained)
    assert outside.sent_requests == []

    wide = fake_broker(ticks={"GOLD": tick(bid=Decimal("4601"), ask=Decimal("4602.01"))})
    with pytest.raises(BrokerSafetyError, match="spread"):
        wide.submit_market_order(constrained)
    assert wide.sent_requests == []

    expired = fake_broker(
        ticks={"GOLD": tick(time_utc=NOW + timedelta(seconds=30))},
        clock=lambda: NOW + timedelta(seconds=30),
    )
    with pytest.raises(BrokerSafetyError, match="expired"):
        expired.submit_market_order(constrained)
    assert expired.sent_requests == []


def test_request_boundary_rejects_binary_float_and_missing_numeric_sl() -> None:
    with pytest.raises(TypeError, match="not float"):
        request(volume=0.01)
    with pytest.raises(ValueError, match="stop_loss"):
        request(stop_loss=Decimal("0"))


def test_check_rejection_prevents_send() -> None:
    rejected = BrokerResult(
        outcome=BrokerOutcome.REJECTED,
        stage="order_check",
        retcode=10016,
        comment="invalid stops",
    )
    broker = fake_broker(check_results=[rejected])

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.REJECTED
    assert len(broker.checked_requests) == 1
    assert broker.sent_requests == []


def test_ambiguous_fake_send_is_returned_once_and_never_retried() -> None:
    ambiguous = BrokerResult(
        outcome=BrokerOutcome.RECONCILE_REQUIRED,
        stage="order_send",
        retcode=10012,
        comment="timeout after submission",
    )
    broker = fake_broker(send_results=[ambiguous])

    result = broker.submit_market_order(request())

    assert result.requires_reconciliation is True
    assert len(broker.sent_requests) == 1


class StubMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_MARGIN_MODE_EXCHANGE = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ACCOUNT_TRADE_MODE_REAL = 2
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
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TYPE_BUY_STOP_LIMIT = 6
    ORDER_TYPE_SELL_STOP_LIMIT = 7
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_CANCEL = 10007
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_TIMEOUT = 10012
    TRADE_RETCODE_INVALID = 10013
    TRADE_RETCODE_INVALID_VOLUME = 10014
    TRADE_RETCODE_INVALID_PRICE = 10015
    TRADE_RETCODE_INVALID_STOPS = 10016
    TRADE_RETCODE_TRADE_DISABLED = 10017
    TRADE_RETCODE_MARKET_CLOSED = 10018
    TRADE_RETCODE_NO_MONEY = 10019
    TRADE_RETCODE_PRICE_CHANGED = 10020
    TRADE_RETCODE_PRICE_OFF = 10021
    TRADE_RETCODE_INVALID_EXPIRATION = 10022
    TRADE_RETCODE_TOO_MANY_REQUESTS = 10024
    TRADE_RETCODE_NO_CHANGES = 10025
    TRADE_RETCODE_SERVER_DISABLES_AT = 10026
    TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
    TRADE_RETCODE_CONNECTION = 10031
    TRADE_RETCODE_LIMIT_VOLUME = 10034
    TRADE_RETCODE_INVALID_ORDER = 10035
    TRADE_RETCODE_INVALID_FILL = 10030

    def __init__(self, *, send_retcode: int = TRADE_RETCODE_DONE) -> None:
        self.send_retcode = send_retcode
        self.initialized_with: str | None = None
        self.shutdown_calls = 0
        self.order_check_calls: list[dict[str, object]] = []
        self.order_send_calls: list[dict[str, object]] = []
        self.account_trade_mode = self.ACCOUNT_TRADE_MODE_DEMO
        self.symbol_execution_mode = self.SYMBOL_TRADE_EXECUTION_MARKET
        self.check_retcode = 0
        self.check_returns_none = False
        self.open_positions: list[SimpleNamespace] = []
        self.pending_orders: list[SimpleNamespace] = []
        self.orders_get_calls: list[str | None] = []
        self.orders_get_returns_none = False
        self.orders_get_error: Exception | None = None

    def initialize(self, path: str | None = None) -> bool:
        self.initialized_with = path
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            connected=True, trade_allowed=True, tradeapi_disabled=False
        )

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            login=10001,
            server="XM-Demo 1",
            company="XM Test",
            trade_mode=self.account_trade_mode,
            trade_allowed=True,
            trade_expert=True,
            currency="USD",
            margin_mode=2,
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
            volume_max=50.0,
            volume_step=0.01,
            trade_stops_level=10,
            trade_freeze_level=0,
            filling_mode=self.SYMBOL_FILLING_FOK,
            trade_exemode=self.symbol_execution_mode,
            order_mode=(self.SYMBOL_ORDER_MARKET | self.SYMBOL_ORDER_SL | self.SYMBOL_ORDER_TP),
        )

    def symbol_select(self, name: str, selected: bool) -> bool:
        return name == "GOLD" and selected

    def symbol_info_tick(self, name: str) -> SimpleNamespace | None:
        if name != "GOLD":
            return None
        return SimpleNamespace(
            bid=4601.00,
            ask=4601.59,
            time_msc=int(NOW.timestamp() * 1000),
        )

    def order_check(self, payload: dict[str, object]) -> SimpleNamespace | None:
        self.order_check_calls.append(payload)
        if self.check_returns_none:
            return None
        return SimpleNamespace(
            retcode=self.check_retcode,
            comment="check",
            price=payload.get("price", 4601.0),
            volume=payload["volume"],
            order=0,
            deal=0,
        )

    def order_send(self, payload: dict[str, object]) -> SimpleNamespace:
        self.order_send_calls.append(payload)
        result = SimpleNamespace(
            retcode=self.send_retcode,
            comment="send",
            price=payload.get("price", 4601.0),
            volume=payload["volume"],
            order=7001 if self.send_retcode == self.TRADE_RETCODE_DONE else 0,
            deal=7002 if self.send_retcode == self.TRADE_RETCODE_DONE else 0,
        )
        if self.send_retcode == self.TRADE_RETCODE_DONE:
            self.open_positions.append(
                SimpleNamespace(
                    ticket=7001,
                    identifier=7001,
                    symbol=payload["symbol"],
                    type=payload["type"],
                    volume=payload["volume"],
                    price_open=payload.get("price", 4601.0),
                    sl=payload["sl"],
                    tp=payload.get("tp", 0),
                    magic=payload["magic"],
                    comment=payload["comment"],
                    time_msc=int(NOW.timestamp() * 1000),
                )
            )
        return result

    def positions_get(self, *, symbol: str | None = None) -> tuple[SimpleNamespace, ...]:
        if symbol is None:
            return tuple(self.open_positions)
        return tuple(item for item in self.open_positions if item.symbol == symbol)

    def orders_get(
        self, *, symbol: str | None = None
    ) -> tuple[SimpleNamespace, ...] | None:
        self.orders_get_calls.append(symbol)
        if self.orders_get_error is not None:
            raise self.orders_get_error
        if self.orders_get_returns_none:
            return None
        if symbol is None:
            return tuple(self.pending_orders)
        return tuple(item for item in self.pending_orders if item.symbol == symbol)


def test_mt5_adapter_uses_exact_demo_gate_check_then_one_send() -> None:
    module = StubMT5()
    broker = MetaTrader5Broker(
        policy=policy(),
        terminal_path="C:/Program Files/XM MT5/terminal64.exe",
        mt5_module=module,
        clock=lambda: NOW,
    )

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert result.order_id == "7001"
    assert module.initialized_with == "C:/Program Files/XM MT5/terminal64.exe"
    assert len(module.order_check_calls) == 1
    assert len(module.order_send_calls) == 1
    sent = module.order_send_calls[0]
    assert sent["symbol"] == "GOLD"
    assert sent["volume"] == 0.01
    assert sent["sl"] == 4618.0
    assert sent["tp"] == 4595.0
    assert sent["comment"] == "tgxm-deadbeef-0"
    read_back = broker.read_back_market_order(request(), result)
    assert read_back is not None
    assert read_back.position_id == "7001"
    assert read_back.stop_loss == Decimal("4618.0")


def test_mt5_lists_pending_orders_with_exact_identity_and_side_mapping() -> None:
    module = StubMT5()
    module.pending_orders.extend(
        [
            SimpleNamespace(
                ticket=8001,
                symbol="GOLD",
                type=module.ORDER_TYPE_BUY_LIMIT,
                volume_current=0.02,
                magic=0,
                comment="manual-pending",
                time_setup_msc=int(NOW.timestamp() * 1000),
            ),
            SimpleNamespace(
                ticket=8002,
                symbol="GOLD",
                type=module.ORDER_TYPE_SELL_STOP,
                volume_current=0.03,
                magic=17001,
                comment="tgxm-pending-2",
                time_setup_msc=int(NOW.timestamp() * 1000),
            ),
        ]
    )
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    orders = broker.list_pending_orders("GOLD")

    assert module.orders_get_calls == ["GOLD"]
    assert [item.order_id for item in orders] == ["8001", "8002"]
    assert [item.side for item in orders] == ["BUY", "SELL"]
    assert [item.volume for item in orders] == [Decimal("0.02"), Decimal("0.03")]
    assert orders[1].account_id == "10001"
    assert orders[1].magic == 17001
    assert orders[1].comment == "tgxm-pending-2"
    assert orders[1].client_reference == "tgxm-pending-2"


@pytest.mark.parametrize("failure", ["none", "exception", "malformed"])
def test_mt5_pending_order_read_failures_are_closed_and_redacted(failure: str) -> None:
    module = StubMT5()
    secret = "PRIVATE_PENDING_COMMENT"
    if failure == "none":
        module.orders_get_returns_none = True
    elif failure == "exception":
        module.orders_get_error = RuntimeError(secret)
    else:
        module.pending_orders.append(
            SimpleNamespace(
                ticket=8001,
                symbol="GOLD",
                type=999,
                volume_current=0.02,
                magic=0,
                comment=secret,
                time_setup_msc=int(NOW.timestamp() * 1000),
            )
        )
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    with pytest.raises(BrokerUnavailableError) as captured:
        broker.list_pending_orders()

    assert secret not in str(captured.value)


@pytest.mark.parametrize("retcode", [10012, 10031, 19999])
def test_mt5_timeout_connection_and_unknown_are_reconcile_required(retcode: int) -> None:
    module = StubMT5(send_retcode=retcode)
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(module.order_send_calls) == 1


def test_mt5_definitive_rejection_is_not_retried() -> None:
    module = StubMT5(send_retcode=StubMT5.TRADE_RETCODE_INVALID_STOPS)
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.REJECTED
    assert len(module.order_send_calls) == 1


def test_mt5_failed_order_check_never_calls_order_send() -> None:
    module = StubMT5()
    module.check_retcode = StubMT5.TRADE_RETCODE_INVALID_STOPS
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.REJECTED
    assert module.order_send_calls == []


def test_mt5_rechecks_account_and_rejects_live_before_every_send() -> None:
    module = StubMT5()
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)
    assert broker.check_market_order(request()).outcome is BrokerOutcome.CHECK_PASSED
    module.account_trade_mode = module.ACCOUNT_TRADE_MODE_REAL

    with pytest.raises(LiveAccountRejected):
        broker.submit_market_order(request())
    assert module.order_send_calls == []


def test_mt5_rechecks_account_after_order_check_immediately_before_send() -> None:
    class SwitchAfterCheckMT5(StubMT5):
        def order_check(self, payload):
            result = super().order_check(payload)
            self.account_trade_mode = self.ACCOUNT_TRADE_MODE_REAL
            return result

    module = SwitchAfterCheckMT5()
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    with pytest.raises(LiveAccountRejected):
        broker.submit_market_order(request())
    assert len(module.order_check_calls) == 1
    assert module.order_send_calls == []


def test_mt5_rereads_quote_after_order_check_before_send() -> None:
    class MoveAfterCheckMT5(StubMT5):
        moved = False

        def order_check(self, payload):
            result = super().order_check(payload)
            self.moved = True
            return result

        def symbol_info_tick(self, name):
            raw = super().symbol_info_tick(name)
            if raw is not None and self.moved:
                raw.bid = 4608.0
                raw.ask = 4608.5
            return raw

    module = MoveAfterCheckMT5()
    module.symbol_execution_mode = module.SYMBOL_TRADE_EXECUTION_INSTANT
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)
    constrained = request(
        entry_low=Decimal("4601"),
        entry_high=Decimal("4605"),
        max_spread_points=Decimal("100"),
        expires_at_utc=NOW + timedelta(seconds=30),
    )

    with pytest.raises(BrokerSafetyError, match="entry range"):
        broker.submit_market_order(constrained)
    assert len(module.order_check_calls) == 1
    assert module.order_send_calls == []


def test_mt5_market_execution_rejects_strict_bounds_before_any_trade_call() -> None:
    module = StubMT5()
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)
    constrained = request(
        entry_low=Decimal("4601"),
        entry_high=Decimal("4605"),
        max_spread_points=Decimal("100"),
        expires_at_utc=NOW + timedelta(seconds=30),
    )

    with pytest.raises(BrokerSafetyError, match="Market Execution"):
        broker.submit_market_order(constrained)

    assert module.order_check_calls == []
    assert module.order_send_calls == []


def test_mt5_market_execution_omits_non_binding_request_price() -> None:
    module = StubMT5()
    broker = MetaTrader5Broker(policy=policy(), mt5_module=module, clock=lambda: NOW)

    result = broker.submit_market_order(request())

    assert result.outcome is BrokerOutcome.ACCEPTED
    assert "price" not in module.order_check_calls[0]
    assert "price" not in module.order_send_calls[0]


def test_readback_does_not_fallback_when_returned_order_id_mismatches() -> None:
    broker = fake_broker(positions=[position(position_id="old", identifier="old")])
    result = BrokerResult(
        outcome=BrokerOutcome.ACCEPTED,
        stage="order_send",
        retcode=10009,
        comment="accepted",
        request=request(),
        price=Decimal("4601"),
        volume=Decimal("0.01"),
        order_id="new-order",
    )

    assert broker.read_back_market_order(request(), result) is None


def test_mt5_import_is_lazy_and_missing_optional_package_is_clear(monkeypatch) -> None:
    broker = MetaTrader5Broker(policy=policy())

    def missing(name: str) -> object:
        assert name == "MetaTrader5"
        raise ImportError("not installed")

    monkeypatch.setattr("tgxm.broker.importlib.import_module", missing)
    with pytest.raises(BrokerUnavailableError, match="optional"):
        broker.discover_account()


# -- broker server clock ----------------------------------------------------


class ManagedStubMT5(StubMT5):
    """Adds protection changes and position closes to the MT5 stub."""

    TRADE_ACTION_SLTP = 2

    def order_send(self, payload: dict[str, object]) -> SimpleNamespace:
        action = payload.get("action")
        if action == self.TRADE_ACTION_SLTP:
            self.order_send_calls.append(payload)
            for item in self.open_positions:
                if item.ticket == payload["position"]:
                    item.sl = payload["sl"]
                    item.tp = payload["tp"]
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE, comment="sltp", price=0, volume=0,
                order=0, deal=0,
            )
        if action == self.TRADE_ACTION_DEAL and "position" in payload:
            self.order_send_calls.append(payload)
            if not self.close_leaves_position_open:
                self.open_positions = [
                    item
                    for item in self.open_positions
                    if item.ticket != payload["position"]
                ]
            return SimpleNamespace(
                retcode=self.close_retcode,
                comment="close",
                price=payload.get("price", 4601.0),
                volume=payload["volume"],
                order=7100,
                deal=7101,
            )
        return super().order_send(payload)

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.close_retcode = self.TRADE_RETCODE_DONE
        self.close_leaves_position_open = False


def managed_broker(
    module: ManagedStubMT5, **changes: object
) -> MetaTrader5Broker:
    return MetaTrader5Broker(
        policy=policy(),
        mt5_module=module,
        clock=lambda: NOW,
        **changes,  # type: ignore[arg-type]
    )


def open_stub_position(module: ManagedStubMT5, **changes: object) -> None:
    values: dict[str, object] = {
        "ticket": 7001,
        "identifier": 7001,
        "symbol": "GOLD",
        "type": module.POSITION_TYPE_BUY,
        "volume": 0.01,
        "price_open": 4601.00,
        "sl": 4590.00,
        "tp": 4620.00,
        "magic": 17001,
        "comment": "tgxa-owned",
        "time_msc": int(NOW.timestamp() * 1000),
    }
    values.update(changes)
    module.open_positions.append(SimpleNamespace(**values))


def test_server_offset_is_measured_from_a_fresh_quote() -> None:
    server_epoch = (NOW + timedelta(hours=3)).timestamp() + 1
    assert (
        detect_server_utc_offset_minutes(
            server_epoch, NOW, max_residual_seconds=Decimal("5")
        )
        == 180
    )


def test_server_offset_measurement_rejects_a_stalled_feed() -> None:
    stale = (NOW + timedelta(hours=3)).timestamp() - 400
    with pytest.raises(StaleTickError):
        detect_server_utc_offset_minutes(stale, NOW, max_residual_seconds=Decimal("5"))


def test_server_offset_measurement_rejects_an_implausible_clock() -> None:
    absurd = (NOW + timedelta(days=3)).timestamp()
    with pytest.raises(BrokerSafetyError):
        detect_server_utc_offset_minutes(absurd, NOW, max_residual_seconds=Decimal("5"))


def test_measured_offset_makes_a_server_time_tick_read_as_fresh() -> None:
    module = ManagedStubMT5()
    ahead = int((NOW + timedelta(hours=3)).timestamp() * 1000)
    module.symbol_info_tick = lambda name: (  # type: ignore[assignment]
        None
        if name != "GOLD"
        else SimpleNamespace(bid=4601.00, ask=4601.59, time_msc=ahead)
    )
    broker = managed_broker(module, server_utc_offset_minutes=None)
    tick = broker.get_tick("GOLD")
    assert broker.server_utc_offset_minutes == 180
    assert broker.server_offset_source == "detected"
    assert tick.time_utc == NOW


def test_unshifted_server_time_fails_the_freshness_gate() -> None:
    module = ManagedStubMT5()
    ahead = int((NOW + timedelta(hours=3)).timestamp() * 1000)
    module.symbol_info_tick = lambda name: (  # type: ignore[assignment]
        None
        if name != "GOLD"
        else SimpleNamespace(bid=4601.00, ask=4601.59, time_msc=ahead)
    )
    broker = managed_broker(module)
    with pytest.raises(StaleTickError):
        broker.check_market_order(request(magic=17001))


# -- position management ----------------------------------------------------


def test_breakeven_stop_is_applied_and_read_back() -> None:
    module = ManagedStubMT5()
    open_stub_position(module)
    broker = managed_broker(module)
    (position,) = broker.list_open_positions("GOLD")

    updated = broker.modify_position_protection(
        position,
        stop_loss=Decimal("4601.00") - Decimal("1.00"),
        take_profit=position.take_profit,
        expected_magic=17001,
        expected_client_reference="tgxa-owned",
    )
    assert updated.stop_loss == Decimal("4600.00")
    assert updated.take_profit == Decimal("4620.00")
    assert module.order_send_calls[-1]["action"] == module.TRADE_ACTION_SLTP


def test_protection_change_refuses_a_position_the_bot_does_not_own() -> None:
    module = ManagedStubMT5()
    open_stub_position(module, magic=0, comment="manual")
    broker = managed_broker(module)
    (position,) = broker.list_open_positions("GOLD")
    with pytest.raises(BrokerSafetyError, match="not owned"):
        broker.modify_position_protection(
            position,
            stop_loss=Decimal("4600.00"),
            take_profit=None,
            expected_magic=17001,
            expected_client_reference="tgxa-owned",
        )
    assert module.order_send_calls == []


def test_protection_change_refuses_a_stop_on_the_wrong_side() -> None:
    module = ManagedStubMT5()
    open_stub_position(module)
    broker = managed_broker(module)
    (position,) = broker.list_open_positions("GOLD")
    with pytest.raises(BrokerSafetyError, match="below the current Bid"):
        broker.modify_position_protection(
            position,
            stop_loss=Decimal("4700.00"),
            take_profit=None,
            expected_magic=17001,
            expected_client_reference="tgxa-owned",
        )
    assert module.order_send_calls == []


def test_close_position_is_accepted_only_when_the_ticket_is_gone() -> None:
    module = ManagedStubMT5()
    open_stub_position(module)
    broker = managed_broker(module)
    (position,) = broker.list_open_positions("GOLD")

    result = broker.close_position(
        position, expected_magic=17001, expected_client_reference="tgxa-owned"
    )
    assert result.outcome is BrokerOutcome.ACCEPTED
    assert result.stage == "close_position"
    assert broker.list_open_positions("GOLD") == ()
    payload = module.order_send_calls[-1]
    assert payload["position"] == 7001
    assert payload["type"] == module.ORDER_TYPE_SELL


def test_close_that_leaves_the_position_open_requires_reconciliation() -> None:
    module = ManagedStubMT5()
    module.close_leaves_position_open = True
    open_stub_position(module)
    broker = managed_broker(module)
    (position,) = broker.list_open_positions("GOLD")

    result = broker.close_position(
        position, expected_magic=17001, expected_client_reference="tgxa-owned"
    )
    assert result.outcome is BrokerOutcome.RECONCILE_REQUIRED
    assert len(module.order_send_calls) == 1
