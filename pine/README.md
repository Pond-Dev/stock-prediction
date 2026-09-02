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
  always shown live in the table's `ตัวกรอง TF ใหญ่` row as
  `chart → HTF (mode)`:

  | Mode | Higher timeframe used |
  |---|---|
  | **Preset ladder** (default) | The next standard timeframe a trader normally checks, keyed on the chart: 1m/2m/3m → **15m**, 5m/10m/15m → **1H**, 30m/45m/1H → **4H**, 2H–4H → **1D**, 1D → **1W**, 1W → **1M**. Seconds charts, anything not on the ladder (1M, 6H/8H/12H), and any rung that would not be strictly above the chart (a 7-day chart, say) fall back to the multiplier. |
  | **Multiplier (x current chart)** | Chart timeframe × "Multiplier" (default 4): 1m → 4m, 15m → 1H, 30m → 2H, 1H → 4H, 1D → 4D. When the exact product is not a timeframe TradingView supports it is rounded **up** to the next one that is (8H × 4 = 32H → 2D), capped at 12 months. |
  | **Fixed timeframe** | Always the timeframe picked in "Fixed timeframe" (default 1H), whatever chart you are on. |

  Whatever the mode, the chosen timeframe must be **strictly higher than the
  chart's**. If it is not (for example Fixed = 1H while you're looking at an
  H1 or H4 chart), the indicator refuses to use it: the `เทรนด์ TF ใหญ่` row
  turns **orange**, and while "Require higher-timeframe trend agreement" is on
  the `ตอนนี้`/`ควรทำ` rows say so in orange, the `ตัวกรอง TF ใหญ่` row adds
  "บล็อกทุกสัญญาณ", and **no BUY/SELL can fire** until you pick a higher
  timeframe, switch mode, or turn the filter off (with the filter off that row
  just says "ปิดตัวกรองอยู่" in gray). It never silently falls
  back to a same-or-lower timeframe, which `request.security` cannot
  evaluate meaningfully.

## The crossover level: knowing the trigger price before the bar closes

The BUY/SELL label only appears *after* the EMAs have crossed. The table's
`Cross price` row tells you the exact close price that would make them cross
**on the current bar**, before it happens — plotted as a dotted ray across
the chart (cyan when a close *above* it flips the pair, magenta when a close
*below* does).

It is arithmetic, not a forecast. An EMA is
`EMA = α·close + (1-α)·EMA[1]` with `α = 2/(period+1)`, so setting
`emaFast == emaSlow` and solving for `close` gives one exact number:

```
Cross price = [ (1-αslow)·EMAslow[1] − (1-αfast)·EMAfast[1] ] / (αfast − αslow)
```

Every input is a value from the **last closed bar**, so the level is fixed
for the whole of the current bar instead of drifting with the live price.
Close this bar past it and `ta.crossover`/`ta.crossunder` fires; close short
of it and it does not. Nothing here says the price *will* get there.

- `Cross distance` shows the gap between the live price and that level in
  ATR units — the honest reality check. `0.30 ATR away` is one ordinary
  bar's range; `6.00 ATR away` means the cross is not happening this bar no
  matter what the candles look like.
- The row turns **yellow** once the distance is inside "Pre-cross alert
  distance (ATR)" (default 0.25), which is the same condition the two
  "EMA cross level near" alerts fire on.
- `-` means the level is unavailable: the EMAs are still warming up, both
  periods are set to the same value, or the arithmetic lands on a
  non-positive price (which no instrument can reach).
- A cross at the level is **not** an entry. The RSI and higher-timeframe
  filters are still applied afterwards, so a BUY/SELL may still be declined
  — see the filters section above.
- Turn the ray off with "Show next-bar EMA crossover price" in Settings; the
  value stays available in TradingView's Data Window as `EMA crossover
  price`.

## Trade management after entry (TP1 → breakeven → TP2)

Once a BUY/SELL confirms, the indicator keeps tracking it bar by bar, not
just at entry:

- **TP1 hit** → label switches to "ถึงเป้า 1", and Stop Loss moves to
  breakeven (the entry price) — turn off "Move Stop Loss to breakeven after
  TP1" in Settings to keep the original ATR-based Stop Loss instead. The Stop
  Loss line on the chart moves to show this live.
- **TP2 hit** or **Stop Loss hit** (original, or breakeven after TP1) closes
  the trade. By default ("Clear marker immediately when a trade closes") the
  marker and lines disappear right away and `ตอนนี้` returns to
  "รอจังหวะ — ยังไม่มีไม้" —
  the outcome is what the "target hit"/"stopped out" alerts are for, not
  something left sitting on the chart. Turn that setting off to instead see
  a frozen "ถึงเป้าหมาย"/"โดนตัดขาดทุน" label until the next fresh signal
  replaces it.
- The `ตอนนี้` row says "เริ่มอ่อนแรง" (and `ควรทำ` turns orange) whenever
  price closes back on the wrong side of the fast EMA while still in the
  trade — an early warning that momentum is fading, before the slow EMA
  cross that would fully invalidate/cancel it. It clears itself
  automatically once price closes back on the right side.
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

## Reading the table

The top two rows answer "what now?" so you never have to derive it from the
indicator rows underneath:

| Row | What it tells you |
|---|---|
| `ตอนนี้` | The one-line verdict: loading, misconfigured, holding a trade, or waiting. |
| `ควรทำ` | The next action. While waiting it lists exactly what the next signal still needs, e.g. `รอ BUY · EMA ต้องตัดขึ้น ✗ · RSI ✗ · 4H ✗` — a `✓` marks a condition already satisfied. |
| `เทรนด์ชาร์ตนี้` / `เทรนด์ TF ใหญ่` | The raw EMA relationship on each timeframe. |
| `เข้าที่ราคา` / `ตัดขาดทุน` / `เป้าที่ 1` / `เป้าที่ 2` | The live levels of the trade being tracked. |
| `แรงซื้อ-ขาย` | RSI plus which side it currently supports. |
| `ช่วงแกว่ง/แท่ง` | ATR, plus how far the Stop Loss would sit from entry right now. |
| `ข้อมูลที่โหลด` | Bars available; turns orange while the slow EMA is still warming up. |
| `ตัวกรอง TF ใหญ่` | Which higher timeframe is in use and where it came from. |

`ตอนนี้` only changes on a *confirmed* signal (passed both filters) or when
the raw EMA relationship that produced it reverses. `เทรนด์ชาร์ตนี้` and
`เทรนด์ TF ใหญ่` always show the current raw relationship on their own
timeframe, filters or not. So "ถือ BUY อยู่" while `เทรนด์ชาร์ตนี้` says
ขาลง should never happen — if you see that, it's a bug; but "ถือ BUY อยู่"
while `เทรนด์ TF ใหญ่` says ขาลง can happen briefly (state hasn't been
invalidated by an unfiltered reversal on the chart's own timeframe yet) and
is normal.

## "I don't see any BUY/SELL marker"

That's a normal state, not a bug, and the table's `ตอนนี้`/`ควรทำ` rows say
which of these it is:

- **"กำลังโหลดข้อมูล..."** — `ta.ema` with the default 50-period slow EMA
  needs 50 bars of history before it is defined at all; before that, a
  crossover cannot be evaluated. Load more history (zoom out, or switch to a
  timeframe with a longer available history).
- **"ตั้งค่าผิด — ไม่มีสัญญาณแน่นอน"** —
  the higher timeframe you picked is not above the chart's timeframe
  (typically Fixed mode after switching to a higher chart). Pick a higher
  one, switch to Preset ladder/Multiplier, or turn the higher-timeframe
  filter off. Signals are deliberately blocked rather than evaluated
  against a meaningless timeframe.
- **A small gray triangle appears at a spot with no colored BUY/SELL
  label** — turn on "Show raw crossovers a filter blocked (debug)" in
  Settings first; it's off by default. It marks a bar where an EMA
  crossover *did* happen, but RSI or the higher-timeframe trend blocked it
  — the filters working as intended, not a bug.
- **Turn off "Require RSI confirmation" and/or "Require higher-timeframe
  trend agreement"** to see raw EMA crossovers as full BUY/SELL signals,
  unfiltered. Useful for checking whether the rule is finding crossovers at
  all before deciding a filter is too strict for your taste.
- Otherwise: **"รอจังหวะ — ยังไม่มีไม้"** — the fast EMA genuinely has not
  crossed the slow EMA since you added the indicator. A signal only fires
  the *instant* that relationship flips, so an already-crossed pair produces
  nothing until it crosses back. The `ควรทำ` row names which direction can
  fire next and which conditions are still missing.

## Setting a real-time alert

Click the clock/alarm icon on the chart toolbar → **Add Alert** → set
**Condition** to this indicator → pick one of "Advisory BUY (entry)",
"...SELL (entry)", "...TP1 hit", "...target hit", "...stopped out", or one of
the two "EMA cross level near" pre-cross warnings (add one alert per
condition you want, per symbol/chart) → set **Trigger** to
**Once Per Bar Close**, not "Once Per Bar".

This rule is computed from `ta.ema`/`ta.rsi`/`ta.atr` on the still-forming
live bar, so a signal can appear and then vanish before that bar actually
closes (a well-known Pine behavior called repainting). "Once Per Bar Close"
waits for the bar to finish before alerting, matching exactly what
`tgxm predict` evaluates from already-closed MT5 candles. It cannot predict a
price outcome; it reacts the instant its rule becomes true on each newly
closed bar, going forward from when you add it. The one thing it does state
ahead of time is the `Cross price` level — the price the EMAs cross at, not a
claim that the price will reach it.

The two "EMA cross level near" conditions are the exception where **Once Per
Bar** can be the right trigger: their whole point is to warn you while the
bar is still open. They are computed from last-closed-bar values only, so
unlike the entry signals they do not repaint within the bar. They fire on
proximity to the level, never on a confirmed entry — the RSI and
higher-timeframe filters have not been applied at that point.

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
