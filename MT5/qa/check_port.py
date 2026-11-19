"""Compare the actual .mq5 calculations to PineTS. Linux C++ host shim, NOT MetaEditor.
Usage: python qa/check_port.py oracle.json (oracle from pine_oracle.cjs). No trading/terminal.
"""
import json, math, re, subprocess, sys, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]; src=(root/'FC-Hull-Either-16-Structure.mq5').read_text(); oracle=json.loads(Path(sys.argv[1]).read_text())
keys='b hi lo ema atr raw structure valid filled display S.side S.pending S.kind S.bias S.broken S.origin S.extreme S.reaction buyLo buyHi sellLo sellHi rawSL S.cash S.totalNet S.maxDD S.gWin S.gLoss S.wins S.losses S.flats S.rawBuys S.rawSells S.conflicts S.repeats S.structN S.suggestN S.fillN S.vetoN tie S.tip S.legOuter S.seenPullback (S.breakNow?S.bias*S.kind:0)'.split();assert len(keys)==len(oracle['keys'])
# Small native API stand-ins; the signal, simulation and the actual OnCalculate scheduler are not reimplemented.
pre='''#include <bits/stdc++.h>
using namespace std;using datetime=long long;using color=int;using ENUM_INDEXBUFFER_TYPE=int;using ENUM_BASE_CORNER=int;using ENUM_ANCHOR_POINT=int;using ENUM_LINE_STYLE=int;
const double EMPTY_VALUE=DBL_MAX; const string _Symbol="FIX";const int _Period=1,_Digits=3;const double _Point=.001;
#define ZeroMemory(x) x={}
#define MathRound round
#define MathFloor floor
#define MathCeil ceil
#define MathSqrt sqrt
#define MathAbs abs
#define MathMax max
#define MathMin min
const int INDICATOR_DATA=0,INDICATOR_COLOR_INDEX=1,INDICATOR_CALCULATIONS=2,CORNER_LEFT_UPPER=0,CORNER_RIGHT_UPPER=1,CORNER_RIGHT_LOWER=2,CORNER_LEFT_LOWER=3,INIT_SUCCEEDED=0,INIT_FAILED=-1,INIT_PARAMETERS_INCORRECT=-2,PERIOD_MN1=31,CHARTEVENT_CHART_CHANGE=9;
vector<double>* bindings[36];int roles[36];int alerts=0;set<string> objects;
bool SetIndexBuffer(int n,vector<double>&v,int k){if(n<0||n>=36)return false;bindings[n]=&v;roles[n]=k;return true;}
template<class T>bool ArraySetAsSeries(const T&,bool b){assert(!b);return true;}template<class T>int ArrayResize(T&v,int n){v.resize(n);return n;}
template<class...T>void PlotIndexSetInteger(T...x){}template<class...T>void PlotIndexSetDouble(T...x){}template<class...T>void IndicatorSetInteger(T...x){}template<class...T>void IndicatorSetString(T...x){}template<class...T>void ObjectSetInteger(T...x){}template<class...T>void ObjectSetString(T...x){}template<class...T>void ObjectMove(T...x){}template<class...T>void Print(T...x){}template<class...T>void Alert(T...x){alerts++;}
template<class...T>bool ObjectCreate(int,const string &n,T...x){objects.insert(n);return true;} int ObjectFind(int,const string &n){return objects.count(n)?0:-1;}void ObjectsDeleteAll(int,const string &pre){assert(!pre.empty());for(auto i=objects.begin();i!=objects.end();)if(i->find(pre)==0)i=objects.erase(i);else ++i;}
template<class...T>long ChartGetInteger(T...x){return 1000;}template<class...T>string StringFormat(const char *fmt,T...x){char b[300];snprintf(b,300,fmt,x...);return b;}string IntegerToString(long long n){return to_string(n);}string DoubleToString(double n,int d){return StringFormat("%.*f",d,n);}int StringLen(const string &s){return s.size();}string EnumToString(int){return "M1";}template<class...T>string TimeToString(T...x){return "time";}double SymbolInfoDouble(const string&,int){return .001;}int PeriodSeconds(int){return 60;}bool IsStopped(){return false;}long long GetTickCount64(){return 121;}struct MqlDateTime{int year,mon,day,hour,min,sec;};bool TimeToStruct(datetime,MqlDateTime&d){return true;}datetime StructToTime(const MqlDateTime&d){return 0;}
'''
for i,k in enumerate(sorted(set(re.findall(r'\b(?:clr[A-Z]\w*|OBJPROP_\w+|PLOT_\w+|CHART_\w+|TIME_(?:DATE|SECONDS)|OBJ_\w+|ANCHOR_\w+|STYLE_\w+|SYMBOL_TRADE_TICK_SIZE|INDICATOR_(?:DIGITS|SHORTNAME)|BORDER_FLAT)\b',src)))): pre+=f'const int {k}={i+10};\n'
s=re.sub(r'^#property[^\n]*|^input group[^\n]*','',src,flags=re.M);s=re.sub(r'^input ','',s,flags=re.M);s=s.replace("C'22,40,43'",'0x2b2816');s=re.sub(r'(const )?(double|datetime|long|int) &(\w+)\[\]',lambda m:(m[1] or '')+f'vector<{m[2]}>& {m[3]}',s)
s=re.sub(r'\b(double|PlanStamp|CaptionStamp) ((?:\w+\[\],)*\w+\[\]);',lambda m:f'vector<{m[1]}> '+m[2].replace('[]','')+';',s)
opts={'hullLen':'InHull','swingLen':'InSwing','atrLength':'InATRLen','targetR':'InTargetR','exitOpposite':'InOpposite','useEma':'InUseEMA','emaLength':'InEMALen','slippageTicks':'InSlipTicks','tickSize':'InSlipTickSize','commissionPercent':'InCommission','allowShort':'InShort','stopMode':'InStop','sourceMode':'InSource','alternateOnly':'InAlternate','labelMode':'InView','pullbackBars':'InPullback','extraPips':'InExtraPips','pointValue':'InPointValue','equityPercent':'InPct'}
maptext={'Confirmed pivot (original chart)':'FC_PIVOT','Directional':'FC_DIRECTIONAL','High only':'FC_HIGH','Low only':'FC_LOW','Either source':'FC_EITHER','Structure-qualified crosses':'FC_STRUCTURE','Simulated fills only':'FC_FILLS','All Hull crosses (debug)':'FC_RAW'}
body='\nint main(int argc,char **argv){int cs=stoi(argv[1]);\n';
for j,case in enumerate(oracle['cases']):
 body+=f'if(cs=={j}){{'+''.join(opts[k]+'='+(maptext.get(str(v),str(v).lower()))+';' for k,v in case['opts'].items())+'}\n'
body+='''vector<datetime> t;vector<double> o,h,l,c;datetime tm;double op,hp,lp,cp;while(cin>>tm>>op>>hp>>lp>>cp){t.push_back(tm);o.push_back(op);h.push_back(hp);l.push_back(lp);c.push_back(cp);}int n=t.size();t.push_back(t.back()+60);o.push_back(c.back());h.push_back(c.back());l.push_back(c.back());c.push_back(c.back());vector<double> wh(n+1),wl(n+1),wb(n+1);FCState S;ResetState(S);vector<FCBar> rs;vector<double> cash;
cout<<setprecision(16);for(int i=0;i<n;i++){FCBar r;ResetBar(r);Curves(i,h,l,c,wh,wl,wb,r);StepClosed(S,r,i,t[i],t[i]+60,o[i],h,l,c,.001);rs.push_back(r);cash.push_back(S.cash);
'''
body+='double vals[]={'+','.join('(double)'+(x if x.startswith(('S.','(')) else 'r.'+x) for x in keys)+'};for(double v:vals)cout<<(v==EMPTY_VALUE?NAN:v)<<" ";cout<<"\\n";}\n'
body+='''InTable=(n<50);InTextCount=(n<50?5:0);InPlans=(n<50);InBuyAlerts=true;InSellAlerts=true;assert(OnInit()==0);for(int i=0;i<36;i++){assert(bindings[i]);bindings[i]->resize(n+1);}vector<long> ticks(n+1);vector<int> spread(n+1);
int prev=0,expectedA=0;auto update=[&](int count,int p){return OnCalculate(count,p,t,o,h,l,c,ticks,ticks,spread);};
int initial=n/3+1;for(int count=initial;count<=n+1;count++){
 int i=count-2;assert(update(count,prev)==count);assert(engine.cash==cash[i]);assert(RawCode[i]==rs[i].raw&&StructureCode[i]==rs[i].structure&&ValidCode[i]==rs[i].valid&&FillCode[i]==rs[i].filled&&ConflictCode[i]==rs[i].conflict);
 assert(GuardCode[i]==engine.origin && TipCode[i]==engine.extreme && BackCode[i]==engine.reaction && BiasCode[i]==engine.bias && EventCode[i]==(engine.breakNow?engine.bias*engine.kind:0) && PullCode[i]==(engine.seenPullback?1:0));
 assert((BuyArrow[i]!=EMPTY_VALUE)==(rs[i].display==1));assert((SellArrow[i]!=EMPTY_VALUE)==(rs[i].display==-1));
 if(prev && rs[i].display && t[i]+60==t[i+1])expectedA++; assert(alerts==expectedA);
 double old=h[count-1];h[count-1]+=38;assert(update(count,count)==count);h[count-1]=old;
 assert(committed==count-1&&RawCode[count-1]==0&&ValidCode[count-1]==0&&FillCode[count-1]==0&&EventCode[count-1]==0&&BuyArrow[count-1]==EMPTY_VALUE&&engine.cash==cash[i]&&alerts==expectedA);prev=count;}
 assert(update(n+1,0)==n+1&&engine.cash==S.cash&&engine.suggestN==S.suggestN&&alerts==expectedA);OnDeinit(0);assert(objects.empty());return 0;}'''
with tempfile.TemporaryDirectory() as tmp:
 path=Path(tmp);(path/'qa.cpp').write_text(pre+s+body);subprocess.run(['g++','-std=c++17','-O2','-ffp-contract=off','-Wno-narrowing',str(path/'qa.cpp'),'-o',str(path/'qa')],check=True)
 for j,case in enumerate(oracle['cases']):
  dat=''.join(' '.join(map(str,b))+'\n' for b in oracle['fixtures'][case['fixture']]);out=subprocess.run([str(path/'qa'),str(j)],input=dat,text=True,capture_output=True);assert out.returncode==0,(case['name'],out.stderr)
  actual=out.stdout.splitlines();assert len(actual)==len(case['rows'])
  for i,(line,ref) in enumerate(zip(actual,case['rows'])):
   vs=list(map(float,line.split()));assert len(vs)==len(ref)
   for k,(a,b) in enumerate(zip(vs,ref)):
    assert (math.isnan(a) if b is None else math.isclose(a,b,rel_tol=2e-10,abs_tol=2e-8)),(case['name'],i,oracle['keys'][k],a,b)
  print('PASS',case['name'],len(actual),'closed bars')
print('PASS: actual calculation source, incremental/full replay, no forming-bar marks, one eligible popup, no attach/backfill alert. C++ compatibility shim; not a native MT5 compile.')
