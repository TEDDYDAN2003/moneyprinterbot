// Optional offline chart check. Install pinets@0.13.1 in an isolated folder and
// put its node_modules on NODE_PATH. No terminal, API key, or network feed is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { PineTS, Indicator } = require('pinets');
const source = fs.readFileSync(path.resolve(__dirname, '../../indicators/FC-Hull-Either-16-v2.pine'), 'utf8');
const bars = JSON.parse(fs.readFileSync(path.join(__dirname, 'reference.json'), 'utf8')).bars.slice(0,-1);
for (let i=0;i<145;i++) {
  const w = i%42<24 ? 6 : .0625;
  bars.push({open:100,low:100-w,high:100+w,close:100});
}
for (let i=0;i<115;i++) {
  const v=100+Math.round(2*Math.sin(i*.27)*1e4)/1e4;
  bars.push({open:v,high:v,low:v,close:v});
}
function wma(v,n) {
  return v.map((_,i) => {
    if (i+1<n) return null;
    let sum=0; for(let k=1;k<=n;k++){const x=v[i-n+k];if (x===null) return null;sum+=k*x;}
    return sum/(n*(n+1)/2);
  });
}
function mix(cols,fn){return cols[0].map((_,i)=>{const v=cols.map(c=>c[i]);return v.includes(null)?null:fn(...v)});}
const c=bars.map(v=>v.close), b=wma(mix([wma(c,2),wma(c,4),wma(c,8)],(x,y,z)=>3*x-y-z),8);
function cross(src){
  const v=bars.map(bar=>bar[src]), a=wma(mix([wma(v,8),wma(v,16)],(x,y)=>2*x-y),4);
  return a.map((x,i)=>i===0||[x,a[i-1],b[i],b[i-1]].includes(null)?0:
    b[i]>x && b[i-1]<a[i-1]?1:b[i]<x && b[i-1]>a[i-1]?-1:0);
}
const lo=cross('low'), hi=cross('high'), conflict=lo.map((x,i)=>+(x!==0 && x===-hi[i]));
const expected=lo.map((x,i)=>conflict[i]?0:x||hi[i]);
async function run(count,forming=false) {
  const base=forming?Date.now()-((count-1)*60000+10000):Date.UTC(2024,0,1);
  const feed=bars.slice(0,count).map((r,i)=>({...r,volume:10,openTime:base+i*60000,closeTime:base+(i+1)*60000}));
  const provider={getMarketData:async()=>feed,getSymbolInfo:async()=>({ticker:'FX',tickerid:'TEST:FX',minmove:1,mintick:.001,pricescale:1000,pointvalue:1,timezone:'Etc/UTC',type:'forex',session:'24x7'}),configure(){}};
  const engine=new PineTS(provider,'TEST:FX','1',count);engine.setAlertMode('all');
  const ctx=await engine.run(new Indicator(source));
  for(const [name,want] of [['Selected Hull signal (+1 / -1 / 0)',expected],['Low-source cross (+1 / -1 / 0)',lo],['High-source cross (+1 / -1 / 0)',hi],['Conflict skipped',conflict]]){
    const v=want.slice(0,count);if(forming) v[count-1]=0;
    assert.deepEqual(ctx.plots[name].data.map(p=>p.value),v,name);
  }
  assert.equal(ctx.alerts.length,expected.slice(0,count-Number(forming)).filter(Boolean).length);
}
(async()=>{
  assert(conflict.some(Boolean) && lo.some((x,i)=>x===1&&hi[i]===1) && lo.some((x,i)=>x===-1&&hi[i]===-1));
  await run(bars.length);
  for (const side of [1,-1]) {const i=expected.findIndex((v,i)=>v===side&&i>45);assert(i>=0);await run(i+1,true);}
  console.log(`PASS: ${bars.length} completed candles, both independent sources, coalescing/conflicts, one alert per raw signal, and live exclusion in both directions.`);
})().catch(e=>{console.error(e.stack||String(e));process.exitCode=1;});
