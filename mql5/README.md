# MQL5 Expert Advisor

`TgxmEmaRsiAtrDemo.mq5` runs the EMA/RSI/ATR rule set **inside the MetaTrader 5
terminal**. It is a third front end for the same rule that
[`pine/ema_rsi_atr_advisory.pine`](../pine/ema_rsi_atr_advisory.pine) draws on a
TradingView chart and that the Python worker
([src/tgxm/autotrader.py](../src/tgxm/autotrader.py)) trades from outside the
terminal — the difference is only where it runs.

| | Pine indicator | Python `tgxm autotrade` | This EA |
| --- | --- | --- | --- |
| Runs in | TradingView chart | a Python process beside MT5 | the MT5 terminal itself |
| Places orders | never | yes, via the MT5 Python API | yes, natively |
| Needs a process running | no | yes | no, the terminal hosts it |
| Durable order intents in SQLite | no | yes | no, it uses MT5 deal history |

## The rule

Identical to the Pine script, evaluated only on **closed** bars:

1. EMA fast crosses EMA slow (up → BUY, down → SELL).
2. RSI confirms: `50 < RSI < RsiOverbought` for a BUY, `RsiOversold < RSI < 50`
   for a SELL.
3. A higher timeframe agrees. `HigherTimeframe = PERIOD_CURRENT` follows the
   Pine preset ladder (M1 → M15, M5/M15 → H1, M30/H1 → H4, H2–H4 → D1,
   D1 → W1, W1 → MN1). A higher timeframe that cannot be judged blocks the
   entry; it never passes it.

Protection is measured from the signal bar's close, as the Pine script draws
it: `SL = close ∓ ATR × AtrStopLossMult`, and the order's Take Profit is the
second ATR target. The first target is the breakeven trigger, derived at
runtime as a fraction of the distance to the order's own Take Profit, so the
rule survives a restart with no saved state and stays correct whichever way
that target was set.

`TakeProfitMoney` replaces the ATR target with a fixed profit in account
currency (`0` keeps the ATR target). The price distance is measured with
`OrderCalcProfit`, not with `SYMBOL_TRADE_TICK_VALUE`: that field disagrees
with the terminal's own profit calculation on some servers - MetaQuotes-Demo
reports `0.1` for XAUUSD where the calculation gives `1.0` - and trusting it
would place the target ten times too far away. The Stop Loss stays ATR-based
either way, so **check that the money target is actually larger than the ATR
stop before using it**; nothing in the EA forces a positive risk-to-reward
ratio.

While a position is open:

- price reaches the first target → the Stop Loss moves to the entry price;
- the EMAs cross back → the position is closed.

## Install

The compiled `TgxmEmaRsiAtrDemo.ex5` must live in the terminal's
`MQL5\Experts` folder. In MetaTrader 5, `File → Open Data Folder` opens the
right directory.

```powershell
$data = "$env:APPDATA\MetaQuotes\Terminal\<your-terminal-id>\MQL5\Experts"
Copy-Item mql5\TgxmEmaRsiAtrDemo.mq5 $data
```

Then compile it: open the file in MetaEditor (F4 from the terminal) and press
F7, or from PowerShell:

```powershell
& "C:\Program Files\MetaTrader 5\MetaEditor64.exe" /compile:"$data\TgxmEmaRsiAtrDemo.mq5" /log
```

Refresh the Navigator panel in the terminal (right-click → Refresh) and the EA
appears under **Expert Advisors**.

## Run

1. Turn on the **Algo Trading** button in the terminal toolbar.
2. Open a chart of the symbol and timeframe you want (for example XAUUSD M1).
3. Drag `TgxmEmaRsiAtrDemo` onto that chart.
4. In the Common tab, make sure **Allow algorithmic trading** is ticked, review
   the Inputs, and press OK.

A smiling face in the top-right corner of the chart means it is running. Its
messages appear in the terminal's **Experts** tab; orders appear in **Trade**.

Steps 2-4 can be done for you at startup instead, with
[`startup-xauusd-m1.ini`](startup-xauusd-m1.ini). The terminal reads that
section only while starting, so it has to be closed first:

```powershell
(Get-Process terminal64).CloseMainWindow(); Start-Sleep 6
& "C:\Program Files\MetaTrader 5\terminal64.exe" /config:"d:\workspace\stock\mql5\startup-xauusd-m1.ini"
```

The file holds no login or password: the terminal reconnects with the account
it already has saved.

**The chart's symbol and timeframe are the strategy's symbol and timeframe.**
Moving it to another chart trades that market instead.

Before running it live, the MT5 **Strategy Tester** (`View → Strategy Tester`)
replays it over history on the same account, which is the quickest way to see
how often the rule actually fires on your symbol.

## Limits it keeps

- **Demo accounts only.** With `DemoAccountsOnly` on (the default) it refuses
  to initialise on any account that is not a Demo account, and
  `AllowedAccountLogin` can pin it to one exact login.
- One fixed lot under `HardLotCap`, rounded **down** onto the broker's volume
  step so it can never be rounded up past the cap.
- A numeric Stop Loss on every entry. A protective price the broker would
  reject cancels the entry; it is never dropped to make the order pass.
- `MaxOpenPositions` own positions on the symbol, a `CooldownBars` wait after
  an entry, and `MaxTradesPerDay` entries per server-time day. The cooldown and
  the daily count are read back from deal history, so restarting the terminal
  does not reset either.
- With `BlockOnForeignTrades` on, it will not open anything while a position it
  does not own is open on that symbol.
- Its own `MagicNumber`. It can only ever modify or close positions carrying
  that number, so a manual trade — or the Python worker's, which uses a
  different one — is never touched.

Because the two implementations use different magic numbers and both block on
foreign positions, running the Python worker and this EA on the same symbol at
the same time is safe but pointless: each will simply wait for the other's
position to close. Pick one.

## Verification status

This EA compiles clean (`0 errors, 0 warnings`) and uses the terminal's own
`iMA`, `iRSI`, and `iATR`, which implement the same EMA and Wilder-smoothed
RSI/ATR definitions the Pine and Python versions use. It has **no automated
test coverage**: the repository's `pytest` suite covers the Python
implementation of the rule, not this one. Treat the Strategy Tester and a
period of watched Demo running as its verification, and read
[.claude/rules/verification/risk-matched-verification.md](../.claude/rules/verification/risk-matched-verification.md)
before changing it.
