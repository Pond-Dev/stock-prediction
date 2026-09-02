from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tgxm.config import IndicatorConfig
from tgxm.indicator import (
    Candle,
    IndicatorError,
    PredictionState,
    TIMEFRAME_MINUTES,
    predict,
)


START = datetime(2026, 1, 1, tzinfo=UTC)

_SETTINGS = IndicatorConfig(
    symbol="GOLD",
    timeframe="M15",
    lookback_bars=60,
    ema_fast_period=4,
    ema_slow_period=9,
    rsi_period=4,
    rsi_overbought=70,
    rsi_oversold=30,
    atr_period=4,
    atr_stop_loss_multiplier=1.5,
    atr_take_profit_multipliers=(1.5, 3.0),
    max_bar_age_multiplier=3,
)


def _candles(closes: list[float], *, start: datetime = START, minutes: int = 15) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    previous_close = Decimal(str(closes[0]))
    for index, value in enumerate(closes):
        open_ = previous_close
        close = Decimal(str(value))
        high = max(open_, close) + Decimal("0.05")
        low = min(open_, close) - Decimal("0.05")
        candles.append(
            Candle(
                time_utc=start + timedelta(minutes=minutes * index),
                open=open_,
                high=high,
                low=low,
                close=close,
            )
        )
        previous_close = close
    return tuple(candles)


# Captured from a real run of predict() against this exact series; see the
# module docstring in tgxm.indicator for the formulas being cross-checked.
_BUY_CLOSES = [100, 100.3, 99.9, 100.2, 99.8, 100.1, 99.9, 100.2, 100.0, 99.9, 100.2]
_SELL_CLOSES = [100, 99.7, 100.1, 99.8, 100.2, 99.9, 100.1, 99.8, 100.0, 100.1, 99.8]


def test_buy_crossover_produces_atr_based_sl_and_tp() -> None:
    candles = _candles(_BUY_CLOSES)
    now = candles[-1].time_utc + timedelta(minutes=1)

    result = predict(candles, _SETTINGS, now_utc=now)

    assert result.state is PredictionState.BUY
    assert result.reason == "ema_crossover_confirmed_by_rsi"
    assert result.bars_used == len(candles)
    assert result.reference_price == Decimal("100.2")
    assert result.stop_loss == Decimal("99.675860595703125")
    assert result.take_profits == (
        Decimal("100.724139404296875"),
        Decimal("101.248278808593750"),
    )
    # SL below entry below both TPs, ascending: the BUY price-relationship invariant.
    assert result.stop_loss < result.reference_price < result.take_profits[0] < result.take_profits[1]
    assert result.indicator_values["rsi"] == Decimal("60.59315812655997650858904712")
    assert result.is_actionable


def test_sell_crossover_produces_atr_based_sl_and_tp() -> None:
    candles = _candles(_SELL_CLOSES)
    now = candles[-1].time_utc + timedelta(minutes=1)

    result = predict(candles, _SETTINGS, now_utc=now)

    assert result.state is PredictionState.SELL
    assert result.reference_price == Decimal("99.8")
    assert result.stop_loss == Decimal("100.324139404296875")
    assert result.take_profits == (
        Decimal("99.275860595703125"),
        Decimal("98.751721191406250"),
    )
    # SL above entry above both TPs, descending: the SELL price-relationship invariant.
    assert result.take_profits[1] < result.take_profits[0] < result.reference_price < result.stop_loss


def test_flat_market_never_signals() -> None:
    candles = _candles([100.0] * 15)
    now = candles[-1].time_utc + timedelta(minutes=1)

    result = predict(candles, _SETTINGS, now_utc=now)

    assert result.state is PredictionState.NO_SIGNAL
    assert not result.is_actionable
    assert result.stop_loss is None
    assert result.take_profits == ()


def test_insufficient_data_is_no_signal_not_an_error() -> None:
    candles = _candles([100, 100.1, 100.2])

    result = predict(candles, _SETTINGS, now_utc=candles[-1].time_utc)

    assert result.state is PredictionState.NO_SIGNAL
    assert result.reason.startswith("insufficient_data")
    assert result.bars_used == 3
    assert result.reference_price is None


def test_stale_last_candle_is_no_signal() -> None:
    candles = _candles(_BUY_CLOSES)
    # Far beyond timeframe_minutes(15) * max_bar_age_multiplier(3) = 45 minutes.
    now = candles[-1].time_utc + timedelta(hours=6)

    result = predict(candles, _SETTINGS, now_utc=now)

    assert result.state is PredictionState.NO_SIGNAL
    assert result.reason.startswith("stale_data")
    assert result.reference_price == candles[-1].close


def test_rsi_filter_blocks_an_overextended_crossover() -> None:
    closes = [100, 100.3, 99.9, 100.2, 99.8, 100.1, 99.9, 100.2, 100.0, 99.9, 101.5]
    candles = _candles(closes)
    now = candles[-1].time_utc + timedelta(minutes=1)

    result = predict(candles, _SETTINGS, now_utc=now)

    assert result.state is PredictionState.NO_SIGNAL
    assert result.reason.startswith("rsi_filter_blocked")
    assert "bullish band" in result.reason


def test_out_of_order_candles_raise_indicator_error() -> None:
    candles = list(_candles(_BUY_CLOSES))
    candles[-1], candles[-2] = candles[-2], candles[-1]

    with pytest.raises(IndicatorError, match="ascending"):
        predict(tuple(candles), _SETTINGS, now_utc=START + timedelta(days=1))


def test_ema_fast_period_must_be_less_than_slow() -> None:
    bad_settings = IndicatorConfig(
        ema_fast_period=10, ema_slow_period=10, atr_take_profit_multipliers=(1.0,)
    )
    candles = _candles(_BUY_CLOSES)

    with pytest.raises(IndicatorError, match="ema_fast_period"):
        predict(candles, bad_settings, now_utc=candles[-1].time_utc)


def test_unsupported_timeframe_raises_indicator_error() -> None:
    bad_settings = IndicatorConfig(timeframe="M2")
    candles = _candles(_BUY_CLOSES)

    with pytest.raises(IndicatorError, match="unsupported timeframe"):
        predict(candles, bad_settings, now_utc=candles[-1].time_utc)


def test_naive_now_utc_is_rejected() -> None:
    candles = _candles(_BUY_CLOSES)

    with pytest.raises(IndicatorError, match="timezone-aware"):
        predict(candles, _SETTINGS, now_utc=datetime(2026, 1, 1))


def test_candle_rejects_non_positive_and_naive_input() -> None:
    with pytest.raises(IndicatorError, match="timezone-aware"):
        Candle(
            time_utc=datetime(2026, 1, 1),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
        )
    with pytest.raises(IndicatorError, match="positive"):
        Candle(
            time_utc=START,
            open=Decimal(0),
            high=Decimal(1),
            low=Decimal(0),
            close=Decimal(1),
        )
    with pytest.raises(IndicatorError, match="high must not be less than low"):
        Candle(
            time_utc=START,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(2),
            close=Decimal(1),
        )


def test_timeframe_minutes_matches_the_supported_set() -> None:
    assert set(TIMEFRAME_MINUTES) == {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
    assert TIMEFRAME_MINUTES["M15"] == 15
    assert TIMEFRAME_MINUTES["H4"] == 240
