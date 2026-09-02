from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tgxm.broker import AccountNotAllowlisted, BrokerUnavailableError, DemoAccountPolicy
from tgxm.indicator import Candle
from tgxm.indicator_feed import IndicatorDataError, MetaTrader5CandleSource


def policy(**overrides: object) -> DemoAccountPolicy:
    defaults: dict[str, object] = {
        "allowed_demo_accounts": frozenset({"10001"}),
        "allowed_servers": frozenset({"XM-Demo"}),
        "allowed_symbols": frozenset({"GOLD"}),
        "max_tick_age_seconds": 5,
    }
    defaults.update(overrides)
    return DemoAccountPolicy(**defaults)


class FakeMT5Module:
    """Minimal fake mirroring the constants tests/test_webtrader_broker.py relies on."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_MARGIN_MODE_EXCHANGE = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_FOK = 1
    SYMBOL_TRADE_EXECUTION_INSTANT = 1
    SYMBOL_ORDER_MARKET = 1
    SYMBOL_ORDER_SL = 16
    SYMBOL_ORDER_TP = 32
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440

    def __init__(self) -> None:
        self.connected = True
        self.rows: list[dict[str, object]] = []
        self.copy_rates_calls: list[tuple[str, int, int, int]] = []

    def initialize(self, path: str | None = None) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(connected=self.connected, trade_allowed=False, tradeapi_disabled=True)

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            login=10001,
            server="XM-Demo",
            company="XM",
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
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
            order_mode=(self.SYMBOL_ORDER_MARKET | self.SYMBOL_ORDER_SL | self.SYMBOL_ORDER_TP),
        )

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int):
        self.copy_rates_calls.append((symbol, timeframe, start, count))
        return self.rows


def _row(time: int, o: float, h: float, low: float, c: float) -> dict[str, object]:
    return {"time": time, "open": o, "high": h, "low": low, "close": c}


def test_fetch_candles_converts_rows_and_verifies_demo_identity() -> None:
    module = FakeMT5Module()
    module.rows = [
        _row(1_700_000_000, 100.0, 100.5, 99.5, 100.2),
        _row(1_700_000_900, 100.2, 100.8, 100.0, 100.6),
    ]
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    candles = source.fetch_candles("GOLD", "M15", 2)

    assert candles == (
        Candle(
            time_utc=datetime.fromtimestamp(1_700_000_000, tz=UTC),
            open=Decimal("100.0"),
            high=Decimal("100.5"),
            low=Decimal("99.5"),
            close=Decimal("100.2"),
        ),
        Candle(
            time_utc=datetime.fromtimestamp(1_700_000_900, tz=UTC),
            open=Decimal("100.2"),
            high=Decimal("100.8"),
            low=Decimal("100.0"),
            close=Decimal("100.6"),
        ),
    )
    assert module.copy_rates_calls == [("GOLD", module.TIMEFRAME_M15, 0, 2)]


def test_fetch_candles_rejects_non_demo_account() -> None:
    module = FakeMT5Module()
    module.rows = [_row(1_700_000_000, 100.0, 100.5, 99.5, 100.2)]
    live_policy = policy(allowed_demo_accounts=frozenset({"99999"}))
    source = MetaTrader5CandleSource(policy=live_policy, mt5_module=module)

    with pytest.raises(AccountNotAllowlisted):
        source.fetch_candles("GOLD", "M15", 1)


def test_fetch_candles_rejects_unknown_symbol() -> None:
    module = FakeMT5Module()
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    with pytest.raises(Exception, match="not exactly allowlisted"):
        source.fetch_candles("GOLD2", "M15", 1)


def test_fetch_candles_raises_on_empty_history() -> None:
    module = FakeMT5Module()
    module.rows = []
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    with pytest.raises(IndicatorDataError, match="no candles"):
        source.fetch_candles("GOLD", "M15", 5)


def test_fetch_candles_raises_when_copy_rates_returns_none() -> None:
    module = FakeMT5Module()

    def copy_rates_from_pos(symbol, timeframe, start, count):
        return None

    module.copy_rates_from_pos = copy_rates_from_pos  # type: ignore[assignment]
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    with pytest.raises(IndicatorDataError, match="copy_rates_from_pos failed"):
        source.fetch_candles("GOLD", "M15", 5)


def test_fetch_candles_rejects_non_positive_count() -> None:
    module = FakeMT5Module()
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    with pytest.raises(ValueError, match="positive"):
        source.fetch_candles("GOLD", "M15", 0)


def test_unsupported_timeframe_fails_closed() -> None:
    module = FakeMT5Module()
    module.rows = [_row(1_700_000_000, 100.0, 100.5, 99.5, 100.2)]
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    with pytest.raises(IndicatorDataError, match="unsupported timeframe"):
        source.fetch_candles("GOLD", "M2", 1)


def test_disconnected_terminal_fails_closed() -> None:
    module = FakeMT5Module()
    module.connected = False
    source = MetaTrader5CandleSource(policy=policy(), mt5_module=module)

    with pytest.raises(BrokerUnavailableError):
        source.fetch_candles("GOLD", "M15", 1)
