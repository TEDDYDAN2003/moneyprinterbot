// No launcher code runs on file://, sample/import, ordinary reporting, or hosted mirrors.
(() => {
  const token = document.documentElement.dataset.demoKey;
  if (
    !isCollector ||
    !token ||
    location.protocol !== 'http:' ||
    !['127.0.0.1', 'localhost'].includes(location.hostname)
  )
    return;
  const box = $('demoDesk'),
    dlg = $('demoConfirm');
  const labels = {
    idle: 'Not started here',
    checking: 'Checking',
    ready: 'Ready for review',
    blocked: 'Blocked',
    starting: 'Starting · unverified',
    running: 'Demo armed',
    exited: 'Process exited'
  };
  let info = {},
    ticket = '',
    tag = '',
    inAction = false,
    seq = 0,
    fault = '';
  const live = () =>
    source === 'live' &&
    state.state === 'live' &&
    !networkErr &&
    lastReply &&
    Date.now() - lastReply < 30000 &&
    Date.now() - Date.parse(state.last_sync) < 30000;
  const allowed = () =>
    live() &&
    info.verified &&
    !fault &&
    !inAction &&
    info.phase === 'ready' &&
    !!ticket &&
    tag === info.review_id &&
    info.remaining > 0;
  function dismiss() {
    if (dlg.open) dlg.close();
    $('demoPhrase').value = '';
    $('demoArm').disabled = true;
  }
  function sync() {
    $('workspaceMode').textContent = source === 'live' ? 'LOCAL DEMO CONTROLS' : 'READ-ONLY WORKSPACE';
    box.hidden = source !== 'live';
    if (box.hidden || !live()) {
      ticket = '';
      dismiss();
    }
    $('demoCheck').disabled =
      !live() ||
      !info.verified ||
      !!fault ||
      inAction ||
      ['starting', 'running', 'checking'].includes(info.phase);
    $('demoOpen').disabled = !allowed();
    $('demoArm').disabled = !allowed() || $('demoPhrase').value !== 'ARM DEMO';
  }
  function display() {
    box.dataset.phase = fault ? 'unavailable' : info.phase || 'idle';
    $('demoBadge').textContent = fault ? 'Unavailable' : labels[info.phase] || 'Connecting';
    $('demoState').textContent =
      fault ||
      (!info.verified && ['idle', 'ready', 'blocked'].includes(info.phase)
        ? 'Waiting for a fresh USD hedging demo snapshot from the selected MT5 installation.'
        : info.message || 'Waiting for a verified journal connection.') +
        (info.pid ? ' · PID ' + info.pid : '') +
        (info.phase === 'ready'
          ? tag === info.review_id && ticket
            ? ' · ' + info.remaining + 's left'
            : ' · Run a new preflight in this tab to confirm.'
          : '');
    const p = info.review || {},
      rows = p.profiles || [];
    $('demoReview').hidden = !rows.length;
    $('demoProfiles').replaceChildren(
      ...rows.map((s) => {
        const tr = el('tr');
        for (const v of [
          s.symbol,
          s.broker,
          s.lots,
          s.target_usd,
          s.rate,
          s.legs.join(' / '),
          s.max_batch_lots
        ])
          tr.append(el('td', '', String(v)));
        return tr;
      })
    );
    const pair = Object.entries(p.pairs || {})
      .map(([a, b]) => a + ' → ' + b)
      .join(', ');
    $('demoContext').textContent = rows.length
      ? `USD hedging demo ${p.account_hint ? p.account_hint + ' · ' : ''}key ${p.account} · magic ${p.magic} · ${pair} · ${p.route}${p.route === 'direct_retest_limit' ? (p.market_fallback ? ' (market fallback ON)' : ' (market fallback OFF)') : ''} · ${p.exit_policy} · ${p.positions} open / ${p.orders} pending`
      : '';
    $('demoIssues').replaceChildren(
      ...[...(p.issues || []), ...(p.notes || [])].map((x) => el('li', '', x))
    );
    if (!inAction && (info.phase !== 'ready' || tag !== info.review_id)) {
      ticket = '';
      dismiss();
    }
    sync();
  }
  async function refresh() {
    if (source !== 'live') return sync();
    const n = ++seq;
    try {
      const r = await fetch('/api/demo/status', {
        cache: 'no-store',
        signal: AbortSignal.timeout(8000)
      });
      if (!r.ok) throw Error();
      const v = await r.json();
      if (n !== seq) return;
      info = v;
      fault = '';
    } catch (_) {
      if (n !== seq) return;
      ticket = '';
      fault = 'Control service unavailable. Check the scanner console before retrying.';
      dismiss();
    }
    display();
  }
  async function action(name, body) {
    if (inAction || !live()) return;
    inAction = true;
    const sourceEpoch = epoch;
    message('');
    ++seq;
    fault = '';
    ticket = '';
    sync();
    $('demoState').textContent =
      name === 'check'
        ? 'Checking MT5, private profiles and local regression tests. This can take a few minutes.'
        : 'Waiting for scanner startup checks…';
    try {
      const r = await fetch('/api/demo/' + name, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Hull-Control': token },
        body: JSON.stringify(body),
        cache: 'no-store'
      });
      const v = await r.json();
      if (!r.ok) throw Error(v.error || 'Action rejected.');
      if (name === 'check' && v.ok && sourceEpoch === epoch && live()) {
        ticket = v.ticket;
        tag = v.review_id;
      }
    } catch (e) {
      message(e.message + ' No automatic retry. Check the Windows desktop.');
    } finally {
      inAction = false;
      await refresh();
    }
  }
  $('demoCheck').onclick = () => action('check', {});
  $('demoOpen').onclick = () => {
    if (!allowed()) return;
    const p = info.review;
    $('demoConfirmPlan').textContent =
      `Demo ${p.account_hint ? p.account_hint + ' · ' : ''}key ${p.account} · ${p.route} · ` +
      p.profiles
        .map(
          (x) =>
            `${x.broker}: ${x.legs.join('/')} × ${x.lots} lots, $${x.target_usd} base per ticket (${x.rate})`
        )
        .join('; ');
    $('demoPhrase').value = '';
    $('demoArm').disabled = true;
    dlg.showModal();
    $('demoPhrase').focus();
  };
  $('demoPhrase').oninput = sync;
  $('demoCancel').onclick = dismiss;
  $('demoArm').onclick = () => {
    if (!allowed() || $('demoPhrase').value !== 'ARM DEMO') return;
    const t = ticket;
    dismiss();
    action('arm', { ticket: t, confirmation: 'ARM DEMO' });
  };
  demoViewChanged = sync;
  sync();
  refresh();
  setInterval(() => {
    if (!inAction) refresh();
  }, 2500);
})();
