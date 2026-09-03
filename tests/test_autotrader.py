from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tgxm.autotrader import (
    AUTOTRADE_MAGIC,
    AutoTradeStatus,
    AutoTrader,
    ManagementAction,
    client_reference_for,
    closed_candles,
    quantize_price,
    resolve_higher_timeframe,
    signal_id_for,
    strategy_settings,
)
from tgxm.broker import (
    AccountSnapshot,
    BrokerOutcome,
    BrokerResult,
    DemoAccountPolicy,
    FakeBroker,
    PositionSnapshot,
    SymbolSnapshot,
    TickSnapshot,
)
from tgxm.config import AppConfig, ConfigError
from tgxm.indicator import Candle, CrossoverState, PredictionState, TrendState, crossover_state
from tgxm.store import IntentStatus, OrderIntent, SQLiteStore


UTC = timezone.utc
START = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"

#: (seed, last index) pairs whose deterministic walk ends on a clean EMA cross
#: with RSI inside the Pine band.  Each test asserts that property, so a change
#: to the rule set fails loudly instead of silently testing nothing.
BUY_FIXTURE = (3, 122)
SELL_FIXTURE = (1, 149)


def _walk(seed: int, count: int) -> list[Decimal]:
    """Deterministic price walk in cents; no randomness crosses test runs."""

    value = 400_000
    state = seed
    closes: list[Decimal] = []
    for _ in range(count):
        state = (state * 1103515245 + 12345) % (2**31)
        value += (state % 41) - 20
        closes.append(Decimal(value) / Decimal(100))
    return closes


def _candles(closes: list[Decimal], *, start: datetime = START, minutes: int = 1) -> list[Candle]:
    series: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        series.append(
            Candle(
                time_utc=start + timedelta(minutes=minutes * index),
                open=previous,
                high=max(previous, close) + Decimal("0.30"),
                low=min(previous, close) - Decimal("0.30"),
                close=close,
            )
        )
        previous = close
    return series


def signal_candles(fixture: tuple[int, int]) -> list[Candle]:
    seed, end = fixture
    return _candles(_walk(seed, end + 1))


def trending_candles(direction: str, *, count: int = 120, minutes: int = 15) -> list[Candle]:
    """A monotonic series, so the EMA pair is unambiguously up or down."""

    step = Decimal("0.5") if direction == "UP" else Decimal("-0.5")
    closes = [Decimal("4000") + step * index for index in range(count)]
    return _candles(closes, start=START - timedelta(minutes=minutes * count), minutes=minutes)


class StubCandleSource:
    """Returns a fixed series per timeframe; it cannot submit anything."""

    def __init__(self, series: dict[str, list[Candle]]) -> None:
        self.series = series
        self.requests: list[tuple[str, str, int]] = []

    def fetch_candles(
        self, exact_symbol: str, timeframe: str, count: int
    ) -> tuple[Candle, ...]:
        self.requests.append((exact_symbol, timeframe, count))
        candles = self.series[timeframe]
        return tuple(candles[-count:])


def policy() -> DemoAccountPolicy:
    return DemoAccountPolicy(
        allowed_demo_accounts=frozenset({"5001"}),
        allowed_servers=frozenset({"Broker-Demo"}),
        allowed_symbols=frozenset({SYMBOL}),
        max_tick_age_seconds=Decimal("5"),
    )


def account() -> AccountSnapshot:
    return AccountSnapshot(
        login="5001",
        server="Broker-Demo",
        company="Broker Ltd",
        is_demo=True,
        connected=True,
        trade_allowed=True,
        trade_api_disabled=False,
        currency="USD",
        margin_mode="RETAIL_HEDGING",
    )


def symbol() -> SymbolSnapshot:
    return SymbolSnapshot(
        symbol=SYMBOL,
        visible=True,
        trade_mode="FULL",
        digits=2,
        point=Decimal("0.01"),
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("50"),
        volume_step=Decimal("0.01"),
        stops_level_points=0,
        freeze_level_points=0,
        filling_flags=1,
        execution_mode=2,
    )


def config(**autotrade_changes: object) -> AppConfig:
    base = AppConfig.default()
    changes: dict[str, object] = {
        "enabled": True,
        "trade_enabled": True,
        "broker_symbol": SYMBOL,
        "timeframe": "M1",
        "cooldown_bars": 0,
    }
    changes.update(autotrade_changes)
    autotrade = replace(base.autotrade, **changes)  # type: ignore[arg-type]
    return replace(
        base,
        broker=replace(base.broker, adapter="mt5", terminal_path="C:/mt5/terminal64.exe"),
        autotrade=autotrade,
    ).validate()


def build(
    tmp_path: Path,
    *,
    fixture: tuple[int, int] = BUY_FIXTURE,
    trend: str = "UP",
    bid: Decimal = Decimal("4001.65"),
    ask: Decimal = Decimal("4001.75"),
    positions: tuple[PositionSnapshot, ...] = (),
    demo_active: bool = True,
    forming_bar: bool = True,
    **autotrade_changes: object,
) -> tuple[AutoTrader, FakeBroker, SQLiteStore, list[Candle]]:
    app_config = config(**autotrade_changes)
    candles = signal_candles(fixture)
    now = candles[-1].time_utc + timedelta(minutes=1)
    series = list(candles)
    if forming_bar:
        # MT5 always hands back the bar that is still being written.
        series.append(
            Candle(
                time_utc=now,
                open=candles[-1].close,
                high=candles[-1].close + Decimal("5"),
                low=candles[-1].close - Decimal("5"),
                close=candles[-1].close + Decimal("4"),
            )
        )
    source = StubCandleSource(
        {"M1": series, "M15": trending_candles(trend), "H1": trending_candles(trend, minutes=60)}
    )
    clock = lambda: now  # noqa: E731 - a frozen clock keeps the fixture reproducible
    broker = FakeBroker(
        policy=policy(),
        account=account(),
        symbols={SYMBOL: symbol()},
        ticks={SYMBOL: TickSnapshot(symbol=SYMBOL, bid=bid, ask=ask, time_utc=now)},
        positions=positions,
        clock=clock,
    )
    store = SQLiteStore(tmp_path / "autotrade.sqlite3")
    trader = AutoTrader(
        config=app_config,
        store=store,
        broker=broker,
        candle_source=source,
        position_manager=broker,
        demo_active=demo_active,
        clock=clock,
    )
    return trader, broker, store, candles


# -- pure helpers -----------------------------------------------------------


def test_fixtures_really_cross_with_rsi_inside_the_band() -> None:
    buy = signal_candles(BUY_FIXTURE)
    sell = signal_candles(SELL_FIXTURE)
    assert crossover_state(buy, ema_fast_period=20, ema_slow_period=50) is CrossoverState.UP
    assert crossover_state(sell, ema_fast_period=20, ema_slow_period=50) is CrossoverState.DOWN


def test_closed_candles_drops_the_bar_still_forming() -> None:
    candles = _candles([Decimal("4000"), Decimal("4001"), Decimal("4002")])
    now = candles[-1].time_utc + timedelta(seconds=30)
    kept = closed_candles(candles, timeframe="M1", now_utc=now)
    assert [candle.time_utc for candle in kept] == [
        candles[0].time_utc,
        candles[1].time_utc,
    ]


def test_closed_candles_keeps_a_bar_the_moment_its_period_ends() -> None:
    candles = _candles([Decimal("4000"), Decimal("4001")])
    now = candles[-1].time_utc + timedelta(minutes=1)
    assert len(closed_candles(candles, timeframe="M1", now_utc=now)) == 2


def test_quantize_price_snaps_onto_the_broker_grid() -> None:
    assert quantize_price(Decimal("4000.653846"), Decimal("0.01")) == Decimal("4000.65")
    assert quantize_price(Decimal("4000.655"), Decimal("0.01")) == Decimal("4000.66")


def test_signal_id_is_stable_per_bar_and_side() -> None:
    bar = datetime(2026, 9, 2, 9, 15, tzinfo=UTC)
    assert signal_id_for(SYMBOL, "M1", bar, "BUY") == "auto-XAUUSD-M1-20260902T091500Z-BUY"
    assert client_reference_for("5001", "auto-x") == client_reference_for("5001", "auto-x")
    assert client_reference_for("5001", "auto-x") != client_reference_for("5002", "auto-x")
    assert len(client_reference_for("5001", "auto-x")) <= 31


def test_higher_timeframe_follows_the_pine_ladder_or_is_off() -> None:
    assert resolve_higher_timeframe(config()) == "M15"
    assert resolve_higher_timeframe(config(timeframe="M15")) == "H1"
    assert resolve_higher_timeframe(config(require_higher_timeframe_agreement=False)) is None
    assert resolve_higher_timeframe(config(higher_timeframe="H1")) == "H1"


def test_strategy_settings_take_rule_parameters_from_the_indicator_section() -> None:
    settings = strategy_settings(config())
    assert settings.symbol == SYMBOL
    assert settings.timeframe == "M1"
    assert settings.ema_fast_period == AppConfig.default().indicator.ema_fast_period


# -- entry ------------------------------------------------------------------


def test_confirmed_crossover_opens_one_verified_demo_position(tmp_path: Path) -> None:
    trader, broker, store, candles = build(tmp_path)
    decision = trader.run_cycle()

    assert decision.status is AutoTradeStatus.OPEN
    assert decision.prediction is not None
    assert decision.prediction.state is PredictionState.BUY
    assert decision.higher_timeframe_trend is TrendState.UP
    assert decision.bar_time_utc == candles[-1].time_utc

    (request,) = broker.sent_requests
    assert request.symbol == SYMBOL
    assert request.side == "BUY"
    assert request.volume == Decimal("0.01")
    assert request.magic == AUTOTRADE_MAGIC
    # Snapped to the broker grid, and still the ATR distances the rule chose.
    assert request.stop_loss == Decimal("4000.65")
    assert request.take_profit == Decimal("4003.79")

    intent = store.get_order_intent(decision.intent_id or 0)
    assert intent is not None
    assert intent.status is IntentStatus.OPEN
    assert intent.signal_id.startswith("auto-XAUUSD-M1-")
    assert intent.request_metadata["first_take_profit"] == "4002.75"
    assert intent.request_metadata["bar_time_utc"] == candles[-1].time_utc.isoformat()
    store.close()


def test_sell_crossover_opens_a_sell(tmp_path: Path) -> None:
    trader, broker, store, _ = build(
        tmp_path,
        fixture=SELL_FIXTURE,
        trend="DOWN",
        bid=Decimal("3999.55"),
        ask=Decimal("3999.65"),
    )
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.OPEN
    (request,) = broker.sent_requests
    assert request.side == "SELL"
    assert request.stop_loss > request.take_profit
    store.close()


def test_higher_timeframe_disagreement_blocks_the_entry(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path, trend="DOWN")
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.NO_SIGNAL
    assert "higher_timeframe_filter_blocked" in decision.reason
    assert broker.sent_requests == []
    assert store.list_order_intents() == []
    store.close()


def test_filter_can_be_turned_off_like_the_pine_input(tmp_path: Path) -> None:
    trader, _, store, _ = build(
        tmp_path, trend="DOWN", require_higher_timeframe_agreement=False
    )
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.OPEN
    assert decision.higher_timeframe_trend is None
    store.close()


def test_trade_disabled_never_persists_an_intent(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path, demo_active=False, trade_enabled=False)
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.TRADE_DISABLED
    assert store.list_order_intents() == []
    assert broker.sent_requests == []
    store.close()


def test_an_unactivated_process_never_submits(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path, demo_active=False)
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.DEMO_NOT_ACTIVE
    assert store.list_order_intents() == []
    assert broker.sent_requests == []
    store.close()


def test_an_open_position_blocks_a_repeat_after_a_restart(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    assert trader.run_cycle().status is AutoTradeStatus.OPEN

    # A fresh worker over the same durable store is exactly a restart.
    restarted, broker2, store2, _ = build(tmp_path)
    broker2.positions.extend(broker.positions)
    assert restarted.run_cycle().status is AutoTradeStatus.EXPOSURE_BLOCKED
    assert broker2.sent_requests == []
    store.close()
    store2.close()


def test_durable_intent_blocks_a_repeat_when_no_position_is_open(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    first = trader.run_cycle()
    assert first.status is AutoTradeStatus.OPEN

    restarted, broker2, store2, _ = build(tmp_path)
    broker2.positions.clear()  # position closed at the broker meanwhile
    decision = restarted.run_cycle()
    assert decision.status is AutoTradeStatus.INTENT_EXISTS
    assert broker2.sent_requests == []
    store.close()
    store2.close()


def test_manual_position_on_the_symbol_blocks_a_new_entry(tmp_path: Path) -> None:
    manual = PositionSnapshot(
        account_id="5001",
        position_id="9001",
        symbol=SYMBOL,
        side="SELL",
        volume=Decimal("0.10"),
        price_open=Decimal("4000.00"),
        stop_loss=Decimal("4010.00"),
        take_profit=None,
        magic=0,
        comment="manual",
        time_utc=START,
    )
    trader, broker, store, _ = build(tmp_path, positions=(manual,))
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.MANUAL_EXPOSURE_BLOCKED
    assert broker.sent_requests == []
    store.close()


def test_wide_spread_blocks_the_entry(tmp_path: Path) -> None:
    trader, broker, store, _ = build(
        tmp_path, bid=Decimal("4001.00"), ask=Decimal("4002.00"), max_spread_points=50
    )
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.SPREAD_BLOCKED
    assert store.list_order_intents() == []
    store.close()


def _record_earlier_entry(store: SQLiteStore, bar_time: datetime) -> None:
    """Persist a completed strategy intent on an earlier bar of the same market."""

    signal_id = signal_id_for(SYMBOL, "M1", bar_time, "BUY")
    store.create_order_intent(
        OrderIntent(
            account_id="5001",
            signal_id=signal_id,
            signal_revision=0,
            leg_index=0,
            broker_symbol=SYMBOL,
            side="BUY",
            volume=Decimal("0.01"),
            stop_loss=Decimal("3990.00"),
            take_profit=Decimal("4010.00"),
            client_reference=client_reference_for("5001", signal_id),
            request_metadata={"bar_time_utc": bar_time.isoformat()},
        )
    )


def test_cooldown_blocks_an_entry_taken_a_few_bars_ago(tmp_path: Path) -> None:
    trader, broker, store, candles = build(tmp_path, cooldown_bars=5)
    _record_earlier_entry(store, candles[-1].time_utc - timedelta(minutes=2))
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.COOLDOWN
    assert broker.sent_requests == []
    store.close()


def test_an_older_entry_outside_the_cooldown_does_not_block(tmp_path: Path) -> None:
    trader, broker, store, candles = build(tmp_path, cooldown_bars=2)
    _record_earlier_entry(store, candles[-1].time_utc - timedelta(minutes=30))
    assert trader.run_cycle().status is AutoTradeStatus.OPEN
    assert len(broker.sent_requests) == 1
    store.close()


def test_daily_limit_stops_further_entries(tmp_path: Path) -> None:
    trader, broker, store, candles = build(tmp_path, max_trades_per_day=1)
    _record_earlier_entry(store, candles[-1].time_utc - timedelta(minutes=30))
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.DAILY_LIMIT_REACHED
    assert broker.sent_requests == []
    store.close()


def test_ambiguous_broker_result_locks_the_intent_without_retrying(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    broker.queue_send_result(
        BrokerResult(
            outcome=BrokerOutcome.RECONCILE_REQUIRED,
            stage="order_send",
            retcode=10012,
            comment="timeout",
        )
    )
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.RECONCILE_REQUIRED
    assert len(broker.sent_requests) == 1
    intent = store.get_order_intent(decision.intent_id or 0)
    assert intent is not None
    assert intent.status is IntentStatus.RECONCILE_REQUIRED
    store.close()


def test_rejected_check_records_the_rejection_and_sends_nothing(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    broker.queue_check_result(
        BrokerResult(
            outcome=BrokerOutcome.REJECTED,
            stage="order_check",
            retcode=10016,
            comment="invalid stops",
        )
    )
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.BROKER_REJECTED
    assert broker.sent_requests == []
    intent = store.get_order_intent(decision.intent_id or 0)
    assert intent is not None
    assert intent.status is IntentStatus.BROKER_REJECTED
    store.close()


# -- management -------------------------------------------------------------


def _opened(trader: AutoTrader, broker: FakeBroker, store: SQLiteStore) -> PositionSnapshot:
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.OPEN
    return broker.positions[-1]


def test_opposite_crossover_closes_the_position_and_closes_the_intent(
    tmp_path: Path,
) -> None:
    trader, broker, store, _ = build(tmp_path)
    position = _opened(trader, broker, store)

    # Re-run the worker over a series whose last bar crosses the other way.
    reversal, broker2, store2, _ = build(
        tmp_path,
        fixture=SELL_FIXTURE,
        trend="DOWN",
        bid=Decimal("3999.55"),
        ask=Decimal("3999.65"),
    )
    broker2.positions.append(position)
    decision = reversal.run_cycle()

    actions = {item.action for item in decision.management}
    assert ManagementAction.CLOSED_ON_REVERSAL in actions
    assert position.position_id in broker2.closed_positions
    intents = [item for item in store2.list_order_intents() if item.id == 1]
    assert intents[0].status is IntentStatus.CLOSED
    store.close()
    store2.close()


def test_first_target_moves_the_stop_to_breakeven(tmp_path: Path) -> None:
    trader, broker, store, candles = build(tmp_path)
    position = _opened(trader, broker, store)
    assert position.stop_loss == Decimal("4000.65")

    # Price has since reached the first ATR target (4002.75).
    follow_up, broker2, store2, _ = build(
        tmp_path, bid=Decimal("4002.80"), ask=Decimal("4002.90")
    )
    broker2.positions.append(position)
    decision = follow_up.run_cycle()

    assert ManagementAction.BREAKEVEN_APPLIED in {
        item.action for item in decision.management
    }
    moved = broker2.positions[-1]
    assert moved.stop_loss == position.price_open
    assert moved.take_profit == position.take_profit
    store.close()
    store2.close()


def test_breakeven_is_not_triggered_by_the_signal_bar_itself(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    position = _opened(trader, broker, store)

    unchanged, broker2, store2, _ = build(tmp_path)
    broker2.positions.append(position)
    decision = unchanged.run_cycle()
    assert decision.management == ()
    assert broker2.protection_changes == []
    store.close()
    store2.close()


def test_management_needs_the_volatile_demo_activation(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    position = _opened(trader, broker, store)

    inactive, broker2, store2, _ = build(
        tmp_path,
        fixture=SELL_FIXTURE,
        trend="DOWN",
        bid=Decimal("3999.55"),
        ask=Decimal("3999.65"),
        demo_active=False,
    )
    broker2.positions.append(position)
    decision = inactive.run_cycle()
    assert decision.management == ()
    assert broker2.closed_positions == []
    store.close()
    store2.close()


def test_a_position_without_a_durable_intent_is_reported_never_managed(
    tmp_path: Path,
) -> None:
    orphan = PositionSnapshot(
        account_id="5001",
        position_id="9500",
        symbol=SYMBOL,
        side="BUY",
        volume=Decimal("0.01"),
        price_open=Decimal("4000.00"),
        stop_loss=Decimal("3990.00"),
        take_profit=Decimal("4010.00"),
        magic=AUTOTRADE_MAGIC,
        comment="tgxa-unknown",
        time_utc=START,
    )
    trader, broker, store, _ = build(
        tmp_path,
        fixture=SELL_FIXTURE,
        trend="DOWN",
        bid=Decimal("3999.55"),
        ask=Decimal("3999.65"),
        positions=(orphan,),
    )
    decision = trader.run_cycle()
    (outcome,) = decision.management
    assert outcome.action is ManagementAction.FAILED
    assert broker.closed_positions == []
    store.close()


# -- configuration ----------------------------------------------------------


def test_autotrade_requires_the_mt5_adapter() -> None:
    base = AppConfig.default()
    broken = replace(
        base,
        autotrade=replace(base.autotrade, enabled=True),
        broker=replace(base.broker, terminal_path="C:/mt5/terminal64.exe"),
    )
    with pytest.raises(ConfigError, match="mt5"):
        broken.validate()


def test_breakeven_needs_a_target_beyond_the_first_multiplier() -> None:
    with pytest.raises(ConfigError, match="breakeven"):
        config(take_profit_index=1)


def test_higher_timeframe_must_be_strictly_higher() -> None:
    with pytest.raises(ConfigError, match="strictly higher"):
        config(timeframe="H1", higher_timeframe="M15")


def test_a_position_closed_by_the_broker_is_reconciled_to_closed(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    first = trader.run_cycle()
    assert first.status is AutoTradeStatus.OPEN

    # The broker's own Stop Loss or Take Profit closed it between cycles.
    later, broker2, store2, _ = build(tmp_path)
    decision = later.run_cycle()

    reconciled = [
        item
        for item in decision.management
        if item.action is ManagementAction.RECONCILED_CLOSED
    ]
    assert len(reconciled) == 1
    intent = store2.get_order_intent(first.intent_id or 0)
    assert intent is not None
    assert intent.status is IntentStatus.CLOSED
    store.close()
    store2.close()


def test_an_ambiguous_intent_is_never_reconciled_to_closed(tmp_path: Path) -> None:
    trader, broker, store, _ = build(tmp_path)
    broker.queue_send_result(
        BrokerResult(
            outcome=BrokerOutcome.RECONCILE_REQUIRED,
            stage="order_send",
            retcode=10012,
            comment="timeout",
        )
    )
    decision = trader.run_cycle()
    assert decision.status is AutoTradeStatus.RECONCILE_REQUIRED

    later, broker2, store2, _ = build(tmp_path)
    follow_up = later.run_cycle()
    assert follow_up.management == ()
    intent = store2.get_order_intent(decision.intent_id or 0)
    assert intent is not None
    assert intent.status is IntentStatus.RECONCILE_REQUIRED
    store.close()
    store2.close()
