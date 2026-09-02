# Pine Script companion indicator

`ema_rsi_atr_advisory.pine` is a **view-only** TradingView indicator. It
expresses the same core rule as `tgxm predict`
([src/tgxm/indicator.py](../src/tgxm/indicator.py)) — EMA fast/slow crossover,
with ATR-based Stop-Loss/Take-Profit — but runs directly on a TradingView
chart using TradingView's own data feed instead of MT5. Two optional filters
are layered on top to cut down false signals: an RSI band, and agreement with
a higher-timeframe trend.

Not tied to GOLD or any one symbol: apply it to any chart TradingView can
show. The ATR-based Stop-Loss/Take-Profit distances scale to whatever
instrument is on screen, and its higher-timeframe filter auto-scales to a
multiple of whatever chart timeframe you're on (see below) — the same
settings work whether you're looking at XAUUSD on H1 or a random altcoin on
a 1-minute chart.

## Load it

1. Open any chart on TradingView.
2. Open **Pine Editor** (bottom panel) → **New** → **Blank indicator**.
3. Replace the template with the contents of `ema_rsi_atr_advisory.pine`.
4. Click **Add to Chart** — it applies to whatever symbol/timeframe the
   current chart is on, and keeps working if you switch symbols without
   re-adding it.

## Only the current signal, not history

The chart only ever shows **one** BUY/SELL marker and one set of Stop-Loss/
Take-Profit lines: the most recent one. Older markers are deleted the moment
a new signal replaces them, and the Stop-Loss/Take-Profit lines are rays that
start at the signal bar and extend forward. The status table (top-right)
always shows the current state.

## Two confirmation filters (both on by default)

- **RSI band** — a crossover only confirms if RSI is between 50 and
  Overbought (for BUY) or between Oversold and 50 (for SELL). Declines a
  crossover that happens right at an already-extended move.
- **Higher-timeframe trend agreement** — a BUY only confirms if a higher
  timeframe's own EMA fast/slow relationship is also bullish, mirrored for
  SELL. This is the change most likely to reduce false signals: it declines
  a crossover that fights the bigger trend. Each filter has its own
  Settings toggle; turn either off independently to see its effect.

  The higher timeframe is not fixed — it is computed as **your current
  chart's timeframe × "Higher-timeframe multiplier"** (default 4), shown
  live in the table's `Auto HTF` row. On an H1 chart that's H4; on a
  1-minute chart that's 4 minutes; on Daily that's 4 days. This is what
  makes the filter meaningful on any symbol/timeframe combination without
  retuning it by hand every time you switch charts.

## Trade management after entry (TP1 → breakeven → TP2)

Once a BUY/SELL confirms, the indicator keeps tracking it bar by bar, not
just at entry:

- **TP1 hit** → label switches to "TP1 HIT", and Stop Loss moves to
  breakeven (the entry price) — turn off "Move Stop Loss to breakeven after
  TP1" in Settings to keep the original ATR-based Stop Loss instead. The Stop
  Loss line on the chart moves to show this live.
- **TP2 hit** or **Stop Loss hit** (original, or breakeven after TP1) closes
  the trade. By default ("Clear marker immediately when a trade closes") the
  marker and lines disappear right away and `State` returns to `NO_SIGNAL` —
  the outcome is what the "target hit"/"stopped out" alerts are for, not
  something left sitting on the chart. Turn that setting off to instead see
  a frozen "TARGET HIT"/"STOPPED" label until the next fresh signal replaces
  it.
- **Caution** row turns "YES" whenever price closes back on the wrong side
  of the fast EMA while still in the trade — an early warning that momentum
  is fading, before the slow EMA cross that would fully invalidate/cancel
  it. It clears itself automatically once price closes back on the right
  side.
- Once Stop Loss/TP2 closes the trade, the EMA-reversal invalidation rule
  no longer applies to it — a closed trade stays closed until the next
  fresh signal, it never comes back.
- Same-bar ambiguity: if one bar's range touches both Stop Loss and a
  Take-Profit level, this checks Stop Loss first, then TP2, then TP1 — a
  conservative assumption, not a real fill simulation. On a fast-moving bar
  the actual outcome could differ.

Separate alerts exist for each step (TP1 hit, target hit, stopped out), on
top of the entry alerts — set all of them to **Once Per Bar Close** for the
same repainting reason as the entry alerts, described below.

## "State" vs "Bias"/"HTF trend" can legitimately differ

`State` only changes on a *confirmed* signal (passed both filters) or when
the raw EMA relationship that produced it reverses. `Bias` and `HTF trend`
always show the current raw relationship on their own timeframe, filters or
not. So `State: BUY` while `Bias: EMA fast < slow` should never happen — if
you see that, it's a bug; but `State: BUY` while `HTF trend: bearish` can
happen briefly (state hasn't been invalidated by an unfiltered reversal on
the chart's own timeframe yet) and is normal.

## "I don't see any BUY/SELL marker"

That's a normal state (`NO_SIGNAL`), not a bug, and the table (top-right)
and chart now explain why:

- **`Bars loaded` is small / `Bias` says "warming up"** — `ta.ema` with the
  default 50-period slow EMA needs 50 bars of history before it is defined
  at all; before that, a crossover cannot be evaluated. Load more history
  (zoom out, or switch to a timeframe with a longer available history).
- **A small gray triangle appears at a spot with no colored BUY/SELL
  label** — turn on "Show raw crossovers a filter blocked (debug)" in
  Settings first; it's off by default. It marks a bar where an EMA
  crossover *did* happen, but RSI or the higher-timeframe trend blocked it
  — the filters working as intended, not a bug.
- **Turn off "Require RSI confirmation" and/or "Require higher-timeframe
  trend agreement"** to see raw EMA crossovers as full BUY/SELL signals,
  unfiltered. Useful for checking whether the rule is finding crossovers at
  all before deciding a filter is too strict for your taste.
- Otherwise: the fast EMA genuinely has not crossed the slow EMA since you
  added the indicator. `Bias` in the table shows which side it is
  currently on — a signal only fires the instant that relationship flips
  and, by default, both filters agree.

## Setting a real-time alert

Click the clock/alarm icon on the chart toolbar → **Add Alert** → set
**Condition** to this indicator → pick one of "Advisory BUY (entry)",
"...SELL (entry)", "...TP1 hit", "...target hit", or "...stopped out" (add
one alert per condition you want, per symbol/chart) → set **Trigger** to
**Once Per Bar Close**, not "Once Per Bar".

This rule is computed from `ta.ema`/`ta.rsi`/`ta.atr` on the still-forming
live bar, so a signal can appear and then vanish before that bar actually
closes (a well-known Pine behavior called repainting). "Once Per Bar Close"
waits for the bar to finish before alerting, matching exactly what
`tgxm predict` evaluates from already-closed MT5 candles. It cannot predict a
price outcome; it reacts the instant its rule becomes true on each newly
closed bar, going forward from when you add it.

## What it is not

- It is declared with `indicator()`, not `strategy()`: it cannot place,
  modify, or close an order on TradingView or anywhere else.
- It does not read or write anything in this repository's Telegram-to-XM
  pipeline, database, or configuration. It is a second, unrelated front end
  for the same advisory rule as `tgxm predict`; see "Advisory technical
  indicator" in [CONTEXT.md](../CONTEXT.md).
- Unlike the Python implementation, **this file has not been executed or
  compile-checked** — there is no Pine Script compiler in this environment.
  TradingView's Pine Editor will report any syntax error the first time you
  add it to a chart; if it does, share the error and it can be fixed.
