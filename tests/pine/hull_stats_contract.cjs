// Code contracts only. All candles below are hand-built; no downloads or market study.
// PineTS 0.13.1: PINETS_MODULE=/path/to/pinets node tests/pine/hull_stats_contract.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { PineTS, Indicator } = require(process.env.PINETS_MODULE || 'pinets');
const code = fs.readFileSync(path.join(__dirname, '../../indicators/FC-Hull-Dynamic-Source.pine'), 'utf8');
const nativeCode = code.slice(0, code.indexOf('// lookahead_off:'));
const start = Date.UTC(2024, 0, 1), step = 900000;
const sym = { ticker: 'TEST', tickerid: 'QA:TEST', prefix: 'QA', type: 'forex', basecurrency: 'XAU', currency: 'USD', mintick: 1, pointvalue: 1, timezone: 'Etc/UTC', pricescale: 1, minmove: 1, session: '24x7' };
const timed = (a) => a.map(([open, high, low, close], i) => ({openTime:start+i*step, closeTime:start+(i+1)*step, open, high, low, close, volume:1}));
function aggregate(b, n) {
  assert(Number.isInteger(n) && n >= 1);
  const out = [];
  for (let i=0; i+n<=b.length; i+=n) {
    const s=b.slice(i,i+n);
    out.push({...s[0], closeTime:s.at(-1).closeTime, high:Math.max(...s.map(x=>x.high)), low:Math.min(...s.map(x=>x.low)), close:s.at(-1).close, volume:s.length});
  }
  return out;
}
async function run(text, bars, tf='15', inputs={}, symbol=sym) {
  const provider = {getMarketData:async(_s,t)=>aggregate(bars, +t/15), getSymbolInfo:async()=>symbol, configure(){}};
  return new PineTS(provider,symbol.tickerid,tf,Math.floor(bars.length*15/+tf)).run(new Indicator(text,inputs));
}
const vals=(x,key)=>x.plots[key].data.map(q=>q.value);
const choose=(v)=>'array.from('+v.join(', ')+').get(bar_index)';
const mirror=(rows)=>rows.map(([o,h,l,c])=>[200-o,200-l,200-h,200-c]);
const controls={length:4,n:1,lots:1,cash:4,usdTick:1};

async function levels() {
  // O,H,L,C, activeBE, already-away-from-fill, raw relative to side, target ticks, result.
  const cases=[
    [100,100,100,100,true,false,0,4,0], [100,102,100,102,true,false,0,4,0],
    [100,102,100,100,true,false,0,4,-1], [99,106,99,105,true,true,0,4,-1],
    [104,106,99,105,true,true,0,4,1], [101,105,100,104,true,true,0,4,-1],
    [101,105,101,104,true,true,0,4,1], [101,102,101,102,true,true,-1,4,-2],
    [101,105,101,104,true,true,-1,4,1], [100,103,97,101,false,false,0,4,0],
    [100,105,97,101,false,false,0,4,1], [100,100,100,100,true,true,0,4,-1],
    [100,101,100,100,true,false,0,1,1], [100,106,100,104,true,false,0,4,-1]
  ];
  for (const d of [1,-1]) {
    const ohlc=cases.map(x=>x.slice(0,4));
    const text=nativeCode+`\nplot(f_exit(${d},100,${choose(cases.map(x=>x[7]))},${choose(cases.map(x=>x[4]))},${choose(cases.map(x=>x[5]))},${choose(cases.map(x=>d*x[6]))}),"decision")\n`;
    assert.deepEqual(vals(await run(text,timed(d===1?ohlc:mirror(ohlc))), 'decision'),cases.map(x=>x[8]));
  }
  for (const [symbol,opts,want] of [
    [{...sym,mintick:.01},{lots:.1,cash:10},100],
    [{...sym,basecurrency:'EUR',mintick:.00001},{lots:.01,cash:1},100],
    [{...sym,type:'futures',mintick:.25,pointvalue:50},{lots:2,cash:51},3],
    [{...sym,currency:'JPY'},{lots:.2,cash:1.01,usdTick:2.5},3],
  ]) {
    const ctx=await run(nativeCode+'\nplot(f_ticks(),"ticks")',timed([[100,101,99,100]]),'15',opts,symbol);
    assert.equal(vals(ctx,'ticks')[0],want);
  }
}
async function score() {
  const ohlc=[[99,101,98,99],[99,112,88,100],[100,108,90,105],[105,111,100,103],[103,105,97,100],[99,99,89,95],[95,99,93,98],[98,100,97,99],[99,120,77,100]];
  const text=nativeCode+`\nint qSignal=${choose([1,1,0,-1,-1,1,-1,1,0])}\nint qBias=${choose([-1,1,1,1,-1,1,1,1,1])}\nRow q=f_score(qSignal,qBias,10,true)\n`+['wins','losses','beLosses','side','points'].map(k=>`plot(q.${k},"${k}")`).join('\n');
  const x=await run(text,timed(ohlc),'15',{...controls,bePct:50,pointIn:.25,endAt:start+8*step});
  assert.deepEqual(vals(x,'wins'),     [0,0,0,0,0,1,1,1,1]);
  assert.deepEqual(vals(x,'losses'),   [0,0,0,1,1,1,2,2,2]);
  assert.deepEqual(vals(x,'beLosses'), [0,0,0,1,1,1,1,1,1]);
  assert.deepEqual(vals(x,'side'),     [0,1,1,0,-1,1,0,1,1]);
  assert.equal(vals(x,'points').at(-1),40);
}
const fields=['wins','losses','beLosses','side','points','asianW','asianN','londonW','londonN','nyW','nyN','kzW','kzN'];
const plots=fields.map(k=>`plot(q.${k},"${k}")`).join('\n');
const dateBar=(closeTime,[open=100,high=101,low=99,close=100]=[])=>({openTime:closeTime-step,closeTime,open,high,low,close,volume:1});
const date=(iso)=>Date.parse(iso+'Z');

async function clocks() {
  // Time is a fill instant, not the open of its containing 15/30/45m bar.
  const points=[
    ['2024-01-02T00:00','1000'],['2024-01-02T06:59','1000'],['2024-01-02T07:00','0000'],
    ['2024-01-02T08:00','0100'],['2024-01-02T12:59','0100'],['2024-01-02T13:00','0111'],
    ['2024-01-02T15:59','0111'],['2024-01-02T16:00','0010'],['2024-01-02T22:00','0000'],
    // US has switched; UK has not. London and NY overlap before the killzone starts.
    ['2024-03-18T12:00','0110'],['2024-03-18T13:00','0111'],
    // Both in daylight time: the same local windows occur an hour earlier in UTC.
    ['2024-07-01T07:00','0100'],['2024-07-01T12:00','0111'],['2024-07-01T15:00','0010'],
    ['2024-07-01T21:00','0000'],
  ];
  const probe=nativeCode+'\n[a0,l0,n0,k0]=f_tags(time_close)\n'+['a0','l0','n0','k0'].map(v=>`plot(${v} ? 1 : 0,"${v}")`).join('\n');
  let x=await run(probe,points.map(([stamp])=>dateBar(date(stamp))));
  const tags=(ctx,i)=>['a0','l0','n0','k0'].map(k=>vals(ctx,k)[i]).join('');
  points.forEach(([stamp,mask],i)=>assert.equal(tags(x,i),mask,stamp));
  // User-edited overnight / full-day windows; a wide KZ cannot label NY-only hours.
  const custom=[['2024-01-02T00:00','1000'],['2024-01-02T02:00','0000'],['2024-01-02T13:00','0111'],['2024-01-02T16:00','0010'],['2024-01-02T22:00','1000']];
  x=await run(probe,custom.map(([stamp])=>dateBar(date(stamp))),'15',{asianHours:'2200-0200',kzHours:'0000-0000'});
  custom.forEach(([stamp,mask],i)=>assert.equal(tags(x,i),mask,stamp));
}
async function attribution(){
  // Each OLD position resolves before a new one takes the same close. Includes
  // an outside-session win, an unresolved trade, two profitable opposite-signal losses,
  // and entries exactly at London open, overlap start, London close and NY close.
  const closeAt=['06:45','08:00','13:00','16:00','22:00','23:00','23:45'];
  const price=[[100,102,98,100],[100,111,99,105],[105,106,101,102],[102,113,101,111],[111,114,105,110],[110,120,108,118],[118,120,116,118]];
  const bars=closeAt.map((h,i)=>dateBar(date('2024-01-02T'+h),price[i]));
  const text=nativeCode+`\nint signal0=${choose([1,-1,1,-1,1,0,1])}\nRow q=f_score(signal0,signal0,10,true)\n`+plots;
  const x=await run(text,bars,'15',{...controls,bePct:100});
  const want={wins:[0,1,1,2,2,3,3],losses:[0,0,1,1,2,2,2],side:[1,-1,1,-1,1,0,1],beLosses:[0,0,0,0,0,0,0],
    asianW:[0,1,1,1,1,1,1],asianN:[0,1,1,1,1,1,1],londonW:[0,0,0,1,1,1,1],londonN:[0,0,1,2,2,2,2],
    nyW:[0,0,0,1,1,1,1],nyN:[0,0,0,1,2,2,2],kzW:[0,0,0,1,1,1,1],kzN:[0,0,0,1,1,1,1]};
  for(const [key,v] of Object.entries(want))assert.deepEqual(vals(x,key),v,key);
}
async function timeframes(){
  const cycle=[100,103,99,102,104,105,107,105,103,108,109,111,102,101,100,98,106,101,99,97,96,107,108,110];
  let last=100;
  const bars=timed(Array.from({length:150},(_,i)=>{let c=cycle[i%cycle.length],o=last;last=c;return [o,Math.max(c,o)+1,Math.min(c,o)-1,c]}));
  const expected=[],cfg={...controls,bePct:50};
  for(const tf of ['15','30','45']){
    const text=nativeCode+'\nRow q=f_study()\n'+plots+'\nplot(raw,"raw")\nplot(native.eventCode,"ev")\nplot(dir,"ctx")';
    const x=await run(text,bars,tf,cfg);
    expected.push(Object.fromEntries(fields.map(k=>[k,vals(x,k).at(-1)])));
    const bias=vals(x,'ctx'),ev=vals(x,'ev'),buy=vals(x,'BUY'),sell=vals(x,'SELL'),held=vals(x,'side');
    let pending=false,firsts=0;
    vals(x,'raw').forEach((d,i)=>{
      if(Math.abs(ev[i])===2)pending=true;
      const aligned=d!==0 && d===bias[i];
      assert.equal(!!buy[i],aligned && d===1);
      assert.equal(!!sell[i],aligned && d===-1);
      if(aligned && pending){ if(!i || held[i-1]!==d)firsts++;pending=false;assert(buy[i]||sell[i]);assert.equal(held[i],d); }
    });
    if(tf==='15')assert(firsts>0,'fixture must actually see a first aligned cross after MSS');
  }
  const pair=(r,g)=>g==='all'?[r.wins,r.wins+r.losses]:[r[g+'W'],r[g+'N']];
  const value=(w,n)=>n?(100*w/n).toFixed(1)+'% ('+n+')':'— (0)';
  for(const host of ['15','30','45','90']){
    const table=(await run(code,bars,host,cfg)).plots.__tables__.data[0].value[0];
    assert.deepEqual([table.columns,table.rows],[5,8]);
    const txt=table.cells.map(row=>row.map(c=>c.text));
    assert.deepEqual(txt[0],['Win rate (n)','15m','30m','45m','All TFs']);
    assert.deepEqual(txt.slice(1,6).map(r=>r[0]),['Overall','Asian','London','New York','NY–London KZ']);
    ['all','asian','london','ny','kz'].forEach((key,j)=>{
      const pairs=expected.map(r=>pair(r,key));
      pairs.forEach(([w,n],i)=>assert.equal(txt[j+1][i+1],value(w,n),host+'/'+key+'/'+i));
      assert.equal(txt[j+1][4],value(pairs.reduce((s,q)=>s+q[0],0),pairs.reduce((s,q)=>s+q[1],0)),key+' pooled');
    });
    expected.forEach((r,i)=>{
      assert.equal(txt[6][i+1],r.wins+' / '+r.losses);
      assert.equal(txt[7][i+1],r.points+' · '+(r.side===1?'BUY':r.side===-1?'SELL':'flat'));
    });
  }
  // PineTS does not implement general recursive security(); verify each chosen
  // structure clock directly in both directions versus the native structure input.
  for(const [tf,override] of [['15','45'],['45','15']]){
    const extra='\nvar int qb=0\nvar int qs=na\nif not na(studyFrame) and studyFrame.closedAt <= studyAsOf\n    qb:=studyFrame.bias\n    qs:=studyFrame.closedAt\nplot(qb,"qb")\nplot(qs,"qs")\nplot(dir,"cd")\nplot(seen,"cs")';
    const x=await run(nativeCode+extra,bars,tf,{...cfg,tfIn:override});
    assert.deepEqual(vals(x,'qb'),vals(x,'cd'));assert.deepEqual(vals(x,'qs'),vals(x,'cs'));
    const z=await run(nativeCode+'\nplot(native.bias,"b0")\nplot(native.closedAt,"t0")',bars,override,cfg);
    const b=vals(z,'b0'),t=vals(z,'t0');
    vals(x,'qb').forEach((d,i)=>{
      const j=+tf<+override?Math.floor(i*(+tf)/(+override))-1:(i+1)*(+tf)/(+override)-1;
      assert.equal(d,j<0?0:b[j]);assert.equal(vals(x,'qs')[i],j<0?NaN:t[j]);
    });
  }
}
async function pooling(){
  const stub=code.slice(0,code.lastIndexOf('if barstate.islast'));
  const r=(w,l,ok)=>`Row.new(time_close, ${w}, ${l}, 0, 0, na, na, 10, ${ok}, true, 0, 0, 0, 0, 0, 0, 0, 0)`;
  for(const [ok,expect] of [[true,'25.0% (12)'],[false,'n/a']]){
    const text=stub+`if barstate.islast\n    f_panel(${r(1,0,true)},${r(1,8,true)},${r(1,1,ok)})\n`;
    // Table-only fixture: no use of extra security contexts is needed here.
    const x=await run(text.replace(/Row book = f_study\(\)[\s\S]*?(?=f_closed\()/,''),timed([[100,101,99,100]]));
    const cells=x.plots.__tables__.data[0].value[0].cells;
    assert.equal(cells[1][4].text,expect); // 3/12, not (100 + 11.1 + 50) / 3
    assert.equal(cells[2][4].text,ok?'— (0)':'n/a');
  }
}
(async()=>{await levels();await score();await clocks();await attribution();await timeframes();await pooling();console.log('PASS: synthetic Pine contracts for sessions, DST, immutable entry attribution, pooled counts, valid post-MSS crosses and native timeframe calculations.');})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
