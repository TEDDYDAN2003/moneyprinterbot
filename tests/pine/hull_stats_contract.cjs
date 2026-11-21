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
  const text=nativeCode+`\nint qSignal=${choose([1,1,0,-1,-1,1,-1,1,0])}\nint qBias=${choose([1,1,1,-1,-1,1,1,1,1])}\nbool qSkip=${choose([true,false,false,true,false,false,false,false,false])}\nRow q=f_score(qSignal,qBias,qSkip,10,true)\n`+['wins','losses','beLosses','side','points'].map(k=>`plot(q.${k},"${k}")`).join('\n');
  const x=await run(text,timed(ohlc),'15',{...controls,bePct:50,pointIn:.25,endAt:start+8*step});
  assert.deepEqual(vals(x,'wins'),     [0,0,0,0,0,1,1,1,1]);
  assert.deepEqual(vals(x,'losses'),   [0,0,0,1,1,1,2,2,2]);
  assert.deepEqual(vals(x,'beLosses'), [0,0,0,1,1,1,1,1,1]);
  assert.deepEqual(vals(x,'side'),     [0,1,1,0,-1,1,0,1,1]);
  assert.equal(vals(x,'points').at(-1),40);
}
async function timeframes() {
  const cycle=[100,103,99,102,104,105,107,105,103,108,109,111,102,101,100,98,106,101,99,97,96,107,108,110];
  let last=100;
  const bars=timed(Array.from({length:150},(_,i)=>{let c=cycle[i%cycle.length], o=last;last=c;return [o,Math.max(c,o)+1,Math.min(c,o)-1,c]}));
  const expected={};
  for (const tf of ['15','30','45']) {
    const text=nativeCode+'\nRow q = f_study()\nplot(q.wins,"win")\nplot(q.losses,"loss")\nplot(q.side,"side")\nplot(epoch,"epoch")\nplot(raw,"raw")\nplot(dir,"context")\nplot(skipped ? 1 : 0,"skip")\n';
    const x=await run(text,bars,tf,controls);
    expected[tf]=['win','loss','side'].map(k=>vals(x,k).at(-1));
    let used=NaN;
    const epochs=vals(x,'epoch'),bias=vals(x,'context'),excluded=vals(x,'skip'),b=vals(x,'BUY'),s=vals(x,'SELL');
    vals(x,'raw').forEach((d,i)=>{
      const candidate=d!==0 && d===bias[i];
      const skip=candidate && Number.isFinite(epochs[i]) && epochs[i]!==used;
      if(skip) used=epochs[i];
      assert.equal(!!excluded[i],skip);
      assert.equal(!!b[i],candidate && !skip && d===1);
      assert.equal(!!s[i],candidate && !skip && d===-1);
    });
    if(tf==='15')assert(excluded.includes(1));
  }
  for (const tf of ['15','30','45','90']) {
    const table=(await run(code,bars,tf,controls)).plots.__tables__.data[0].value[0];
    assert.deepEqual([table.columns,table.rows],[7,4]);
    table.cells.slice(1).forEach((row,i)=>{
      const cells=row.map(cell=>cell.text), lane=['15','30','45'][i];
      const [w,l,s]=expected[lane];
      assert.deepEqual([cells[0], +cells[1], +cells[2], +cells[3], cells[6]],[lane+'m',w+l,w,l,s===1?'BUY':s===-1?'SELL':'—'],tf+' host / '+lane+' native');
      if (w+l) assert.equal(parseFloat(cells[4]),+((100*w/(w+l)).toFixed(1)));
    });
  }
  // PineTS does not implement general recursive security(); verify each override
  // directly at its signal-TF boundary, in both directions, versus the drawing clock.
  for(const [tf,override] of [['15','45'],['45','15']]) {
    const extra='\nvar int qBias=0\nvar int qShift=na\nif not na(studyFrame) and studyFrame.closedAt <= studyAsOf\n    qBias:=studyFrame.bias\n    qShift:=studyFrame.lastMSS\nplot(qBias,"qb")\nplot(qShift,"qs")\nplot(dir,"cd")\nplot(epoch,"ce")\n';
    const x=await run(nativeCode+extra,bars,tf,{...controls,tfIn:override});
    assert.deepEqual(vals(x,'qb'),vals(x,'cd'));
    assert.deepEqual(vals(x,'qs'),vals(x,'ce'));
    const z=await run(nativeCode+'\nplot(native.bias,"b0")\nplot(native.lastMSS,"m0")',bars,override,controls);
    const b=vals(z,'b0'),m=vals(z,'m0');
    vals(x,'qb').forEach((d,i)=>{
      const j=+tf<+override?Math.floor(i*(+tf)/(+override))-1:(i+1)*(+tf)/(+override)-1;
      assert.equal(d,j<0?0:b[j]);
      assert.equal(vals(x,'qs')[i],j<0?NaN:m[j]);
    });
  }
}
(async()=>{await levels(); await score(); await timeframes(); console.log('PASS: synthetic pricing, long/short level ordering, BE arming, opposite exits, close-only sampling, first-after-MSS and native timeframe contracts.');})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
