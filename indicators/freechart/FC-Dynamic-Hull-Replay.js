// The Free Chart • Script tab (factory BODY, not a module). Overlay ON.
// Chart-study rules, not the MT5 execution strategy. All state begins afresh in calc().
const TF = [15, 30, 45],
  MIN = 60000;
const COLORS = ['#51D3C2', '#73AEFF', '#E9ACF4'];
const finite = Number.isFinite;
const validBar = (b) =>
  b &&
  finite(b.timestamp) &&
  finite(b.open) &&
  finite(b.high) &&
  finite(b.low) &&
  finite(b.close) &&
  b.high >= Math.max(b.open, b.close) &&
  b.low <= Math.min(b.open, b.close);
const zeros = () => [0, 0, 0]; // wins / losses / definitely closed with unknown outcome
function cfg(x) {
  const c = {
    lots: 0.1,
    cash: 10,
    chartTick: 0,
    usdTick: 0,
    brokerTickSize: 0,
    pointIn: 0,
    targetMode: 'Gross USD',
    fixedTicks: 10,
    bePct: 0,
    inspectTF: '1m',
    clock: 'Next visible bar open',
    startAt: '',
    endAt: '',
    length: 16,
    n: 3,
    source: 'Dynamic',
    neutral: 'High',
    asia: '00:00-07:00',
    london: '08:00-16:00',
    ny: '08:00-17:00',
    kz: '13:00-16:00',
    focus: '15m',
    showHull: true,
    auditMarks: false,
    showPanel: true,
    showOpenTP: true,
    color15: COLORS[0],
    color30: COLORS[1],
    color45: COLORS[2],
    ...x,
  };
  function utc(s, fallback) {
    s = String(s || '').trim();
    if (!s) return fallback;
    // Treat zone-less clock text as UTC, never as the viewer's local time.
    if (!/^\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?Z?)?$/.test(s)) return NaN;
    const iso = s.length === 10 ? s + 'T00:00:00Z' : s.replace(' ', 'T').replace(/Z?$/, 'Z');
    const t = Date.parse(iso);
    return finite(t) && new Date(t).toISOString().slice(0, 10) === s.slice(0, 10) ? t : NaN;
  }
  c.from = utc(c.startAt, -Infinity);
  c.until = utc(c.endAt, Infinity);
  c.fineMs = parseInt(c.inspectTF, 10) * MIN;
  c.focusN = parseInt(c.focus, 10);
  c.full = c.clock === 'Completed-bar steps only';
  const ceil = (n, p) => Math.ceil(Number(n.toFixed(p)));
  c.ticks = NaN;
  if (c.chartTick > 0 && finite(c.chartTick)) {
    if (c.targetMode === 'Chart ticks (no USD conversion)') c.ticks = c.fixedTicks;
    else if (c.usdTick > 0 && c.lots > 0 && c.cash > 0) {
      const brokerTicks = Math.max(1, ceil(c.cash / (c.lots * c.usdTick), 9));
      c.ticks = Math.max(1, ceil((brokerTicks * (c.brokerTickSize || c.chartTick)) / c.chartTick, 8));
    }
  }
  if (!Number.isSafeInteger(c.ticks) || c.ticks <= 0) c.ticks = NaN;
  c.colors = [c.color15, c.color30, c.color45];
  return c;
}
function barEnd(t, span, next, exchange) {
  let end = t + span;
  if (finite(next) && next > t) end = Math.min(end, next);
  if (exchange.session && exchange.session !== '24x7') {
    const s = bv.session(t, exchange.session, exchange.timezone);
    if (s && s.end > t) end = Math.min(end, s.end);
  }
  return end;
}
// Confirm the rightmost equal pivot only after n right-hand bars. Test OLD
// structure levels before admitting pivots newly confirmed on the same close.
function structure(s, bars, i, n) {
  const b = bars[i],
    j = i - n,
    m = bars[j];
  let ph = i >= 2 * n,
    pl = ph;
  for (let k = 1; k <= n && (ph || pl); ++k) {
    if (m.high < bars[j - k].high || m.high <= bars[j + k].high) ph = false;
    if (m.low > bars[j - k].low || m.low >= bars[j + k].low) pl = false;
  }
  let d = s.d,
    ev = 0,
    level;
  if (d && s.guard && d * (b.close - s.guard.v) < 0) {
    ev = -2 * d;
    level = s.guard;
    s.guard = s.tip;
  } else if (d >= 0 && s.hi && b.close > s.hi.v && (d === 1 || (s.lastL && s.lastL.v < b.close))) {
    ev = d ? 1 : 3;
    level = s.hi;
    if (!d || (s.pullL && (!s.guard || s.pullL.v > s.guard.v))) s.guard = s.pullL || s.lastL;
  } else if (d <= 0 && s.lo && b.close < s.lo.v && (d === -1 || (s.lastH && s.lastH.v > b.close))) {
    ev = d ? -1 : -3;
    level = s.lo;
    if (!d || (s.pullH && (!s.guard || s.pullH.v < s.guard.v))) s.guard = s.pullH || s.lastH;
  }
  if (ev) {
    d = Math.sign(ev);
    s.broken = level.v;
    s.leg = i;
    s.origin = level.i;
    s.window = j;
    s.hi = s.lo = s.pullH = s.pullL = null;
    s.tip = { i, v: d === 1 ? b.high : b.low };
  } else if (d) {
    const v = d === 1 ? b.high : b.low;
    if (!s.tip || d * (v - s.tip.v) >= 0) s.tip = { i, v };
  }
  const inLeg = s.leg == null || j >= s.leg;
  const inWindow = s.window != null && j >= s.window && j > s.origin;
  if (ph) {
    const p = { i: j, v: m.high };
    s.lastH = p;
    if (d > 0 && s.pullL && j > s.pullL.i && (!s.guard || s.pullL.v > s.guard.v)) s.guard = s.pullL;
    const beyond = s.broken == null || p.v > s.broken;
    if ((inLeg || (d > 0 && !s.hi && inWindow)) && d >= 0 && beyond && (!s.hi || p.v >= s.hi.v)) {
      s.hi = p;
      s.pullL = null;
    }
    if (inLeg && d <= 0 && s.lo && j > s.lo.i && (!s.pullH || p.v >= s.pullH.v)) s.pullH = p;
  }
  if (pl) {
    const p = { i: j, v: m.low };
    s.lastL = p;
    if (d < 0 && s.pullH && j > s.pullH.i && (!s.guard || s.pullH.v < s.guard.v)) s.guard = s.pullH;
    const beyond = s.broken == null || p.v < s.broken;
    if ((inLeg || (d < 0 && !s.lo && inWindow)) && d <= 0 && beyond && (!s.lo || p.v <= s.lo.v)) {
      s.lo = p;
      s.pullH = null;
    }
    if (inLeg && d >= 0 && s.hi && j > s.hi.i && (!s.pullL || p.v <= s.pullL.v)) s.pullL = p;
  }
  s.d = d;
  return ev;
}
// Ask the platform's WMA only about finite inner values. Null warmup must not
// turn into zero when the Hull's outer smoothing pass begins.
function curves(bars, c) {
  const half = Math.max(1, Math.floor(c.length / 2)),
    root = Math.max(1, Math.round(Math.sqrt(c.length)));
  const w = (v, n) => bv.ma(v, n, 'WMA');
  const pad = (buf, n, k) => Array(k).fill(null).concat(w(buf, n));
  function aCurve(v) {
    const x = w(v, half),
      y = w(v, c.length),
      k = Math.min(v.length, c.length - 1),
      z = [];
    for (let i = k; i < v.length; i++) z.push(2 * x[i] - y[i]);
    return pad(z, root, k);
  }
  const close = bars.map((v) => v.close),
    x = w(close, Math.max(1, Math.floor(half / 3))),
    y = w(close, Math.max(1, Math.floor(half / 2))),
    z = w(close, half),
    k = Math.min(close.length, half - 1),
    inner = [];
  for (let i = k; i < close.length; i++) inner.push(3 * x[i] - y[i] - z[i]);
  const b = pad(inner, half, k),
    dynamic = c.source === 'Dynamic';
  const lo = dynamic || c.source === 'Low' ? aCurve(bars.map((q) => q.low)) : null;
  const hi = dynamic || c.source === 'High' ? aCurve(bars.map((q) => q.high)) : null;
  const fixed =
    lo || hi
      ? null
      : aCurve(
          bars.map((q) =>
            c.source === 'Open'
              ? q.open
              : c.source === 'Close'
                ? q.close
                : c.source === 'HLC3'
                  ? (q.high + q.low + q.close) / 3
                  : c.source === 'OHLC4'
                    ? (q.open + q.high + q.low + q.close) / 4
                    : (q.high + q.low) / 2
          )
        );
  return { lo, hi, fixed, b };
}
function packets(input, tf, host, c, exchange, cap) {
  // Each requested native candle is calculated ON ITS OWN SERIES. Small packets
  // retain prior native results: a larger host can jump across multiple closes,
  // and the editor may project a DEVELOPING requested candle on its last bar.
  const span = tf * MIN,
    back = 2 * Math.ceil(host / span) + 2,
    result = new Array(input.length);
  let size = 0;
  while (
    size < input.length &&
    validBar(input[size]) &&
    input[size].timestamp <= cap &&
    (!size || input[size].timestamp > input[size - 1].timestamp)
  )
    size++;
  const bars = input.slice(0, size),
    { lo, hi, fixed, b } = curves(bars, c),
    s = { d: 0 };
  let recent = [],
    exact = false,
    shortRun = 0,
    wrong = false;
  for (let i = 0; i < size; i++) {
    const q = bars[i],
      t = q.timestamp,
      end = barEnd(t, span, input[i + 1]?.timestamp, exchange);
    if (i) {
      const elapsed = t - bars[i - 1].timestamp;
      shortRun = elapsed < span ? shortRun + 1 : 0;
      if (shortRun >= 2) wrong = true;
      if (elapsed === span) exact = true;
    } // don't silently accept e.g. M1 for M45
    const ev = structure(s, bars, i, c.n),
      a =
        fixed ||
        (c.source === 'Low'
          ? lo
          : c.source === 'High'
            ? hi
            : s.d === 1
              ? lo
              : s.d === -1 || c.neutral === 'High'
                ? hi
                : null);
    let raw = 0;
    if (a && finite(a[i]) && finite(b[i]) && finite(a[i - 1]) && finite(b[i - 1]))
      raw = b[i] > a[i] && b[i - 1] < a[i - 1] ? 1 : b[i] < a[i] && b[i - 1] > a[i - 1] ? -1 : 0;
    const item = {
      i,
      t,
      end,
      ok: exact && !wrong,
      wrong,
      bias: s.d,
      ev,
      raw,
      a: a?.[i],
      b: b[i],
      o: q.open,
      h: q.high,
      l: q.low,
      c: q.close,
    };
    recent = recent.slice(1 - back);
    recent.push(item);
    result[i] = recent;
  }
  // Only as-of-time history can be projected; no counter based on future bars.
  // Invalid/duplicate bars invalidate later history, never the good prefix.
  const nextT = input[size]?.timestamp;
  const bad = size < input.length && !(nextT > cap);
  const lastGood = recent.length ? recent[recent.length - 1].end : -Infinity;
  const tail = bad
    ? recent.concat({ bad: true, t: Math.max(lastGood, finite(nextT) ? nextT : lastGood) })
    : recent;
  for (let i = size; i < input.length; i++) result[i] = tail;
  return result;
}
function tape(hostRows, host, c) {
  const span = c.fineMs,
    m = new Map();
  m.nextStamp = new Map();
  let prior = -Infinity;
  function add(b, carry = false) {
    if (!b || !finite(b.timestamp)) return;
    const t = b.timestamp;
    if (m.has(t)) {
      const old = m.get(t);
      if (
        !carry ||
        !old ||
        old.open !== b.open ||
        old.high !== b.high ||
        old.low !== b.low ||
        old.close !== b.close
      )
        m.set(t, null);
      return;
    }
    if (t < prior) {
      m.set(prior, null);
      m.set(t, null);
      return;
    }
    if (finite(prior)) m.nextStamp.set(prior, t);
    m.set(t, b);
    prior = t;
  }
  if (span === host) {
    for (const b of hostRows) add(b);
  } else if (span < host) {
    const xs = bv.securityLowerTf({ symbol: '', timeframe: c.inspectTF, lookahead: false });
    if (!xs) return null;
    for (const group of xs) if (group) for (const b of group) add(b);
  } else {
    // e.g. a 1m replay with a 5m inspection choice
    const xs = bv.security({ symbol: '', timeframe: c.inspectTF, lookahead: false }, (bars) =>
      bars.map((b, i) => [bars[i - 1], b])
    );
    if (!xs) return null;
    for (const group of xs) if (group) for (const b of group) add(b, true);
  }
  return m;
}
function microSuccessor(tape, t) {
  return tape.nextStamp?.get(t);
}
function covered(v, tape, c) {
  if (!tape || !tape.size) return false;
  const grid = (x) => Math.round(x / c.chartTick),
    count = Math.ceil((v.end - v.t) / c.fineMs);
  if (count <= 0 || count > 45) return false;
  let hi = -Infinity,
    lo = Infinity,
    tail;
  for (let k = 0; k < count; k++) {
    const t = v.t + k * c.fineMs,
      b = tape.get(t);
    if (!validBar(b) || (!k && grid(b.open) !== grid(v.o))) return false;
    // The source has no explicit microbar close stamp. Never trust a slice
    // that could include prints after a short native/session close.
    const next = microSuccessor(tape, t);
    if (t + c.fineMs > v.end || (finite(next) && next < Math.min(t + c.fineMs, v.end))) return false;
    hi = Math.max(hi, grid(b.high));
    lo = Math.min(lo, grid(b.low));
    tail = b;
  }
  return hi === grid(v.h) && lo === grid(v.l) && grid(tail.close) === grid(v.c);
}
// Same discrete-price evidence rule as Pine f_piece. No guessed O-H-L-C path.
function piece(p, o, h, l, c, unit) {
  const g = (x) => p.d * (Math.round(x / unit) - p.grid),
    op = g(o),
    best = g(p.d === 1 ? h : l),
    worst = g(p.d === 1 ? l : h),
    cp = g(c);
  const a = p.armed,
    away = p.away,
    maybe = p.maybe,
    goal = p.ticks;
  if (op >= goal) return maybe ? 2 : 1;
  if (a && (op < 0 || (op === 0 && away))) return -1;
  if (best >= goal)
    return maybe || (a && (worst < 0 || (worst === 0 && (op > 0 || away || goal > 1)))) ? 2 : 1;
  if (a && (worst < 0 || (worst === 0 && (op > 0 || away || (best > 0 && cp === 0))))) return -1;
  if (a && !away && op === 0 && worst === 0 && best > 0) p.maybe = true;
  p.away = away || best !== 0 || worst !== 0;
  return 0;
}
function sessionMask(t, c) {
  const inWin = (window, tz) => bv.time(t, window, tz) != null;
  const a = inWin(c.asia, 'UTC'),
    l = inWin(c.london, 'Europe/London'),
    n = inWin(c.ny, 'America/New_York');
  return 1 | (a ? 2 : 0) | (l ? 4 : 0) | (n ? 8 : 0) | (l && n && inWin(c.kz, 'Europe/London') ? 16 : 0);
}
function book() {
  return {
    counts: [zeros(), zeros(), zeros(), zeros(), zeros()],
    p: null,
    fine: 0,
    coarse: 0,
    be: 0,
    mixed: 0,
    held: 0,
    last: null,
    first: null,
    lastTest: null,
    seen: -1,
    problem: '',
    entered: 0,
    exit: 0,
  };
}
function score(k, v, c, m, hostIndex) {
  k.entered = k.exit = 0;
  k.last = v;
  if (!v.ok || v.end < c.from || v.end > c.until) return;
  if (k.first === null) k.first = v.end;
  k.lastTest = v.end;
  if (!finite(c.ticks)) return;
  const p = k.p;
  if (p) {
    let e = 0;
    if (covered(v, m, c)) {
      k.fine++;
      for (let t = v.t; t < v.end; t += c.fineMs) {
        const b = m.get(t);
        e = piece(p, b.open, b.high, b.low, b.close, c.chartTick);
        if (e) break;
      }
    } else {
      k.coarse++;
      e = piece(p, v.o, v.h, v.l, v.c, c.chartTick);
    }
    if (!e && v.raw === -p.d) e = p.maybe ? -3 : -2;
    if (e) {
      // Copy on settlement: browsing an earlier visible candle must never see later totals.
      k.counts = k.counts.map((v) => v.slice());
      k.exit = e;
      const col = e === 1 ? 0 : e === 2 ? 2 : 1;
      for (let g = 0; g < 5; g++) if (p.mask & (1 << g)) k.counts[g][col]++;
      if (e === -1) k.be++;
      if (e === -3) k.mixed++;
      k.p = null;
    } else {
      const gain = p.d * (Math.round(v.c / c.chartTick) - p.grid);
      p.armed = p.armed || gain >= Math.ceil((p.ticks * c.bePct) / 100);
      if (p.maybe && v.raw === p.d && v.bias === p.d) k.held++;
    }
  }
  // Resolve the OLD trade first; its entry-session membership is immutable.
  if (!k.p && v.raw && v.raw === v.bias) {
    const grid = Math.round(v.c / c.chartTick),
      tp = (grid + v.raw * c.ticks) * c.chartTick;
    k.p = {
      d: v.raw,
      at: v.end,
      index: hostIndex,
      entry: v.c,
      grid,
      tp,
      ticks: c.ticks,
      mask: sessionMask(v.end, c),
      armed: c.bePct === 0,
      away: false,
      maybe: false,
    };
    k.entered = v.raw;
  }
}
function rate(v) {
  if (!v) return 'n/a';
  const [w, l, u] = v,
    n = w + l + u;
  if (!n) return '— (0)';
  const s = u
    ? (Math.floor((1000 * w) / n) / 10).toFixed(1) + '–' + (Math.ceil((1000 * (w + u)) / n) / 10).toFixed(1)
    : ((100 * w) / n).toFixed(1);
  return s + '% (' + n + ')';
}
function sum(rows) {
  return rows.every(Boolean) ? rows.reduce((p, v) => [p[0] + v[0], p[1] + v[1], p[2] + v[2]], zeros()) : null;
}
function status(k, c) {
  if (k.problem) return k.problem;
  if (!k.last) return 'waiting';
  if (!k.last.ok) return 'check feed';
  if (k.first == null) return 'no window';
  if (!finite(c.ticks)) return 'set sizing';
  return '';
}
function panelRows(model, price) {
  const { books, c, stamp } = model,
    states = books.map((k) => status(k, c)),
    all = states.every((s) => !s);
  const rows = [['Win rate (n)', '15m', '30m', '45m', 'All TFs']];
  for (const [i, name] of ['Overall', 'Asian', 'London', 'New York', 'NY–Lon KZ'].entries()) {
    const counts = books.map((k, j) => (states[j] ? null : k.counts[i]));
    rows.push([name, ...counts.map(rate), rate(sum(counts))]);
  }
  const totals = books.map((k, i) => (states[i] ? null : k.counts[0]));
  const wlu = (v) => (v ? v.join('/') : 'n/a');
  rows.push(['W / L / ?', ...totals.map(wlu), wlu(sum(totals))]);
  const pct = (f, b) => (f + b ? ((100 * f) / (f + b)).toFixed(0) + '%' : '—');
  rows.push([
    c.inspectTF + ' coverage',
    ...books.map((k, i) => (states[i] ? '—' : pct(k.fine, k.coarse))),
    all
      ? pct(
          books.reduce((s, k) => s + k.fine, 0),
          books.reduce((s, k) => s + k.coarse, 0)
        )
      : 'n/a',
  ]);
  const side = (p) => (p.d === 1 ? 'BUY' : 'SELL') + (p.maybe ? '?' : '');
  rows.push([
    'Position',
    ...books.map((k, i) => states[i] || (k.p ? side(k.p) : 'flat')),
    all ? books.filter((k) => k.p).length + ' held' : 'n/a',
  ]);
  rows.push([
    'Open TP price',
    ...books.map((k, i) => (!states[i] && k.p && stamp <= c.until ? price(k.p.tp) : '—')),
    '—',
  ]);
  const pts = (c.ticks * c.chartTick) / (c.pointIn || c.chartTick);
  rows.push([
    'TP Δ / pts',
    ...books.map((k, i) =>
      !states[i] ? price(c.ticks * c.chartTick) + ' / ' + Number(pts.toFixed(4)) : '—'
    ),
    c.targetMode === 'Gross USD' ? 'USD' : 'ticks',
  ]);
  return rows;
}
function board(args, rows, note, colors) {
  const r = bv.bounds(args),
    ctx = args.ctx;
  const w = Math.min(550, r.width - 12);
  if (w < 100 || r.height < 22) return;
  const x = r.right - w - 6,
    y = r.top + 6,
    line = Math.max(12, Math.min(18, (r.height - 34) / (rows.length || 1)));
  const left = Math.min(94, w * 0.22),
    col = (w - left) / 4;
  ctx.save();
  ctx.setLineDash([]);
  ctx.textBaseline = 'middle';
  ctx.font = bv.font(args, { size: w < 390 ? 9 : 11, weight: '500' }).css;
  ctx.fillStyle = '#142331';
  ctx.fillRect(x, y, w, rows.length * line + 22);
  for (let i = 0; i < rows.length; i++) {
    ctx.fillStyle = i === 0 ? '#243746' : i === 1 ? '#1D4146' : i % 2 === 0 ? '#192C3B' : '#142331';
    ctx.fillRect(x, y + i * line, w, line);
    for (let j = 0; j < 5; j++) {
      const xx = j ? x + left + (j - 0.5) * col : x + 6;
      ctx.fillStyle =
        i === 0 && j >= 1 && j <= 3 ? colors[j - 1] : i === 9 ? '#B1F7E8' : j === 4 ? '#AFDBE6' : '#EDF4F7';
      ctx.textAlign = j ? 'center' : 'left';
      ctx.fillText(rows[i][j], xx, y + (i + 0.5) * line, (j ? col : left) - 6);
    }
  }
  ctx.fillStyle = '#CCD7E0';
  ctx.textAlign = 'left';
  ctx.fillText(note, x + 6, y + rows.length * line + 11, w - 12);
  ctx.restore();
}
// BEGIN EDITOR LIFECYCLE
return {
  // Target guides are canvas-only. A platform-owned *latest* price-scale tag
  // must not disclose a later TP when replay/scroll stops on an earlier bar.
  figures: [
    { key: 'a', title: 'Focus Hull A: ', type: 'line', styles: () => ({ color: '#AACED6', size: 1 }) },
    { key: 'b', title: 'Focus Hull B: ', type: 'line', styles: () => ({ color: '#E0A870', size: 1 }) },
  ],
  calc(dataList, indicator) {
    const c = cfg(indicator.extendData?.inputs || {}),
      size = dataList.length,
      books = TF.map(book);
    const model = { c, books, stamp: 0, notice: '', index: size - 1 };
    indicator._fcReplay = model; // replace on every call, including empty/rewind
    const blank = (msg) => {
      model.notice = msg;
      return dataList.map(() => ({}));
    };
    if (size < 2) return blank('Waiting for chart candles.');
    const host = bv.detectBarMs(dataList),
      exchange = bv.syminfo() || {};
    if (!finite(host) || host < MIN || host > 45 * MIN || host % MIN !== 0)
      return blank('Use standard 1m–45m candles. 15m is recommended.');
    if (!(c.from <= c.until)) return blank('Check Test from/through. Use ISO UTC, e.g. 2026-11-18T08:00Z.');
    if (
      !Number.isInteger(c.n) ||
      c.n < 1 ||
      c.n > 50 ||
      !Number.isInteger(c.length) ||
      c.length < 2 ||
      c.length > 500
    )
      return blank('Invalid Hull or swing length.');
    for (let i = 0; i < size; i++)
      if (!validBar(dataList[i]) || (i && dataList[i].timestamp <= dataList[i - 1].timestamp))
        return blank('Invalid, duplicate or unordered host candles.');
    c.from = Math.max(c.from, dataList[0].timestamp);
    const clock = (i) =>
      c.full
        ? barEnd(dataList[i].timestamp, host, dataList[i + 1]?.timestamp, exchange)
        : dataList[i].timestamp;
    const cap = clock(size - 1);
    model.stamp = cap;
    let m;
    try {
      m = tape(dataList, host, c);
    } catch (_) {
      m = new Map();
      model.microNote = c.inspectTF + ' feed unavailable · using native OHLC';
    }
    // Undefined = request is still loading. Known-empty groups use native OHLC.
    if (m === null) return blank('Loading ' + c.inspectTF + ' exit-inspection candles…');
    const streams = TF.map((tf, j) => {
      try {
        const expr = (rs) => packets(rs, tf, host, c, exchange, cap);
        const v =
          host === tf * MIN
            ? expr(dataList)
            : bv.security({ symbol: '', timeframe: tf + 'm', lookahead: false, gaps: false }, expr);
        if (!v) books[j].problem = 'loading';
        return v;
      } catch (_) {
        books[j].problem = 'no ' + tf + 'm feed';
        return null;
      }
    });
    const result = new Array(size);
    const snapshot = (k) => ({
      seen: k.seen,
      last: k.last,
      first: k.first,
      problem: k.problem,
      counts: k.counts,
      fine: k.fine,
      coarse: k.coarse,
      p: k.p ? { ...k.p } : null,
    });
    let view = books.map(snapshot);
    for (let i = 0; i < size; i++) {
      const asof = clock(i),
        row = {};
      result[i] = row;
      for (let j = 0; j < 3; j++) {
        const k = books[j],
          tf = TF[j],
          pack = streams[j]?.[i];
        if (!pack || k.problem) continue;
        for (const v of pack) {
          if (v.bad) {
            if (v.t <= asof) k.problem = 'bad history';
            break;
          }
          if (v.end > asof || v.i <= k.seen) continue;
          if (v.wrong) {
            k.problem = 'wrong interval';
            break;
          }
          // If a host hole hid requested closes, don't pretend a complete book.
          if (k.seen >= 0 && v.i !== k.seen + 1) {
            k.problem = 'history gap';
            break;
          }
          k.seen = v.i;
          score(k, v, c, m, i);
          if (c.auditMarks && tf === c.focusN && (k.entered || k.exit)) {
            if (!row.marks) row.marks = [];
            if (k.exit)
              row.marks.push({
                px: v.h,
                tag: k.exit === 1 ? 'TP' : k.exit === 2 ? '?' : k.exit === -1 ? 'BE' : 'L',
                up: false,
              });
            if (k.entered)
              row.marks.push({
                px: v.c,
                tag: tf + 'm ' + (k.entered === 1 ? 'BUY' : 'SELL'),
                up: k.entered === 1,
              });
          }
        }
        if (k.problem || !k.last) continue;
        if (c.showHull && tf === c.focusN) {
          row.a = k.last.a ?? undefined;
          row.b = k.last.b ?? undefined;
        }
        if (k.p && asof <= c.until) {
          if (c.showOpenTP) row['tp' + tf] = k.p.tp;
        }
      }
      // Share immutable snapshots between host bars until a native book advances.
      if (books.some((k, j) => k.seen !== view[j].seen || k.problem !== view[j].problem))
        view = books.map((k, j) =>
          k.seen === view[j].seen && k.problem === view[j].problem ? view[j] : snapshot(k)
        );
      row.books = view;
      row.asOf = asof;
    }
    model.digits =
      c.chartTick > 0
        ? Math.min(8, (c.chartTick.toFixed(8).replace(/0+$/, '').split('.')[1] || '').length)
        : 2;
    return result;
  },
  draw(args) {
    const { indicator } = args,
      model = indicator._fcReplay;
    if (!model) return false;
    const { c } = model,
      range = bv.visible(args),
      result = indicator.result || [];
    let index = Math.min(model.index, Math.floor(range.to));
    // Some visible ranges include a padding candle beyond the right-hand edge.
    if (index >= 0 && bv.xOfIndex(args, index) > bv.bounds(args).right) index--;
    const current = result[index],
      books = current?.books || model.books,
      stamp = current?.asOf ?? model.stamp;
    const price = (x) => bv.priceText(args, x, model.digits);
    if (c.auditMarks) {
      const lo = Math.max(0, Math.floor(range.from)),
        hi = Math.min(index, Math.ceil(range.to));
      for (let i = lo; i <= hi; i++)
        for (const v of result[i]?.marks || [])
          bv.label(args, {
            index: i,
            price: v.px,
            text: v.tag,
            color: '#F0F7F9',
            background: '#29475B',
            align: 'center',
            baseline: v.up ? 'top' : 'bottom',
            offsetY: v.up ? 6 : -6,
            size: 10,
          });
    }
    if (!model.notice && c.showOpenTP && stamp <= c.until) {
      for (let j = 0; j < 3; j++) {
        const k = books[j],
          p = k.p;
        if (!p || k.problem || p.index > range.to || index + 3 < range.from) continue;
        const right = Math.min(index + 3, Math.floor(range.to)),
          left = Math.max(p.index, Math.ceil(range.from));
        bv.fitScale(args, { fromIndex: left, toIndex: Math.max(left, right), top: p.tp, bottom: p.tp });
        if (right > left)
          bv.line(args, {
            fromIndex: left,
            toIndex: right,
            price: p.tp,
            color: c.colors[j],
            dash: [6, 3],
            width: 2,
          });
        bv.label(args, {
          index: Math.min(index, right),
          price: p.tp,
          text: TF[j] + 'm ' + (p.d === 1 ? 'BUY' : 'SELL') + (p.maybe ? '?' : '') + ' TP ' + price(p.tp),
          color: '#142331',
          background: c.colors[j],
          align: 'right',
          baseline: 'bottom',
          size: 10,
          offsetY: -2,
        });
      }
    }
    if (c.showPanel || model.notice || !finite(c.ticks)) {
      const states = books.map((k) => status(k, c));
      if (model.timeKey !== stamp && current) {
        model.timeKey = stamp;
        model.timeText = new Date(stamp).toISOString().slice(0, 16).replace('T', ' ') + 'Z';
      }
      const note =
        model.notice ||
        (!finite(c.ticks)
          ? 'Inputs: set chart tick + USD/tick at 1 lot, or choose Chart ticks.'
          : states.some((s) => s === 'loading')
            ? 'Loading native candles…'
            : states.some((s) => s === 'history gap')
              ? 'Missing native closes. Reload history or use a smaller chart interval.'
              : model.microNote ||
                (model.timeText || '') +
                  (c.full ? ' · closed-bar replay' : ' · at visible open') +
                  ' · gross TP');
      // Cheap, bounded 3-lane formatting only when the visible book or cutoff changes.
      if (!model.notice && (!current || !current.books)) return false;
      const outside = stamp > c.until;
      if (!model.notice && (model.displayBooks !== books || model.afterEnd !== outside)) {
        model.displayBooks = books;
        model.afterEnd = outside;
        model.rows = panelRows({ books, c, stamp }, (px) => px.toFixed(model.digits));
      }
      board(args, model.rows || [], note, c.colors);
    }
    return false; // let KlineCharts paint the focus curves
  },
};
