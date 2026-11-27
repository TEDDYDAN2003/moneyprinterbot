// Embedded in the reporter only on explicit opt-in. A preflight is never a fill.
(() => {
  const key = document.documentElement.dataset.targetKey;
  if (!key || !isCollector) return;
  let model = null, busy = false;
  const inputs = new Map();
  const msg = text => { $('targetMsg').textContent = text; };
  const changes = () => {
    const edits = Object.create(null);
    for (const row of model?.profiles || []) {
      const node = inputs.get(row.symbol);
      const v = node.value === '' ? null : node.valueAsNumber;
      if (!node.validity.valid || (v !== null && (!Number.isFinite(v) || v <= 0))) throw Error('Enter positive USD bases, or leave an override blank to inherit.');
      if (v !== row.override) edits[row.symbol] = v;
    }
    return edits;
  };
  const buttons = () => {
    let valid = true, dirty = false;
    try { dirty = Object.keys(changes()).length !== 0; } catch (_) { valid = false; }
    $('targetSave').disabled = !model || busy || !valid || !dirty;
    $('targetCheck').disabled = !model || busy || !valid || dirty;
    $('targetReload').disabled = busy;
    for (const input of inputs.values()) input.disabled = busy;
  };
  function draw(result) {
    model = result; inputs.clear(); $('targetRows').replaceChildren();
    for (const r of result.profiles) {
      const tr = el('tr'), cell = value => tr.appendChild(el('td', '', value));
      cell(`${r.symbol}${r.broker ? ' / ' + r.broker : ''} · ${r.lots} · ${r.rate}`);
      cell(String(r.yaml_base));
      const input = el('input'); input.type = 'number'; input.step = 'any'; input.min = '0';
      input.placeholder = String(r.yaml_base); input.value = r.override ?? '';
      input.setAttribute('aria-label', r.symbol + ' USD base override');
      cell('').appendChild(input); inputs.set(r.symbol, input);
      input.oninput = () => {
        for (const x of $('targetRows').querySelectorAll('[data-preview]')) x.textContent = '—';
        msg('Save changes, then run a fresh preflight.'); buttons();
      };
      for (const side of ['BUY','SELL']) {
        const p = r.preview?.[side];
        cell(!p ? '—' : p.status !== 'indicative' ? p.status : `$${nfmt.format(p.entry_spread_usd)} → $${nfmt.format(p.initial_goal_usd)}`).dataset.preview = side;
      }
      cell(r.quote_ms ? new Date(r.quote_ms).toISOString() : '—').dataset.preview = 'time';
      $('targetRows').appendChild(tr);
    }
  }
  async function action(verb) {
    if (busy || source !== 'live') return;
    busy = true; buttons(); msg(verb === 'preflight' ? 'Reading current demo quotes and checking profiles…' : 'Reading settings…');
    try {
      const options = {cache:'no-store'};
      if (verb) Object.assign(options, {method:'POST', headers:{'Content-Type':'application/json','X-Hull-Target-Key':key}, body:JSON.stringify(verb === 'save' ? {revision:model.revision,changes:changes()} : {revision:model.revision})});
      const res = await fetch('/api/targets' + (verb ? '/' + verb : ''), options);
      const result = await res.json(); if (!res.ok) throw Error(result.error || 'Settings unavailable.');
      draw(result);
      msg(verb === 'preflight' ? ['Indicative only. No orders sent.', ...(result.review || [])].join(' ') : verb === 'save' ? 'Saved for the next preflight and new scanner starts.' : 'Saved settings. Run preflight for current spread and goals.');
    } catch (e) { for (const x of $('targetRows').querySelectorAll('[data-preview]')) x.textContent = '—'; msg(e.message); }
    finally {busy = false; buttons();}
  }
  function visibility() {
    $('targetControls').hidden = source !== 'live';
    $('targetHelp').hidden = source === 'live';
  }
  visibility();
  $('targetSettings').ontoggle = () => {visibility(); if ($('targetSettings').open && !model) action('');};
  $('targetReload').onclick = () => action(''); $('targetSave').onclick = () => action('save'); $('targetCheck').onclick = () => action('preflight');
  document.addEventListener('hull-source', visibility);
})();
