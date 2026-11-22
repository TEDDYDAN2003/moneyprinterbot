"""Compile the shipped MQ5 in a local C++ terminal shim. Fabricated times/quotes only.
Run: PYTHONPATH=src python MT5/qa/check_dynamic_lanes.py (requires g++). Not MetaEditor.
"""

import math
import random
import re
import subprocess
import tempfile
from pathlib import Path

from bot.hull.bar import Bar
from bot.hull.config import Indicator
from bot.hull.curves import LiveTick
from bot.hull.signal import Hull

root = Path(__file__).resolve().parents[1]
source = (root / "FC-Dynamic-Hull-Lanes.mq5").read_text()
assert not re.search(
    r"\b(OrderSend|MqlTradeRequest|CTrade|AccountInfo\w*|WebRequest|FileOpen|PERIOD_M45)\b",
    source,
)
pre = (root / "qa/dynamic_lane_shim.hpp").read_text() + "\n"
for i, k in enumerate(
    sorted(
        set(
            re.findall(
                r"\b(?:clr[A-Z]\w*|OBJPROP_\w+|CORNER_\w+|TIME_(?:DATE|MINUTES)|OBJ_\w+|ANCHOR_\w+|INDICATOR_(?:DATA|SHORTNAME)|SYMBOL_CHART_MODE)\b",
                source,
            )
        )
    )
):
    pre += f"const int {k}={i + 10};\n"
s = re.sub(r"^#property[^\n]*|^input ", "", source, flags=re.MULTILINE)
s = re.sub(
    r"(const )?(\w+) &(\w+)\[\]", lambda m: (m[1] or "") + f"vector<{m[2]}>& {m[3]}", s
)
s = s.replace("string marks[],text;", "string marks[];string text;")
s = re.sub(
    r"\b(MqlRates|double|PointAB|string) ((?:\w+\[\],)*\w+\[\]);",
    lambda m: "vector<" + m[1] + "> " + m[2].replace("[]", "") + ";",
    s,
)
body = r"""
void dump(HullState &st,Result &r){cout<<r.bias<<' '<<r.ev<<' '<<r.raw<<' '<<r.eligible<<' '<<r.skip<<' '<<r.lowA<<' '<<r.highA<<' '<<r.b<<' '<<r.sl<<' '<<st.guard<<' '<<st.epoch<<' '<<st.established<<' '<<st.failedCounter<<' '<<r.waived<<' '<<r.exitClose<<'\n';}
void edge(int d){ // every projection is protective-only; only the *later close* may waive
 HullState st{};vector<MqlRates> q;for(int i=0;i<30;i++)q.push_back({90000+900*i,100,101,99,100});
 int i=q.size()-1; st.count=i;st.bias=st.established=d;st.closed=st.epoch=q[i].time;st.failedCounter=st.epoch-4*900;
 st.oldLow=st.lastL=99;st.oldHigh=st.lastH=101;st.oldB=d==1?98:102;st.atr=1;Result r;
 for(int j=0;j<4;j++){Step(st,q,i,15,false,r);assert(r.raw==d&&!r.eligible&&!r.waived&&!r.skip&&!r.sl&&!st.skipBar);}
 double saved=AtrBuffer;AtrBuffer=0;Step(st,q,i,15,true,r);AtrBuffer=saved;
 assert(r.raw==d&&r.waived&&r.eligible==d&&st.skipBar==q[i].time+900);
 assert(abs(r.sl-((d==1?99:101)-d*TradeTick))<1e-10);
}
int main(int argc,char**argv){int tf=stoi(argv[1]),depth=stoi(argv[2]);Swing=depth;TradeTick=stod(argv[3]);NeutralWait=stoi(argv[4]);edge(1);edge(-1);
 vector<MqlRates> q;MqlRates x;while(cin>>x.time>>x.open>>x.high>>x.low>>x.close)q.push_back(x);HullState st{};cout<<setprecision(17);
 if(tf){for(int i=0;i<(int)q.size();i++){Result r;
   if(argc>5&&i){MqlRates full=q[i];q[i].low=q[i].high=q[i].close=q[i].open;
      for(int ms:{0,1}){Step(st,q,i,tf,false,r);dump(st,r);}q[i]=full;
      Step(st,q,i,tf,false,r);dump(st,r);}
   Step(st,q,i,tf,true,r);dump(st,r);}return 0;}
 feed[15]=q;datetime start=q.front().time;assert(start%2700!=0);for(int j=0;j<(int)q.size();j+=96)feed[1440].push_back(q[j]);
 for(int j=0;j+1<(int)q.size();j+=2)feed[30].push_back({q[j].time,q[j].open,max(q[j].high,q[j+1].high),min(q[j].low,q[j+1].low),q[j+1].close});
 assert(OnInit()==INIT_SUCCEEDED);AlertFreshMSS=true;vector<datetime> times;vector<double> price;vector<long> vol;vector<int> sp;
 for(int i=84;i<(int)q.size();i++){
  tick={q[i].time+1,(q[i].time+1)*1000,q[i].close,q[i].close+.01};times.push_back(q[i].time);ALine.resize(times.size());BLine.resize(times.size());
  int n=times.size();OnCalculate(n,n-1,times,price,price,price,price,vol,vol,sp);if(n==1)assert(!alerts);
  for(int j=0;j<3;j++){int f=lanes[j].tf;assert(lanes[j].state.closed==start+(i/(f/15))*f*60);}
  int old=alerts,ct=lanes[2].state.count;OnCalculate(n,n,times,price,price,price,price,vol,vol,sp);assert(old==alerts&&ct==lanes[2].state.count);
 }
 vector<MqlRates> out;datetime tail;int n=Fetch(45,false,4,out,tail);assert(n==4);
 for(auto b:out){int i=(b.time-start)/900;assert(i%3==0&&b.open==q[i].open&&b.close==q[i+2].close&&b.high==max({q[i].high,q[i+1].high,q[i+2].high})&&b.low==min({q[i].low,q[i+1].low,q[i+2].low}));}
 assert(alerts>0);OnDeinit(0);assert(objects.empty());return 0;}
"""
with tempfile.TemporaryDirectory() as td:
    path = Path(td)
    (path / "shim.cpp").write_text(pre + s + body)
    exe = path / "check"
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-O2",
            "-ffp-contract=off",
            str(path / "shim.cpp"),
            "-o",
            str(exe),
        ],
        check=True,
    )
    hits = cases = live_cases = returns = waived = 0
    waived_sides = set()
    for tf in [15, 30, 45]:
        rng = random.Random(tf)
        end = 1400.0
        bars = []
        D = 1700006400 + 3600
        for i in range(550):
            start = end
            end += rng.choice([-5, -3, -1, 0, 1, 4, 7])
            bars.append(
                Bar(
                    D + i * tf * 60,
                    start,
                    max(start, end) + rng.randrange(4),
                    min(start, end) - rng.randrange(4),
                    end,
                )
            )
        text = "".join(f"{b.t} {b.o} {b.h} {b.l} {b.c}\n" for b in bars)
        for depth, trade_tick, wait in [(3, 0.01, 0), (1, 0.25, 1)]:
            cfg = Indicator(
                swing_len=depth,
                neutral_mode="wait" if wait else "high",
            )
            for mixed in (False, True):
                m = Hull(tf, cfg, 0.01, tick_size=trade_tick)
                out = subprocess.check_output(
                    [
                        str(exe),
                        str(tf),
                        str(depth),
                        str(trade_tick),
                        str(wait),
                        *(["ticks"] if mixed else []),
                    ],
                    input=text,
                    text=True,
                ).splitlines()
                readings = []
                for candle in bars:
                    if mixed and m.prev:
                        op = Bar(candle.t, candle.o, candle.o, candle.o, candle.o)
                        for developing, stamp in [
                            (op, candle.t * 1000),
                            (op, candle.t * 1000 + 1),
                            (candle, (candle.t + tf * 60) * 1000 - 1),
                        ]:
                            tick = LiveTick(stamp, developing.c, developing.c + 0.01)
                            readings.append(
                                m.gate.apply(m.preview(developing, tick), observe=True)
                            )
                    readings.append(m.update(candle))
                for r, line in zip(readings, out, strict=True):
                    want = [
                        r.structure.bias,
                        0 if r.trigger else r.structure.event_code,
                        r.label,
                        r.entry_side,
                        r.entry_reason == "first_trend_cross_after_mss",
                        r.a_low,
                        r.a_high,
                        r.b,
                        r.raw_sl,
                        r.structure.guard.price if r.structure.guard else None,
                        r.structure.epoch,
                        r.structure.established_bias,
                        r.structure.failed_counter_mss_s,
                        r.waived_first_cross,
                        r.entry_reason == "mss_close_exit_only",
                    ]
                    vals = list(map(float, line.split()))
                    for j, (v, w) in enumerate(zip(vals, want, strict=True)):
                        assert math.isclose(v, w or 0, rel_tol=1e-10, abs_tol=1e-9), (
                            tf,
                            r.signal_s,
                            j,
                            v,
                            w,
                        )
                    if mixed:
                        live_cases += int(r.trigger is not None)
                    else:
                        cases += 1
                        hits += r.confirmed_mss
                        returns += r.confirmed_mss and r.structure.failed_return
                        if r.waived_first_cross:
                            waived += 1
                            waived_sides.add(r.entry_side)
        if tf == 15:
            subprocess.run(
                [str(exe), "0", "3", "0.01", "0"], input=text, text=True, check=True
            )
    assert (
        0 < returns < hits and waived > 0 and waived_sides == {-1, 1} and live_cases > 0
    )
    print(
        f"PASS: actual MQ5 calculation / Python parity: {cases} synthetic native closes, {hits} MSS events ({returns} failed counter-move returns, {waived} first crosses waived); {live_cases} exit-only live projections; frozen closed-wick stops on two trade grids; M45 OHLC, 3-lane OnCalculate, no attach/duplicate alert. C++ shim, NOT MetaEditor."
    )
