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
instrument is on screen, and its higher-timeframe filter picks a sensible
higher timeframe for whatever chart you're on (1m → 15m, 15m → 1H, 30m → 4H
by default; see below) — the same settings work whether you're looking at
XAUUSD on H1 or a random altcoin on a 1-minute chart.

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

## Closed bars only: what you saw live is what you see after a reload

Two rules make the chart reproducible, so a marker or a filter decision never
changes after the fact:

- **A BUY/SELL exists only once the bar has closed.** The crossover, RSI and
  higher-timeframe checks are gated on `barstate.isconfirmed`, which is
  always true on historical bars and true only on the final update of the
  live bar. A cross that shows up mid-bar and unwinds before the close
  therefore never draws a marker, never changes the tracked state, and never
  fires an entry alert. This is the same closed-bar rule `tgxm predict` /
  `tgxm autotrade` evaluate from closed MT5 candles.
- **The higher-timeframe trend is read from the last *closed*
  higher-timeframe bar**, on historical and live bars alike, using
  TradingView's documented non-repainting form
  (`request.security(..., expression[1], lookahead = barmerge.lookahead_on)`).
  Without it, historical bars would judge closed higher-timeframe bars while
  the live bar judged the one still forming, so a filter decision made live
  could differ from what the chart shows after a reload. The cost is that the
  `HTF trend` row lags one higher-timeframe bar (on a 3m chart, up to 15
  minutes) behind the raw higher-timeframe EMAs.

The EMA lines, the background colour, and the `Bias`/`RSI`/`ATR` rows are
still live values of the forming bar, like any indicator; only the signals,
the tracked trade, and the higher-timeframe judgement are closed-bar.

## Two confirmation filters (both on by default)

- **RSI band** — a crossover only confirms if RSI is between 50 and
  Overbought (for BUY) or between Oversold and 50 (for SELL). Declines a
  crossover that happens right at an already-extended move.
- **Higher-timeframe trend agreement** — a BUY only confirms if a higher
  timeframe's own EMA fast/slow relationship is also bullish, mirrored for
  SELL. This is the change most likely to reduce false signals: it declines
  a crossover that fights the bigger trend. Each filter has its own
  Settings toggle; turn either off independently to see its effect.

  Which higher timeframe is used is chosen by **"Higher timeframe comes
  from"** in Settings (group *Higher-timeframe filter*), and the result is
  always shown live in the table's `HTF source` row as `chart → HTF (mode)`:

  | Mode | Higher timeframe used |
  |---|---|
  | **Preset ladder** (default) | The next standard timeframe a trader normally checks, keyed on the chart: 1m/2m/3m → **15m**, 5m/10m/15m → **1H**, 30m/45m/1H → **4H**, 2H–4H → **1D**, 1D → **1W**, 1W → **1M**. Seconds charts, anything not on the ladder (1M, 6H/8H/12H), and any rung that would not be strictly above the chart (a 7-day chart, say) fall back to the multiplier. |
  | **Multiplier (x current chart)** | Chart timeframe × "Multiplier" (default 4): 1m → 4m, 15m → 1H, 30m → 2H, 1H → 4H, 1D → 4D. When the exact product is not a timeframe TradingView supports it is rounded **up** to the next one that is (8H × 4 = 32H → 2D), capped at 12 months. |
  | **Fixed timeframe** | Always the timeframe picked in "Fixed timeframe" (default 1H), whatever chart you are on. |

  Whatever the mode, the chosen timeframe must be **strictly higher than the
  chart's**. If it is not (for example Fixed = 1H while you're looking at an
  H1 or H4 chart), the indicator refuses to use it: the `HTF trend` row turns
  **orange** and says `invalid: 1H is not above chart`, and while "Require
  higher-timeframe trend agreement" is on the `HTF source` row adds
  `FILTER BLOCKS ALL SIGNALS` in orange and **no BUY/SELL can fire** until
  you pick a higher timeframe, switch mode, or turn the filter off (with the
  filter off that row just adds `filter off` in gray). It never silently
  falls back to a same-or-lower timeframe, which `request.security` cannot
  evaluate meaningfully.

The background is tinted **green** while this chart's EMAs and the higher
timeframe agree upward, **red** while they agree downward, and left clear
while they disagree — the clear stretch is exactly where a crossover would
be declined by the higher-timeframe filter.

## Trade management after entry (TP1 → breakeven → TP2)

Once a BUY/SELL confirms, the indicator keeps tracking it bar by bar, not
just at entry:

- **TP1 hit** → the label becomes `BUY — TP1 HIT`, the `State` row becomes
  `BUY (TP1_HIT)`, `Take Profit 1` gains `(HIT)`, and the Stop Loss moves to
  breakeven (the entry price; `Stop Loss` shows `(breakeven)`) — turn off
  "Move Stop Loss to breakeven after TP1" in Settings to keep the original
  ATR-based Stop Loss instead. The Stop Loss line on the chart moves to show
  this live.
- **TP2 hit** or **Stop Loss hit** (original, or breakeven after TP1) closes
  the trade. By default ("Clear marker immediately when a trade closes") the
  marker and lines disappear right away and `State` returns to `NO_SIGNAL` —
  the outcome is what the "target hit"/"stopped out" alerts are for, not
  something left sitting on the chart. Turn that setting off to instead see
  a frozen `BUY — TARGET HIT` (teal) / `BUY — STOPPED` (gray) label, and
  `BUY (TARGET_HIT)` / `BUY (STOPPED)` in the `State` row, until the next
  fresh signal replaces it.
- The `Caution` row says `YES — weakening` (orange) whenever price is on the
  wrong side of the fast EMA while still in the trade — an early warning that
  momentum is fading, before the slow EMA cross that would fully cancel it.
  It clears itself once price is back on the right side.
- A raw reverse crossover on the chart's own timeframe (fast EMA crossing
  back through the slow one at a bar close, filters or not) cancels an `OPEN`
  trade outright: the marker and lines are removed and `State` returns to
  `NO_SIGNAL`. After TP1 the trade is instead left to run to breakeven or
  TP2.
- Once Stop Loss/TP2 closes the trade, the reversal rule no longer applies
  to it — a closed trade stays closed until the next fresh signal, it never
  comes back.
- Same-bar ambiguity: if one bar's range touches both Stop Loss and a
  Take-Profit level, this checks Stop Loss first, then TP2, then TP1 — a
  conservative assumption, not a real fill simulation. On a fast-moving bar
  the actual outcome could differ.
- Stop Loss / Take Profit touches are detected from the bar's high/low as
  the bar forms; a touch cannot un-happen, so unlike an entry this does not
  wait for the close. The signal bar itself is not checked, and the entry
  price is that bar's close, which a real fill would not get — this is an
  advisory, not a fill simulation.

Separate alerts exist for each step (TP1 hit, target hit, stopped out), on
top of the entry alerts; see "Setting a real-time alert" below.

## Reading the table

| Row | What it tells you |
|---|---|
| `Symbol` | Ticker and chart timeframe the values below belong to. |
| `State` | `NO_SIGNAL`, or `BUY (OPEN)` / `SELL (OPEN)`, `… (TP1_HIT)`, and — only with "Clear marker immediately" off — `… (TARGET_HIT)` / `… (STOPPED)`. Changes only on a confirmed signal, a tracked exit, or a reverse crossover at a bar close. |
| `Caution` | `YES — weakening` (orange) while price is on the wrong side of the fast EMA during a trade; otherwise `no`. |
| `Bias` | The raw EMA relationship on this chart, live: `EMA fast > slow` / `EMA fast < slow`, or `warming up`. |
| `HTF trend` | The higher timeframe's own EMA relationship from its last closed bar: `bullish (15m)` / `bearish (15m)`, `warming up (15m)`, or orange `invalid: … is not above chart`. |
| `Entry` / `Stop Loss` / `Take Profit 1` / `Take Profit 2` | The live levels of the trade being tracked; `-` when there is none. |
| `RSI` / `ATR` | Current values on this chart. |
| `Bars loaded` | Bars available; orange while the slow EMA is still warming up. |
| `HTF source` | Which higher timeframe is in use and where it came from, e.g. `3m → 15m (ladder)`; adds `filter off` or `FILTER BLOCKS ALL SIGNALS`. |

`Bias` and `HTF trend` always show the raw relationship on their own
timeframe, filters or not. `BUY (OPEN)` while `Bias` says `EMA fast < slow`
can only be seen mid-bar (the live EMAs have crossed back but the bar has
not closed yet); if it survives the close it's a bug. `BUY (TP1_HIT)` with
`Bias` against it, or any `BUY` while `HTF trend` says bearish, can happen
and is normal: after TP1 only Stop Loss/TP2 end the trade, and the higher
timeframe never cancels one.

## "I don't see any BUY/SELL marker"

That's a normal state, not a bug, and the table says which of these it is:

- **`Bias` says `warming up` / `Bars loaded` is orange** — `ta.ema` with the
  default 50-period slow EMA needs 50 bars of history before it is defined
  at all; before that, a crossover cannot be evaluated. Load more history
  (zoom out, or switch to a timeframe with a longer available history). The
  higher timeframe has its own warm-up: `HTF trend` says `warming up (…)`
  until its slow EMA is defined, and no signal can pass until it is.
- **`HTF trend` is orange and `HTF source` says `FILTER BLOCKS ALL SIGNALS`**
  — the higher timeframe you picked is not above the chart's timeframe
  (typically Fixed mode after switching to a higher chart). Pick a higher
  one, switch to Preset ladder/Multiplier, or turn the higher-timeframe
  filter off. Signals are deliberately blocked rather than evaluated
  against a meaningless timeframe.
- **A small gray triangle appears at a spot with no colored BUY/SELL
  label** — turn on "Show raw crossovers a filter blocked (debug)" in
  Settings first; it's off by default. It marks a closed bar where an EMA
  crossover *did* happen, but RSI or the higher-timeframe trend blocked it
  — the filters working as intended, not a bug.
- **Turn off "Require RSI confirmation" and/or "Require higher-timeframe
  trend agreement"** to see raw EMA crossovers as full BUY/SELL signals,
  unfiltered. Useful for checking whether the rule is finding crossovers at
  all before deciding a filter is too strict for your taste.
- **The bar hasn't closed yet** — a cross you can see forming on the live
  bar draws nothing until that bar closes; see "Closed bars only" above.
- Otherwise: **`State` is `NO_SIGNAL`** — the fast EMA genuinely has not
  crossed the slow EMA at a bar close since you added the indicator. A
  signal only fires the *instant* that relationship flips, so an
  already-crossed pair produces nothing until it crosses back.

## Setting a real-time alert

Click the clock/alarm icon on the chart toolbar → **Add Alert** → set
**Condition** to this indicator → pick one of "Advisory BUY (entry)",
"Advisory SELL (entry)", "Advisory TP1 hit", "Advisory target hit",
"Advisory stopped out" (add one alert per condition you want, per
symbol/chart) → set **Trigger**:

- The two **entry** conditions are only ever true on the closing update of a
  bar (see "Closed bars only"), so "Once Per Bar" and "Once Per Bar Close"
  behave the same for them; **Once Per Bar Close** remains the recommended,
  unambiguous choice. Either way the alert fires the instant the rule becomes
  true on a newly closed bar, going forward from when you add it. It cannot
  predict a price outcome.
- The three **exit** conditions become true on the first update in which the
  bar's high/low touches the level. **Once Per Bar** gives the earliest
  warning; **Once Per Bar Close** waits for the bar to finish. Both report
  the same touch.

The alert message carries `{{ticker}}` and `{{close}}` — the close at the
moment the alert fires, not the exact level touched.

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
