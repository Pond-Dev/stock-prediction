"""Deterministic, rule-based technical indicator: advisory only.

This module never contacts Telegram or a broker execution path.  It
consumes already-fetched historical candles and returns a :class:`Prediction`
for a human to read before deciding anything.  Nothing produced here is a
``CanonicalSignal`` or an ``Order Intent``; per the ``signal-authority`` rule
only a versioned allowlisted Channel Profile and a deterministic parse may
produce a signal the bot can act on.  ``tgxm predict`` never persists this
output and never calls a broker adapter's mutation methods.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol


class IndicatorError(ValueError):
    """Raised when candle input or settings are structurally invalid."""


class PredictionState(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NO_SIGNAL = "NO_SIGNAL"


#: Minutes per supported timeframe.  Kept independent from any broker-specific
#: constant table so this module has no MT5/Telegram dependency of its own.
TIMEFRAME_MINUTES: Mapping[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


#: Pine `ladderRung`: the higher timeframe a trader normally checks next, keyed
#: on the chart's own timeframe (1m/2m/3m -> 15m, 5m/10m/15m -> 1H,
#: 30m/45m/1H -> 4H, 2H-4H -> 1D).  Only rungs this module can evaluate appear
#: here, so ``D1`` has no entry: its Pine rung (1W) is not a supported
#: timeframe and a caller must not silently fall back to a lower one.
HIGHER_TIMEFRAME_LADDER: Mapping[str, str] = {
    "M1": "M15",
    "M5": "H1",
    "M15": "H1",
    "M30": "H4",
    "H1": "H4",
    "H4": "D1",
}


class TrendState(StrEnum):
    """Direction of the EMA pair on one timeframe, or ``UNKNOWN``.

    ``UNKNOWN`` is not "no opinion, carry on": it means the timeframe could not
    be judged (too little history, or the two EMAs are exactly equal).  A
    caller using this as a filter must treat it as a block, the way the Pine
    script blocks every signal when its higher timeframe is invalid.
    """

    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class CrossoverState(StrEnum):
    """Raw EMA cross on the latest candle, before any filter.

    This is Pine's `ta.crossover`/`ta.crossunder` on its own.  The entry rule
    adds the RSI and higher-timeframe filters on top; the exit rule ("close
    when the EMAs cross back") deliberately does not, exactly as the Pine
    script's `reversed` branch ignores them.
    """

    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"


def crossover_state(
    candles: Sequence[Candle],
    *,
    ema_fast_period: int,
    ema_slow_period: int,
) -> CrossoverState:
    """Report whether the EMA pair crossed on the last candle of ``candles``."""

    fast_period = int(ema_fast_period)
    slow_period = int(ema_slow_period)
    if min(fast_period, slow_period) < 1:
        raise IndicatorError("indicator periods must be positive")
    if fast_period >= slow_period:
        raise IndicatorError("ema_fast_period must be less than ema_slow_period")
    if len(candles) < 2:
        return CrossoverState.NONE
    _validate_candle_order(candles)
    closes = [candle.close for candle in candles]
    fast = _ema_series(closes, fast_period)
    slow = _ema_series(closes, slow_period)
    fast_now, slow_now = fast[-1], slow[-1]
    fast_previous, slow_previous = fast[-2], slow[-2]
    if None in (fast_now, slow_now, fast_previous, slow_previous):
        return CrossoverState.NONE
    assert fast_now is not None and slow_now is not None
    assert fast_previous is not None and slow_previous is not None
    if fast_previous <= slow_previous and fast_now > slow_now:
        return CrossoverState.UP
    if fast_previous >= slow_previous and fast_now < slow_now:
        return CrossoverState.DOWN
    return CrossoverState.NONE


def higher_timeframe(timeframe: str) -> str:
    """Return the Pine preset ladder's higher timeframe for ``timeframe``."""

    rung = HIGHER_TIMEFRAME_LADDER.get(str(timeframe))
    if rung is None:
        raise IndicatorError(
            f"no supported higher timeframe for {timeframe}; choose one explicitly"
        )
    return rung


def trend_state(
    candles: Sequence[Candle],
    *,
    ema_fast_period: int,
    ema_slow_period: int,
) -> TrendState:
    """Judge one timeframe by its EMA pair, as Pine's `htfBullish`/`htfBearish`.

    Returns :attr:`TrendState.UNKNOWN` rather than guessing when the history is
    too short to define both EMAs or the two are exactly equal.
    """

    fast_period = int(ema_fast_period)
    slow_period = int(ema_slow_period)
    if min(fast_period, slow_period) < 1:
        raise IndicatorError("indicator periods must be positive")
    if fast_period >= slow_period:
        raise IndicatorError("ema_fast_period must be less than ema_slow_period")
    if not candles:
        return TrendState.UNKNOWN
    _validate_candle_order(candles)
    closes = [candle.close for candle in candles]
    fast = _ema_series(closes, fast_period)[-1]
    slow = _ema_series(closes, slow_period)[-1]
    if fast is None or slow is None or fast == slow:
        return TrendState.UNKNOWN
    return TrendState.UP if fast > slow else TrendState.DOWN


class IndicatorSettings(Protocol):
    """Structural settings shape; :class:`tgxm.config.IndicatorConfig` satisfies it."""

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


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLC bar.  Validates its own static shape at construction."""

    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise IndicatorError("candle time_utc must be timezone-aware")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise IndicatorError(f"candle {name} must be a positive finite decimal")
        if self.high < self.low:
            raise IndicatorError("candle high must not be less than low")
        if not self.low <= self.open <= self.high:
            raise IndicatorError("candle open must be within [low, high]")
        if not self.low <= self.close <= self.high:
            raise IndicatorError("candle close must be within [low, high]")


@dataclass(frozen=True, slots=True)
class Prediction:
    """Advisory outcome.  ``NO_SIGNAL`` is a normal result, never an error."""

    state: PredictionState
    reason: str
    generated_at_utc: datetime
    symbol: str
    timeframe: str
    bars_used: int
    reference_price: Decimal | None
    stop_loss: Decimal | None
    take_profits: tuple[Decimal, ...]
    indicator_values: Mapping[str, Decimal]

    @property
    def is_actionable(self) -> bool:
        return self.state is not PredictionState.NO_SIGNAL


def _to_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise IndicatorError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IndicatorError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise IndicatorError(f"{name} must be a finite decimal")
    return result


def _validate_candle_order(candles: Sequence[Candle]) -> None:
    for previous, current in zip(candles, candles[1:]):
        if current.time_utc <= previous.time_utc:
            raise IndicatorError("candles must be strictly ascending by time_utc")


def _ema_series(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Standard EMA.  ``series[i]`` is defined once ``i >= period - 1``."""

    if period < 1:
        raise IndicatorError("ema period must be positive")
    series: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period:
        return series
    multiplier = Decimal(2) / Decimal(period + 1)
    previous = sum(closes[:period], Decimal(0)) / Decimal(period)
    series[period - 1] = previous
    for index in range(period, len(closes)):
        previous = (closes[index] - previous) * multiplier + previous
        series[index] = previous
    return series


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    relative_strength = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + relative_strength))


def _rsi_series(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Wilder-smoothed RSI.  ``series[i]`` is defined once ``i >= period``."""

    if period < 1:
        raise IndicatorError("rsi period must be positive")
    series: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return series
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in zip(closes, closes[1:]):
        change = current - previous
        gains.append(change if change > 0 else Decimal(0))
        losses.append(-change if change < 0 else Decimal(0))
    avg_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
    series[period] = _rsi_from_averages(avg_gain, avg_loss)
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + losses[index]) / Decimal(period)
        series[index + 1] = _rsi_from_averages(avg_gain, avg_loss)
    return series


def _true_range(current: Candle, previous: Candle) -> Decimal:
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def _atr_series(candles: Sequence[Candle], period: int) -> list[Decimal | None]:
    """Wilder-smoothed ATR.  ``series[i]`` is defined once ``i >= period``."""

    if period < 1:
        raise IndicatorError("atr period must be positive")
    series: list[Decimal | None] = [None] * len(candles)
    if len(candles) <= period:
        return series
    ranges = [_true_range(current, previous) for previous, current in zip(candles, candles[1:])]
    previous = sum(ranges[:period], Decimal(0)) / Decimal(period)
    series[period] = previous
    for index in range(period, len(ranges)):
        previous = (previous * (period - 1) + ranges[index]) / Decimal(period)
        series[index + 1] = previous
    return series


def _empty_prediction(
    *, state: PredictionState, reason: str, now: datetime, settings: IndicatorSettings,
    bars_used: int, reference_price: Decimal | None,
    indicator_values: Mapping[str, Decimal],
) -> Prediction:
    return Prediction(
        state=state,
        reason=reason,
        generated_at_utc=now,
        symbol=str(settings.symbol),
        timeframe=str(settings.timeframe),
        bars_used=bars_used,
        reference_price=reference_price,
        stop_loss=None,
        take_profits=(),
        indicator_values=indicator_values,
    )


def predict(
    candles: Sequence[Candle],
    settings: IndicatorSettings,
    *,
    now_utc: datetime | None = None,
    higher_timeframe_trend: TrendState | None = None,
) -> Prediction:
    """Evaluate one deterministic EMA-crossover + RSI + ATR rule set.

    ``higher_timeframe_trend`` is the optional higher-timeframe filter from the
    Pine script.  Left at ``None`` the filter is off, exactly as the Pine
    script behaves with ``useHtfFilter`` unticked.  Supplied, it must agree
    with the crossover direction; :attr:`TrendState.UNKNOWN` blocks every
    signal because an unjudgeable higher timeframe is not agreement.

    This never raises for a "no trade" outcome; it returns
    ``PredictionState.NO_SIGNAL`` with a human-readable ``reason`` so a caller
    always has something safe to display.  It raises :class:`IndicatorError`
    only for structurally invalid input (out-of-order candles, non-positive
    settings), which a correct data-fetch boundary should never produce.
    """

    now = now_utc if now_utc is not None else datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise IndicatorError("now_utc must be timezone-aware")

    ema_fast_period = int(settings.ema_fast_period)
    ema_slow_period = int(settings.ema_slow_period)
    rsi_period = int(settings.rsi_period)
    atr_period = int(settings.atr_period)
    if min(ema_fast_period, ema_slow_period, rsi_period, atr_period) < 1:
        raise IndicatorError("indicator periods must be positive")
    if ema_fast_period >= ema_slow_period:
        raise IndicatorError("ema_fast_period must be less than ema_slow_period")

    timeframe = str(settings.timeframe)
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise IndicatorError(f"unsupported timeframe: {timeframe}")

    # +1 beyond each indicator's own warm-up so both the latest and the prior
    # bar are defined; the crossover check needs two consecutive EMA points.
    required_bars = max(ema_slow_period, rsi_period + 1, atr_period + 1) + 1
    if len(candles) < required_bars:
        return _empty_prediction(
            state=PredictionState.NO_SIGNAL,
            reason=(
                f"insufficient_data: need at least {required_bars} candles, got "
                f"{len(candles)}"
            ),
            now=now,
            settings=settings,
            bars_used=len(candles),
            reference_price=None,
            indicator_values={},
        )

    _validate_candle_order(candles)

    last = candles[-1]
    max_age_minutes = minutes * int(settings.max_bar_age_multiplier)
    age_minutes = (now - last.time_utc).total_seconds() / 60
    if age_minutes < -1 or age_minutes > max_age_minutes:
        return _empty_prediction(
            state=PredictionState.NO_SIGNAL,
            reason=(
                f"stale_data: last candle is {age_minutes:.1f} minutes old, "
                f"limit is {max_age_minutes}"
            ),
            now=now,
            settings=settings,
            bars_used=len(candles),
            reference_price=last.close,
            indicator_values={},
        )

    closes = [candle.close for candle in candles]
    ema_fast = _ema_series(closes, ema_fast_period)
    ema_slow = _ema_series(closes, ema_slow_period)
    rsi = _rsi_series(closes, rsi_period)
    atr = _atr_series(candles, atr_period)

    fast_now, slow_now = ema_fast[-1], ema_slow[-1]
    fast_prev, slow_prev = ema_fast[-2], ema_slow[-2]
    rsi_now = rsi[-1]
    atr_now = atr[-1]
    # Guaranteed non-None by required_bars; asserts document the invariant.
    assert fast_now is not None and slow_now is not None
    assert fast_prev is not None and slow_prev is not None
    assert rsi_now is not None and atr_now is not None

    indicator_values: Mapping[str, Decimal] = {
        "ema_fast": fast_now,
        "ema_slow": slow_now,
        "rsi": rsi_now,
        "atr": atr_now,
    }

    def no_signal(reason: str) -> Prediction:
        return _empty_prediction(
            state=PredictionState.NO_SIGNAL,
            reason=reason,
            now=now,
            settings=settings,
            bars_used=len(candles),
            reference_price=last.close,
            indicator_values=indicator_values,
        )

    if atr_now <= 0:
        return no_signal("degenerate_volatility: ATR is not positive")

    crossed_up = fast_prev <= slow_prev and fast_now > slow_now
    crossed_down = fast_prev >= slow_prev and fast_now < slow_now
    if not crossed_up and not crossed_down:
        return no_signal("no_crossover: EMA fast/slow did not cross on the latest candle")

    rsi_overbought = Decimal(int(settings.rsi_overbought))
    rsi_oversold = Decimal(int(settings.rsi_oversold))

    if crossed_up:
        if not (Decimal(50) < rsi_now < rsi_overbought):
            return no_signal(
                f"rsi_filter_blocked: RSI {rsi_now:.2f} is outside the bullish "
                f"band (50, {rsi_overbought})"
            )
        side = PredictionState.BUY
        required_trend = TrendState.UP
    else:
        if not (rsi_oversold < rsi_now < Decimal(50)):
            return no_signal(
                f"rsi_filter_blocked: RSI {rsi_now:.2f} is outside the bearish "
                f"band ({rsi_oversold}, 50)"
            )
        side = PredictionState.SELL
        required_trend = TrendState.DOWN

    if higher_timeframe_trend is not None and higher_timeframe_trend is not required_trend:
        return no_signal(
            f"higher_timeframe_filter_blocked: higher timeframe is "
            f"{TrendState(higher_timeframe_trend).value}, {side.value} needs "
            f"{required_trend.value}"
        )

    sl_multiplier = _to_decimal(settings.atr_stop_loss_multiplier, "atr_stop_loss_multiplier")
    if sl_multiplier <= 0:
        raise IndicatorError("atr_stop_loss_multiplier must be positive")
    tp_multipliers = [
        _to_decimal(value, "atr_take_profit_multipliers[]")
        for value in settings.atr_take_profit_multipliers
    ]
    if not tp_multipliers:
        raise IndicatorError("atr_take_profit_multipliers must not be empty")
    if any(value <= 0 for value in tp_multipliers):
        raise IndicatorError("atr_take_profit_multipliers must all be positive")
    if tp_multipliers != sorted(set(tp_multipliers)):
        raise IndicatorError("atr_take_profit_multipliers must be strictly ascending")

    entry = last.close
    if side is PredictionState.BUY:
        stop_loss = entry - atr_now * sl_multiplier
        take_profits = tuple(entry + atr_now * multiplier for multiplier in tp_multipliers)
        valid = stop_loss > 0 and stop_loss < entry and all(tp > entry for tp in take_profits)
    else:
        stop_loss = entry + atr_now * sl_multiplier
        take_profits = tuple(entry - atr_now * multiplier for multiplier in tp_multipliers)
        valid = stop_loss > entry and all(0 < tp < entry for tp in take_profits)
    if not valid:
        return no_signal("invalid_price_relationship: computed SL/TP failed a sanity check")

    return Prediction(
        state=side,
        reason="ema_crossover_confirmed_by_rsi",
        generated_at_utc=now,
        symbol=str(settings.symbol),
        timeframe=timeframe,
        bars_used=len(candles),
        reference_price=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
        indicator_values=indicator_values,
    )


__all__ = [
    "Candle",
    "CrossoverState",
    "HIGHER_TIMEFRAME_LADDER",
    "IndicatorError",
    "IndicatorSettings",
    "Prediction",
    "PredictionState",
    "TIMEFRAME_MINUTES",
    "TrendState",
    "crossover_state",
    "higher_timeframe",
    "predict",
    "trend_state",
]
