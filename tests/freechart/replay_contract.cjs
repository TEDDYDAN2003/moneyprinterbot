const assert = require('node:assert/strict');
const { source, schema, minute, start, synthetic, agg, api } = require('./harness.cjs');
const { core, bv, log, ctx, object } = api(),
  cfg = (x) => core.cfg({ chartTick: 1, usdTick: 1, lots: 1, cash: 6, ...x });
const planes = [15, 30, 45],
  candles = synthetic(15 * 430),
  frames = Object.fromEntries([1, 5, ...planes].map((n) => [n, agg(candles, n)]));
const creds = Object.fromEntries(schema.map((x) => [x.id, x.defval]));
for (const { id, defval } of schema) assert.deepEqual(core.cfg({})[id], defval, id + ' fallback');
assert.equal(new Set(schema.map((x) => x.id)).size, schema.length);
assert(!schema.some((x) => x.id === 'showPriceLabel'));
assert.equal(core.cfg(creds).ticks.toString(), 'NaN');
assert.equal(cfg({ lots: 0.1, cash: 10, usdTick: 0.01, chartTick: 0.01 }).ticks, 10000);
assert.equal(
  cfg({ lots: 1, cash: 5, usdTick: 1, chartTick: 0.1, brokerTickSize: 1, pointIn: 0.01 }).ticks,
  50
);
assert.equal(cfg({ cash: 0.07, usdTick: 0.01 }).ticks, 7);
assert.equal(cfg({ cash: 0.0701, usdTick: 0.01 }).ticks, 8);
assert.equal(cfg({ targetMode: 'Chart ticks (no USD conversion)', usdTick: 0, fixedTicks: 7 }).ticks, 7);
assert(Number.isNaN(cfg({ startAt: '2024-02-31' }).from));
assert.equal(cfg({ startAt: '2024-03-18T08:00' }).from, start + 480 * minute);
// Clock tags use entry close, overlap intersection, and local DST (not browser offset).
for (const [time, mask] of [
  ['2024-01-02T07:00Z', 1],
  ['2024-01-02T13:00Z', 29],
  ['2024-01-02T16:00Z', 9],
  ['2024-03-18T12:00Z', 13],
  ['2024-07-01T12:00Z', 29],
  ['2024-07-01T00:00Z', 3],
])
  assert.equal(core.sessionMask(Date.parse(time), cfg({})), mask, time);
function row(i, tf, ohlc, raw, bias = raw) {
  return {
    i,
    t: start + i * tf * minute,
    end: start + (i + 1) * tf * minute,
    ok: true,
    raw,
    bias,
    o: ohlc[0],
    h: ohlc[1],
    l: ohlc[2],
    c: ohlc[3],
  };
}
function micros(v, head) {
  const m = new Map(),
    z = head.at(-1)[3];
  for (let i = 0; i < (v.end - v.t) / minute; i++) {
    const [open, high, low, close] = head[i] || [z, z, z, z],
      timestamp = v.t + i * minute;
    m.set(timestamp, { timestamp, open, high, low, close });
  }
  return m;
}
const mirror = (a) => [200 - a[0], 200 - a[2], 200 - a[1], 200 - a[3]];
for (const d of [1, -1])
  for (const tf of planes) {
    const price = (a) => (d === 1 ? a : mirror(a)),
      make = (i, b, r) => row(i, tf, price(b), d * r),
      c = cfg({ cash: 4 });
    const fill = make(0, [100, 110, 90, 100], 1),
      test = make(1, [101, 106, 99, 105], 0);
    const win = [
        [101, 104, 101, 104],
        [104, 106, 99, 105],
      ],
      be = [
        [101, 103, 99, 101],
        [101, 106, 101, 105],
      ],
      unk = [[101, 106, 99, 105]];
    for (const [tape, e] of [
      [win, 1],
      [be, -1],
      [unk, 2],
    ]) {
      const k = core.book();
      core.score(k, fill, c, null, 0);
      assert.equal(k.p.tp, 100 + 4 * d);
      assert.deepEqual(k.counts[0], [0, 0, 0]);
      const m = micros(test, tape.map(price));
      core.score(k, test, c, m, 1);
      assert.equal(k.p, null);
      assert.equal(k.exit, e);
      assert.equal(k.fine, 1);
      assert.equal(k.coarse, 0);
      assert.deepEqual(k.counts[0], [+(e === 1), +(e < 0), +(e === 2)]);
    }
    for (const corrupt of [
      (m) => m.delete(test.t),
      (m) => m.delete(test.t + 3 * minute),
      (m) => (m.get(test.t + 6 * minute).close += 99),
    ]) {
      const k = core.book(),
        m = micros(test, win.map(price));
      corrupt(m);
      core.score(k, fill, c, null, 0);
      core.score(k, test, c, m, 1);
      assert.equal(k.exit, 2);
      assert.equal(k.coarse, 1);
    }
    const k = core.book();
    core.score(k, fill, c, null, 0);
    core.score(k, make(1, [100, 102, 100, 102], 1), c, null, 1);
    assert(k.p.maybe);
    assert.equal(k.p.at, fill.end);
    assert.equal(k.held, 1);
    core.score(k, make(2, [102, 103, 101, 102], -1), c, null, 2);
    assert.equal(k.exit, -3);
    assert.equal(k.entered, -d);
    assert.equal(k.p.tp, price([102, 103, 101, 102])[3] - d * 4);
    assert.equal(k.mixed, 1);
    assert.deepEqual(k.counts[0], [0, 1, 0]);
  }
assert.equal(core.rate([1, 7, 1]), '11.1–22.3% (9)');
assert.equal(core.rate([0, 0, 0]), '— (0)');
function shot(ks) {
  return ks.map((k) => ({
    counts: k.counts,
    at: k.p?.at,
    tp: k.p?.tp,
    side: k.p?.d,
    maybe: k.p?.maybe,
    fine: k.fine,
    coarse: k.coarse,
    be: k.be,
    mixed: k.mixed,
    held: k.held,
  }));
}
function calc(host, endAt, opts = {}, src = frames, ind = { extendData: {} }) {
  log.hostStep = host;
  log.data = src;
  log.base = src[host].filter((b) => b.timestamp <= endAt);
  const x = { ...cfg({}), ...opts };
  ind.extendData.inputs = x;
  const res = object.calc(log.base, ind);
  ind.result = res;
  assert.equal(res.length, log.base.length);
  return ind;
}
const until = start + 5940 * minute,
  sizes = [1, 5, 15, 30, 45];
let golden = null;
for (const host of sizes) {
  // Strict mode as of the explicit common visible open (the native provider may have later candles in memory).
  const ind = calc(host, until),
    ks = ind._fcReplay.books;
  assert(ks.every((k) => !k.problem && k.first !== null && k.counts[0].reduce((a, b) => a + b, 0) > 0));
  if (!golden) golden = shot(ks);
  else assert.deepEqual(shot(ks), golden, host + 'm independent of host');
  const earlier = until - 90 * minute,
    short = calc(host, earlier, {}, frames, ind),
    clean = calc(host, earlier);
  assert.deepEqual(shot(short._fcReplay.books), shot(clean._fcReplay.books), 'rewind');
  assert.deepEqual(shot(calc(host, until, {}, frames, ind)._fcReplay.books), golden, 'fast forward');
  assert.deepEqual(shot(calc(host, until, {}, frames, ind)._fcReplay.books), golden, 'repeat');
  const trunc = Object.fromEntries(
    [1, 5, ...planes].map((tf) => [tf, frames[tf].filter((b) => b.timestamp <= until)])
  );
  assert.deepEqual(shot(calc(host, until, {}, trunc)._fcReplay.books), golden, 'no future needed');
  const changed = Object.fromEntries(
    [1, 5, ...planes].map((tf) => [
      tf,
      frames[tf].map((q) =>
        q.timestamp + tf * minute > until ? { ...q, high: q.high + 1000, close: q.high + 500 } : q
      ),
    ])
  );
  assert.deepEqual(shot(calc(host, until, {}, changed)._fcReplay.books), golden, 'future OHLC invisible');
  const full = calc(host, until - host * minute, { clock: 'Completed-bar steps only' });
  assert.deepEqual(shot(full._fcReplay.books), golden, 'last FULL bar is settled in the opt-in mode');
}
// Sizing change rebuilds all books, not just the last level. Known-empty fine history falls back; loading is not zero trades.
const base = calc(15, until),
  noData = calc(15, until, {}, { ...frames, 1: [] });
assert(noData._fcReplay.books.some((k) => k.coarse > 0));
log.loading.add('1m');
const waiting = calc(15, until);
assert.match(waiting._fcReplay.notice, /Loading/);
assert(waiting.result.every((q) => !Object.keys(q).length));
log.loading.clear();
log.loading.add('45m');
const partial = calc(15, until);
const r = core.panelRows(partial._fcReplay, (x) => String(x));
assert.equal(r[1][4], 'n/a');
assert.notEqual(r[1][1], 'n/a');
log.loading.clear();
const blocked = calc(15, until, { usdTick: 0 });
assert(blocked._fcReplay.books.every((k) => !k.p && k.counts[0].every((x) => !x)));
assert.equal(core.panelRows(blocked._fcReplay, String)[1][4], 'n/a');
// Find an actually held entry on fabricated OHLC; exercise drawing, the TP dot/price tag,
// changing host bars, and target removal on a subsequent confirmed exit.
let saw = 0;
for (let i = 75; i < 210 && !saw; i++) {
  const at = frames[15][i].timestamp,
    ind = calc(15, at);
  const k = ind._fcReplay.books[0];
  if (!k.p) continue;
  const target = k.p.tp;
  log.lines = [];
  log.labels = [];
  log.texts = [];
  object.draw({ ctx, indicator: ind });
  assert.equal(ind.result.at(-1).tp15, target);
  assert(log.lines.some((s) => s.price === target));
  assert(log.labels.some((s) => s.text.startsWith('15m ') && s.text.includes(' TP ')));
  assert(log.texts.includes('Open TP price'));
  assert(ind.result.slice(0, -1).every((q) => q.tp15 === q.books[0].p?.tp));
  const off = calc(15, at, { showOpenTP: false });
  assert.equal(off.result.at(-1).tp15, undefined);
  assert.equal(off._fcReplay.books[0].p.tp, target);
  for (let j = i + 1; j < i + 20; j++) {
    const next = calc(15, frames[15][j].timestamp);
    if (next._fcReplay.books[0].p?.at === k.p.at) {
      assert.equal(next.result.at(-1).tp15, target);
      continue;
    }
    if (!next._fcReplay.books[0].p) {
      assert.equal(next.result.at(-1).tp15, undefined);
      log.labels = [];
      object.draw({ ctx, indicator: next });
      assert(!log.labels.some((s) => s.text.startsWith('15m ') && s.text.includes(' TP ')));
      saw++;
    }
    break;
  }
}
// The renderer might keep bars past the viewport. Pan back without recalculating:
// both the table AND labelled guides must equal a separate run stopped right there.
const long = calc(15, until);
const visible = bv.visible;
for (const i of [75, 94, 126, 150, 202, 252]) {
  bv.visible = () => ({ from: 0, to: i });
  log.texts = [];
  log.labels = [];
  object.draw({ ctx, indicator: long });
  const overlayText = [...log.texts],
    chips = log.labels.map((q) => q.text);
  const clipped = calc(15, frames[15][i].timestamp);
  assert.deepEqual(
    core.panelRows({ c: long._fcReplay.c, books: long.result[i].books, stamp: long.result[i].asOf }, String),
    core.panelRows(clipped._fcReplay, String)
  );
  log.texts = [];
  log.labels = [];
  object.draw({ ctx, indicator: clipped });
  assert.deepEqual(log.texts, overlayText);
  assert.deepEqual(
    log.labels.map((q) => q.text),
    chips
  );
}
bv.visible = visible;
// Reject duplicate, out-of-order, extra off-grid inspection bars even when their
// prices would not change the parent's O/H/L/C. Do not pick the convenient path.
const v = row(0, 15, [101, 106, 99, 105], 0),
  seq = [
    ...micros(v, [
      [101, 104, 101, 104],
      [104, 106, 99, 105],
    ]).values(),
  ];
const lower = bv.securityLowerTf;
for (const bad of [
  [seq[0], ...seq],
  [seq[1], seq[0], ...seq.slice(2)],
  [seq[0], { ...seq[0], timestamp: v.t + 30000 }, ...seq.slice(1)],
]) {
  bv.securityLowerTf = () => [bad];
  const m = core.tape([], 15 * minute, cfg({}));
  assert(!core.covered(v, m, cfg({})));
}
bv.securityLowerTf = lower;
const wrong = calc(15, until, {}, { ...frames, 45: frames[1] });
assert.equal(core.panelRows(wrong._fcReplay, String)[1][3], 'n/a');
// Per-bar display at end of test: hold for the rate denominator; no current TP after cutoff.
const entryBar = long.result.findIndex((r) => r.tp15 != null);
assert(entryBar > 0);
const timedOut = calc(15, frames[15][entryBar + 7].timestamp, {
  endAt: new Date(long.result[entryBar].asOf).toISOString(),
});
assert(timedOut._fcReplay.books[0].p);
assert.equal(timedOut.result.at(-1).tp15, undefined);
assert.equal(core.panelRows(timedOut._fcReplay, String)[9][1], '—');
assert(saw, 'observed real lifecycle on fixture');
console.log(
  'PASS Free Chart factory + JSON, two-clock/no-lookahead replay incl. rewind/repeat, 5 host intervals, both directions per lane, TP lifecycle, sessions, coverage, missing sizing/data and pooling. Synthetic only.'
);
