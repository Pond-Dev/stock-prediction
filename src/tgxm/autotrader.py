"""Local indicator strategy worker for the MT5 Demo terminal.

This worker is the second, independent way an order can be created in this
project.  It does not read Telegram and no Channel Profile is involved: it
evaluates the deterministic EMA-crossover + RSI + ATR + higher-timeframe rule
set from ``pine/ema_rsi_atr_advisory.pine`` over MT5 candle history and decides
its own entries.  The ``signal-authority`` rule governs Telegram content, which
this module never touches; every other hard invariant still applies unchanged:

* a uniquely keyed Order Intent is persisted before submission, keyed to the
  exact bar that produced the signal, so a restart cannot re-enter that bar;
* the Demo account allowlist, symbol identity, tick freshness, volume, and
  price relationships are re-proved by the broker adapter on every submission;
* an ambiguous broker result becomes ``RECONCILE_REQUIRED`` and is never
  retried from here;
* position management can only relocate protection the bot placed or close a
  position the bot proved it owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
import hashlib
from typing import Any, Callable, Mapping, Protocol, Sequence

from tgxm.broker import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    BrokerOutcome,
    BrokerResult,
    MarketOrderRequest,
    PositionManager,
    PositionSnapshot,
    SymbolSnapshot,
    TickSnapshot,
)
from tgxm.config import AppConfig
from tgxm.indicator import (
    Candle,
    CrossoverState,
    IndicatorError,
    Prediction,
    PredictionState,
    TIMEFRAME_MINUTES,
    TrendState,
    crossover_state,
    higher_timeframe,
    predict,
    trend_state,
)
from tgxm.policy import PolicyError, fixed_volume
from tgxm.store import IntentStatus, OrderIntent, OrderIntentRecord, SQLiteStore


#: Ownership tag for strategy positions.  Deliberately different from the
#: Telegram engine's magic so the two features can never claim each other's
#: positions, and so a human can tell them apart in the terminal.
AUTOTRADE_MAGIC = 26082702

#: Prefix of every strategy ``signal_id``; also how strategy intents are told
#: apart from Telegram intents in the shared store.
SIGNAL_PREFIX = "auto"


class AutoTradeStatus(StrEnum):
    DISABLED = "DISABLED"
    NO_SIGNAL = "NO_SIGNAL"
    BAR_ALREADY_EVALUATED = "BAR_ALREADY_EVALUATED"
    TRADE_DISABLED = "TRADE_DISABLED"
    DEMO_NOT_ACTIVE = "DEMO_NOT_ACTIVE"
    SPREAD_BLOCKED = "SPREAD_BLOCKED"
    EXPOSURE_BLOCKED = "EXPOSURE_BLOCKED"
    MANUAL_EXPOSURE_BLOCKED = "MANUAL_EXPOSURE_BLOCKED"
    COOLDOWN = "COOLDOWN"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"
    INTENT_EXISTS = "INTENT_EXISTS"
    BROKER_REJECTED = "BROKER_REJECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    SUBMITTED_UNVERIFIED = "SUBMITTED_UNVERIFIED"
    OPEN = "OPEN"
    PARTIAL_OPEN = "PARTIAL_OPEN"


class ManagementAction(StrEnum):
    BREAKEVEN_APPLIED = "BREAKEVEN_APPLIED"
    CLOSED_ON_REVERSAL = "CLOSED_ON_REVERSAL"
    CLOSE_UNCONFIRMED = "CLOSE_UNCONFIRMED"
    RECONCILED_CLOSED = "RECONCILED_CLOSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ManagementOutcome:
    action: ManagementAction
    position_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class AutoTradeDecision:
    """One evaluation of one closed bar, plus any management it performed."""

    status: AutoTradeStatus
    reason: str
    symbol: str
    timeframe: str
    bar_time_utc: datetime | None = None
    signal_id: str | None = None
    prediction: Prediction | None = None
    higher_timeframe_trend: TrendState | None = None
    intent_id: int | None = None
    broker_result: BrokerResult | None = None
    management: tuple[ManagementOutcome, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Render an operator-facing summary that contains no secret values."""

        return {
            "status": self.status.value,
            "reason": self.reason,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_time_utc": (
                None if self.bar_time_utc is None else self.bar_time_utc.isoformat()
            ),
            "signal_id": self.signal_id,
            "higher_timeframe_trend": (
                None
                if self.higher_timeframe_trend is None
                else self.higher_timeframe_trend.value
            ),
            "prediction_state": (
                None if self.prediction is None else self.prediction.state.value
            ),
            "prediction_reason": (
                None if self.prediction is None else self.prediction.reason
            ),
            "intent_id": self.intent_id,
            "broker_outcome": (
                None if self.broker_result is None else self.broker_result.outcome.value
            ),
            "management": [
                {
                    "action": item.action.value,
                    "position_id": item.position_id,
                    "reason": item.reason,
                }
                for item in self.management
            ],
        }


class CandleSource(Protocol):
    """Read-only candle history; it has no order-submitting capability."""

    def fetch_candles(
        self, exact_symbol: str, timeframe: str, count: int
    ) -> tuple[Candle, ...]: ...


@dataclass(frozen=True, slots=True)
class StrategySettings:
    """One :class:`~tgxm.indicator.IndicatorSettings` bound to the traded market.

    The rule parameters stay in the ``indicator`` configuration section so the
    Pine script, ``tgxm predict``, and this worker cannot drift apart; only the
    symbol and timeframe come from the ``autotrade`` section.
    """

    symbol: str
    timeframe: str
    lookback_bars: int
    ema_fast_period: int
    ema_slow_period: int
    rsi_period: int
    rsi_overbought: int
    rsi_oversold: int
    atr_period: int
    atr_stop_loss_multiplier: float
    atr_take_profit_multipliers: tuple[float, ...]
    max_bar_age_multiplier: int


def strategy_settings(config: AppConfig) -> StrategySettings:
    indicator = config.indicator
    return StrategySettings(
        symbol=config.autotrade.broker_symbol,
        timeframe=config.autotrade.timeframe,
        lookback_bars=indicator.lookback_bars,
        ema_fast_period=indicator.ema_fast_period,
        ema_slow_period=indicator.ema_slow_period,
        rsi_period=indicator.rsi_period,
        rsi_overbought=indicator.rsi_overbought,
        rsi_oversold=indicator.rsi_oversold,
        atr_period=indicator.atr_period,
        atr_stop_loss_multiplier=indicator.atr_stop_loss_multiplier,
        atr_take_profit_multipliers=tuple(indicator.atr_take_profit_multipliers),
        max_bar_age_multiplier=indicator.max_bar_age_multiplier,
    )


def resolve_higher_timeframe(config: AppConfig) -> str | None:
    """Return the higher timeframe to filter with, or ``None`` when off."""

    autotrade = config.autotrade
    if not autotrade.require_higher_timeframe_agreement:
        return None
    if autotrade.higher_timeframe == "auto":
        return higher_timeframe(autotrade.timeframe)
    return autotrade.higher_timeframe


def closed_candles(
    candles: Sequence[Candle], *, timeframe: str, now_utc: datetime
) -> tuple[Candle, ...]:
    """Drop the bar that is still forming.

    MT5 always returns the current, incomplete bar alongside history.  A bar
    opening at ``T`` on an ``M``-minute timeframe is only closed once
    ``now >= T + M``; anything later than that is still being written and its
    high, low, and close can change, so a rule evaluated on it is not
    reproducible.
    """

    minutes = TIMEFRAME_MINUTES.get(str(timeframe))
    if minutes is None:
        raise IndicatorError(f"unsupported timeframe: {timeframe}")
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise IndicatorError("now_utc must be timezone-aware")
    latest_open = now_utc.astimezone(UTC) - timedelta(minutes=minutes)
    return tuple(candle for candle in candles if candle.time_utc <= latest_open)


def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    """Snap a computed price onto the broker's price grid."""

    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    steps = (value / tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return steps * tick_size


def signal_id_for(symbol: str, timeframe: str, bar_time_utc: datetime, side: str) -> str:
    """Key one signal to the exact bar that produced it.

    The store's unique constraint on this id is what makes a restart, a crash
    between submission and read-back, or a second worker unable to enter the
    same bar twice.
    """

    stamp = bar_time_utc.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{SIGNAL_PREFIX}-{symbol}-{timeframe}-{stamp}-{side}"


def client_reference_for(account_id: str, signal_id: str) -> str:
    """Broker comment that ties a position back to one durable intent."""

    material = f"{account_id}|{signal_id}".encode("utf-8")
    return "tgxa-" + hashlib.sha256(material).hexdigest()[:20]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AutoTrader:
    """Evaluates one market on a fixed timeframe and manages what it opened.

    ``demo_active`` is a volatile runtime gate: like the Telegram runtime, a
    persisted configuration alone can never submit an order.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        broker: BrokerAdapter,
        candle_source: CandleSource,
        position_manager: PositionManager | None = None,
        demo_active: bool = False,
        magic: int = AUTOTRADE_MAGIC,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config.validate()
        self.store = store
        self.broker = broker
        self.candle_source = candle_source
        self.position_manager = position_manager
        self.demo_active = bool(demo_active)
        self.magic = int(magic)
        self.clock = clock
        if self.magic < 0:
            raise ValueError("magic must be non-negative")
        if self.demo_active and not self.config.autotrade.trade_enabled:
            raise ValueError("demo_active requires autotrade.trade_enabled")
        self.settings = strategy_settings(self.config)
        self.higher_timeframe = resolve_higher_timeframe(self.config)
        self._evaluated_bar: datetime | None = None

    # -- reading -----------------------------------------------------------

    def _fetch_closed(self, timeframe: str, now: datetime) -> tuple[Candle, ...]:
        # One extra bar so dropping the forming one still leaves a full window.
        candles = self.candle_source.fetch_candles(
            self.settings.symbol, timeframe, self.settings.lookback_bars + 1
        )
        return closed_candles(candles, timeframe=timeframe, now_utc=now)

    def _higher_timeframe_trend(self, now: datetime) -> TrendState | None:
        if self.higher_timeframe is None:
            return None
        candles = self._fetch_closed(self.higher_timeframe, now)
        return trend_state(
            candles,
            ema_fast_period=self.settings.ema_fast_period,
            ema_slow_period=self.settings.ema_slow_period,
        )

    def _owned_positions(self) -> tuple[PositionSnapshot, ...]:
        positions = self.broker.list_open_positions(self.settings.symbol)
        return tuple(
            position for position in positions if position.magic == self.magic
        )

    def _foreign_positions(self) -> tuple[PositionSnapshot, ...]:
        positions = self.broker.list_open_positions(self.settings.symbol)
        return tuple(
            position for position in positions if position.magic != self.magic
        )

    def _strategy_intents(self) -> list[OrderIntentRecord]:
        prefix = f"{SIGNAL_PREFIX}-{self.settings.symbol}-{self.settings.timeframe}-"
        return [
            intent
            for intent in self.store.list_order_intents()
            if intent.signal_id.startswith(prefix)
        ]

    # -- management --------------------------------------------------------

    def _intent_for_position(
        self, position: PositionSnapshot
    ) -> OrderIntentRecord | None:
        for intent in self._strategy_intents():
            if intent.client_reference == position.comment:
                return intent
        return None

    def reconcile_closed_positions(self) -> tuple[ManagementOutcome, ...]:
        """Close the books on intents whose broker position is gone.

        The broker-side Stop Loss and Take Profit are the primary exits, and
        they fire without telling this worker.  Reading broker state before
        deciding anything - rather than assuming the store is still accurate -
        is what the ``idempotency-and-reconciliation`` rule asks for.  Only an
        intent that names an exact broker position is reconciled: an ambiguous
        submission has no position id and stays locked for a human.
        """

        live_ids = {
            position.position_id
            for position in self.broker.list_open_positions(self.settings.symbol)
        }
        outcomes: list[ManagementOutcome] = []
        for intent in self._strategy_intents():
            if intent.status not in {IntentStatus.OPEN, IntentStatus.PARTIAL_OPEN}:
                continue
            position_id = intent.broker_position_id
            if position_id is None or position_id in live_ids:
                continue
            self.store.transition_order_intent(
                intent.id,
                IntentStatus.CLOSED,
                expected_status=intent.status,
                detail={"closed_by": "broker_side_protection_or_operator"},
            )
            outcomes.append(
                ManagementOutcome(
                    action=ManagementAction.RECONCILED_CLOSED,
                    position_id=position_id,
                    reason="broker no longer reports this position as open",
                )
            )
        return tuple(outcomes)

    def manage_positions(
        self, candles: Sequence[Candle], tick: TickSnapshot
    ) -> tuple[ManagementOutcome, ...]:
        """Apply the Pine script's two in-trade rules to owned positions.

        Both rules are configuration-gated and both re-prove ownership at the
        broker before acting.  Neither can widen risk: the breakeven rule only
        moves a Stop Loss to the entry price, and the reversal rule closes.
        """

        autotrade = self.config.autotrade
        manager = self.position_manager
        if manager is None or not candles:
            return ()
        if not self.demo_active:
            # Managing a position is a broker mutation.  It needs the same
            # volatile activation as opening one; an unactivated run leaves the
            # broker-side Stop Loss and Take Profit doing their job untouched.
            return ()
        if not (
            autotrade.close_on_opposite_crossover
            or autotrade.move_stop_to_breakeven_after_tp1
        ):
            return ()

        cross = crossover_state(
            candles,
            ema_fast_period=self.settings.ema_fast_period,
            ema_slow_period=self.settings.ema_slow_period,
        )
        outcomes: list[ManagementOutcome] = []
        for position in self._owned_positions():
            intent = self._intent_for_position(position)
            if intent is None:
                # A position tagged with the bot's magic but with no durable
                # intent is unexplained; it is reported, never managed.
                outcomes.append(
                    ManagementOutcome(
                        action=ManagementAction.FAILED,
                        position_id=position.position_id,
                        reason="no durable intent matches this position",
                    )
                )
                continue
            reversed_now = (
                autotrade.close_on_opposite_crossover
                and (
                    (position.side == "BUY" and cross is CrossoverState.DOWN)
                    or (position.side == "SELL" and cross is CrossoverState.UP)
                )
            )
            if reversed_now:
                outcomes.append(self._close_on_reversal(manager, position, intent))
                continue
            if autotrade.move_stop_to_breakeven_after_tp1:
                outcome = self._maybe_move_to_breakeven(
                    manager, position, intent, candles, tick
                )
                if outcome is not None:
                    outcomes.append(outcome)
        return tuple(outcomes)

    def _close_on_reversal(
        self,
        manager: PositionManager,
        position: PositionSnapshot,
        intent: OrderIntentRecord,
    ) -> ManagementOutcome:
        try:
            result = manager.close_position(
                position,
                expected_magic=self.magic,
                expected_client_reference=intent.client_reference,
                deviation_points=self.config.execution.deviation_points,
            )
        except (BrokerError, ValueError) as exc:
            return ManagementOutcome(
                action=ManagementAction.FAILED,
                position_id=position.position_id,
                reason=f"close rejected: {exc}",
            )
        if result.outcome is BrokerOutcome.ACCEPTED:
            self.store.transition_order_intent(
                intent.id,
                IntentStatus.CLOSED,
                expected_status=intent.status,
                detail={"closed_by": "opposite_crossover"},
            )
            return ManagementOutcome(
                action=ManagementAction.CLOSED_ON_REVERSAL,
                position_id=position.position_id,
                reason="EMA crossed back against the open position",
            )
        if result.outcome is BrokerOutcome.RECONCILE_REQUIRED:
            self.store.transition_order_intent(
                intent.id,
                IntentStatus.RECONCILE_REQUIRED,
                expected_status=intent.status,
                error_code=result.retcode,
                error_message="close result is ambiguous and must be reconciled",
            )
            return ManagementOutcome(
                action=ManagementAction.CLOSE_UNCONFIRMED,
                position_id=position.position_id,
                reason="close result is ambiguous; no second attempt is made",
            )
        return ManagementOutcome(
            action=ManagementAction.FAILED,
            position_id=position.position_id,
            reason=f"broker rejected the close: {result.comment}",
        )

    def _maybe_move_to_breakeven(
        self,
        manager: PositionManager,
        position: PositionSnapshot,
        intent: OrderIntentRecord,
        candles: Sequence[Candle],
        tick: TickSnapshot,
    ) -> ManagementOutcome | None:
        first_target = intent.request_metadata.get("first_take_profit")
        entry_bar = intent.request_metadata.get("bar_time_utc")
        if first_target is None or entry_bar is None:
            return None
        target = Decimal(str(first_target))
        entry_bar_time = datetime.fromisoformat(str(entry_bar)).astimezone(UTC)
        breakeven = position.price_open
        if position.stop_loss is not None and position.stop_loss == breakeven:
            return None
        # Only bars strictly after the signal bar count: the signal bar's own
        # range predates the entry and would fake an immediate trigger.
        after_entry = [
            candle for candle in candles if candle.time_utc > entry_bar_time
        ]
        if position.side == "BUY":
            reached = tick.bid >= target or any(
                candle.high >= target for candle in after_entry
            )
        else:
            reached = tick.ask <= target or any(
                candle.low <= target for candle in after_entry
            )
        if not reached:
            return None
        try:
            manager.modify_position_protection(
                position,
                stop_loss=breakeven,
                take_profit=position.take_profit,
                expected_magic=self.magic,
                expected_client_reference=intent.client_reference,
            )
        except (BrokerError, ValueError) as exc:
            return ManagementOutcome(
                action=ManagementAction.FAILED,
                position_id=position.position_id,
                reason=f"breakeven stop rejected: {exc}",
            )
        return ManagementOutcome(
            action=ManagementAction.BREAKEVEN_APPLIED,
            position_id=position.position_id,
            reason="first take profit reached; stop moved to the entry price",
        )

    # -- entry -------------------------------------------------------------

    def run_cycle(self) -> AutoTradeDecision:
        """Evaluate the latest closed bar once and act on it."""

        autotrade = self.config.autotrade
        symbol_name = self.settings.symbol
        timeframe = self.settings.timeframe
        if not autotrade.enabled:
            return AutoTradeDecision(
                status=AutoTradeStatus.DISABLED,
                reason="autotrade.enabled is false",
                symbol=symbol_name,
                timeframe=timeframe,
            )

        now = self.clock()
        account = self.broker.discover_account()
        symbol = self.broker.discover_symbol(symbol_name)
        tick = self.broker.get_tick(symbol_name)
        candles = self._fetch_closed(timeframe, now)
        trend = self._higher_timeframe_trend(now)
        # Reconcile first: a position the broker's own Stop Loss or Take Profit
        # closed must not still look open to the rules below.
        management = self.reconcile_closed_positions() + self.manage_positions(
            candles, tick
        )

        if not candles:
            return AutoTradeDecision(
                status=AutoTradeStatus.NO_SIGNAL,
                reason="no closed candle is available yet",
                symbol=symbol_name,
                timeframe=timeframe,
                higher_timeframe_trend=trend,
                management=management,
            )
        bar_time = candles[-1].time_utc
        prediction = predict(
            candles, self.settings, now_utc=now, higher_timeframe_trend=trend
        )
        def decision(
            status: AutoTradeStatus, reason: str, **extra: Any
        ) -> AutoTradeDecision:
            return AutoTradeDecision(
                status=status,
                reason=reason,
                symbol=symbol_name,
                timeframe=timeframe,
                bar_time_utc=bar_time,
                prediction=prediction,
                higher_timeframe_trend=trend,
                management=management,
                **extra,
            )

        if prediction.state is PredictionState.NO_SIGNAL:
            self._evaluated_bar = bar_time
            return decision(AutoTradeStatus.NO_SIGNAL, prediction.reason)
        if self._evaluated_bar == bar_time:
            return decision(
                AutoTradeStatus.BAR_ALREADY_EVALUATED,
                "this closed bar was already acted on in this process",
            )
        if not autotrade.trade_enabled:
            self._evaluated_bar = bar_time
            return decision(
                AutoTradeStatus.TRADE_DISABLED, "autotrade.trade_enabled is false"
            )
        if not self.demo_active:
            self._evaluated_bar = bar_time
            return decision(
                AutoTradeStatus.DEMO_NOT_ACTIVE,
                "Demo submission was not activated for this process run",
            )

        blocked = self._entry_blocked(bar_time, symbol, tick)
        if blocked is not None:
            self._evaluated_bar = bar_time
            status, reason = blocked
            return decision(status, reason)

        self._evaluated_bar = bar_time
        return self._submit(account, symbol, prediction, bar_time, management, decision)

    def _entry_blocked(
        self, bar_time: datetime, symbol: SymbolSnapshot, tick: TickSnapshot
    ) -> tuple[AutoTradeStatus, str] | None:
        autotrade = self.config.autotrade
        spread_points = (tick.ask - tick.bid) / symbol.point
        if spread_points > Decimal(autotrade.max_spread_points):
            return (
                AutoTradeStatus.SPREAD_BLOCKED,
                f"spread {spread_points} points exceeds the configured limit",
            )
        if self.config.risk.manual_exposure_policy == "block":
            foreign = self._foreign_positions()
            if foreign:
                return (
                    AutoTradeStatus.MANUAL_EXPOSURE_BLOCKED,
                    "another position on this symbol is not owned by this worker",
                )
        owned = self._owned_positions()
        if len(owned) >= autotrade.max_open_positions:
            return (
                AutoTradeStatus.EXPOSURE_BLOCKED,
                f"{len(owned)} owned position(s) already open on {symbol.symbol}",
            )

        intents = self._strategy_intents()
        minutes = TIMEFRAME_MINUTES[self.settings.timeframe]
        if autotrade.cooldown_bars > 0:
            cooldown_start = bar_time - timedelta(
                minutes=minutes * autotrade.cooldown_bars
            )
            for intent in intents:
                previous = intent.request_metadata.get("bar_time_utc")
                if previous is None:
                    continue
                previous_bar = datetime.fromisoformat(str(previous)).astimezone(UTC)
                if cooldown_start <= previous_bar < bar_time:
                    return (
                        AutoTradeStatus.COOLDOWN,
                        f"an entry was taken within the last {autotrade.cooldown_bars} bars",
                    )
        today = self.clock().astimezone(UTC).date()
        today_count = sum(
            1
            for intent in intents
            if intent.created_at_utc.astimezone(UTC).date() == today
        )
        if today_count >= autotrade.max_trades_per_day:
            return (
                AutoTradeStatus.DAILY_LIMIT_REACHED,
                f"{today_count} entries already taken today",
            )
        return None

    def _submit(
        self,
        account: AccountSnapshot,
        symbol: SymbolSnapshot,
        prediction: Prediction,
        bar_time: datetime,
        management: tuple[ManagementOutcome, ...],
        decision: Callable[..., AutoTradeDecision],
    ) -> AutoTradeDecision:
        autotrade = self.config.autotrade
        side = prediction.state.value
        try:
            volume = fixed_volume(self.config.risk)
        except PolicyError as exc:
            return decision(AutoTradeStatus.BROKER_REJECTED, str(exc))
        assert prediction.stop_loss is not None
        stop_loss = quantize_price(prediction.stop_loss, symbol.tick_size)
        targets = tuple(
            quantize_price(value, symbol.tick_size) for value in prediction.take_profits
        )
        take_profit = targets[autotrade.take_profit_index - 1]
        signal_id = signal_id_for(
            self.settings.symbol, self.settings.timeframe, bar_time, side
        )
        reference = client_reference_for(account.login, signal_id)
        metadata: Mapping[str, Any] = {
            "source": "indicator_strategy",
            "rule": "ema_rsi_atr_advisory",
            "timeframe": self.settings.timeframe,
            "higher_timeframe": self.higher_timeframe,
            "bar_time_utc": bar_time.isoformat(),
            "reference_price": str(prediction.reference_price),
            "first_take_profit": str(targets[0]),
            "take_profit_index": autotrade.take_profit_index,
            "all_take_profits": [str(value) for value in targets],
            "indicator_values": {
                name: str(value) for name, value in prediction.indicator_values.items()
            },
            "execution_adapter": "mt5",
        }
        request = MarketOrderRequest(
            account_id=account.login,
            signal_id=signal_id,
            leg_index=0,
            symbol=symbol.symbol,
            side=side,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_reference=reference,
            magic=self.magic,
            deviation_points=self.config.execution.deviation_points,
            max_spread_points=Decimal(autotrade.max_spread_points),
        )
        append = self.store.create_order_intent(
            OrderIntent(
                account_id=account.login,
                signal_id=signal_id,
                signal_revision=0,
                leg_index=0,
                broker_symbol=symbol.symbol,
                side=side,
                volume=volume,
                stop_loss=stop_loss,
                take_profit=take_profit,
                entry_price=prediction.reference_price,
                expected_risk=None,
                client_reference=reference,
                request_metadata=metadata,
            )
        )
        intent = append.record
        if not append.created:
            return decision(
                AutoTradeStatus.INTENT_EXISTS,
                f"durable intent already exists in state {intent.status.value}",
                signal_id=signal_id,
                intent_id=intent.id,
            )

        try:
            check = self.broker.check_market_order(request)
        except (BrokerError, ValueError) as exc:
            self.store.transition_order_intent(
                intent.id,
                IntentStatus.BROKER_REJECTED,
                expected_status=IntentStatus.INTENT_PERSISTED,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            return decision(
                AutoTradeStatus.BROKER_REJECTED,
                str(exc),
                signal_id=signal_id,
                intent_id=intent.id,
            )
        if check.outcome is not BrokerOutcome.CHECK_PASSED:
            self.store.transition_order_intent(
                intent.id,
                IntentStatus.BROKER_REJECTED,
                expected_status=IntentStatus.INTENT_PERSISTED,
                error_code=check.retcode,
                error_message=check.comment,
            )
            return decision(
                AutoTradeStatus.BROKER_REJECTED,
                f"broker order_check did not pass: {check.comment}",
                signal_id=signal_id,
                intent_id=intent.id,
                broker_result=check,
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
            # Once SUBMITTING is durable the outcome is ambiguous; this path
            # never retries, exactly like the Telegram engine.
            self.store.transition_order_intent(
                intent.id,
                IntentStatus.RECONCILE_REQUIRED,
                expected_status=IntentStatus.SUBMITTING,
                error_code=type(exc).__name__,
                error_message="broker submission raised; reconciliation required",
            )
            return decision(
                AutoTradeStatus.RECONCILE_REQUIRED,
                "broker submission raised after SUBMITTING; no retry is permitted",
                signal_id=signal_id,
                intent_id=intent.id,
            )

        position: PositionSnapshot | None = None
        readback_error: str | None = None
        if sent.accepted:
            try:
                position = self.broker.read_back_market_order(request, sent)
            except (BrokerError, ValueError) as exc:
                readback_error = str(exc)

        if sent.outcome is BrokerOutcome.REJECTED:
            status, intent_status = (
                AutoTradeStatus.BROKER_REJECTED,
                IntentStatus.BROKER_REJECTED,
            )
        elif position is not None:
            intent_status = (
                IntentStatus.PARTIAL_OPEN
                if sent.outcome is BrokerOutcome.PARTIAL
                else IntentStatus.OPEN
            )
            status = (
                AutoTradeStatus.PARTIAL_OPEN
                if intent_status is IntentStatus.PARTIAL_OPEN
                else AutoTradeStatus.OPEN
            )
        else:
            intent_status = IntentStatus.RECONCILE_REQUIRED
            status = (
                AutoTradeStatus.RECONCILE_REQUIRED
                if sent.outcome is BrokerOutcome.RECONCILE_REQUIRED
                else AutoTradeStatus.SUBMITTED_UNVERIFIED
            )
        updated = self.store.transition_order_intent(
            intent.id,
            intent_status,
            expected_status=IntentStatus.SUBMITTING,
            broker_order_id=sent.order_id,
            broker_deal_id=sent.deal_id,
            broker_position_id=None if position is None else position.position_id,
            error_code=sent.retcode if intent_status is not IntentStatus.OPEN else None,
            error_message=(
                sent.comment
                if intent_status is IntentStatus.BROKER_REJECTED
                else (
                    None
                    if intent_status in {IntentStatus.OPEN, IntentStatus.PARTIAL_OPEN}
                    else readback_error
                    or "broker-side protection read-back is required"
                )
            ),
            detail={"broker_outcome": sent.outcome.value},
        )
        return decision(
            status,
            (
                "broker rejected the request"
                if intent_status is IntentStatus.BROKER_REJECTED
                else (
                    "protected broker position verified by exact read-back"
                    if intent_status in {IntentStatus.OPEN, IntentStatus.PARTIAL_OPEN}
                    else "submission is locked pending broker order/position read-back"
                )
            ),
            signal_id=signal_id,
            intent_id=updated.id,
            broker_result=sent,
        )


__all__ = [
    "AUTOTRADE_MAGIC",
    "AutoTradeDecision",
    "AutoTradeStatus",
    "AutoTrader",
    "CandleSource",
    "ManagementAction",
    "ManagementOutcome",
    "SIGNAL_PREFIX",
    "StrategySettings",
    "client_reference_for",
    "closed_candles",
    "quantize_price",
    "resolve_higher_timeframe",
    "signal_id_for",
    "strategy_settings",
]
