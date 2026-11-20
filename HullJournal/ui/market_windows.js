// Fixed public quote study. Nothing in this module writes to the collector or scanner.
const marketStudy = JSON.parse($('marketEvidence').textContent);
const broadKeys = ['Asia-Pacific', 'London', 'London + New York', 'New York'];
const bellKeys = ['Tokyo open', 'Frankfurt open', 'New York open'];
const bucketLabels = {'London': 'London before NY', 'New York': 'NY after London'};
const labelWindow = k => bucketLabels[k] || k;
const bellClocks = [
  {symbol:'JP225m', key:bellKeys[0], city:'Tokyo', zone:'Asia/Tokyo', minute:540, local:'09:00 JST', eat:'03:00 year-round', utc:'00:00 year-round'},
  {symbol:'DE30m', key:bellKeys[1], city:'Frankfurt', zone:'Europe/Berlin', minute:540, local:'09:00 Berlin', eat:'10:00 summer / 11:00 winter', utc:'07:00 summer / 08:00 winter'},
  {symbol:'US500m', key:bellKeys[2], city:'New York', zone:'America/New_York', minute:570, local:'09:30 ET', eat:'16:30 summer / 17:30 winter', utc:'13:30 summer / 14:30 winter'}
];
const clockFormat = new Map(), bellCache = new Map();
const reliable = s => !!s && s.days >= 10 && s.n >= 100;
const fmtBp = n => Number.isFinite(n) ? n.toFixed(2) : '—';
const publicMarket = sym => marketStudy.markets.find(m => m.symbol === sym);
let useBells=false, sortWindow=broadKeys[0], activityDesc=true, focusExport=[];

function broadBucket(iso) {
  const group=sessionAt(iso), names=group.split(' + ');
  if(names.includes('London') && names.includes('New York')) return broadKeys[2];
  if(names.includes('London')) return broadKeys[1];
  if(names.includes('New York')) return broadKeys[3];
  if(names.includes('Tokyo') || names.includes('Sydney')) return broadKeys[0];
  return group;
}
function bellBucket(iso) {
  const key=Math.floor(Date.parse(iso)/60000);
  if(!Number.isFinite(key)) return 'Time unavailable';
  if(bellCache.has(key)) return bellCache.get(key);
  let tag='Outside these hours';
  try {
    for(const b of bellClocks) {
      if(!clockFormat.has(b.zone)) clockFormat.set(b.zone,new Intl.DateTimeFormat('en-GB',{timeZone:b.zone,weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}));
      const p=Object.fromEntries(clockFormat.get(b.zone).formatToParts(key*60000).map(x=>[x.type,x.value]));
      const wall=(Number(p.hour)%24)*60+Number(p.minute);
      if(!['Sun','Sat'].includes(p.weekday) && b.minute<=wall && wall<b.minute+60) {tag=b.key; break;}
    }
  } catch {return 'Timezone data unavailable';}
  if(bellCache.size>160000) bellCache.clear();
  bellCache.set(key,tag); return tag;
}
function strongest(m, keys, metric='range') {
  return keys.filter(k=>reliable(m.stats[k])).sort((a,b)=>m.stats[b][metric]-m.stats[a][metric])[0];
}
function selectWindows(bells, key) {
  useBells=bells; sortWindow=key || (bells?bellKeys:broadKeys)[0]; activityDesc=true; renderResearch(shown);
}
function focusOn(m) {
  $('focusKey').value=m.symbol; renderResearch(shown); $('focusMarket').scrollIntoView({block:'start'});
}
function drawBellCards() {
  const tz=$('zone').value;
  $('openingCards').replaceChildren(...bellClocks.map((b,i)=>{
    const m=publicMarket(b.symbol), btn=el('button','bellCard'); btn.type='button';
    btn.setAttribute('aria-pressed',String(useBells && sortWindow===b.key && $('focusKey').value===m.symbol));
    btn.title=`Compare ${m.key} during the ${b.city} opening hour`;
    const top=el('span','bellTop');top.append(el('small','',`0${i+1} / CASH OPEN`),el('span','','↗'));
    const meta=el('span','bellMetric');meta.append(el('b','',fmtBp(m.stats[b.key]?.range)+' bp'),el('span','','typical M5 range'));
    btn.append(top,el('strong','bellCity',b.city),el('span','bellInstrument',m.key+' · '+b.local),meta,el('small','bellTime',`${tz}: ${tz==='EAT'?b.eat:b.utc}`));
    btn.onclick=()=>{$('focusKey').value=m.symbol; $('activityKind').value=''; $('activityMetric').value='range'; selectWindows(true,b.key);};return btn;
  }));
}
function drawMatrix() {
  const keys=useBells?bellKeys:broadKeys, metric=$('activityMetric').value;
  const kind=$('activityKind').value;
  const ordered=marketStudy.markets.filter(m=>!kind||m.kind===kind).sort((a,b)=>{
    const x=a.stats[sortWindow],y=b.stats[sortWindow];
    if(reliable(x)!==reliable(y)) return reliable(x)?-1:1;
    return (reliable(x)?(activityDesc?-1:1)*(x[metric]-y[metric]):0) || a.key.localeCompare(b.key);
  });
  $('broadWindows').setAttribute('aria-pressed',String(!useBells)); $('cashWindows').setAttribute('aria-pressed',String(useBells));
  const metricName=metric==='range'?'range':'spread';
  const top=ordered.find(m=>reliable(m.stats[sortWindow]));
  $('matrixSummary').textContent=top?`${labelWindow(sortWindow)}: ${top.key} has the ${activityDesc?'widest':'narrowest'} typical ${metricName} (${fmtBp(top.stats[sortWindow][metric])} bp) in this selection (${ordered.length} ${ordered.length===1?'market':'markets'}). Numbers are per five-minute bar.`:'Insufficient observations for this ranking.';
  const h=$('matrixHead');h.replaceChildren(el('th','','Market / underlying'));
  for(const k of keys) {
    const th=el('th','num');th.scope='col';th.setAttribute('aria-sort',sortWindow===k?(activityDesc?'descending':'ascending'):'none');
    const btn=el('button','',labelWindow(k)+(k===sortWindow?(activityDesc?' ↓':' ↑'):' ↕')); btn.title='Rank by '+labelWindow(k);
    btn.onclick=()=>{activityDesc=sortWindow===k?!activityDesc:true;sortWindow=k;drawMatrix();drawBellCards();};th.append(btn);h.append(th);
  }
  $('matrixBody').replaceChildren(...ordered.map(m=>{
    const tr=el('tr'), td=el('td'), btn=el('button','marketKey',m.key+' ↘');btn.type='button';btn.title='Inspect '+m.symbol;btn.onclick=()=>focusOn(m);
    td.append(btn,el('small','marketBase',m.base));tr.append(td);
    const peak=strongest(m,keys,metric);
    for(const k of keys) {
      const s=m.stats[k], good=reliable(s), cell=el('td','num mono');
      const value=el('span',good&&k===peak?'refPeak':'refCell',good?fmtBp(s[metric]):'—');
      if(good&&k===peak) value.append(el('small','peakWord','peak'));
      cell.title=s?`${s.n.toLocaleString('en-US')} complete bars on ${s.days} UTC dates${good?'':' · too thin to rank'}`:'No qualifying quotes';
      cell.append(value);tr.append(cell);
    }
    return tr;
  }));
}
function journalLabel() {
  if(source==='sample' || [...data.tickets.slice(0,1),...data.setups.slice(0,1)].some(r=>r.account_key==='SAMPLE')) return 'Synthetic sample';
  return source==='file'?'Imported CSV':'MT5 journal';
}
function drawFocus(rs) {
  const m=publicMarket($('focusKey').value), unit=scope==='setups'?'setups':'tickets', keys=useBells?bellKeys:broadKeys;
  // An alias may identify a market, but do not silently pool P/L across contracts (e.g. USTEC vs USTEC_x100).
  const selected=$('symbol').value;
  const match=selected && m.aliases.includes(selected) ? selected : m.symbol;
  const rows=rs.filter(r=>r.symbol===match), groups=new Map();
  for(const r of rows) {
    const stamp=r.open_time_utc, ms=Date.parse(stamp);
    const k=useBells?bellBucket(stamp):broadBucket(stamp);
    if(!groups.has(k)) groups.set(k,{n:0,net:0,wins:0,days:new Set(),first:Infinity,last:-Infinity});
    const a=groups.get(k), net=Number(r.net_profit);
    a.n++;a.net+=net;a.wins+=Number(net>0);
    if(Number.isFinite(ms)) {a.days.add(Math.floor(ms/86400000));a.first=Math.min(a.first,ms);a.last=Math.max(a.last,ms);}
  }
  const extra=useBells?'Outside these hours':'Off-session';
  const order=[...keys,...(groups.has(extra)||m.stats[extra]?[extra]:[]),...[...groups.keys()].filter(k=>!keys.includes(k)&&k!==extra).sort()];
  const best=strongest(m,broadKeys), bell=strongest(m,bellKeys);
  $('focusTitle').textContent=m.key+' · '+m.base;
  $('focusIdentity').textContent=`Reference: ${m.symbol}${m.key.includes(' / ')?' · DE40 and DE30 point to this one broker contract.':''}`;
  $('focusInsight').textContent=best&&bell?`Largest full-session range: ${labelWindow(best)} (${fmtBp(m.stats[best].range)} bp). Largest of the three opening hours: ${bell} (${fmtBp(m.stats[bell].range)} bp).`: 'Too little quote history to rank this market.';
  const src=journalLabel(), ccy=currency();
  $('focusSource').textContent=`${src} · ${match} only · ${rows.length} filtered ${unit} · by opening fill · ${ccy}. ${!rows.length?'No matching journal records. The quote benchmark is still available.':'The benchmark dates are fixed; the journal follows your filters.'}`;
  $('focusUnit').textContent=scope==='setups'?'Setups':'Tickets';
  focusExport=[];const body=order.map(k=>{
    const tr=el('tr'), s=m.stats[k], a=groups.get(k), n=a?.n || 0;
    const ref=s && reliable(s); const avg=n?a.net/n:null;
    const cells=[labelWindow(k),ref?fmtBp(s.range):'—',ref?fmtBp(s.spread):'—',s?s.n.toLocaleString('en-US'):'—',String(n),n?addSign(a.net):'—',n?addSign(avg):'—'];
    for(const [i,v] of cells.entries())tr.append(el('td',i?'num mono':'',v));
    focusExport.push({reference_start_utc:marketStudy.start,reference_end_utc:marketStudy.end,reference_symbol:m.symbol,window_family:useBells?'opening_hours':'broad_sessions',entry_window:k,median_m5_range_bp:ref?s.range:'',median_spread_bp:ref?s.spread:'',m5_bars:s?.n??'',utc_dates:s?.days??'',journal_source:src,account_key:data[scope][0]?.account_key||'',journal_symbol:match,unit,currency:ccy,close_date_filter_zone:$('zone').value,closed_from:$('from').value,closed_through:$('to').value,first_entry_utc:a&&Number.isFinite(a.first)?new Date(a.first).toISOString():'',last_entry_utc:a&&Number.isFinite(a.last)?new Date(a.last).toISOString():'',journal_entry_utc_dates:a?.days.size??0,records:n,wins:a?.wins??0,win_rate_pct:n?Number((100*a.wins/n).toFixed(4)):'',net_pl:n?Number(a.net.toFixed(8)):'',average_net:n?Number(avg.toFixed(8)):''});
    return tr;
  });$('focusBody').replaceChildren(...body);
}
function exportFocusCsv() {
  if(!focusExport.length)return;
  const keys=Object.keys(focusExport[0]);
  const recs=focusExport.map(row=>keys.map(k=>typeof row[k]==='number'?String(row[k]):csvValue(k,row[k])).join(','));
  const url=URL.createObjectURL(new Blob(['\ufeff'+[keys.join(','),...recs].join('\r\n')+'\r\n'],{type:'text/csv;charset=utf-8'}));
  const a=el('a');a.href=url;a.download=`hull-${publicMarket($('focusKey').value).symbol}-${scope}-session-comparison.csv`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
}
function renderResearch(rs) {drawBellCards();drawMatrix();drawFocus(rs);}
for(const m of marketStudy.markets) $('focusKey').append(new Option(m.key,m.symbol));
$('focusKey').onchange=()=>renderResearch(shown);
$('activityMetric').onchange=$('activityKind').onchange=()=>drawMatrix();
$('broadWindows').onclick=()=>selectWindows(false);$('cashWindows').onclick=()=>selectWindows(true);
$('exportFocus').onclick=exportFocusCsv;
