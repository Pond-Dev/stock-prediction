import test from 'node:test';
import assert from 'node:assert/strict';
import { mirrorBars, pullbackBars, runSource, transformBars, values } from './runtime.mjs';

const ENTRY_INPUTS = {
  fastLength: 4, slowLength: 8, slopeBars: 1,
  useAdx: false, useHtf: false, useRoomFilter: false,
};

// Scale one known price-action scenario into distinct quote/tick/contract
// conventions. These synthetic symbols do not represent real market results.
const assets = [
  {
    label: 'stock', ticker: 'TEST:SHARES', chartTimeframe: '1D', scale: 0.1,
    mintick: 0.01, symbolInfo: { type: 'stock', currency: 'USD', pointvalue: 1 },
    quantityUnit: 'shares', valuePerUnit: 1, qtyStep: 1, minQty: 1, riskCash: 100,
  },
  {
    label: 'FX', ticker: 'TEST:EURUSD', chartTimeframe: '5', scale: 0.0005,
    mintick: 0.00001, symbolInfo: { type: 'forex', basecurrency: 'EUR', currency: 'USD', pointvalue: 100000 },
    quantityUnit: 'lots', valuePerUnit: 100000, qtyStep: 0.01, minQty: 0.01, riskCash: 100,
  },
  {
    label: 'crypto', ticker: 'TEST:BTCUSD', chartTimeframe: '60', scale: 30,
    mintick: 0.1, symbolInfo: { type: 'crypto', basecurrency: 'BTC', currency: 'USD', pointvalue: 1 },
    quantityUnit: 'coins', valuePerUnit: 1, qtyStep: 0.0001, minQty: 0.0001, riskCash: 100,
  },
  {
    label: 'futures', ticker: 'TEST:INDEXFUT', chartTimeframe: '1', scale: 2.5,
    mintick: 0.25, symbolInfo: { type: 'futures', currency: 'USD', pointvalue: 50 },
    quantityUnit: 'contracts', valuePerUnit: 50, qtyStep: 1, minQty: 1, riskCash: 1000,
  },
];

for (const asset of assets) {
  test(`${asset.label} price scale and tick size preserve directional entries and bounded quantity`, async () => {
    for (const direction of [1, -1]) {
      const rawBars = direction === 1 ? pullbackBars() : mirrorBars(pullbackBars());
      const bars = transformBars(rawBars, { scale: asset.scale, timeframe: asset.chartTimeframe });
      const { context } = await runSource(bars, {
        ...ENTRY_INPUTS,
        riskCash: asset.riskCash, valuePerUnit: asset.valuePerUnit,
        qtyStep: asset.qtyStep, minQty: asset.minQty, quantityUnit: asset.quantityUnit,
      }, asset);
      const signals = values(context, 'Entry signal');
      assert.equal(signals[101], direction);
      assert.equal(signals.filter(Boolean).length, 1);
      const entry = values(context, 'Plan entry')[101];
      const stop = values(context, 'Plan stop')[101];
      const target = values(context, 'Plan target')[101];
      for (const price of [entry, stop, target]) {
        assert.ok(Math.abs(price / asset.mintick - Math.round(price / asset.mintick)) < 1e-6, `off tick grid: ${price}`);
      }
      assert.ok(direction * (entry - stop) > 0);
      assert.ok(direction * (target - entry) > 0);
      const quantity = values(context, 'Plan quantity')[101];
      const riskPerUnit = Math.abs(entry - stop) * asset.valuePerUnit;
      assert.ok(quantity >= asset.minQty);
      assert.ok(quantity * riskPerUnit <= asset.riskCash + 1e-7);
      assert.ok((quantity + asset.qtyStep) * riskPerUnit > asset.riskCash);
      assert.ok(Math.abs(quantity / asset.qtyStep - Math.round(quantity / asset.qtyStep)) < 1e-6);
    }
  });
}

test('currency and quantity labels do not silently perform exchange-rate conversion', async () => {
  const inputs = { ...ENTRY_INPUTS, riskCash: 100, valuePerUnit: 1, qtyStep: 1, minQty: 1 };
  const usd = await runSource(pullbackBars(), { ...inputs, riskCurrency: 'USD', quantityUnit: 'shares' });
  const thb = await runSource(pullbackBars(), { ...inputs, riskCurrency: 'THB', quantityUnit: 'units' });
  assert.deepEqual(values(thb.context, 'Plan quantity'), values(usd.context, 'Plan quantity'));
});
