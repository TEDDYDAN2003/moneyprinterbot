// Compare the DELIVERED JS to the actual native Pine study; fabricated minute data only.
// Run with PINETS_MODULE=/path/to/pinets (0.13.1). Not either hosted platform.
const assert = require('node:assert/strict'),
  path = require('node:path'),
  fs = require('node:fs');
const { PineTS, Indicator } = require(process.env.PINETS_MODULE || 'pinets');
const { minute, synthetic, agg, api } = require('./harness.cjs');
const src = fs
  .readFileSync(path.join(__dirname, '../../indicators/FC-Hull-Dynamic-Source.pine'), 'utf8')
  .split('// lookahead_off:')[0];
const fields = [
  'wins',
  'losses',
  'unknown',
  'beLosses',
  'mixedLoss',
  'side',
  'entry',
  'target',
  'entryAt',
  'exitCode',
  'fineBars',
  'coarseBars',
  'heldSignals',
];
const tags = ['asian', 'london', 'ny', 'kz'];
for (const t of tags) for (const z of ['W', 'N', 'U']) fields.push(t + z);
const probe =
  src +
  '\nRow q=f_study()\nplot(native.bias,"bias")\nplot(native.eventCode,"event")\nplot(raw,"sig")\nplot(a,"A")\nplot(b,"B")\nplot(q.entered ? 1 : 0,"entered")\nplot(q.possibleBE ? 1 : 0,"maybe")\n' +
  fields.map((k) => `plot(q.${k},"${k}")`).join('\n');
const sym = {
  ticker: 'QA',
  tickerid: 'SYNTH:QA',
  prefix: 'SYNTH',
  type: 'cfd',
  basecurrency: '',
  currency: 'USD',
  mintick: 1,
  pointvalue: 1,
  timezone: 'Etc/UTC',
  pricescale: 1,
  minmove: 1,
  session: '24x7',
};
const mins = synthetic(15 * 420),
  { core, bv } = api(),
  horizon = mins.at(-1).timestamp + minute;
const frames = Object.fromEntries([1, 5, 15, 30, 45].map((tf) => [tf, agg(mins, tf)]));
const prov = {
  getMarketData: async (_s, t) =>
    frames[+t].map((b) => ({ ...b, openTime: b.timestamp, closeTime: b.timestamp + +t * minute })),
  getSymbolInfo: async () => sym,
  configure() {},
};
(async () => {
  let n = 0,
    entries = 0,
    flips = 0;
  for (const settings of [{}, { length: 7, n: 2, source: 'HL2', bePct: 55, inspectTF: '5m' }])
    for (const tf of [15, 30, 45]) {
      const c = core.cfg({ chartTick: 1, usdTick: 1, lots: 1, cash: 13, ...settings });
      const opts = {
        lots: 1,
        cash: 13,
        usdTick: 1,
        length: c.length,
        n: c.n,
        src: c.source,
        inspectTF: String(c.fineMs / minute),
        bePct: c.bePct,
      };
      const x = await new PineTS(prov, sym.tickerid, String(tf), frames[tf].length).run(
        new Indicator(probe, opts)
      );
      const out = core.packets(frames[tf], tf, tf * minute, c, bv.syminfo(), horizon),
        book = core.book(),
        m = new Map(frames[c.fineMs / minute].map((b) => [b.timestamp, b]));
      for (let i = 0; i < out.length; i++) {
        const v = out[i].at(-1);
        core.score(book, v, c, m, i);
        const p = book.p,
          [wins, losses, unknown] = book.counts[0];
        const want = {
          bias: v.bias,
          event: v.ev,
          sig: v.raw,
          entered: +!!book.entered,
          maybe: +!!p?.maybe,
          wins,
          losses,
          unknown,
          beLosses: book.be,
          mixedLoss: book.mixed,
          side: p?.d || 0,
          exitCode: book.exit,
          fineBars: book.fine,
          coarseBars: book.coarse,
          heldSignals: book.held,
        };
        if (p) Object.assign(want, { entry: p.entry, target: p.tp, entryAt: p.at });
        for (let g = 0; g < 4; g++) {
          const t = tags[g],
            [w, l, u] = book.counts[g + 1];
          Object.assign(want, { [t + 'W']: w, [t + 'U']: u, [t + 'N']: w + l + u });
        }
        for (const [k, y] of Object.entries(want))
          assert.equal(x.plots[k].data[i].value, y, `${tf}m ${i} ${k} (${c.source})`);
        for (const [k, y] of [
          ['A', v.a],
          ['B', v.b],
        ]) {
          const z = x.plots[k].data[i].value;
          assert(y == null ? !Number.isFinite(z) : Math.abs(y - z) < 1e-7, `${tf}m ${i} ${k}: ${z} vs ${y}`);
        }
        n++;
        if (book.entered) entries++;
        if (Math.abs(v.ev) === 2) flips++;
      }
    }
  assert(entries > 0 && flips > 0);
  console.log(
    `PASS Free Chart/PineTS native parity: ${n} fabricated lane closes, ${entries} counted entries, ${flips} structure shifts; Hull A/B, raw signals, open levels, session counts and TP/BE/opposite/? evidence.`
  );
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
