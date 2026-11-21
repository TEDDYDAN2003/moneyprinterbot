// Documented Free Chart contract for fabricated candles. No network or market inputs.
const fs = require('node:fs'),
  path = require('node:path'),
  assert = require('node:assert/strict');
const dir = path.join(__dirname, '../../indicators/freechart');
const source = fs.readFileSync(path.join(dir, 'FC-Dynamic-Hull-Replay.js'), 'utf8');
const schema = JSON.parse(fs.readFileSync(path.join(dir, 'FC-Dynamic-Hull-Replay.inputs.json'), 'utf8'));
const minute = 60000,
  start = Date.UTC(2024, 2, 18, 0); // US daylight saving, London still on GMT
function synthetic(n, seed = 2342) {
  const u = () => (seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296;
  let v = 25000;
  return Array.from({ length: n }, (_, i) => {
    const o = v;
    v += Math.floor(u() * 11) - 5;
    return {
      timestamp: start + i * minute,
      open: o,
      high: Math.max(o, v) + Math.floor(u() * 4),
      low: Math.min(o, v) - Math.floor(u() * 4),
      close: v,
      volume: 1,
    };
  });
}
function agg(rows, n) {
  const out = [];
  for (let i = 0; i + n <= rows.length; i += n) {
    const q = rows.slice(i, i + n);
    out.push({
      ...q[0],
      high: Math.max(...q.map((x) => x.high)),
      low: Math.min(...q.map((x) => x.low)),
      close: q.at(-1).close,
      volume: q.length,
    });
  }
  return out;
}
const tfVal = (s) => parseInt(s, 10);
const formatters = new Map();
function tzMinutes(t, z) {
  if (!formatters.has(z))
    formatters.set(
      z,
      new Intl.DateTimeFormat('en-GB', { timeZone: z, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    );
  const txt = formatters.get(z).format(t);
  return +txt.slice(0, 2) * 60 + Number(txt.slice(-2));
}
function api(hostStep = 15) {
  const log = {
    requests: [],
    lines: [],
    labels: [],
    texts: [],
    base: [],
    data: {},
    hostStep,
    loading: new Set(),
    developing: true,
  };
  const ctx = {
    save() {},
    restore() {},
    setLineDash() {},
    fillRect() {},
    fillText(t) {
      log.texts.push(t);
    },
  };
  const bv = {
    detectBarMs: () => log.hostStep * minute,
    syminfo: () => ({ ticker: 'QA', timezone: 'UTC', session: '24x7' }),
    session: () => null,
    time(t, s, z) {
      const [a, b] = s.split('-').map((x) => Number(x.slice(0, 2)) * 60 + Number(x.slice(3)));
      const m = tzMinutes(t, z);
      return a === b || (a < b ? m >= a && m < b : m >= a || m < b) ? t : null;
    },
    ma(v, n, type) {
      assert.equal(type, 'WMA');
      return v.map((_, i) => {
        if (i + 1 < n) return null;
        let sum = 0;
        for (let j = 0; j < n; j++) sum += v[i - n + 1 + j] * (j + 1);
        return sum / ((n * (n + 1)) / 2);
      });
    },
    fitScale() {},
    xOfIndex: () => 0,
    bounds: () => ({ left: 0, top: 0, right: 650, bottom: 400, width: 650, height: 400 }),
    visible: () => ({ from: 0, to: log.base.length + 3 }),
    font: () => ({ css: '11px sans-serif' }),
    priceText: (_, x) => x.toFixed(2),
    line: (_, s) => log.lines.push(s),
    label: (_, s) => log.labels.push(s),
    security(spec, expr) {
      assert.equal(spec.lookahead, false);
      log.requests.push(['native', spec.timeframe]);
      if (log.loading.has(spec.timeframe)) return undefined;
      const tf = tfVal(spec.timeframe),
        r = log.data[tf] || [],
        values = expr ? expr(r) : r;
      assert.equal(values.length, r.length, 'one value per requested bar');
      let j = -1;
      return log.base.map((b, i) => {
        const end = b.timestamp + log.hostStep * minute;
        // The last chart bar may receive a developing requested bar instead of the prior close.
        while (
          j + 1 < r.length &&
          (log.developing && i === log.base.length - 1
            ? r[j + 1].timestamp < end
            : r[j + 1].timestamp + tf * minute <= end)
        )
          j++;
        return j < 0 ? undefined : values[j];
      });
    },
    securityLowerTf(spec) {
      assert.equal(spec.lookahead, false);
      log.requests.push(['slices', spec.timeframe]);
      if (log.loading.has(spec.timeframe)) return undefined;
      const tf = tfVal(spec.timeframe),
        r = log.data[tf] || [],
        step = log.hostStep * minute;
      let j = 0;
      return log.base.map((b) => {
        const a = [];
        while (j < r.length && r[j].timestamp < b.timestamp) j++;
        while (j < r.length && r[j].timestamp < b.timestamp + step) a.push(r[j++]);
        return a;
      });
    },
  };
  const object = new Function('bv', source)(bv);
  // Test hooks instrument the source at its final factory return ONLY; shipped code exposes no debug API.
  const core = new Function(
    'bv',
    source.slice(0, source.indexOf('// BEGIN EDITOR LIFECYCLE')) +
      '\nreturn {cfg,packets,structure,curves,score,book,piece,covered,sessionMask,status,panelRows,rate,barEnd,tape};'
  )(bv);
  return { bv, log, ctx, object, core };
}
module.exports = { source, schema, minute, start, synthetic, agg, api };
