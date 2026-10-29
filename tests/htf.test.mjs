import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { Indicator } from 'pinets';
import { SOURCE_URL, START_TIME, BAR_MS, candle, mirrorBars, pullbackBars, risingBars, runPine, runSource, timeframeMs, transformBars, values } from './runtime.mjs';

const source = await readFile(SOURCE_URL, 'utf8');
const inputs = { fastLength: 4, slowLength: 8, slopeBars: 1, useAdx: false, useRoomFilter: false };
const diagnostic = expression => '\nplot(' + expression + ', "Test diagnostic", display=display.data_window)\n';
const contextPlots = '\nplot(confirmedHtfSide, "HTF side")\nplot(confirmedHtfReady ? 1 : 0, "HTF ready")\nplot(confirmedMainHtfReady ? 1 : 0, "Main ready")\nplot(displayTrend, "Displayed side")\nplot(pendingSide, "Pending side")\n';
const mainTitle = 'Confirmed main trend';
const confirmationTitle = 'Confirmed confirmation trend';
const localTitle = 'Confirmed trend (1 up / 0 unclear / -1 down)';
const liveLabels = context => (context.plots.__labels__?.data.at(-1)?.value ?? []).filter(label => !label._deleted);

// Independent synthetic histories isolate the two higher-timeframe gates.
// Include closed bars before the chart begins and enough bars to cover it.
function higherBars(timeframe, side = 1) {
  const rising = risingBars(64);
  const bars = side === 1 ? rising : side === -1 ? mirrorBars(rising)
    : rising.map((bar, i) => candle(i, 2500, 2501, 2499, 2500));
  return transformBars(bars, { timeframe, start: START_TIME - 20 * timeframeMs(timeframe) });
}

function pullbackAt(index) {
  const bars = risingBars(index);
  const price = 2000 + 2 * index;
  bars.push(candle(index, price, price + 0.2, price - 4, price - 2));
  bars.push(candle(index + 1, price - 2, price + 2.5, price - 2.5, price + 2));
  return bars;
}

// PineTS 0.13.1 implements main_period as the current context's period,
// unlike TradingView's stable parent period. A valid Manual request's
// secondary execution therefore sees equal intervals and skips the guard.
// Auto requests at the 12M cap can encounter the same runtime limitation.
// This wrapper uses the unchanged production inputs, HTF helper, selection
// block, and request expressions at global scope for Manual component tests.
const helperStart = source.indexOf('f_htfSnapshot() =>');
const helperEnd = source.indexOf('f_nearestLevels(', helperStart);
const selectionStart = source.indexOf('string chartTimeframe =');
const selectionEnd = source.indexOf('int htfSide =', selectionStart);
const confirmationRequest = source.match(/^\s*\[requestedSide, requestedReady\] = (request\.security\(.*\))$/m)?.[1];
const mainRequest = source.match(/^\s*\[requestedMainSide, requestedMainReady\] = (request\.security\(.*\))$/m)?.[1];
assert.ok(helperStart >= 0 && helperEnd > helperStart && selectionStart >= 0 && selectionEnd > selectionStart && confirmationRequest && mainRequest);
const manualComponentSource = request => source.slice(0, source.indexOf('string roomGroup'))
  + '\n' + source.slice(helperStart, helperEnd)
  + '\n' + source.slice(selectionStart, selectionEnd)
  + '\n[side, ready] = ' + request
  + '\nplot(side, "HTF side")\nplot(ready ? 1 : 0, "HTF ready")\n';

const statusAssignment = source.match(/^\s*entryStatus := .*$/m)?.[0].trim();
assert.ok(statusAssignment);
// Execute the exact timeframe selection and production status guard without
// security calls for the cap boundary. Remaining state is a ready fixture;
// no timeframe selection or entry gating algorithm is reimplemented here.
const selectionComponentSource = source.slice(0, source.indexOf('string roomGroup'))
  + '\n' + source.slice(selectionStart, selectionEnd)
  + '\nbool entryDataReady = true\nbool newsBlocked = false\nint activeSide = 0\nbool planClosed = false'
  + '\nbool mainHtfReady = true\nbool htfReady = true\nint mainHtfSide = 1\nint htfSide = 1\nint trend = 1\nbool cooldownOk = true'
  + '\nvar string entryStatus = ""\n' + statusAssignment;

test('the complete production script parses in the pinned supplementary runtime', () => {
  assert.doesNotThrow(() => new Indicator(source).prepare());
});

test('the full source reports confirmed upward and downward 1H and 4H contexts', async () => {
  const up = risingBars(160);
  for (const [bars, side] of [[up, 1], [mirrorBars(up), -1]]) {
    const { context, provider } = await runSource(bars, inputs, { diagnostics: contextPlots });
    assert.equal(values(context, 'HTF ready').at(-1), 1);
    assert.equal(values(context, 'HTF side').at(-1), side);
    assert.equal(values(context, 'Main ready').at(-1), 1);
    assert.equal(values(context, mainTitle).at(-1), side);
    assert.equal(values(context, 'Displayed side').at(-1), side);
    assert.deepEqual([...new Set(provider.requests.map(request => request.timeframe))], ['15', '60', '240']);
  }
});

test('changing the current hourly candle cannot change full-source HTF context before the next hour', async () => {
  const originalBars = risingBars(104);
  const modifiedBars = originalBars.map(bar => ({ ...bar }));
  for (let i = 100; i < 104; i++) modifiedBars[i] = candle(i, 100, 100.2, 98, 99);
  const original = await runSource(originalBars, inputs, { diagnostics: contextPlots });
  const modified = await runSource(modifiedBars, inputs, { diagnostics: contextPlots });
  assert.deepEqual(values(modified.context, 'HTF side'), values(original.context, 'HTF side'));
  assert.equal(values(modified.context, 'HTF side')[103], 1);

  modifiedBars.push(candle(104, 99, 100, 98, 99));
  const nextHour = await runSource(modifiedBars, inputs, { diagnostics: contextPlots });
  assert.equal(values(nextHour.context, 'HTF side')[104], -1);
});

test('the full HTF filter permits aligned long and short setups and blocks opposing confirmation', async () => {
  for (const direction of [1, -1]) {
    const bars = pullbackBars({ direction });
    const mainBars = higherBars('240', direction);
    const aligned = await runSource(bars, inputs, { higherTimeframeBars: { '240': mainBars } });
    assert.equal(values(aligned.context, mainTitle)[101], direction);
    assert.equal(values(aligned.context, confirmationTitle)[101], direction);
    assert.equal(values(aligned.context, 'Entry signal')[101], direction);
    const opposing = await runSource(bars, inputs, {
      higherTimeframeBars: { '60': higherBars('60', -direction), '240': mainBars },
    });
    assert.equal(values(opposing.context, confirmationTitle)[101], -direction);
    assert.equal(values(opposing.context, mainTitle)[101], direction);
    assert.ok(values(opposing.context, 'Entry signal').every(side => side === 0));
  }
});

test('an opposing or neutral main trend blocks both directions despite chart and confirmation agreement', async () => {
  for (const direction of [1, -1]) {
    for (const mainSide of [-direction, 0]) {
      const { context } = await runSource(pullbackBars({ direction }), inputs, {
        higherTimeframeBars: { '240': higherBars('240', mainSide) },
        diagnostics: contextPlots + diagnostic('entryStatus == "WAIT: MAIN TREND UNCLEAR" ? 1 : 0'),
      });
      assert.equal(values(context, confirmationTitle)[101], direction);
      assert.equal(values(context, localTitle)[101], direction);
      assert.equal(values(context, mainTitle)[101], mainSide);
      assert.equal(values(context, 'Main ready')[101], 1, 'neutral must be ready rather than missing data');
      assert.equal(values(context, 'Test diagnostic')[101], mainSide === 0 ? 1 : 0);
      assert.ok(values(context, 'Entry signal').every(side => side === 0));
      assert.ok(values(context, 'Pending side').every(side => side === 0));
    }
  }
});

test('main-trend warm-up blocks entries until both requested histories are ready', async () => {
  const { context } = await runSource(pullbackBars(), inputs, {
    diagnostics: contextPlots + diagnostic('entryStatus == "WAIT: HTF DATA" ? 1 : 0'),
  });
  assert.equal(values(context, 'HTF ready')[101], 1);
  assert.equal(values(context, 'Main ready')[101], 0);
  assert.equal(values(context, 'Test diagnostic')[101], 1);
  assert.ok(values(context, 'Entry signal').every(side => side === 0));
});

test('changing an unfinished 4H candle cannot change the main trend until its next interval', async () => {
  const originalBars = risingBars(176);
  const modifiedBars = originalBars.map(bar => ({ ...bar }));
  for (let i = 160; i < 176; i++) modifiedBars[i] = candle(i, 100, 100.2, 98, 99);
  const original = await runSource(originalBars, inputs, { diagnostics: contextPlots });
  const modified = await runSource(modifiedBars, inputs, { diagnostics: contextPlots });
  assert.deepEqual(values(modified.context, mainTitle), values(original.context, mainTitle));
  assert.equal(values(modified.context, mainTitle)[175], 1);
  assert.equal(values(modified.context, confirmationTitle)[175], -1, '1H can change while 4H is still open');
  assert.equal(values(modified.context, 'Displayed side')[175], 1);
  modifiedBars.push(candle(176, 99, 100, 98, 99));
  const nextMainBar = await runSource(modifiedBars, inputs, { diagnostics: contextPlots });
  assert.equal(values(nextMainBar.context, mainTitle)[176], -1);
  assert.equal(values(nextMainBar.context, 'Displayed side')[176], -1);
});

test('a newly confirmed opposing main trend cancels a pending pullback only when the chart bar closes', async () => {
  for (const direction of [1, -1]) {
    const upBars = pullbackAt(111);
    const bars = direction === 1 ? upBars : mirrorBars(upBars);
    const mainBars = higherBars('240');
    const changeIndex = mainBars.findIndex(bar => bar.openTime === bars[96].openTime);
    assert.ok(changeIndex >= 0);
    mainBars[changeIndex] = { ...mainBars[changeIndex], open: 100, high: 100.2, low: 98, close: 99 };
    const hourlyBars = higherBars('60', direction);
    const directedMainBars = direction === 1 ? mainBars : mirrorBars(mainBars);
    const { context } = await runSource(bars, inputs, {
      higherTimeframeBars: { '60': hourlyBars, '240': directedMainBars },
      diagnostics: contextPlots,
    });
    assert.equal(values(context, 'Pending side')[111], direction);
    assert.equal(values(context, mainTitle)[111], direction);
    assert.equal(values(context, mainTitle)[112], -direction);
    assert.equal(values(context, localTitle)[112], direction, 'chart remains aligned with the original setup');
    assert.equal(values(context, confirmationTitle)[112], direction);
    assert.equal(values(context, 'Pending side')[112], 0);
    assert.ok(values(context, 'Entry signal').every(side => side === 0));

    // Shift all three fixture sessions together so the final chart bar is
    // still open, although the preceding 4H bar has already closed.
    const shift = Date.now() - (bars.length - 0.5) * BAR_MS - START_TIME;
    const shiftTimes = history => history.map(bar => ({ ...bar, openTime: bar.openTime + shift, closeTime: bar.closeTime + shift }));
    const open = await runSource(shiftTimes(bars), inputs, {
      higherTimeframeBars: { '60': shiftTimes(hourlyBars), '240': shiftTimes(directedMainBars) },
      diagnostics: contextPlots + '\nplot(mainHtfSide, "Available main side")\n',
    });
    assert.equal(values(open.context, 'Available main side')[112], -direction);
    assert.equal(values(open.context, mainTitle)[112], direction, 'headline waits for the current chart bar to close');
    assert.equal(values(open.context, 'Pending side')[112], direction);
    assert.ok(values(open.context, 'Entry signal').every(side => side === 0));
  }
});

test('countertrend chart setups keep the main headline and background without issuing an opposite entry', async () => {
  for (const mainSide of [1, -1]) {
    const bars = pullbackBars({ direction: -mainSide });
    const { context } = await runSource(bars, inputs, {
      higherTimeframeBars: { '60': higherBars('60', mainSide), '240': higherBars('240', mainSide) },
      diagnostics: contextPlots + diagnostic('entryStatus == "WAIT: CHART ALIGNMENT" ? 1 : 0'),
    });
    assert.equal(values(context, mainTitle)[101], mainSide);
    assert.equal(values(context, confirmationTitle)[101], mainSide);
    assert.equal(values(context, localTitle)[101], -mainSide);
    assert.equal(values(context, 'Displayed side')[101], mainSide);
    assert.equal(values(context, 'Test diagnostic')[101], 1);
    assert.ok(values(context, 'Entry signal').every(side => side === 0));
    const labels = liveLabels(context);
    assert.equal(labels.length, 1, 'no price-plan labels without an entry');
    assert.ok(labels[0].text.startsWith(mainSide === 1 ? '4 ชม. ↑ เทรนด์ใหญ่ขึ้น · มอง BUY' : '4 ชม. ↓ เทรนด์ใหญ่ลง · มอง SELL'));
    assert.match(labels[0].text, /15 นาที · สวนเทรนด์ใหญ่ · รอก่อน/);
    assert.doesNotMatch(labels[0].text, /(?:BUY|SELL) · ยืนยันแท่งปิด/);
    const background = context.plots['Confirmed main trend background'].data.at(-1);
    assert.equal(background.options.color, mainSide === 1 ? '#00a6760D' : '#e646500D');
    const disabled = await runSource(bars, { ...inputs, useHtf: false }, { diagnostics: contextPlots });
    assert.equal(values(disabled.context, 'Entry signal')[101], -mainSide);
    assert.equal(values(disabled.context, 'Displayed side')[101], -mainSide);
  }
});

test('a changed main trend keeps an existing reference plan frozen and labels it as an unsupported old plan', async () => {
  const bars = pullbackBars({ countAfter: 11 });
  const mainBars = higherBars('240');
  const changeIndex = mainBars.findIndex(bar => bar.openTime === bars[96].openTime);
  mainBars[changeIndex] = { ...mainBars[changeIndex], open: 100, high: 100.2, low: 98, close: 99 };
  const { context } = await runSource(bars, inputs, {
    higherTimeframeBars: { '60': higherBars('60'), '240': mainBars },
  });
  assert.equal(values(context, 'Entry signal')[101], 1);
  assert.equal(values(context, mainTitle)[101], 1);
  assert.equal(values(context, mainTitle)[112], -1);
  assert.equal(values(context, 'Active plan side')[112], 1);
  assert.equal(values(context, 'Entry signal')[112], 0);
  for (const title of ['Plan entry', 'Plan stop', 'Plan target', 'Plan net R']) {
    assert.equal(values(context, title)[112], values(context, title)[101], title);
  }
  const labels = liveLabels(context);
  assert.equal(labels.length, 4);
  const status = labels.find(label => label.text.startsWith('4 ชม.'));
  assert.match(status.text, /เทรนด์ใหญ่ลง/);
  assert.match(status.text, /ติดตามแผน BUY เดิม · เทรนด์ใหญ่ไม่หนุนแล้ว/);
  assert.doesNotMatch(status.text, /(?:BUY|SELL) · ยืนยันแท่งปิด/);
});

test('Auto selects a supported higher interval across minute, daily, weekly, and monthly charts', async () => {
  const cases = [['1', '4'], ['5', '20'], ['15', '60'], ['60', '240'], ['240', '960'], ['1D', '4D'], ['1W', '4W'], ['1M', '4M']];
  for (const [chartTimeframe, expected] of cases) {
    const bars = transformBars(risingBars(16), { timeframe: chartTimeframe, start: Date.UTC(2000, 0, 1) });
    const { context } = await runSource(bars, { ...inputs, useHtf: false }, {
      chartTimeframe,
      diagnostics: diagnostic('selectedHtf == "' + expected + '" and htfValid and mainHtfValid and selectedMainHtfSeconds > selectedHtfSeconds ? 1 : 0'),
    });
    assert.equal(values(context, 'Test diagnostic').at(-1), 1, chartTimeframe + ' should select ' + expected);
  }
});

test('the unchanged Manual confirmation and main components request their configured intervals', async () => {
  const cases = [
    [confirmationRequest, { htfMode: 'Manual', manualHtf: '240' }, '240'],
    [mainRequest, { mainHtfMode: 'Manual', manualMainHtf: '120' }, '120'],
  ];
  for (const [request, overrides, expected] of cases) {
    const { context, provider } = await runPine(manualComponentSource(request), risingBars(320), { ...inputs, ...overrides });
    assert.equal(values(context, 'HTF side').at(-1), 1);
    assert.equal(values(context, 'HTF ready').at(-1), 1);
    assert.deepEqual([...new Set(provider.requests.map(request => request.timeframe))], ['15', expected]);
  }
});

test('main intervals at or below confirmation and tick intervals block entries, unless the filter is disabled', async () => {
  for (const manualMainHtf of ['60', '15', '5', '', '100T']) {
    const overrides = { ...inputs, mainHtfMode: 'Manual', manualMainHtf };
    const blocked = await runSource(pullbackBars(), overrides, {
      diagnostics: diagnostic('entryStatus == "INVALID MAIN HTF" ? 1 : 0'),
    });
    assert.equal(values(blocked.context, 'Test diagnostic').at(-1), 1, manualMainHtf);
    assert.ok(values(blocked.context, 'Entry signal').every(side => side === 0));
    const disabled = await runSource(pullbackBars(), { ...overrides, useHtf: false });
    assert.equal(values(disabled.context, 'Entry signal')[101], 1);
  }
});

test('the exact Auto selection and status components block equal capped main and confirmation intervals', async () => {
  const chartTimeframe = '4M';
  const bars = transformBars(pullbackBars(), { timeframe: chartTimeframe, start: Date.UTC(1900, 0, 1) });
  const { context, provider } = await runPine(selectionComponentSource, bars, inputs, {
    chartTimeframe,
    diagnostics: diagnostic('htfValid and not mainHtfValid and entryStatus == "INVALID MAIN HTF" ? 1 : 0'),
  });
  assert.equal(values(context, 'Test diagnostic').at(-1), 1);
  assert.deepEqual([...new Set(provider.requests.map(request => request.timeframe))], ['4M']);
});

test('equal or lower manual HTFs block entries visibly, while disabling the filter ignores them', async () => {
  for (const manualHtf of ['15', '5', '']) {
    const overrides = { ...inputs, htfMode: 'Manual', manualHtf };
    const blocked = await runSource(pullbackBars(), overrides, {
      diagnostics: diagnostic('entryStatus == "INVALID HTF: SELECT HIGHER / OFF" ? 1 : 0'),
    });
    assert.equal(values(blocked.context, 'Test diagnostic').at(-1), 1);
    assert.ok(values(blocked.context, 'Entry signal').every(side => side === 0));
    const disabled = await runSource(pullbackBars(), { ...overrides, useHtf: false });
    assert.equal(values(disabled.context, 'Entry signal')[101], 1);
  }
});

test('Auto at the maximum 12-month chart interval is invalid only when the HTF filter is enabled', async () => {
  const chartTimeframe = '12M';
  const bars = transformBars(pullbackBars(), { timeframe: chartTimeframe, start: Date.UTC(1900, 0, 1) });
  const blocked = await runSource(bars, inputs, {
    chartTimeframe,
    diagnostics: diagnostic('entryStatus == "INVALID HTF: SELECT HIGHER / OFF" ? 1 : 0'),
  });
  assert.equal(values(blocked.context, 'Test diagnostic').at(-1), 1);
  assert.ok(values(blocked.context, 'Entry signal').every(side => side === 0));
  const disabled = await runSource(bars, { ...inputs, useHtf: false }, { chartTimeframe });
  assert.equal(values(disabled.context, 'Entry signal')[101], 1);
});
