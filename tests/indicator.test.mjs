import test from 'node:test';
import assert from 'node:assert/strict';
import { BAR_MS, candle, mirrorBars, pullbackBars, risingBars, runSource, values } from './runtime.mjs';

const TREND = 'Confirmed trend (1 up / 0 unclear / -1 down)';
const ENTRY_INPUTS = {
  fastLength: 4, slowLength: 8, slopeBars: 1, useAdx: false,
  useHtf: false, useRoomFilter: false,
};
const sideValues = context => values(context, 'Entry signal');
const signalIndexes = context => sideValues(context).flatMap((side, index) => side ? [index] : []);
const diagnostic = expression => `\nplot(${expression}, "Test diagnostic", display=display.data_window)\n`;

test('trend classification handles rising, falling, and flat price histories', async () => {
  const up = risingBars();
  const flat = up.map((bar, i) => candle(i, 2500, 2501, 2499, 2500));
  for (const [bars, expected] of [[up, 1], [mirrorBars(up), -1], [flat, 0]]) {
    const { context } = await runSource(bars, { useHtf: false });
    assert.equal(values(context, TREND).at(-1), expected);
  }
});

test('an open bar cannot change the last confirmed trend', async () => {
  const count = 320;
  const start = Date.now() - (count - 0.5) * BAR_MS;
  const bars = risingBars(count, start);
  bars[count - 1] = { ...bars[count - 1], low: 1, close: 1 };
  const { context } = await runSource(bars, { useHtf: false });
  const trend = values(context, TREND);
  assert.equal(trend.at(-2), 1);
  assert.equal(trend.at(-1), 1, 'intrabar collapse must not commit a new trend');
});

test('invalid EMA ordering is rejected instead of silently inverting the rules', async () => {
  await assert.rejects(
    runSource(risingBars(), { fastLength: 50, slowLength: 20 }),
    /Fast EMA must be shorter than Slow EMA/,
  );
});

test('long and short entries require a separate pullback bar, then a later confirming close', async () => {
  for (const direction of [1, -1]) {
    const { context } = await runSource(pullbackBars({ direction }), ENTRY_INPUTS);
    assert.deepEqual(signalIndexes(context), [101]);
    assert.equal(sideValues(context)[100], 0, 'the touch bar only arms the setup');
    assert.equal(sideValues(context)[101], direction);
    assert.equal(values(context, 'Active plan side')[101], direction);
    const entry = values(context, 'Plan entry')[101];
    const stop = values(context, 'Plan stop')[101];
    const target = values(context, 'Plan target')[101];
    assert.ok(direction * (entry - stop) > 0);
    assert.ok(direction * (target - entry) > 0);
    assert.ok(values(context, 'Plan net R')[101] >= 1.5);
  }
});

test('a continuous trend without a pullback does not issue entry signals', async () => {
  const { context } = await runSource(risingBars(), ENTRY_INPUTS);
  assert.deepEqual(signalIndexes(context), []);
});

test('a still-open trigger candle cannot issue a BUY or SELL', async () => {
  for (const direction of [1, -1]) {
    const bars = pullbackBars({ direction });
    const start = Date.now() - (bars.length - 0.5) * BAR_MS;
    const shifted = bars.map((bar, i) => ({ ...bar, openTime: start + i * BAR_MS, closeTime: start + (i + 1) * BAR_MS }));
    const { context } = await runSource(shifted, ENTRY_INPUTS);
    assert.deepEqual(signalIndexes(context), []);
    assert.equal(values(context, 'Active plan side').at(-1), 0);
  }
});

test('active reference levels are frozen and do not repeat entry signals', async () => {
  const { context } = await runSource(pullbackBars({ countAfter: 5 }), ENTRY_INPUTS);
  assert.deepEqual(signalIndexes(context), [101]);
  for (const title of ['Plan entry', 'Plan stop', 'Plan target', 'Plan net R']) {
    const series = values(context, title);
    assert.ok(series.slice(101).every(value => value === series[101]), title);
  }
});

test('signal-bar extremes cannot retroactively close a plan created at its close', async () => {
  const bars = pullbackBars();
  bars[101].high = 2300;
  const { context } = await runSource(bars, ENTRY_INPUTS);
  assert.equal(sideValues(context).at(-1), 1);
  assert.ok(bars[101].high > values(context, 'Plan target').at(-1));
  assert.equal(values(context, 'Active plan side').at(-1), 1);
});

test('a later candle touching both SL and TP closes the reference plan as ambiguous', async () => {
  const bars = pullbackBars();
  bars.push(candle(102, 2202, 2250, 2150, 2202));
  const { context } = await runSource(bars, ENTRY_INPUTS, {
    diagnostics: diagnostic('planStatus == "BOTH TOUCHED / ORDER UNKNOWN" ? 1 : 0'),
  });
  assert.equal(values(context, 'Active plan side').at(-1), 0);
  assert.equal(values(context, 'Test diagnostic').at(-1), 1);
  assert.equal(sideValues(context).at(-1), 0, 'cannot re-enter on the plan-ending bar');
});

test('a reference plan expires at its configured age', async () => {
  const { context } = await runSource(pullbackBars({ countAfter: 3 }), { ...ENTRY_INPUTS, maxPlanBars: 2 }, {
    diagnostics: diagnostic('planStatus == "EXPIRED" ? 1 : 0'),
  });
  assert.equal(values(context, 'Active plan side')[102], 1);
  assert.equal(values(context, 'Active plan side')[103], 0);
  assert.equal(values(context, 'Test diagnostic')[103], 1);
});

test('unknown room blocks entries unless explicitly allowed', async () => {
  const bars = pullbackBars();
  const blocked = await runSource(bars, { ...ENTRY_INPUTS, useRoomFilter: true });
  assert.deepEqual(signalIndexes(blocked.context), []);
  const allowed = await runSource(bars, { ...ENTRY_INPUTS, useRoomFilter: true, allowUnknownRoom: true });
  assert.deepEqual(signalIndexes(allowed.context), [101]);
});

test('confirmed resistance caps TP, and insufficient room rejects the entry', async () => {
  const blockedBars = pullbackBars();
  blockedBars[90].high = 2208;
  const blocked = await runSource(blockedBars, { ...ENTRY_INPUTS, useRoomFilter: true });
  assert.deepEqual(signalIndexes(blocked.context), []);
  const allowedBars = pullbackBars();
  allowedBars[90].high = 2214;
  const allowed = await runSource(allowedBars, { ...ENTRY_INPUTS, useRoomFilter: true });
  assert.deepEqual(signalIndexes(allowed.context), [101]);
  assert.ok(values(allowed.context, 'Plan target').at(-1) < 2214);
  assert.ok(values(allowed.context, 'Plan net R').at(-1) >= 1.5);
  assert.ok(values(allowed.context, 'Plan net R').at(-1) < 1.9, 'room cap reduces the nominal 2R target');
});

test('an entry exactly at a confirmed level is blocked even when unknown room is allowed', async () => {
  const bars = pullbackBars();
  bars[90].high = bars[101].close;
  for (const fixture of [bars, mirrorBars(bars)]) {
    const { context } = await runSource(fixture, {
      ...ENTRY_INPUTS, useRoomFilter: true, allowUnknownRoom: true,
    }, {
      diagnostics: diagnostic('entryStatus == "BLOCKED: REWARD / RISK" ? 1 : 0'),
    });
    assert.deepEqual(signalIndexes(context), []);
    assert.equal(values(context, 'Test diagnostic').at(-1), 1);
  }
});

test('cost buffer rejects a signal whose net reward/risk is below the threshold', async () => {
  const { context } = await runSource(pullbackBars(), { ...ENTRY_INPUTS, costBuffer: 2 });
  assert.deepEqual(signalIndexes(context), []);
});

test('quantity estimate floors to broker increments without exceeding the risk budget', async () => {
  const { context } = await runSource(pullbackBars(), {
    ...ENTRY_INPUTS, riskCash: 100, valuePerUnit: 100, qtyStep: 0.01, minQty: 0.01, costBuffer: 0.5,
  });
  assert.deepEqual(signalIndexes(context), [101]);
  const lots = values(context, 'Plan quantity').at(-1);
  const riskPerUnit = (values(context, 'Plan entry').at(-1) - values(context, 'Plan stop').at(-1) + 0.5) * 100;
  assert.ok(lots * riskPerUnit <= 100 + 1e-8);
  assert.ok((lots + 0.01) * riskPerUnit > 100);
  assert.ok(Math.abs(lots / 0.01 - Math.round(lots / 0.01)) < 1e-8);
});

test('a budget smaller than the minimum quantity risk blocks the signal', async () => {
  const { context } = await runSource(pullbackBars(), { ...ENTRY_INPUTS, riskCash: 1, valuePerUnit: 100, qtyStep: 0.01, minQty: 0.01 }, {
    diagnostics: diagnostic('entryStatus == "BLOCKED: BELOW MIN QTY" ? 1 : 0'),
  });
  assert.deepEqual(signalIndexes(context), []);
  assert.equal(values(context, 'Test diagnostic').at(-1), 1);
});

test('manual-news blocking includes the exact signal-bar close boundary', async () => {
  const bars = pullbackBars();
  const eventTime = bars.at(-1).closeTime;
  for (const eventNumber of [1, 2, 3]) {
    const inputs = {
      ...ENTRY_INPUTS, newsBefore: 0, newsAfter: 0,
      [`event${eventNumber}Enabled`]: true, [`event${eventNumber}Time`]: eventTime,
    };
    const blocked = await runSource(bars, inputs);
    assert.deepEqual(signalIndexes(blocked.context), [], `event ${eventNumber}`);
    const outside = await runSource(bars, { ...inputs, [`event${eventNumber}Time`]: eventTime + 1 });
    assert.deepEqual(signalIndexes(outside.context), [101], `event ${eventNumber} outside bar`);
  }
});

test('enabling a news slot without a date is rejected', async () => {
  await assert.rejects(
    runSource(pullbackBars(), { ...ENTRY_INPUTS, event1Enabled: true, event1Time: 0 }),
    /Set a date and time/,
  );
});

test('the last allowed setup bar can trigger, but the following bar expires it', async () => {
  const bars = pullbackBars();
  bars[101] = candle(101, 2198, 2199, 2197.5, 2198.5);
  bars.push(candle(102, 2198.5, 2203, 2198, 2202));
  const expired = await runSource(bars, { ...ENTRY_INPUTS, maxPullbackBars: 1 }, {
    diagnostics: diagnostic('entryStatus == "PULLBACK EXPIRED" ? 1 : 0'),
  });
  assert.deepEqual(signalIndexes(expired.context), []);
  assert.equal(values(expired.context, 'Test diagnostic').at(-1), 1);
  const allowed = await runSource(bars, { ...ENTRY_INPUTS, maxPullbackBars: 2 });
  assert.deepEqual(signalIndexes(allowed.context), [102]);
});

test('losing the trend cancels a pending pullback rather than carrying it into recovery', async () => {
  const bars = pullbackBars();
  bars[101] = candle(101, 2198, 2200, 2189, 2190);
  bars.push(candle(102, 2190, 2206, 2190, 2205));
  const { context } = await runSource(bars, ENTRY_INPUTS);
  assert.equal(values(context, TREND)[101], 0);
  assert.equal(values(context, TREND)[102], 1);
  assert.deepEqual(signalIndexes(context), []);
});

test('a pivot level is unavailable until its right-hand confirmation bars have closed', async () => {
  const bars = risingBars(96);
  bars[90].high = 2214;
  const { context } = await runSource(bars, { ...ENTRY_INPUTS, showLevels: true });
  const resistance = values(context, 'Known resistance');
  assert.ok(resistance.slice(90, 93).every(Number.isNaN));
  assert.equal(resistance[93], 2214);
});
