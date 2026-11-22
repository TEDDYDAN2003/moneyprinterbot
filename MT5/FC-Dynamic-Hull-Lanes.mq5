// FC Dynamic Hull / independent lane monitor. Custom indicator, NOT an EA.
// Reads chart prices only. Never sends requests to the trade server.
#property strict
#property version "1.20"
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots 2
#property indicator_type1 DRAW_LINE
#property indicator_type2 DRAW_LINE
#property indicator_color1 clrMediumSeaGreen
#property indicator_color2 clrRoyalBlue
#property indicator_label1 "Selected Hull A (confirmed focus lane)"
#property indicator_label2 "Hull B close (confirmed focus lane)"
#property indicator_width1 2
#property indicator_width2 1
#property description "Independent M15, M30 and synthetic M45. A confirmed MSS means exit that lane only. Read-only signals, not fills."

enum FC_FOCUS { Focus_M15=15, Focus_M30=30, Focus_M45=45 };
input FC_FOCUS Focus=Focus_M15; // Hull lines from this lane; panel always shows all three
input int Swing=3;              // Confirmed swing bars on each side
input bool NeutralWait=false;   // False = use High while neutral (no entry)
input int AtrLength=14;
input double AtrBuffer=0.1;
input double ExtraPips=0.0;
input double PipSize=0.01;      // Used only when ExtraPips > 0
input int MarksPerLane=30;      // Maximum most recent labels per lane, 0 = panel only
input bool OnlyFocusMarks=false;
input bool AlertFreshMSS=false; // Freshly observed closed shifts only; never on attach/history

// BEGIN PORTABLE CORE -- also executed by the fabricated-candle contract.
struct HullState {
   int bias,count,established; datetime closed,epoch,skipBar,failedCounter;
   bool firstWaived;
   double hi,lo,lastH,lastL,pullH,pullL,guard,tip,broken;
   datetime hiT,loT,pullHT,pullLT,legT,originT,windowT,guardT,tipT,lastHT,lastLT;
   double oldLow,oldHigh,oldB,prevC,atr,sumTR;
};
double TradeTick=0.0; // from SYMBOL_TRADE_TICK_SIZE; never infer it from pip size
struct Result {int bias,ev,raw,eligible; bool skip,waived,exitClose; double lowA,highA,a,b,sl;};

double Field(const MqlRates &x,const int f){return f==1?x.low:f==-1?x.high:x.close;}
double W(const MqlRates &q[],const int i,const int n,const int f){
   double sum=0; for(int k=0;k<n;k++)sum+=(k+1)*Field(q[i-n+1+k],f);
   return sum/(n*(n+1)/2.0);
}
double Curve(const MqlRates &q[],const int i,const int f){
   int n=f==0?8:4,inner=f==0?8:16; if(i+1<n+inner-1)return 0;
   double sum=0; for(int k=0;k<n;k++){int j=i-n+1+k;
      double v=f==0?3*W(q,j,2,0)-W(q,j,4,0)-W(q,j,8,0):2*W(q,j,8,f)-W(q,j,16,f);
      sum+=(k+1)*v;
   }return sum/(n*(n+1)/2.0);
}
int StructureStep(HullState &s,const MqlRates &q[],const int i){
   int j=i-Swing; bool ph=j>=Swing,pl=ph; datetime pt=j>=0?q[j].time:0;
   if(ph)for(int k=1;k<=Swing;k++){
      if(q[j].high<q[j-k].high||q[j].high<=q[j+k].high)ph=false;
      if(q[j].low>q[j-k].low||q[j].low>=q[j+k].low)pl=false;
   }
   int d=s.bias,ev=0; double level=0; datetime origin=0;
   if(d!=0&&s.guard>0&&d*(q[i].close-s.guard)<0){ev=-2*d;level=s.guard;origin=s.guardT;s.guard=s.tip;s.guardT=s.tipT;}
   else if(d>=0&&s.hi>0&&q[i].close>s.hi&&(d==1||(s.lastL>0&&s.lastL<q[i].close))){
      ev=d==0?3:1;level=s.hi;origin=s.hiT;
      if(d==0||(s.pullL>0&&(s.guard==0||s.pullL>s.guard))){s.guard=s.pullL>0?s.pullL:s.lastL;s.guardT=s.pullL>0?s.pullLT:s.lastLT;}
   }else if(d<=0&&s.lo>0&&q[i].close<s.lo&&(d==-1||(s.lastH>0&&s.lastH>q[i].close))){
      ev=d==0?-3:-1;level=s.lo;origin=s.loT;
      if(d==0||(s.pullH>0&&(s.guard==0||s.pullH<s.guard))){s.guard=s.pullH>0?s.pullH:s.lastH;s.guardT=s.pullH>0?s.pullHT:s.lastHT;}
   }
   // Keep the timestamp of the actual guard: it bounds pivots spanning a break.
   if(ev!=0){ d=ev>0?1:-1; s.broken=level;s.legT=q[i].time;s.originT=origin;s.windowT=pt;
      s.hi=0;s.lo=0;s.pullL=0;s.pullH=0;s.tip=d==1?q[i].high:q[i].low;s.tipT=q[i].time;
   }else if(d!=0){double v=d==1?q[i].high:q[i].low;if(s.tip==0||d*(v-s.tip)>=0){s.tip=v;s.tipT=q[i].time;}}
   bool inLeg=s.legT==0||(pt>0&&pt>=s.legT);
   bool inWindow=s.windowT>0&&pt>=s.windowT&&(s.originT==0||pt>s.originT);
   if(ph){double h=q[j].high;s.lastH=h;s.lastHT=pt;
      if(d>0&&s.pullL>0&&pt>s.pullLT&&(s.guard==0||s.pullL>s.guard)){s.guard=s.pullL;s.guardT=s.pullLT;}
      bool beyond=s.broken==0||h>s.broken;
      if((inLeg||(d>0&&s.hi==0&&beyond&&inWindow))&&d>=0&&beyond&&(s.hi==0||h>=s.hi)){
         s.hi=h;s.hiT=pt;s.pullL=0;}
      if(inLeg&&d<=0&&s.lo>0&&pt>s.loT&&(s.pullH==0||h>=s.pullH)){s.pullH=h;s.pullHT=pt;}
   }
   if(pl){double l=q[j].low;s.lastL=l;s.lastLT=pt;
      if(d<0&&s.pullH>0&&pt>s.pullHT&&(s.guard==0||s.pullH<s.guard)){s.guard=s.pullH;s.guardT=s.pullHT;}
      bool beyond=s.broken==0||l<s.broken;
      if((inLeg||(d<0&&s.lo==0&&beyond&&inWindow))&&d<=0&&beyond&&(s.lo==0||l<=s.lo)){
         s.lo=l;s.loT=pt;s.pullH=0;}
      if(inLeg&&d>=0&&s.hi>0&&pt>s.hiT&&(s.pullL==0||l<=s.pullL)){s.pullL=l;s.pullLT=pt;}
   }
   s.bias=d;return ev;
}
void Step(HullState &s,const MqlRates &q[],const int i,const int tf,const bool commit,Result &r){
   ZeroMemory(r); datetime end=q[i].time+tf*60;
   if(commit){
      if(s.closed!=q[i].time){s.established=0;s.failedCounter=0;} // unknown interval: no failed-leg proof
      int oldBias=s.bias;r.ev=StructureStep(s,q,i);
      if(MathAbs(r.ev)==2){
         s.failedCounter=(s.established==s.bias&&oldBias==-s.bias)?s.epoch:0;
         s.epoch=end;s.skipBar=0;s.firstWaived=false;
      }else if(MathAbs(r.ev)==3||MathAbs(r.ev)==1)s.established=s.bias;
      double tr=q[i].high-q[i].low;
      if(s.count>0)tr=MathMax(tr,MathMax(MathAbs(q[i].high-s.prevC),MathAbs(q[i].low-s.prevC)));
      s.count++;if(s.count<=AtrLength){s.sumTR+=tr;if(s.count==AtrLength)s.atr=s.sumTR/AtrLength;}
      else s.atr=(s.atr*(AtrLength-1)+tr)/AtrLength;
   }
   r.bias=s.bias;r.lowA=Curve(q,i,1);r.highA=Curve(q,i,-1);r.b=Curve(q,i,0);
   int pick=s.bias==1?1:(s.bias==-1||!NeutralWait)?-1:0;
   r.a=pick==1?r.lowA:pick==-1?r.highA:0;double old=pick==1?s.oldLow:s.oldHigh;
   if(pick!=0&&s.count>=(commit?20:19)){
      if(r.b>r.a&&s.oldB<old)r.raw=1;else if(r.b<r.a&&s.oldB>old)r.raw=-1;
   }
   // A live raw cross is an exit notification only. No forming wick, entry,
   // first-cross skip or waiver before the native candle actually closes.
   if(commit&&r.raw!=0){double anchor=r.raw==1?q[i].low:q[i].high;
      if(anchor>0&&s.count>=AtrLength&&TradeTick>0){
         double units=(anchor-r.raw*MathMax(TradeTick,AtrBuffer*s.atr+ExtraPips*PipSize))/TradeTick;
         double nearTick=MathRound(units);
         if(MathAbs(units-nearTick)<=4*DBL_EPSILON*MathMax(1.0,MathAbs(units)))units=nearTick;
         r.sl=(r.raw==1?MathFloor(units):MathCeil(units))*TradeTick;
      }
      if(r.raw==r.bias&&r.sl>0&&r.raw*(q[i].close-r.sl)>0){
         bool back=s.failedCounter>0&&s.failedCounter<s.epoch&&s.established==s.bias;
         r.exitClose=back&&end<=s.epoch;
         if(!r.exitClose){
            if(s.epoch!=0&&s.skipBar==0){s.skipBar=end;s.firstWaived=back;}
            r.waived=back&&s.firstWaived&&end==s.skipBar;
            r.skip=s.epoch!=0&&end<=s.skipBar&&!r.waived;
            r.eligible=r.skip?0:r.raw;
         }
      }
   }
   if(commit){s.closed=end;s.prevC=q[i].close;s.oldLow=r.lowA;s.oldHigh=r.highA;s.oldB=r.b;}
}
// END PORTABLE CORE

bool Good(const MqlRates &x){return x.time>0&&MathIsValidNumber(x.open)&&MathIsValidNumber(x.high)&&MathIsValidNumber(x.low)&&MathIsValidNumber(x.close)&&x.low>0&&x.low<=MathMin(x.open,x.close)&&x.high>=MathMax(x.open,x.close);}
// D1 supplies the broker's actual daily anchor; never use the PC clock or cast 45 to ENUM_TIMEFRAMES.
int Fetch(const int tf,const bool forming,const int wanted,MqlRates &out[],datetime &lastBase){
   ArrayResize(out,0);lastBase=0;MqlRates base[];ArraySetAsSeries(base,false);
   ENUM_TIMEFRAMES source=tf==30?PERIOD_M30:PERIOD_M15;
   int n=CopyRates(_Symbol,source,forming?0:1,tf==45?(forming?3:3*wanted+2):wanted,base);
   if(n<=0)return -1;
   lastBase=base[n-1].time;
   if(tf!=45){ArrayCopy(out,base);return n;}
   MqlRates days[];ArraySetAsSeries(days,false);
   int nd=CopyRates(_Symbol,PERIOD_D1,(datetime)(base[0].time-86400),lastBase,days);
   if(nd<=0)return -1;for(int i=1;i<nd;i++)if(days[i-1].time>=days[i].time)return -1;
   int day=-1,parts=0,used=0;datetime bucket=0;MqlRates bar;
   for(int i=0;i<n;i++){
      datetime t=base[i].time;if(i>0&&t<=base[i-1].time)return -1;
      while(day+1<nd&&days[day+1].time<=t)day++;
      if(day<0){parts=0;continue;} long age=(long)(t-days[day].time);
      if(age<0||age>=86400||age%900!=0){parts=0;continue;}
      datetime b=days[day].time+(age/2700)*2700;
      if(b!=bucket){parts=0;bucket=b;}
      if(t!=bucket+900*parts||!Good(base[i])){parts=0;continue;}
      if(parts==0)bar=base[i]; else {bar.high=MathMax(bar.high,base[i].high);bar.low=MathMin(bar.low,base[i].low);bar.close=base[i].close;}
      parts++;
      if(parts==3&&!forming){ArrayResize(out,used+1,256);out[used++]=bar;}
   }
   if(forming&&parts>0){ArrayResize(out,1);out[0]=bar;return 1;}
   if(used>wanted){ArrayRemove(out,0,used-wanted);used=wanted;}return used;
}

string prefix;double ALine[],BLine[];
struct PointAB {datetime end; double a,b;};
PointAB trace[];
void Snapshot(const datetime end,const Result &r){int n=ArraySize(trace);if(n>1500){ArrayRemove(trace,0,200);n-=200;}
   ArrayResize(trace,n+1,256);trace[n].end=end;trace[n].a=r.a;trace[n].b=r.b;}
int At(const datetime t){int left=0,right=ArraySize(trace)-1,best=-1;while(left<=right){int mid=(left+right)/2;
   if(trace[mid].end<=t){best=mid;left=mid+1;}else right=mid-1;}return best;}
string Phase(const int d){return d>0?"Bullish":d<0?"Bearish":"Neutral";}
string Side(const int d){return d>0?"BUY":"SELL";}
string Stamp(const datetime t){return t>0?TimeToString(t,TIME_DATE|TIME_MINUTES):"none in history";}
void Panel(const int row,const string text,const color ink){string n=prefix+"panel"+IntegerToString(row);
   if(ObjectFind(0,n)<0){ObjectCreate(0,n,OBJ_LABEL,0,0,0);ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,n,OBJPROP_XDISTANCE,10);ObjectSetInteger(0,n,OBJPROP_YDISTANCE,22+17*row);
      ObjectSetInteger(0,n,OBJPROP_FONTSIZE,9);ObjectSetString(0,n,OBJPROP_FONT,"Consolas");
      ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);}
   ObjectSetString(0,n,OBJPROP_TEXT,text);ObjectSetInteger(0,n,OBJPROP_COLOR,ink);}

class LaneView {
public:
   int tf;HullState state;Result last;MqlRates tape[];string marks[],text;datetime sourceOpen,lastMss,previousSample;
   void Init(const int period){tf=period;Reset();}
   void Reset(){ZeroMemory(state);ZeroMemory(last);ArrayResize(tape,0);sourceOpen=0;lastMss=0;previousSample=0;text="Waiting for rates";
      for(int j=0;j<ArraySize(marks);j++)ObjectDelete(0,marks[j]);ArrayResize(marks,0);if(tf==(int)Focus)ArrayResize(trace,0);}
   void Mark(const double px,const datetime end,const string word,const color ink,const double setupSL=0.0){
      if(MarksPerLane==0||(OnlyFocusMarks&&tf!=(int)Focus))return;
      string n=prefix+IntegerToString(tf)+"_"+IntegerToString(end)+"_"+word;
      if(ObjectFind(0,n)>=0)return;
      if(!ObjectCreate(0,n,OBJ_TEXT,0,end,px))return;
      ObjectSetString(0,n,OBJPROP_TEXT,"M"+IntegerToString(tf)+" "+word);ObjectSetInteger(0,n,OBJPROP_COLOR,ink);
      ObjectSetInteger(0,n,OBJPROP_ANCHOR,tf==30?ANCHOR_LEFT_UPPER:tf==45?ANCHOR_RIGHT_UPPER:ANCHOR_LEFT_LOWER);
      ObjectSetInteger(0,n,OBJPROP_FONTSIZE,8);ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
      ObjectSetString(0,n,OBJPROP_TOOLTIP,_Symbol+" M"+IntegerToString(tf)+" | "+word+" | confirmed "+Stamp(end)+" (broker time)"+(setupSL>0?" | setup SL: "+DoubleToString(setupSL,_Digits):"")+"; signal, not fill.");
      int k=ArraySize(marks);if(k>=MarksPerLane){ObjectDelete(0,marks[0]);ArrayRemove(marks,0,1);k--;}
      ArrayResize(marks,k+1);marks[k]=n;
   }
   bool Poll(const MqlTick &tick){
      ENUM_TIMEFRAMES base=tf==30?PERIOD_M30:PERIOD_M15;datetime latest=iTime(_Symbol,base,0);
      if(latest==0){text="Waiting for native history";return false;}
      bool redraw=false;
      if(latest!=sourceOpen||state.count==0){
         bool warm=state.count==0||tick.time-state.closed>3*tf*60;
         MqlRates rows[];datetime tail;int n=Fetch(tf,false,warm?1200:4,rows,tail);
         int required=MathMax(22,MathMax(AtrLength+1,2*Swing+2));
         if(n<(warm?required:1)){text="Insufficient closed history";return false;}
         if(warm)Reset();
         for(int j=0;j<n;j++){
            if(rows[j].time<state.closed)continue;
            if(!Good(rows[j])||rows[j].time+tf*60>tick.time)continue;
            int k=ArraySize(tape);ArrayResize(tape,k+1,256);tape[k]=rows[j];
            Step(state,tape,k,tf,true,last);
            if(tf==(int)Focus){Snapshot(state.closed,last);redraw=true;}
            if(MathAbs(last.ev)==2){lastMss=state.closed;
               if(!warm||j>=n-400)Mark(rows[j].close,state.closed,"MSS EXIT",clrDarkOrange);
               if(AlertFreshMSS&&!warm&&state.closed>previousSample&&tick.time-state.closed<=tf*60)
                  Alert(_Symbol," M",tf," confirmed MSS: close this lane only | ",Stamp(state.closed));
            }
            if(last.eligible!=0&&(!warm||j>=n-400))Mark(rows[j].close,state.closed,Side(last.eligible)+(last.waived?" (resume)":""),last.eligible==1?clrSeaGreen:clrIndianRed,last.sl);
         }
         // Keep indicator state across shifts; drop only old raw samples, not pivot/ATR/skip state.
         int used=ArraySize(tape),keep=MathMax(150,2*Swing+4);if(used>keep)ArrayRemove(tape,0,used-keep);
         // If the new source bar is visible but shift-1 rates lag, try again next tick.
         if(state.closed>0&&latest>=state.closed&&latest-state.closed<tf*60)sourceOpen=latest;
      }
      MqlRates current[];datetime part;string live="waiting for complete prefix";
      int num=Fetch(tf,true,1,current,part);
      if(num==1&&Good(current[0])&&current[0].time==state.closed&&tick.time>=part&&tick.time<part+PeriodSeconds(base)&&current[0].close==tick.bid){
         MqlTick check;if(SymbolInfoTick(_Symbol,check)&&check.time_msc==tick.time_msc&&check.bid==tick.bid&&check.ask==tick.ask){
            int k=ArraySize(tape);ArrayResize(tape,k+1,256);tape[k]=current[0];Result r;Step(state,tape,k,tf,false,r);ArrayResize(tape,k);
            live=r.raw!=0?"raw "+Side(r.raw)+" (live / exits only)":"no live cross";
         }else live="quote changed";
      }
      text=Phase(state.bias)+" | "+live+" | last MSS: "+Stamp(lastMss);previousSample=tick.time;return redraw;
   }
};
LaneView lanes[3];
int OnInit(){if(Swing<1||Swing>50||AtrLength<1||AtrLength>500||AtrBuffer<0||ExtraPips<0||PipSize<=0||MarksPerLane<0||MarksPerLane>200)return INIT_PARAMETERS_INCORRECT;
   TradeTick=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(!MathIsValidNumber(TradeTick)||TradeTick<=0){Print("No verified symbol trade tick size");return INIT_FAILED;}
   if(SymbolInfoInteger(_Symbol,SYMBOL_CHART_MODE)!=SYMBOL_CHART_MODE_BID){Print("FC Hull needs a bid-price chart matching the bot");return INIT_FAILED;}
   prefix="FCDH_"+IntegerToString((long)GetTickCount64())+"_";while(ObjectFind(0,prefix+"panel0")>=0)prefix+="x";
   if(!SetIndexBuffer(0,ALine,INDICATOR_DATA)||!SetIndexBuffer(1,BLine,INDICATOR_DATA))return INIT_FAILED;
   ArraySetAsSeries(ALine,false);ArraySetAsSeries(BLine,false);IndicatorSetString(INDICATOR_SHORTNAME,"FC Hull 16 • lane monitor");
   Panel(0,"HULL 16   /   CLOSE → NEXT BAR   /   READ-ONLY",clrSlateGray);
   lanes[0].Init(15);lanes[1].Init(30);lanes[2].Init(45);return INIT_SUCCEEDED;}
void OnDeinit(const int reason){if(StringLen(prefix)>0)ObjectsDeleteAll(0,prefix);}
int OnCalculate(const int total,const int prev,const datetime &time[],const double &open[],const double &high[],const double &low[],const double &close[],const long &vol[],const long &realvol[],const int &spread[]){
   if(total<1)return 0;ArraySetAsSeries(time,false);MqlTick q;if(!SymbolInfoTick(_Symbol,q))return prev;
   bool changed=false;for(int j=0;j<3;j++){if(lanes[j].Poll(q))changed=true;
      Panel(j+1,"M"+IntegerToString(lanes[j].tf)+"  "+lanes[j].text,lanes[j].state.bias>0?clrMediumSeaGreen:lanes[j].state.bias<0?clrIndianRed:clrSlateGray);}
   int from=prev<=0||prev>total||changed?0:MathMax(0,prev-1);for(int i=from;i<total;i++){
      int k=At(time[i]);ALine[i]=k>=0&&trace[k].a!=0?trace[k].a:EMPTY_VALUE;BLine[i]=k>=0&&trace[k].b!=0?trace[k].b:EMPTY_VALUE;}
   return total;
}
