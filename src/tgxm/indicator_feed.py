"""Read-only MT5 historical-candle source for the advisory indicator.

This module can never submit an order: it exposes only ``initialize``,
``shutdown``, ``discover_account``, and ``fetch_candles``.  It reuses the same
exact-allowlisted Demo :class:`~tgxm.broker.DemoAccountPolicy` as the
execution broker so ``tgxm predict`` output always reflects the one verified,
Demo-only MT5 connection this project trusts; see the ``trading-safety`` and
``external-trading-boundary`` rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tgxm.broker import AccountSnapshot, BrokerUnavailableError, DemoAccountPolicy, _decimal_from_broker
from tgxm.indicator import Candle, TIMEFRAME_MINUTES
from tgxm.webtrader_broker import _MetaTrader5ReadOnlyOracle


class IndicatorDataError(BrokerUnavailableError):
    """Raised when MT5 cannot supply usable historical candles."""


_MT5_TIMEFRAME_ATTRS: dict[str, str] = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}
if set(_MT5_TIMEFRAME_ATTRS) != set(TIMEFRAME_MINUTES):
    raise AssertionError("MT5 timeframe map has drifted from tgxm.indicator.TIMEFRAME_MINUTES")


class _MetaTrader5CandleOracle(_MetaTrader5ReadOnlyOracle):
    """Adds bar-history retrieval to the shared read-only MT5 oracle.

    Reuses :class:`tgxm.webtrader_broker._MetaTrader5ReadOnlyOracle` so a
    disabled Expert-Advisor/API-trading permission (the normal state for a
    terminal used only to read prices) does not block this advisory feature;
    only ``trading-safety``'s Demo/account/connectivity checks still apply.
    """

    def fetch_rows(self, exact_symbol: str, timeframe: str, count: int) -> Any:
        mt5 = self._ensure_initialized()
        self.discover_symbol(exact_symbol)
        attr = _MT5_TIMEFRAME_ATTRS.get(timeframe)
        if attr is None:
            raise IndicatorDataError(f"unsupported timeframe: {timeframe}")
        timeframe_constant = getattr(mt5, attr, None)
        if timeframe_constant is None:
            raise BrokerUnavailableError(f"MT5 {attr} constant is unavailable")
        rows = mt5.copy_rates_from_pos(exact_symbol, timeframe_constant, 0, count)
        if rows is None:
            raise IndicatorDataError(f"MT5 copy_rates_from_pos failed: {self._last_error()}")
        return rows


class MetaTrader5CandleSource:
    """Narrow read-only MT5 facade used only to build advisory predictions.

    Unlike :class:`~tgxm.webtrader_broker.MetaTrader5ReadOnlyVerifier`, this
    object has no order-check or position-listing methods at all: it cannot
    be mistaken for anything that participates in order execution.
    """

    def __init__(
        self,
        *,
        policy: DemoAccountPolicy,
        terminal_path: str | None = None,
        mt5_module: Any | None = None,
        server_utc_offset_minutes: int = 0,
    ) -> None:
        """``server_utc_offset_minutes`` converts MT5 bar times to true UTC.

        MT5 stamps bars with the *broker server's* clock.  A caller that
        compares bar times against real UTC - to tell a closed bar from the one
        still forming, or to judge how stale the newest bar is - must supply the
        measured offset; :meth:`tgxm.broker.MetaTrader5Broker.resolve_server_utc_offset`
        produces it.  ``0`` keeps the raw server reading, which is all
        ``tgxm predict`` needs to shape one printed suggestion.
        """

        self.policy = policy
        self.server_utc_offset_minutes = int(server_utc_offset_minutes)
        self._oracle = _MetaTrader5CandleOracle(
            policy=policy, terminal_path=terminal_path, mt5_module=mt5_module
        )

    def __enter__(self) -> MetaTrader5CandleSource:
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

    def fetch_candles(self, exact_symbol: str, timeframe: str, count: int) -> tuple[Candle, ...]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        # Re-verified on every call: this is a read path, not a cached handle.
        self._oracle.discover_account()
        rows = self._oracle.fetch_rows(exact_symbol, timeframe, count)
        if len(rows) == 0:
            raise IndicatorDataError(f"MT5 returned no candles for {exact_symbol} {timeframe}")
        offset_seconds = self.server_utc_offset_minutes * 60
        return tuple(
            Candle(
                time_utc=datetime.fromtimestamp(
                    int(row["time"]) - offset_seconds, tz=UTC
                ),
                open=_decimal_from_broker(row["open"], "open"),
                high=_decimal_from_broker(row["high"], "high"),
                low=_decimal_from_broker(row["low"], "low"),
                close=_decimal_from_broker(row["close"], "close"),
            )
            for row in rows
        )


__all__ = ["IndicatorDataError", "MetaTrader5CandleSource"]
