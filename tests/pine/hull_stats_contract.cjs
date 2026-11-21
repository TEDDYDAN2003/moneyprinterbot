// Code contracts only. All candles below are hand-built; no downloads or market study.
// PineTS 0.13.1: PINETS_MODULE=/path/to/pinets node tests/pine/hull_stats_contract.cjs
// The harness checks the real lower-TF request on each native TF and projected
// counter snapshots separately. PineTS short-circuits secondary nested requests;
// this is not a test of TradingView's engine or an exchange's historical tape.
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
async function run(text, bars, tf='15', inputs={}, symbol=sym, minutes=[]) {
  const provider = {getMarketData:async(_s,t)=>+t<15?aggregate(minutes,+t):aggregate(bars, +t/15), getSymbolInfo:async()=>symbol, configure(){}};
  return new PineTS(provider,symbol.tickerid,tf,Math.floor(bars.length*15/+tf)).run(new Indicator(text,inputs));
}
const vals=(x,key)=>x.plots[key].data.map(q=>q.value);
const choose=(v)=>'array.from('+v.join(', ')+').get(bar_index)';
const mirror=(rows)=>rows.map(([o,h,l,c])=>[200-o,200-l,200-h,200-c]);
const controls={length:4,n:1,lots:1,cash:4,usdTick:1};

async function defaultHullLength() {
  // Exercise the shipped input (no length override) against literal 16-base
  // formulas for BOTH curves. Fabricated OHLC only; no trade study.
  const bars=timed(Array.from({length:90},(_,i)=>{
    const c=100+(i*19)%23; return [c,c+1+i%3,c-1-i%5,c];
  }));
  const probe=code.slice(0,code.indexOf('// These context series'))+`
int testBias = bar_index % 2 == 0 ? 1 : -1
[_, aProbe, bProbe, _] = f_hull(testBias)
float aLo16 = ta.wma(2 * ta.wma(low, 8) - ta.wma(low, 16), 4)
float aHi16 = ta.wma(2 * ta.wma(high, 8) - ta.wma(high, 16), 4)
float b16 = ta.wma(3 * ta.wma(close, 2) - ta.wma(close, 4) - ta.wma(close, 8), 8)
plot(length, "length")
plot(aProbe, "a")
plot(bProbe, "b")
plot(testBias == 1 ? aLo16 : aHi16, "wantA")
plot(b16, "wantB")
`;
  for(const tf of ['15','30','45']) {
    const ctx=await run(probe,bars,tf);
    assert(vals(ctx,'length').every(n=>n===16));
    for(const [actual,want] of [['a','wantA'],['b','wantB']]) {
      assert(Number.isFinite(vals(ctx,actual).at(-1))); assert.deepEqual(vals(ctx,actual),vals(ctx,want));
    }
  }
}
async function levels() {
  // O,H,L,C, activeBE, already-away-from-fill, raw relative to side, target ticks, result.
  const cases=[
    [100,100,100,100,true,false,0,4,0], [100,102,100,102,true,false,0,4,0],
    [100,102,100,100,true,false,0,4,-1], [99,106,99,105,true,true,0,4,-1],
    [104,106,99,105,true,true,0,4,1], [101,105,100,104,true,true,0,4,2],
    [101,105,101,104,true,true,0,4,1], [101,102,101,102,true,true,-1,4,-2],
    [101,105,101,104,true,true,-1,4,1], [100,103,97,101,false,false,0,4,0],
    [100,105,97,101,false,false,0,4,1], [100,100,100,100,true,true,0,4,-1],
    [100,101,100,100,true,false,0,1,1], [100,106,100,104,true,false,0,4,2]
  ];
  for (const d of [1,-1]) {
    const ohlc=cases.map(x=>x.slice(0,4));
    const text=nativeCode+`\n[probeExit,_,_,_]=f_exit(${d},100,${choose(cases.map(x=>x[7]))},${choose(cases.map(x=>x[4]))},${choose(cases.map(x=>x[5]))},false,${choose(cases.map(x=>d*x[6]))},array.new<MinutePrint>())\nplot(probeExit,"decision")\n`;
    assert.deepEqual(vals(await run(text,timed(d===1?ohlc:mirror(ohlc))), 'decision'),cases.map(x=>x[8]));
  }
  for (const [symbol,opts,want] of [
    [{...sym,mintick:.01},{lots:.1,cash:10},100],
    [{...sym,basecurrency:'EUR',mintick:.00001},{lots:.01,cash:1},100],
    [{...sym,type:'futures',mintick:.25,pointvalue:50},{lots:2,cash:51},3],
    [{...sym,currency:'JPY'},{lots:.2,cash:1.01,usdTick:2.5},3],
    [{...sym},{lots:1,cash:.07,usdTick:.01},7], // 0.07 / 0.01 has a binary float tail
    [{...sym},{lots:1,cash:.0701,usdTick:.01},8],
    // Distinct broker tick, chart tick and display point; never infer a CFD contract from its name.
    [{...sym,type:'cfd',mintick:.1},{lots:1,cash:5,usdTick:1,brokerTickSize:.2},10],
    [{...sym,type:'cfd',mintick:.1},{lots:1,cash:.01,usdTick:2,brokerTickSize:.01},1],
  ]) {
    const ctx=await run(nativeCode+'\nplot(f_ticks(),"ticks")',timed([[100,101,99,100]]),'15',opts,symbol);
    assert.equal(vals(ctx,'ticks')[0],want);
  }
}
async function score() {
  const ohlc=[[99,101,98,99],[99,112,88,100],[100,108,90,105],[105,111,100,103],[103,105,97,100],[99,99,89,95],[95,99,93,98],[98,100,97,99],[99,120,77,100]];
  const text=nativeCode+`\nint qSignal=${choose([1,1,0,-1,-1,1,-1,1,0])}\nint qBias=${choose([-1,1,1,1,-1,1,1,1,1])}\nRow q=f_score(qSignal,qBias,10,true,array.new<MinutePrint>())\n`+['wins','losses','unknown','beLosses','side','points'].map(k=>`plot(q.${k},"${k}")`).join('\n');
  const x=await run(text,timed(ohlc),'15',{...controls,bePct:50,pointIn:.25,endAt:start+8*step});
  assert.deepEqual(vals(x,'wins'),     [0,0,0,0,0,1,1,1,1]);
  assert.deepEqual(vals(x,'losses'),   [0,0,0,0,0,0,1,1,1]);
  assert.deepEqual(vals(x,'unknown'),  [0,0,0,1,1,1,1,1,1]);
  assert.deepEqual(vals(x,'beLosses'), [0,0,0,0,0,0,0,0,0]);
  assert.deepEqual(vals(x,'side'),     [0,1,1,0,-1,1,0,1,1]);
  assert.equal(vals(x,'points').at(-1),40);
}
const fields=['wins','losses','beLosses','side','points','asianW','asianN','londonW','londonN','nyW','nyN','kzW','kzN','unknown','asianU','londonU','nyU','kzU','mixedLoss','fineBars','coarseBars','heldSignals'];
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
  const text=nativeCode+`\nint signal0=${choose([1,-1,1,-1,1,0,1])}\nRow q=f_score(signal0,signal0,10,true,array.new<MinutePrint>())\n`+plots;
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
  const pair=(r,g)=>g==='all'?[r.wins,r.wins+r.losses+r.unknown,r.unknown]:[r[g+'W'],r[g+'N'],r[g+'U']];
  const value=(w,n,u)=>!n?'— (0)':(u?((Math.floor(1000*w/n)/10).toFixed(1)+'–'+(Math.ceil(1000*(w+u)/n)/10).toFixed(1)):(100*w/n).toFixed(1))+'% ('+n+')';
  for(const host of ['15','30','45','90']){
    const table=(await run(code,bars,host,cfg)).plots.__tables__.data[0].value[0];
    assert.deepEqual([table.columns,table.rows],[5,11]);
    const txt=table.cells.map(row=>row.map(c=>c.text));
    assert.deepEqual(txt[0],['Win rate (n)','15m','30m','45m','All TFs']);
    assert.deepEqual(txt.slice(1,6).map(r=>r[0]),['Overall','Asian','London','New York','NY–London KZ']);
    ['all','asian','london','ny','kz'].forEach((key,j)=>{
      const groups=expected.map(r=>pair(r,key));
      groups.forEach((v,i)=>assert.equal(txt[j+1][i+1],value(...v),host+'/'+key+'/'+i));
      assert.equal(txt[j+1][4],value(...[0,1,2].map(i=>groups.reduce((s,q)=>s+q[i],0))),key+' pooled');
    });
    expected.forEach((r,i)=>{
      assert.equal(txt[6][i+1],r.wins+' / '+r.losses+' / '+r.unknown);
      assert.equal(txt[7][i+1],r.fineBars+r.coarseBars===0?'—':'0.0%');
      assert.equal(txt[9][i+1].replace('?',''),r.side===1?'BUY':r.side===-1?'SELL':'flat');
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
// Independent first-exit oracle. Enumerate all reachable print histories on a
// small price grid (arbitrary jumps and repeated prints), group by observed OHLC,
// then compare the Pine decision to the SET of possible outcomes, not one path.
async function pathOracle(){
  const grid=[-1,0,1,2,3], modes=[[false,false,false],[false,true,false],[true,false,false],[true,true,false],[true,true,true]];
  const shapes=[];for(const o of grid)for(const l of grid)for(const h of grid)for(const c of grid)if(l<=Math.min(o,c)&&h>=Math.max(o,c))shapes.push([o,h,l,c]);
  const cases=[],expected=[];
  for(const d of [1,-1])for(const goal of [1,2,3])for(const [armed,left0,priorStop] of modes){
    const all=new Map();
    // status: 0=still held, 1=target, -1=protection. Quotes keep printing
    // after an exit, so the eventual market H/L/C need not be exit levels.
    function tick(p,s,left){return s!==0?s:p>=goal?1:armed&&(p<0||p===0&&left)?-1:0;}
    for(const o of grid){
      const queue=[[o,o,o,tick(o,0,left0),left0||o!==0]],seen=new Set();
      for(let j=0;j<queue.length;j++){
        const [h,l,c,s,left]=queue[j],k=queue[j].join('/');if(seen.has(k))continue;seen.add(k);
        const shape=[o,h,l,c].join('/');if(!all.has(shape))all.set(shape,new Set());all.get(shape).add(s);
        for(const p of grid)queue.push([Math.max(h,p),Math.min(l,p),p,tick(p,s,left),left||p!==0]);
      }
    }
    for(const shape of shapes){
      const paths=all.get(shape.join('/'));if(priorStop)paths.add(-1);
      assert(!(paths.has(0)&&paths.has(1)), 'A seen TP cannot leave an open path');
      const e=paths.has(0)?0:paths.size===2?2:[...paths][0];
      const away=left0||shape[1]!==0||shape[2]!==0;
      expected.push([e,Number(away),Number(paths.has(-1))]);
      const row=shape.map(p=>100+p);cases.push(d===1?row:mirror([row])[0]);
    }
  }
  const funcs=code.slice(code.indexOf('f_grid('),code.indexOf('// Reject partial,'));
  const probe=`//@version=6\nindicator("QA")\nconst int TAKE=1\nconst int BE=-1\nconst int UNCLEAR=2\n`+funcs+`
int stage = int(math.floor(bar_index / ${shapes.length}))
int mode = stage % 5
int goal = int(math.floor((stage % 15) / 5)) + 1
[ex,away,maybe] = f_piece(stage < 15 ? 1 : -1,100,goal,mode >= 2,mode == 1 or mode >= 3,mode == 4,open,high,low,close)
plot(ex,"x")
plot(away ? 1 : 0,"left")
plot(maybe ? 1 : 0,"maybe")`;
  const x=await run(probe,timed(cases)),es=vals(x,'x'),as=vals(x,'left'),ms=vals(x,'maybe');
  expected.forEach(([e,a,m],i)=>{assert.equal(es[i],e,'exit at synthetic state '+i);if(e===0)assert.deepEqual([as[i],ms[i]],[a,m],'held state '+i);});
  return expected.length;
}
// Equal parent OHLC, different timed 1m paths. Never infer a path from the parent.
async function fineAudit(){
  const startAt = Date.UTC(2024,0,2,15), ms=60000;
  const seed=[[100,110,90,100]];
  const win=[[101,104,101,104],[104,106,99,105]];
  const loss=[[101,103,99,101],[101,106,101,105]];
  const tie=[[101,106,99,105]];
  const results=nativeCode+`\narray<MinutePrint> zz=f_slices()\nint ss=SIGNALS\nRow q=f_score(ss,ss,4,true,zz)\n`+plots+'\nplot(q.exitCode,"exitCode")\nplot(q.possibleBE ? 1 : 0,"maybe")\nplot(q.entered ? 1 : 0,"entered")';
  const cfg={...controls,bePct:0,inspectTF:'1'};
  function tape(tf,d,patterns){
    const rows=patterns.flatMap(part=>{
      const end=part.at(-1)[3]; return part.concat(Array.from({length:tf-part.length},()=>[end,end,end,end]));
    });
    return (d===1?rows:mirror(rows)).map(([open,high,low,close],i)=>({openTime:startAt-tf*ms+i*ms,closeTime:startAt-tf*ms+(i+1)*ms,open,high,low,close,volume:1}));
  }
  async function exec(tf,d,patterns,signals,opts={},change=(m)=>m){
    const mins=tape(tf,d,patterns),root=aggregate(mins,15);
    const ctx=await run(results.replace('SIGNALS',choose(signals.map(v=>d*v))),root,String(tf),{...cfg,...opts},sym,change(mins));
    return Object.fromEntries([...fields,'exitCode','maybe','entered'].map(k=>[k,vals(ctx,k)]));
  }
  for(const tf of [15,30,45])for(const d of [1,-1]){
    for(const [seq,verdict] of [[win,1],[loss,-1],[tie,2]]) {
      const x=await exec(tf,d,[seed,seq],[1,0]);
      assert.deepEqual(x.entered,[1,0]); assert.deepEqual(x.exitCode,[0,verdict],`${tf}m ${d} timed path ${verdict}`);
      assert.deepEqual(x.fineBars,[0,1]);assert.deepEqual(x.coarseBars,[0,0]);
      for(const [k,v] of Object.entries({wins:+(verdict===1),losses:+(verdict<0),beLosses:+(verdict===-1),unknown:+(verdict===2),kzN:1,kzU:+(verdict===2),kzW:+(verdict===1),londonN:1,nyN:1,asianN:0}))assert.equal(x[k][1],v,k);
    }
  }
  // Missing start/interior/end or a disagreement with the parent invalidates
  // the WHOLE minute sequence before even the first apparent hit is trusted.
  const tf=15;
  for(const bad of [m=>[], m=>m.filter((_,i)=>i!==tf),m=>m.filter((_,i)=>i!==tf+7),m=>m.slice(0,-1),m=>m.map((v,i)=>i===tf+8?{...v,high:119}:v)]){
    const x=await exec(tf,1,[seed,win],[1,0],{},bad);
    assert.deepEqual(x.exitCode,[0,2]);assert.deepEqual(x.fineBars,[0,0]);assert.deepEqual(x.coarseBars,[0,1]);
  }
  for(const d of [1,-1]){
    // This is the user's O=low=entry, high>TP example. A sub-bar gap at TP
    // proves a win; a proven pre-TP return proves a loss. Identical native OHLC.
    const straight=[[100,100,100,100],[104,108,104,106]];
    const retest=[[100,102,100,100],[100,108,100,106]];
    const one=[[100,108,100,106]];
    for(const [p,e] of [[straight,1],[retest,-1],[one,2]]){
      const x=await exec(15,d,[seed,p],[1,0]); assert.deepEqual(x.exitCode,[0,e]);
    }
    // Low at unchanged entry could be just the opening print. Do not force
    // a loss, but do not erase the possible earlier BE and invent a later win.
    const maybe=[[100,102,100,102]];
    for(const [after,finalSig,e] of [[[[105,106,105,106]],0,2],[[[100,106,99,105]],0,-1],[[[102,103,101,102]],-1,-3]]){
      const x=await exec(15,d,[seed,maybe,after],[1,1,finalSig]);
      assert.deepEqual(x.maybe,[0,1,0]);assert.deepEqual(x.heldSignals,[0,1,1]);
      assert.deepEqual(x.exitCode,[0,0,e]);assert.deepEqual(x.mixedLoss,[0,0,+(e===-3)]);
      assert.equal(x.side.at(-1),finalSig*d || 0); // opposite can reverse on that close
    }
    const atEnd=await exec(15,d,[seed,maybe,win],[1,0,0],{endAt:startAt+15*ms});
    assert.equal(atEnd.maybe.at(-1),1);assert.equal(atEnd.side.at(-1),d);
    assert.equal(atEnd.wins.at(-1)+atEnd.losses.at(-1)+atEnd.unknown.at(-1),0);
    // A 1m close at +3 cannot arm a 50% BE before the signal candle closes.
    const subWick=await exec(15,d,[seed,[[100,103,99,103],[103,105,98,100]]],[1,0],{bePct:50});
    assert.deepEqual(subWick.exitCode,[0,1]);
    const armed=await exec(15,d,[seed,[[100,103,99,102]],[[100,105,99,105]]],[1,0,0],{bePct:50});
    assert.deepEqual(armed.exitCode,[0,0,-1]);
  }
  // The alternate 5m audit really reads those candles; if both events fall in
  // the same 5m slice, it correctly knows less than the 1m audit.
  const coarseFive=await exec(15,1,[seed,win],[1,0],{inspectTF:'5'});
  assert.deepEqual(coarseFive.fineBars,[0,1]); assert.deepEqual(coarseFive.exitCode,[0,2]);
}
async function priceExample(){
  // Fabricated 25,000 feed. Explicit broker: $1 per 1.00 price move at one lot.
  // Entry candle's extreme cannot close its own trade. Same next 15m candle,
  // three different tapes. A different contract intentionally moves the goal.
  const instrument={...sym,type:'cfd',tickerid:'QA:INDEX',basecurrency:'',mintick:.1,pricescale:10,minmove:1};
  const probe=code.slice(0,code.lastIndexOf('if barstate.islast'))
    .replace(/Row book = f_study\(\)[\s\S]*?(?=f_closed\()/,'int planted = bar_index == 0 ? 1 : 0\nRow book = f_score(planted, planted, f_ticks(), true, f_slices())\n');
  const panelCode=probe+'\nif barstate.islast\n    f_panel(book,book,book)\nplot(book.entry,"ep")\nplot(book.target,"tp")\nplot(book.points,"pp")\nplot(book.exitCode,"verdict")\n';
  const opts={usdTick:1,brokerTickSize:1,pointIn:.1,lots:1,cash:5,auditMarks:true};
  const fill=Array.from({length:15},()=>[25000,25010,24990,25000]);
  const tapes=[
    [[[25000,25000,25000,25000],[25005,25008,25005,25006]],1],
    [[[25000,25002,25000,25000],[25000,25008,25000,25006]],-1],
    [[[25000,25008,25000,25006]],2],
  ];
  function minuteBars(r){return r.map(([open,high,low,close],i)=>({openTime:start+i*60000,closeTime:start+(i+1)*60000,open,high,low,close,volume:1}));}
  for(const [piece,e] of tapes){
    const mins=minuteBars(fill.concat(piece,Array.from({length:15-piece.length},()=>[25006,25006,25006,25006])));
    const x=await run(panelCode,aggregate(mins,15),'15',opts,instrument,mins);
    assert.deepEqual(vals(x,'ep'),[25000,25000]);assert.deepEqual(vals(x,'tp'),[25005,25005]);assert.deepEqual(vals(x,'pp'),[50,50]);
    assert.deepEqual(vals(x,'verdict'),[0,e]);
    const cells=x.plots.__tables__.data[0].value[0].cells;
    assert.equal(cells[7][1].text,'100.0%'); assert.match(cells[8][1].text,/^5(?:\.0+)? \/ 50$/);
    assert.deepEqual(vals(x,'Test entry at close'),[25000,NaN]);assert.deepEqual(vals(x,'Target for that test entry'),[25005,NaN]);
    assert.deepEqual(vals(x,'Test: unknown TP/BE order').map(Boolean),[false,e===2]);
    assert.equal(cells[1][1].text,e===2?'0.0–100.0% (1)':e===1?'100.0% (1)':'0.0% (1)');
  }
  const mins=minuteBars(fill.concat(fill));
  const x=await run(panelCode,aggregate(mins,15),'15',{...opts,usdTick:.5,pointIn:1},instrument,mins);
  assert.equal(vals(x,'tp')[0],25010);assert.equal(vals(x,'pp')[0],10);
  const table=(x)=>x.plots.__tables__.data[0].value[0].cells;
  // Missing sizing at an otherwise eligible entry is NOT evidence of no trades.
  for(const expr of ['int(na)','bar_index == 0 ? int(na) : 10']){
    const bad=panelCode.replace('f_score(planted, planted, f_ticks(),','f_score(planted, planted, '+expr+',');
    const c=table(await run(bad,aggregate(mins,15),'15',opts,instrument,mins));
    assert.equal(c[1][1].text,'n/a');assert.equal(c[1][4].text,'n/a');assert.equal(c[8][1].text,'Set tick $');
  }
  const outside=table(await run(panelCode,aggregate(mins,15),'15',{...opts,startAt:start+86400000},instrument,mins));
  assert.equal(outside[1][1].text,'n/a');assert.equal(outside[8][1].text,'n/a');
}
async function pooling(){
  const stub=code.slice(0,code.lastIndexOf('if barstate.islast')).replace(/Row book = f_study\(\)[\s\S]*?(?=f_closed\()/,'Row book = Row.new()\n');
  const r=(w,l,u,ok)=>`Row.new(time_close, ${w}, ${l}, 0, 0, na, na, 10, ${ok}, true, 0, 0, 0, 0, 0, 0, 0, 0, ${u}, 0, 0, 0, 0, 0, 0, 0, false, 0, time_close, time_close, false, 0)`;
  for(const [ok,u,expect] of [[true,0,'25.0% (12)'],[true,1,'25.0–33.4% (12)'],[false,0,'n/a']]){
    const text=stub+`if barstate.islast\n    f_panel(${r(1,0,0,true)},${r(1,8-u,u,true)},${r(1,1,0,ok)})\n`;
    const x=await run(text,timed([[100,101,99,100]]));
    const cells=x.plots.__tables__.data[0].value[0].cells;
    assert.equal(cells[1][4].text,expect);
    assert.equal(cells[2][4].text,ok?'— (0)':'n/a');
  }
}
async function openGuides(){
  const stub=code.slice(0,code.lastIndexOf('if barstate.islast')).replace(/Row book = f_study\(\)[\s\S]*?(?=f_closed\()/,'Row book = Row.new()\n');
  const test=stub+`\nslot(int d0,int index0,float px,bool doubt) =>
    Row q=Row.new()
    q.side:=d0
    q.entryAt:=time_close-(bar_index-index0)*900000
    q.target:=px
    q.possibleBE:=doubt
    q.validTF:=true
    q.firstTestClose:=time_close
    q.lastTestClose:=time_close
    q.priced:=true
    q.entry:=100
    q.points:=4
    q.wins:=0
    q.losses:=0
    q.beLosses:=0
    q.endTime:=time_close
    q
Row qa=slot(bar_index<2?1:bar_index==2?-1:0,bar_index<2?0:2,bar_index<2?104:96,false)
Row qb=slot(bar_index==1 or bar_index==2?-1:0,1,92,bar_index==2)
Row qc=slot(bar_index==2 or bar_index==3?1:0,2,116,false)
f_panel(qa,qb,qc)
`;
  const at=(i)=>start+(i+1)*step, stages=[
    [[0,104,'15m BUY']],[[0,104,'15m BUY'],[1,92,'30m SELL']],
    [[2,96,'15m SELL'],[1,92,'30m SELL?'],[2,116,'45m BUY']],[[2,116,'45m BUY']],[]
  ];
  const overlay=(x,k)=>x.plots['__'+k+'_overlay__']?.data[0].value||[];
  for(let i=0;i<stages.length;i++){
    const x=await run(test,timed(Array(i+1).fill([100,102,99,100]))),lines=overlay(x,'lines'),labels=overlay(x,'labels'),cells=x.plots.__tables__.data[0].value[0].cells;
    assert.equal(lines.length,stages[i].length);assert.equal(labels.length,lines.length);
    for(const [j,price,tag] of stages[i]){
      assert(lines.some(q=>q.x1===at(j)&&q.x2===at(i)+1&&q.y1===price&&q.y2===price));
      assert(labels.some(q=>q.x===at(i)&&q.y===price&&q.text===tag+' TP '+price));
    }
    for(const [j,tf] of ['15m','30m','45m'].entries()){
      const entry=stages[i].find(q=>q[2].startsWith(tf));assert.equal(cells[10][j+1].text,entry?String(entry[1]):'—');
    }
  }
  for(const [opts,want] of [[{showOpenTP:false},'104'],[{endAt:at(0)},'—']]){
    const x=await run(test,timed(Array(2).fill([100,102,99,100])),'15',opts);
    assert.equal(overlay(x,'lines').length,0);assert.equal(x.plots.__tables__.data[0].value[0].cells[10][1].text,want);
  }
}
(async()=>{await defaultHullLength();await openGuides();await levels();await score();await clocks();await attribution();const histories=await pathOracle();await fineAudit();await priceExample();await timeframes();await pooling();console.log('PASS: '+histories+' exhaustive discrete OHLC/state cases, Hull/16, timestamped lower-bar exits, ambiguous/open paths, coverage fallback, ordered reversals, session tags, pooling, active target guides and bounds (fabricated candles only).');})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
