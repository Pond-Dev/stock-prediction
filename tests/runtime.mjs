import { readFile } from 'node:fs/promises';
import { PineTS, Indicator } from 'pinets';

export const BAR_MS = 15 * 60 * 1000;
export const START_TIME = Date.UTC(2025, 0, 6);
export const SOURCE_URL = new URL('../trend_entries.pine', import.meta.url);

export function normalizeTimeframe(timeframe) {
  const value = String(timeframe);
  if (/^\d+$/.test(value)) return String(Number(value));
  const alias = value.match(/^(\d+)([mhdw])$/);
  if (alias) {
    const [, count, unit] = alias;
    return unit === 'm' ? String(Number(count)) : unit === 'h' ? String(Number(count) * 60) : `${count}${unit.toUpperCase()}`;
  }
  const pine = value.match(/^(\d*)([SDWM])$/);
  if (pine) return `${Number(pine[1] || 1)}${pine[2]}`;
  throw new Error(`Unsupported fixture timeframe: ${timeframe}`);
}

function timeBucket(timestamp, timeframe) {
  const normalized = normalizeTimeframe(timeframe);
  if (/^\d+M$/.test(normalized)) {
    const months = Number(normalized.slice(0, -1));
    const date = new Date(timestamp);
    const firstMonth = Math.floor((date.getUTCFullYear() * 12 + date.getUTCMonth()) / months) * months;
    return [Date.UTC(Math.floor(firstMonth / 12), firstMonth % 12), Date.UTC(Math.floor((firstMonth + months) / 12), (firstMonth + months) % 12)];
  }
  const duration = timeframeMs(normalized);
  const anchor = normalized.endsWith('W') ? Date.UTC(1970, 0, 5) : 0;
  const openTime = anchor + Math.floor((timestamp - anchor) / duration) * duration;
  return [openTime, openTime + duration];
}

export function timeframeMs(timeframe) {
  const normalized = normalizeTimeframe(timeframe);
  if (/^\d+$/.test(normalized)) return Number(normalized) * 60_000;
  const count = Number(normalized.slice(0, -1));
  const unit = normalized.at(-1);
  const factor = { S: 1000, D: 86_400_000, W: 604_800_000 }[unit];
  if (factor) return count * factor;
  throw new Error(`Use calendar buckets for monthly fixture timeframe: ${timeframe}`);
}

export function candle(index, open, high, low, close, start = START_TIME) {
  return {
    openTime: start + index * BAR_MS,
    closeTime: start + (index + 1) * BAR_MS,
    open, high, low, close, volume: 100,
    quoteAssetVolume: 0, numberOfTrades: 0,
    takerBuyBaseAssetVolume: 0, takerBuyQuoteAssetVolume: 0, ignore: 0,
  };
}

export function risingBars(count = 320, start = START_TIME) {
  return Array.from({ length: count }, (_, i) =>
    candle(i, 2000 + i * 2, 2002.2 + i * 2, 1999.8 + i * 2, 2002 + i * 2, start));
}

export function mirrorBars(bars, center = 2500) {
  return bars.map(bar => ({
    ...bar,
    open: 2 * center - bar.open,
    high: 2 * center - bar.low,
    low: 2 * center - bar.high,
    close: 2 * center - bar.close,
  }));
}

export function pullbackBars({ direction = 1, countAfter = 0 } = {}) {
  const bars = risingBars(100);
  bars.push(candle(100, 2200, 2200.2, 2196, 2198));
  bars.push(candle(101, 2198, 2202.5, 2197.5, 2202));
  for (let i = 0; i < countAfter; i++) bars.push(candle(102 + i, 2202, 2203, 2201, 2202));
  return direction === 1 ? bars : mirrorBars(bars);
}

export function transformBars(bars, { scale = 1, offset = 0, timeframe = '15', start = START_TIME } = {}) {
  const normalized = normalizeTimeframe(timeframe);
  const months = normalized.endsWith('M') ? Number(normalized.slice(0, -1)) : 0;
  const duration = months ? 0 : timeframeMs(timeframe);
  const startDate = new Date(start);
  const atIndex = index => months
    ? Date.UTC(startDate.getUTCFullYear(), startDate.getUTCMonth() + index * months, 1)
    : start + index * duration;
  return bars.map((bar, i) => ({
    ...bar,
    open: bar.open * scale + offset,
    high: bar.high * scale + offset,
    low: bar.low * scale + offset,
    close: bar.close * scale + offset,
    openTime: atIndex(i),
    closeTime: atIndex(i + 1),
  }));
}

function aggregate(bars, timeframe) {
  const groups = new Map();
  for (const bar of bars) {
    const [openTime, closeTime] = timeBucket(bar.openTime, timeframe);
    const group = groups.get(openTime);
    if (!group) groups.set(openTime, { ...bar, openTime, closeTime });
    else {
      group.high = Math.max(group.high, bar.high);
      group.low = Math.min(group.low, bar.low);
      group.close = bar.close;
      group.volume += bar.volume;
    }
  }
  return [...groups.values()];
}

// A deterministic, entirely local market-data provider. The runtime, rather
// than these helpers, computes every EMA/ADX/pivot and executes the Pine rules.
export class FixtureProvider {
  constructor(bars, { higherTimeframeBars = {}, chartTimeframe = '15', symbolInfo = {}, mintick } = {}) {
    this.bars = bars;
    this.higherTimeframeBars = Object.fromEntries(Object.entries(higherTimeframeBars).map(([key, value]) => [normalizeTimeframe(key), value]));
    this.chartTimeframe = normalizeTimeframe(chartTimeframe);
    this.symbolInfo = symbolInfo;
    this.mintick = mintick ?? symbolInfo.mintick ?? 0.01;
    this.requests = [];
  }

  configure() {}

  async getMarketData(ticker, timeframe, limit, start, end) {
    this.requests.push({ ticker, timeframe, limit, start, end });
    const normalized = normalizeTimeframe(timeframe);
    const bars = normalized === this.chartTimeframe ? this.bars : this.higherTimeframeBars[normalized] ?? aggregate(this.bars, normalized);
    let selected = bars.filter(b => (start == null || b.openTime >= start) && (end == null || b.openTime <= end));
    if (limit != null && selected.length > limit) selected = selected.slice(-limit);
    return selected.map(b => ({ ...b }));
  }

  async getSymbolInfo(ticker) {
    return {
      ticker: ticker.split(':').at(-1), tickerid: ticker, main_tickerid: ticker,
      description: 'Synthetic indicator fixture', prefix: ticker.split(':')[0] ?? 'TEST', root: ticker.split(':').at(-1),
      type: 'forex', basecurrency: 'XAU', currency: 'USD', timezone: 'Etc/UTC',
      session: '24x7', volumetype: 'tick',
      pointvalue: 1, mincontract: 0.01,
      ...this.symbolInfo,
      mintick: this.mintick, minmove: 1, pricescale: 1 / this.mintick,
    };
  }
}

export async function runSource(bars, inputs = {}, options = {}) {
  const source = await readFile(SOURCE_URL, 'utf8');
  return runPine(source, bars, inputs, options);
}

export async function runPine(source, bars, inputs = {}, options = {}) {
  const provider = new FixtureProvider(bars, options);
  const runtime = new PineTS(provider, options.ticker ?? 'TEST:XAUUSD', options.chartTimeframe ?? '15', bars.length);
  // Optional extra plots expose state for assertions without modifying rules.
  const indicator = new Indicator(source + (options.diagnostics ?? ''), inputs);
  const context = await runtime.run(indicator);
  return { context, runtime, provider, indicator };
}

export function values(context, title) {
  const plot = context.plots[title];
  if (!plot) throw new Error(`Missing plot ${JSON.stringify(title)}. Available: ${Object.keys(context.plots).join(', ')}`);
  return plot.data.map(point => point.value);
}
