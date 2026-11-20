// From MT5: node qa/pine_oracle.cjs qa/candles.json ../indicators/FC-Hull-Either-16-Structure.pine oracle.json
const {PineTS, Indicator} = require('pinets');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const set = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const src = fs.readFileSync(process.argv[3], 'utf8');
const extra = JSON.parse(fs.readFileSync(path.join(__dirname, 'protected_cases.json'), 'utf8'));
Object.assign(set.fixtures, extra.fixtures); Object.assign(set.cases, extra.cases);
assert(src.includes('bos_half_range_close_v4'),'Supply the current v4 Pine source');
const cases = Object.entries(set.cases).map(([k,v]) => [k,v.fixture,v.opts]);
for (const [k,mode] of ['Directional','Low only','High only','Either source'].entries()) {
  cases.push(['alt-'+k,'base',{sourceMode:mode,alternateOnly:true,labelMode:'All Hull crosses (debug)',atrLength:8,pullbackBars:3,targetR:2.7,commissionPercent:.123,slippageTicks:3,tickSize:.001,extraPips:1.25,pointValue:2.5,equityPercent:21,hullLen:k===2?24:16}]);
}
for (const view of ['Structure-qualified crosses','Simulated fills only','All Hull crosses (debug)']) cases.push([view,'base',{labelMode:view}]);
{
  // Former v3 watch/CHoCH/exact-retest setups. In v4 neither side may enter.
  const b = set.fixtures.base.slice(0,421).map(x=>x.slice());
  b[418][3] = 92.198; b[420][3] = 92.281;
  const flip = x => Math.round((200-x)*1000)/1000;
  set.fixtures['no-reversal-buy'] = b;
  set.fixtures['no-reversal-sell'] = b.map(([t,o,h,l,c])=>[t,flip(o),flip(l),flip(h),flip(c)]);
  for (const name of ['no-reversal-buy','no-reversal-sell']) cases.push([name,name,{}]);
}
const keys = ('b channelHi channelLo ema atr selected structureSide suggestion filledSide displaySide side pendingSide eventKind bias broken origin extreme reaction buyLo buyHi sellLo sellHi rawSL cash totalNet maxDD grossWin grossLoss wins losses flats buyLabels sellLabels conflicts repeats structureCount suggestionCount fillCount openVetoCount maybeCloseTie runTip legOuter guardSeen guardEvent').split(' ');
keys.push(...'midRange simFillPx simFillSL simFillTP riskQualified'.split(' '));
const diag = 'int riskQualified = structureSide != 0 and stopOK and stopPlan > 0 and emaOK and (rawBuy ? allowLong : allowShort) ? structureSide : 0\n'+'int guardSeen = seenPullback ? 1 : 0\nint guardEvent = breakNow ? bias * eventKind : 0\n'+keys.map(k=>`plot(${k}, "Q:${k}")`).join('\n');
const sym = {ticker:'FIX',tickerid:'QA:FIX',mintick:.001,pricescale:1000,minmove:1,pointvalue:1,timezone:'Etc/UTC',type:'forex',session:'24x7'};
async function run(bars, opts, live=false) {
  const shift = live ? Date.now()-5000-bars.at(-1)[0]*1000 : 0;
  const data = bars.map(([t,open,high,low,close])=>({openTime:t*1000+shift,closeTime:(t+60)*1000+shift,open,high,low,close,volume:1}));
  const e = new PineTS({getMarketData:async()=>data,getSymbolInfo:async()=>sym,configure(){}},'QA:FIX','1',data.length); e.setAlertMode('all');
  return e.run(new Indicator(src+'\n'+diag+'\nplot(barstate.isconfirmed ? 1 : 0, "Q:closed")',opts));
}
(async()=>{
  const out = {policy:'bos_half_range_close_v4',keys,fixtures:set.fixtures,cases:[]};
  for (const [name,fixture,opts] of cases) {
    const bars = set.fixtures[fixture], ctx = await run(bars,opts);
    const cols = keys.map(k=>ctx.plots['Q:'+k].data.map(p=>p.value));
    out.cases.push({name,fixture,opts,rows:bars.map((_,i)=>cols.map(c=>c[i]))});
    const valid = ctx.plots['Q:displaySide'].data;
    const debug = opts.labelMode === 'All Hull crosses (debug)';
    const expectedAlerts = valid.flatMap((x,i)=>x.value ? [[i,debug ? 'Raw Hull cross · diagnostic only' : `Hull ${x.value===1?'BUY':'SELL'} · selected view`]] : []);
    assert.deepEqual(ctx.alerts.map(a=>[a.bar_index,a.title]),expectedAlerts,name+' exact alert bars and titles');
    if (debug) assert(ctx.alerts.every(a=>a.message.includes('not an entry')),name+' debug alerts must be explicitly diagnostic');
    if (['no-reversal-buy','no-reversal-sell','protected-buy','protected-sell'].includes(name)) {
      for (const i of (name.startsWith('no-reversal')?[410,418,420]:[45,92,93])) {
        const live = await run(bars.slice(0,i+1),opts,true);
        const v = k=>live.plots['Q:'+k].data[i].value;
        assert(live.alerts.every(a=>a.bar_index<i),name+' no alert on the forming candle');
        for (const k of ['closed','selected','guardEvent','suggestion','filledSide']) assert.equal(v(k),0,`${name} unconfirmed ${k}@${i}`);
        for (const k of ['pendingSide','side','origin','extreme','bias']) assert.equal(v(k),ctx.plots['Q:'+k].data[i-1].value,`${name} no early ${k}@${i}`);
        for (const k of ['suggestion','filledSide']) assert.deepEqual(live.plots['Q:'+k].data.slice(0,i).map(p=>p.value),ctx.plots['Q:'+k].data.slice(0,i).map(p=>p.value),`${name} stable prefix ${k}@${i}`);
      }
    }
  }
  fs.writeFileSync(process.argv[4],JSON.stringify(out));
  console.log(out.policy, out.cases.length,'PineTS scenarios',out.cases.reduce((a,c)=>a+c.rows.length,0),'bar decisions',keys.length,'fields');
})().catch(e=>{console.error(e);process.exitCode=1});
