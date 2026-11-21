"""Compile the shipped MQ5 in a local C++ terminal shim. Fabricated times/quotes only.
Run: PYTHONPATH=src python MT5/qa/check_dynamic_lanes.py (requires g++). Not MetaEditor.
"""

import math
import random
import re
import subprocess
import tempfile
from pathlib import Path

from bot.hull.config import Indicator
from bot.hull.signal import Hull
from bot.zones.engine import Bar

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
int main(int argc,char**argv){int tf=stoi(argv[1]),depth=stoi(argv[2]);Swing=depth;PivotStop=stoi(argv[3]);NeutralWait=stoi(argv[4]);
 vector<MqlRates> q;MqlRates x;while(cin>>x.time>>x.open>>x.high>>x.low>>x.close)q.push_back(x);HullState st{};cout<<setprecision(17);
 if(tf){for(int i=0;i<(int)q.size();i++){Result r;Step(st,q,i,tf,true,r);cout<<r.bias<<' '<<r.ev<<' '<<r.raw<<' '<<r.eligible<<' '<<r.skip<<' '<<r.lowA<<' '<<r.highA<<' '<<r.b<<' '<<r.sl<<' '<<st.guard<<' '<<st.epoch<<'\n';}return 0;}
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
    hits = cases = 0
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
        for depth, pivot, wait in [(3, 0, 0), (1, 1, 1)]:
            m = Hull(
                tf,
                Indicator(
                    swing_len=depth,
                    stop="confirmed_pivot" if pivot else "local_pullback",
                    neutral_mode="wait" if wait else "high",
                ),
                0.01,
            )
            out = subprocess.check_output(
                [str(exe), str(tf), str(depth), str(pivot), str(wait)],
                input=text,
                text=True,
            ).splitlines()
            assert len(out) == len(bars)
            for b, line in zip(bars, out):
                r = m.update(b)
                want = [
                    r.structure.bias,
                    r.structure.event_code,
                    r.label,
                    r.entry_side,
                    r.entry_reason == "first_trend_cross_after_mss",
                    r.a_low,
                    r.a_high,
                    r.b,
                    r.raw_sl,
                    r.structure.guard.price if r.structure.guard else None,
                    r.structure.epoch,
                ]
                vals = list(map(float, line.split()))
                assert len(vals) == len(want)
                for j, (v, w) in enumerate(zip(vals, want)):
                    assert math.isclose(v, w or 0, rel_tol=1e-10, abs_tol=1e-9), (
                        tf,
                        b.t,
                        j,
                        v,
                        w,
                    )
                hits += r.confirmed_mss
                cases += 1
        if tf == 15:
            subprocess.run(
                [str(exe), "0", "3", "0", "0"], input=text, text=True, check=True
            )
    assert hits > 0
    print(
        f"PASS: actual MQ5 calculation / Python parity: {cases} synthetic native closes, {hits} MSS events; M45 OHLC, 3-lane OnCalculate, no attach/duplicate alert. C++ shim, NOT MetaEditor."
    )
