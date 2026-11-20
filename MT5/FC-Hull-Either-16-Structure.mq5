// FC Hull Either 16 | Market Structure Suggestions | MT5 custom indicator (not an EA).
// No order, account, network or file APIs. All P/L and occupancy below are chart simulations.
#property version "4.01"
#property description "BOS-proven half-range trend entries at candle close. CHoCH is context only, never a reversal order."
#property indicator_chart_window
#property indicator_buffers 48
#property indicator_plots 39
#property indicator_label1 "Channel neutral: high;Channel neutral: low"
#property indicator_type1 DRAW_FILLING
#property indicator_color1 clrSilver,clrSilver
#property indicator_label2 "Channel BUY: high;Channel BUY: low"
#property indicator_type2 DRAW_FILLING
#property indicator_color2 clrDarkSeaGreen,clrDarkSeaGreen
#property indicator_label3 "Channel SELL: high;Channel SELL: low"
#property indicator_type3 DRAW_FILLING
#property indicator_color3 clrRosyBrown,clrRosyBrown
#property indicator_label4 "BUY band fill: low;BUY band fill: high"
#property indicator_type4 DRAW_FILLING
#property indicator_color4 clrDarkSeaGreen,clrDarkSeaGreen
#property indicator_label5 "SELL band fill: low;SELL band fill: high"
#property indicator_type5 DRAW_FILLING
#property indicator_color5 clrRosyBrown,clrRosyBrown
#property indicator_label6 "Hull B (close)"
#property indicator_type6 DRAW_COLOR_LINE
#property indicator_color6 clrSlateGray,clrSeaGreen,clrIndianRed
#property indicator_width6 2
#property indicator_label7 "Hull A (high)"
#property indicator_type7 DRAW_LINE
#property indicator_color7 clrIndianRed
#property indicator_label8 "Hull A (low)"
#property indicator_type8 DRAW_LINE
#property indicator_color8 clrSeaGreen
#property indicator_label9 "Optional EMA"
#property indicator_type9 DRAW_LINE
#property indicator_color9 clrDarkOrange
#property indicator_width9 2
#property indicator_label10 "Active BUY low"
#property indicator_type10 DRAW_LINE
#property indicator_color10 clrSeaGreen
#property indicator_label11 "Active BUY high"
#property indicator_type11 DRAW_LINE
#property indicator_color11 clrSeaGreen
#property indicator_label12 "Active SELL low"
#property indicator_type12 DRAW_LINE
#property indicator_color12 clrIndianRed
#property indicator_label13 "Active SELL high"
#property indicator_type13 DRAW_LINE
#property indicator_color13 clrIndianRed
#property indicator_label14 "BUY - selected view"
#property indicator_type14 DRAW_ARROW
#property indicator_color14 clrMediumSeaGreen
#property indicator_label15 "SELL - selected view"
#property indicator_type15 DRAW_ARROW
#property indicator_color15 clrTomato
#property indicator_label16 "Debug: conflicting sources"
#property indicator_type16 DRAW_ARROW
#property indicator_color16 clrDarkOrange
#property indicator_label17 "Raw cross (+1/-1/0)"
#property indicator_type17 DRAW_NONE
#property indicator_label18 "Structure pass at close"
#property indicator_type18 DRAW_NONE
#property indicator_label19 "Valid chart suggestion"
#property indicator_type19 DRAW_NONE
#property indicator_label20 "Simulated chart fill"
#property indicator_type20 DRAW_NONE
#property indicator_label21 "Source conflict (1/0)"
#property indicator_type21 DRAW_NONE

#property indicator_label22 "Protected HL/LH"
#property indicator_type22 DRAW_NONE
#property indicator_label23 "Confirmed structural HH/LL"
#property indicator_type23 DRAW_NONE
#property indicator_label24 "Candidate pullback wick (not protected)"
#property indicator_type24 DRAW_NONE
#property indicator_label25 "Structure bias (+1/-1/0)"
#property indicator_type25 DRAW_NONE
#property indicator_label26 "Break on close (signed 2 CHoCH / 3 BOS)"
#property indicator_type26 DRAW_NONE
#property indicator_label27 "Pullback seen (1/0)"
#property indicator_type27 DRAW_NONE

#property indicator_label28 "Reserved (inactive)"
#property indicator_type28 DRAW_NONE
#property indicator_label29 "Reserved (inactive)"
#property indicator_type29 DRAW_NONE
#property indicator_label30 "BOS range midpoint"
#property indicator_type30 DRAW_NONE
#property indicator_label31 "Reserved (inactive)"
#property indicator_type31 DRAW_NONE
#property indicator_label32 "Reserved (inactive)"
#property indicator_type32 DRAW_NONE
#property indicator_label33 "Reserved (inactive)"
#property indicator_type33 DRAW_NONE
#property indicator_label34 "Reserved (inactive)"
#property indicator_type34 DRAW_NONE
#property indicator_label35 "Reserved (inactive)"
#property indicator_type35 DRAW_NONE
#property indicator_label36 "Reserved (inactive)"
#property indicator_type36 DRAW_NONE
#property indicator_label37 "Reserved (inactive)"
#property indicator_type37 DRAW_NONE
#property indicator_label38 "Reserved (inactive)"
#property indicator_type38 DRAW_NONE
#property indicator_label39 "Simulated fill price"
#property indicator_type39 DRAW_NONE

enum FC_VIEW {
   FC_VALID=0,      // Valid suggestions only
   FC_STRUCTURE=1,  // Structure-qualified crosses
   FC_FILLS=2,      // Simulated fills only
   FC_RAW=3         // All Hull crosses (debug)
};
enum FC_SOURCE {
   FC_EITHER=0,       // Either source
   FC_DIRECTIONAL=1,  // Directional (Low BUY / High SELL)
   FC_LOW=2,          // Low only
   FC_HIGH=3          // High only
};
enum FC_STOP {
   FC_LOCAL=0,  // Local pullback (bot)
   FC_PIVOT=1   // Confirmed pivot (original chart)
};
input group "1 | Suggestions and alerts"
input FC_VIEW InView=FC_VALID;              // Labels and alerts: valid / structure / fills / raw
input bool InMarks=true;                    // Show selected chart marks
input bool InBands=false;                   // Draw allowed BUY and SELL bands
input bool InBuyAlerts=false;               // Popup for fresh closed-bar BUY in selected view
input bool InSellAlerts=false;              // Popup for fresh closed-bar SELL in selected view
input group "2 | Hull signal"
input int InHull=16;                        // Hull length
input FC_SOURCE InSource=FC_EITHER;         // Signal source
input bool InAlternate=false;               // Require selected directions to alternate
input bool InChannel=true;                  // Draw both Hull A baselines
input bool InChannelFill=true;              // Shade channel by last displayed signal
input group "3 | Confirmed structure"
input int InSwing=3;                        // Swing bars on EACH side
input group "4 | Entry filters"
input bool InLong=true;                     // Allow BUY suggestions and chart longs
input bool InShort=true;                    // Allow SELL suggestions and chart shorts
input bool InUseEMA=false;                  // Require EMA trend for entries
input int InEMALen=200;                     // EMA length
input bool InShowEMA=true;                  // Plot EMA when enabled
input group "5 | Simulated stops and exits"
input FC_STOP InStop=FC_LOCAL;              // Initial chart stop anchor
input int InPullback=7;                     // Prior bars for local stop (excluding signal bar)
input int InATRLen=14;                      // Wilder ATR length
input double InATRBuffer=0.1;               // Stop buffer x ATR
input double InPipSize=0.01;                // Pip size in price units
input double InExtraPips=0.0;               // Extra stop buffer in pips
input double InTargetR=1.5;                 // Chart TP R multiple (from the trend signal close)
input bool InOpposite=true;                 // Exit on executable opposite (replacement must qualify)
input group "6 | Chart account (NOT the MT5 account)"
input double InCapital=10000.0;             // Initial simulated capital
input double InPct=10.0;                    // Position value as % of simulated cash
input double InCommission=0.0;              // Commission per side (%)
input int InSlipTicks=0;                    // Adverse slippage (ticks)
input double InSlipTickSize=0.01;           // Tick size FOR SLIPPAGE (not stop rounding)
input double InPointValue=1.0;              // Value of one price unit per simulated unit
input group "7 | Chart plans and statistics"
input bool InPlans=false;                   // Draw simulated entry / SL / TP
input int InPlanBars=15;                    // Plan length in bars
input int InKeepPlans=40;                   // Max recent plans to draw
input bool InTable=true;                    // Show simulation panel
input ENUM_BASE_CORNER InCorner=CORNER_RIGHT_UPPER; // Panel corner
input int InTextCount=100;                  // Recent BUY/SELL captions (never used in reference view)

// BEGIN PORTABLE CALCULATION
// Chronological indices: oldest = 0. StepClosed is called ONCE per confirmed candle.
struct FCState {
   double oldB,oldH,oldL,ema,atr,trSum;
   int lastSig,lastBuy,lastSell,rawBuys,rawSells,conflicts,repeats,lastDisplay;
   double knownH,knownL,broken,origin,extreme,reaction,tip;
   double legOuter,retraceWick;
   int bias,kind,tipBar; bool seenPullback,breakNow;
   int pending,side; datetime due;
   double qSL,qTP,qLo,qHi; bool qOpenLo,qOpenHi,exitNext;
   double entry,stop,target,units,entryFee,cash,peak,maxDD,totalNet,gWin,gLoss;
   int wins,losses,flats,structN,suggestN,fillN,vetoN,decision;
};
struct FCBar {
   double b,hi,lo,ema,atr,buyLo,buyHi,sellLo,sellHi,rawSL,fillPx,fillSL,fillTP;
   bool strictBuyLo,strictBuyHi,strictSellLo,strictSellHi,insideBuy,insideSell;
   int raw,structure,valid,filled,display,tie,conflict,riskSide;
   double mid;
};
void ResetState(FCState &s) {
   ZeroMemory(s);
   s.oldB=EMPTY_VALUE; s.oldH=EMPTY_VALUE; s.oldL=EMPTY_VALUE;
   s.ema=EMPTY_VALUE; s.atr=EMPTY_VALUE; s.knownH=EMPTY_VALUE; s.knownL=EMPTY_VALUE;
   s.broken=EMPTY_VALUE; s.origin=EMPTY_VALUE; s.extreme=EMPTY_VALUE;
   s.reaction=EMPTY_VALUE; s.tip=EMPTY_VALUE; s.legOuter=EMPTY_VALUE; s.retraceWick=EMPTY_VALUE; s.tipBar=-1;
   s.lastBuy=-1; s.lastSell=-1; s.cash=InCapital; s.peak=InCapital;
}
void ResetBar(FCBar &r) {
   ZeroMemory(r); r.b=EMPTY_VALUE; r.hi=EMPTY_VALUE; r.lo=EMPTY_VALUE;
   r.ema=EMPTY_VALUE; r.atr=EMPTY_VALUE; r.buyLo=EMPTY_VALUE; r.buyHi=EMPTY_VALUE;
   r.sellLo=EMPTY_VALUE; r.sellHi=EMPTY_VALUE; r.rawSL=EMPTY_VALUE;
   r.mid=EMPTY_VALUE;
   r.fillPx=EMPTY_VALUE; r.fillSL=EMPTY_VALUE; r.fillTP=EMPTY_VALUE;
}
double WmaAt(const double &a[],const int i,const int n) {
   if(i+1<n) return EMPTY_VALUE;
   double total=0.0; int k=i-n+1;
   for(int j=1;j<=n;j++,k++) { if(a[k]==EMPTY_VALUE) return EMPTY_VALUE; total+=j*a[k]; }
   return total/(0.5*n*(n+1));
}
void Curves(const int i,const double &h[],const double &l[],const double &c[],
            double &wh[],double &wl[],double &wb[],FCBar &r) {
   int half=(int)MathMax(1,InHull/2),root=(int)MathMax(1.0,MathFloor(MathSqrt((double)InHull)+0.5));
   double sh=WmaAt(h,i,InHull),sl=WmaAt(l,i,InHull),sb=WmaAt(c,i,half);
   wh[i]=(sh==EMPTY_VALUE ? EMPTY_VALUE : 2.0*WmaAt(h,i,half)-sh);
   wl[i]=(sl==EMPTY_VALUE ? EMPTY_VALUE : 2.0*WmaAt(l,i,half)-sl);
   wb[i]=(sb==EMPTY_VALUE ? EMPTY_VALUE : 3.0*WmaAt(c,i,(int)MathMax(1,half/3))-WmaAt(c,i,(int)MathMax(1,half/2))-sb);
   r.hi=WmaAt(wh,i,root); r.lo=WmaAt(wl,i,root); r.b=WmaAt(wb,i,half);
}
int StrictCross(const double b,const double a,const double prevB,const double prevA) {
   if(b==EMPTY_VALUE || a==EMPTY_VALUE || prevB==EMPTY_VALUE || prevA==EMPTY_VALUE) return 0;
   // Pine compares floats at nine fractional digits; equality is NOT a cross.
   double x=MathRound(b*1e9),y=MathRound(a*1e9),px=MathRound(prevB*1e9),py=MathRound(prevA*1e9);
   return (x>y && px<py ? 1 : (x<y && px>py ? -1 : 0));
}
void SelectCross(FCState &s,FCBar &r,bool &repeat) {
   int lo=StrictCross(r.b,r.lo,s.oldB,s.oldL),hi=StrictCross(r.b,r.hi,s.oldB,s.oldH);
   bool buy=(InSource==FC_HIGH ? hi==1 : InSource==FC_EITHER ? (lo==1||hi==1) : lo==1);
   bool sell=(InSource==FC_LOW ? lo==-1 : InSource==FC_EITHER ? (lo==-1||hi==-1) : hi==-1);
   r.conflict=(buy&&sell?1:0); int candidate=(r.conflict!=0 ? 0 : buy ? 1 : sell ? -1 : 0);
   repeat=(InAlternate && candidate!=0 && candidate==s.lastSig); r.raw=(repeat?0:candidate);
   if(r.raw!=0){s.lastSig=r.raw; if(r.raw==1)s.rawBuys++;else s.rawSells++;}
   else if(r.conflict!=0)s.conflicts++;else if(repeat)s.repeats++;
   s.oldB=r.b; s.oldH=r.hi; s.oldL=r.lo;
}
bool Inside(const double px,const double lo,const double hi,const bool openLo,const bool openHi) {
   if(px==EMPTY_VALUE||lo==EMPTY_VALUE||hi==EMPTY_VALUE)return false;
   // Match Pine comparisons at nine fractional digits, including a decimal midpoint.
   double p=MathRound(px*1e9),a=MathRound(lo*1e9),b=MathRound(hi*1e9);
   return (openLo?p>a:p>=a) && (openHi?p<b:p<=b);
}
void Bands(const FCState &s,FCBar &r) {
   bool range=s.kind==3 && !s.breakNow && s.broken!=EMPTY_VALUE && s.origin!=EMPTY_VALUE && s.extreme!=EMPTY_VALUE && s.bias*(s.extreme-s.origin)>0 && s.bias*(s.extreme-s.broken)>0;
   r.mid=range?MathRound((s.origin+s.extreme)*0.5*1e9)/1e9:EMPTY_VALUE;
   r.buyLo=range&&s.bias==1?s.origin:EMPTY_VALUE; r.buyHi=range&&s.bias==1?r.mid:EMPTY_VALUE;
   r.sellLo=range&&s.bias==-1?r.mid:EMPTY_VALUE; r.sellHi=range&&s.bias==-1?s.origin:EMPTY_VALUE;
   r.strictBuyLo=true;r.strictBuyHi=false;r.strictSellLo=false;r.strictSellHi=true;
}
void UpdateStructure(FCState &s,const int i,const double &h[],const double &l[],const double &c[]) {
   int p=i-InSwing; bool ph=(i>=2*InSwing),pl=ph;
   if(ph) {
      for(int k=1;k<=InSwing;k++){
         if(h[p]<h[p-k] || h[p]<=h[p+k])ph=false;
         if(l[p]>l[p-k] || l[p]>=l[p+k])pl=false;
         if(!ph&&!pl)break;
      }
   }
   double hp=ph?h[p]:EMPTY_VALUE,lp=pl?l[p]:EMPTY_VALUE;
   int ns=0,nk=0; double nb=EMPTY_VALUE,no=EMPTY_VALUE;
   s.breakNow=false;
   if(s.bias==0){
      if(s.knownH!=EMPTY_VALUE && s.knownL!=EMPTY_VALUE && (c[i]>s.knownH || c[i]<s.knownL)){
         ns=c[i]>s.knownH?1:-1; nk=1; nb=ns==1?s.knownH:s.knownL; no=ns==1?s.knownL:s.knownH;
      }
   }else if(s.bias*(c[i]-s.origin)<0){
      ns=-s.bias; nk=2; nb=s.origin; no=s.legOuter;
   }else if(s.extreme!=EMPTY_VALUE && s.bias*(c[i]-s.extreme)>0){
      if(s.seenPullback && s.retraceWick!=EMPTY_VALUE){
         ns=s.bias; nk=3; nb=s.extreme;
         bool promote=s.bias*(s.retraceWick-s.origin)>0 && s.bias*(s.extreme-s.retraceWick)>0;
         no=promote?s.retraceWick:s.origin;
      }else{
         s.extreme=EMPTY_VALUE; s.tip=EMPTY_VALUE; s.tipBar=-1; s.retraceWick=EMPTY_VALUE; s.seenPullback=false;
      }
   }
   if(nk!=0){
      s.bias=ns; s.kind=nk; s.broken=nb; s.origin=no; s.extreme=EMPTY_VALUE;
      s.tip=EMPTY_VALUE; s.tipBar=-1; s.legOuter=EMPTY_VALUE; s.retraceWick=EMPTY_VALUE;
      s.seenPullback=false; s.breakNow=nk>=2;
   }
   if(s.bias!=0){
      double w=s.bias==1?h[i]:l[i],back=s.bias==1?l[i]:h[i];
      if(s.legOuter==EMPTY_VALUE || s.bias*(w-s.legOuter)>=0)s.legOuter=w;
      if(s.extreme==EMPTY_VALUE && (s.tip==EMPTY_VALUE || s.bias*(w-s.tip)>=0)){
         s.tip=w; s.tipBar=i; s.retraceWick=EMPTY_VALUE; s.seenPullback=false;
      }else{
         if(s.retraceWick==EMPTY_VALUE || s.bias*(back-s.retraceWick)<=0)s.retraceWick=back;
         if(!s.seenPullback && i>0 && s.bias*(c[i]-c[i-1])<0)s.seenPullback=true;
      }
      bool tipP=s.bias==1?ph:pl; double tipPx=s.bias==1?hp:lp;
      if(s.extreme==EMPTY_VALUE && tipP && p==s.tipBar && s.bias*(tipPx-s.broken)>0)s.extreme=tipPx;
   }
   s.reaction=s.seenPullback?s.retraceWick:EMPTY_VALUE;
   if(ph)s.knownH=hp; if(pl)s.knownL=lp;
}
void BookExit(FCState &s,const double raw,const bool slipIt,const double slip,const double fee) {
   double xp=raw-s.side*(slipIt?slip:0.0),move=s.side*(xp-s.entry)*s.units*InPointValue;
   double feeOut=MathAbs(xp*s.units*InPointValue)*fee,pnl=move-s.entryFee-feeOut;
   s.cash+=move-feeOut; s.totalNet+=pnl;
   if(pnl>1e-9){s.wins++;s.gWin+=pnl;}else if(pnl< -1e-9){s.losses++;s.gLoss-=pnl;}else s.flats++;
   s.side=0; s.entry=0; s.stop=0; s.target=0; s.units=0; s.entryFee=0;
}
void StepClosed(FCState &s,FCBar &r,const int i,const datetime at,const datetime expectedNext,
                const double o,const double &h[],const double &l[],const double &c[],const double priceTick) {
   double px=c[i]; bool repeat=false; SelectCross(s,r,repeat);
   s.ema=(s.ema==EMPTY_VALUE?px:s.ema+(2.0/(InEMALen+1.0))*(px-s.ema)); r.ema=s.ema;
   double tr=h[i]-l[i]; if(i>0)tr=MathMax(tr,MathMax(MathAbs(h[i]-c[i-1]),MathAbs(l[i]-c[i-1])));
   if(s.atr==EMPTY_VALUE){s.trSum+=tr;if(i+1==InATRLen)s.atr=s.trSum/InATRLen;}
   else s.atr=(s.atr*(InATRLen-1)+tr)/InATRLen;
   r.atr=s.atr; UpdateStructure(s,i,h,l,c); Bands(s,r);
   r.insideBuy=Inside(px,r.buyLo,r.buyHi,r.strictBuyLo,r.strictBuyHi); r.insideSell=Inside(px,r.sellLo,r.sellHi,r.strictSellLo,r.strictSellHi);
   int d=r.raw; r.structure=(d==1&&r.insideBuy?1:d==-1&&r.insideSell?-1:0);
   double anchor=EMPTY_VALUE;
   if(d!=0){
      if(InStop==FC_PIVOT)anchor=d==1?s.knownL:s.knownH;
      else{
         int since=d==1?s.lastSell:s.lastBuy;
         for(int k=1;k<=InPullback && i-k>=0 && i-k>=since;k++){
            double w=d==1?l[i-k]:h[i-k];anchor=anchor==EMPTY_VALUE?w:(d==1?MathMin(anchor,w):MathMax(anchor,w));
         }
      }
      if(d==1)s.lastBuy=i;else s.lastSell=i;
      if(anchor!=EMPTY_VALUE&&s.atr!=EMPTY_VALUE)r.rawSL=anchor-d*(InATRBuffer*s.atr+InExtraPips*InPipSize);
   }
   bool stopOK=(d!=0 && r.rawSL!=EMPTY_VALUE && r.rawSL>0 && d*(px-r.rawSL)>0);
   bool emaOK=!InUseEMA || d*(px-s.ema)>0; double planned=r.rawSL;
   if(stopOK && InStop==FC_LOCAL)planned=(d==1?MathFloor(planned/priceTick+1e-9):MathCeil(planned/priceTick-1e-9))*priceTick;
   r.riskSide=r.structure && (d==1?InLong:InShort) && stopOK && planned>0 && emaOK?r.structure:0;
   double slip=InSlipTicks*InSlipTickSize,fee=InCommission/100.0;
   // Ordered simulation: old gap/queued exit -> frozen plan at open -> protective barriers -> new close.
   if(s.side!=0){bool gs=s.side*(o-s.stop)<=0,gt=s.side*(o-s.target)>=0;
      if(gs||gt||s.exitNext)BookExit(s,o,gs||!gt,slip,fee);}
   if(s.pending!=0){
      int pd=s.pending; double ep=o+pd*slip;
      bool adjacent=s.due>0 && at==s.due,zone=Inside(ep,s.qLo,s.qHi,s.qOpenLo,s.qOpenHi);
      bool bracket=pd*(o-s.qSL)>0 && pd*(ep-s.qSL)>0 && pd*(s.qTP-ep)>0;
      double qty=ep>0?MathMax(0.0,s.cash*InPct/100.0)/(ep*InPointValue):0.0;
      if(s.side==0 && adjacent && zone && bracket && qty>0){
         s.side=pd;s.entry=ep;s.stop=s.qSL;s.target=s.qTP;s.units=qty;s.entryFee=MathAbs(ep*qty*InPointValue)*fee;
         s.cash-=s.entryFee;s.fillN++;r.filled=pd;r.fillPx=ep;r.fillSL=s.stop;r.fillTP=s.target;
      }else{s.vetoN++;s.decision=!adjacent?1:!zone?2:3;}
      s.pending=0;s.due=0;
   }
   if(s.side!=0){
      bool buy=s.side==1; bool hs=buy?l[i]<=s.stop:h[i]>=s.stop,ht=buy?h[i]>=s.target:l[i]<=s.target;
      if(hs||ht){bool tie=hs?(buy?px==l[i]:px==h[i]):(buy?px==h[i]:px==l[i]);
         if(tie)r.tie=s.side; BookExit(s,hs?s.stop:s.target,hs,slip,fee);}
   }
   s.exitNext=InOpposite && s.side!=0 && d==-s.side && stopOK && emaOK;
   bool slot=(s.side==0 || s.exitNext) && d!=r.tie;
   if(r.structure!=0)s.structN++;
   if(d!=0){
      if(r.structure==0)s.decision=s.kind==2?15:4;
      else if(!(d==1?InLong:InShort)||!emaOK)s.decision=5;
      else if(!stopOK || planned<=0)s.decision=6;
      else if(!slot || s.pending!=0)s.decision=7;
      else if(s.cash<=0)s.decision=8;
      else{
         r.valid=d;s.suggestN++;s.pending=d;s.qSL=planned;s.qTP=px+(px-planned)*InTargetR;
         s.qLo=d==1?r.buyLo:r.sellLo;s.qHi=d==1?r.buyHi:r.sellHi;
         s.qOpenLo=d==1?r.strictBuyLo:r.strictSellLo;s.qOpenHi=d==1?r.strictBuyHi:r.strictSellHi;s.due=expectedNext;
         s.decision=d==1?9:11;
      }
   }else if(r.conflict!=0)s.decision=13;else if(repeat)s.decision=14;
   double marked=s.cash+(s.side==0?0.0:(px-s.entry)*s.side*s.units*InPointValue);
   s.peak=MathMax(s.peak,marked);s.maxDD=MathMax(s.maxDD,s.peak-marked);
   r.display=InView==FC_STRUCTURE?r.structure:InView==FC_FILLS?r.filled:InView==FC_RAW?d:r.valid;
   if(r.display!=0)s.lastDisplay=r.display;
}
// END PORTABLE CALCULATION

// Native MT5 rendering & replay adapter. Exposed data buffers are documented in INSTALL-MT5.md.
double F0a[],F0b[],F1a[],F1b[],F2a[],F2b[],FBa[],FBb[],FSa[],FSb[];
double HullB[],HullInk[],LineH[],LineL[],LineEMA[],BandBL[],BandBH[],BandSL[],BandSH[];
double BuyArrow[],SellArrow[],ConflictMark[],RawCode[],StructureCode[],ValidCode[],FillCode[],ConflictCode[];
double GuardCode[],TipCode[],BackCode[],BiasCode[],EventCode[],PullCode[];
double Off33[],Off34[],MidCode[],Off36[],Off37[],Off38[],Off39[],Off40[],Off41[],Off42[],Off43[],FillPrice[];
double WorkH[],WorkL[],WorkB[];
FCState engine;
struct PlanStamp {int bar;double ep,sl,tp;};
struct CaptionStamp {datetime at;double px;int dir;};
PlanStamp planRing[]; CaptionStamp textRing[];
int planCount=0,planNext=0,textCount=0,textNext=0,committed=0;
datetime oldest=0,lastClosed=0,previousForm=0,lastAlert=0;
double gridTick=0.0; string prefix="";

bool Bind(const int index,double &buffer[],const ENUM_INDEXBUFFER_TYPE role=INDICATOR_DATA){
   if(!SetIndexBuffer(index,buffer,role))return false;return ArraySetAsSeries(buffer,false);
}
color Tint(const color ink,const color bg,const int share){
   uint a=(uint)ink,b=(uint)bg; uint result=0;
   for(int k=0;k<3;k++){
      int sh=8*k;uint v=(((a>>sh)&255)*share+((b>>sh)&255)*(100-share))/100;
      result|=(v<<sh);
   }return (color)result;
}
void FillColours(){
   color bg=(color)ChartGetInteger(0,CHART_COLOR_BACKGROUND),inks[5]={clrSlateGray,clrSeaGreen,clrIndianRed,clrSeaGreen,clrIndianRed};
   for(int j=0;j<5;j++){
      color ink=Tint(inks[j],bg,j<3?13:18);PlotIndexSetInteger(j,PLOT_COLOR_INDEXES,2);
      PlotIndexSetInteger(j,PLOT_LINE_COLOR,0,ink);PlotIndexSetInteger(j,PLOT_LINE_COLOR,1,ink);
   }
}
bool OptionsOK(){
   return InHull>=2&&InHull<=500 && InSwing>=1&&InSwing<=100 && InEMALen>=1&&InEMALen<=2000 &&
          InPullback>=1&&InPullback<=100 && InATRLen>=1&&InATRLen<=500 && InATRBuffer>=0&&InATRBuffer<=100 &&
          InPipSize>=1e-8&&InPipSize<=1000 && InExtraPips>=0&&InExtraPips<=100000 && InTargetR>=0.1&&InTargetR<=100 &&
          InCapital>=0.01&&InCapital<=1e9 && InPct>=0.01&&InPct<=100 && InCommission>=0&&InCommission<=10 &&
          InSlipTicks>=0&&InSlipTicks<=10000 && InSlipTickSize>=1e-8&&InSlipTickSize<=1000 &&
          InPointValue>=1e-6&&InPointValue<=1e6 && InPlanBars>=3&&InPlanBars<=200 && InKeepPlans>=1&&InKeepPlans<=100 && InTextCount>=0&&InTextCount<=500;
}
void Owner(){string n=prefix+"owner";ObjectCreate(0,n,OBJ_LABEL,0,0,0);ObjectSetString(0,n,OBJPROP_TEXT,"");ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);}
int OnInit(){
   if(!OptionsOK()){Print("FC Hull: input outside supported range; see INSTALL-MT5.md.");return INIT_PARAMETERS_INCORRECT;}
   if(!(Bind(0,F0a)&&Bind(1,F0b)&&Bind(2,F1a)&&Bind(3,F1b)&&Bind(4,F2a)&&Bind(5,F2b)&&Bind(6,FBa)&&Bind(7,FBb)&&Bind(8,FSa)&&Bind(9,FSb)&&
        Bind(10,HullB)&&Bind(11,HullInk,INDICATOR_COLOR_INDEX)&&Bind(12,LineH)&&Bind(13,LineL)&&Bind(14,LineEMA)&&
        Bind(15,BandBL)&&Bind(16,BandBH)&&Bind(17,BandSL)&&Bind(18,BandSH)&&Bind(19,BuyArrow)&&Bind(20,SellArrow)&&Bind(21,ConflictMark)&&
        Bind(22,RawCode)&&Bind(23,StructureCode)&&Bind(24,ValidCode)&&Bind(25,FillCode)&&Bind(26,ConflictCode)&&
        Bind(27,GuardCode)&&Bind(28,TipCode)&&Bind(29,BackCode)&&Bind(30,BiasCode)&&Bind(31,EventCode)&&Bind(32,PullCode)&&
        Bind(33,Off33)&&Bind(34,Off34)&&Bind(35,MidCode)&&Bind(36,Off36)&&Bind(37,Off37)&&Bind(38,Off38)&&Bind(39,Off39)&&Bind(40,Off40)&&Bind(41,Off41)&&Bind(42,Off42)&&Bind(43,Off43)&&Bind(44,FillPrice)&&
        Bind(45,WorkH,INDICATOR_CALCULATIONS)&&Bind(46,WorkL,INDICATOR_CALCULATIONS)&&Bind(47,WorkB,INDICATOR_CALCULATIONS)))return INIT_FAILED;
   for(int j=0;j<39;j++){PlotIndexSetDouble(j,PLOT_EMPTY_VALUE,j<5?0.0:EMPTY_VALUE);PlotIndexSetInteger(j,PLOT_SHOW_DATA,(j>=5 && j<=26)||j==29||j==38);}
   // Wingdings 251 = ×. Use the same two public marker buffers; change only their presentation.
   bool reference=(InView==FC_RAW);
   PlotIndexSetInteger(13,PLOT_ARROW,reference?251:233);PlotIndexSetInteger(13,PLOT_ARROW_SHIFT,reference?0:8);
   PlotIndexSetInteger(14,PLOT_ARROW,reference?251:234);PlotIndexSetInteger(14,PLOT_ARROW_SHIFT,reference?0:-8);
   if(reference){PlotIndexSetString(13,PLOT_LABEL,"Upward Hull cross (reference)");PlotIndexSetString(14,PLOT_LABEL,"Downward Hull cross (reference)");}
   PlotIndexSetInteger(15,PLOT_ARROW,251);PlotIndexSetInteger(15,PLOT_ARROW_SHIFT,-12);
   gridTick=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);if(gridTick<=0)gridTick=_Point;
   if(gridTick<=0 || PeriodSeconds(_Period)<=0)return INIT_FAILED;
   IndicatorSetInteger(INDICATOR_DIGITS,_Digits);IndicatorSetString(INDICATOR_SHORTNAME,"FC Hull BOS Half-Range v4 ("+IntegerToString(InHull)+")");
   ArrayResize(planRing,InKeepPlans);ArrayResize(textRing,InTextCount);
   string key="FCH_"+IntegerToString((long)GetTickCount64())+"_";int serial=0;
   do{prefix=key+IntegerToString(serial)+"_";serial++;}while(ObjectFind(0,prefix+"owner")>=0);
   Owner();FillColours();ResetState(engine);committed=0;oldest=0;lastClosed=0;previousForm=0;lastAlert=0;
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason){if(StringLen(prefix)>0)ObjectsDeleteAll(0,prefix);}
datetime BarEnd(const datetime t){
   if(_Period!=PERIOD_MN1)return (datetime)((long)t+PeriodSeconds(_Period));
   MqlDateTime d;if(!TimeToStruct(t,d))return 0;d.day=1;d.hour=0;d.min=0;d.sec=0;
   if(d.mon==12){d.year++;d.mon=1;}else d.mon++;return StructToTime(d);
}
void PaintBar(const int i,const double h,const double l,const FCBar &r,const bool closed=true){
   int last=engine.lastDisplay;bool ch=InChannel&&InChannelFill&&r.hi!=EMPTY_VALUE&&r.lo!=EMPTY_VALUE;
   F0a[i]=ch&&last==0?r.hi:0; F0b[i]=ch&&last==0?r.lo:0;
   F1a[i]=ch&&last==1?r.hi:0; F1b[i]=ch&&last==1?r.lo:0;
   F2a[i]=ch&&last==-1?r.hi:0;F2b[i]=ch&&last==-1?r.lo:0;
   bool bf=InBands && r.buyLo!=EMPTY_VALUE && r.buyHi!=EMPTY_VALUE;
   FBa[i]=bf?r.buyLo:0;FBb[i]=bf?r.buyHi:0;
   bool sf=InBands && r.sellLo!=EMPTY_VALUE && r.sellHi!=EMPTY_VALUE;
   FSa[i]=sf?r.sellLo:0;FSb[i]=sf?r.sellHi:0;
   HullB[i]=r.b;HullInk[i]=(last==1?1:last==-1?2:0);LineH[i]=InChannel?r.hi:EMPTY_VALUE;LineL[i]=InChannel?r.lo:EMPTY_VALUE;
   LineEMA[i]=InShowEMA&&InUseEMA?r.ema:EMPTY_VALUE;
   BandBL[i]=InBands?r.buyLo:EMPTY_VALUE;BandBH[i]=InBands?r.buyHi:EMPTY_VALUE;BandSL[i]=InBands?r.sellLo:EMPTY_VALUE;BandSH[i]=InBands?r.sellHi:EMPTY_VALUE;
   bool marks=InMarks&&closed;
   BuyArrow[i]=marks&&r.display==1?(InView==FC_RAW?r.b:l):EMPTY_VALUE;
   SellArrow[i]=marks&&r.display==-1?(InView==FC_RAW?r.b:h):EMPTY_VALUE;
   ConflictMark[i]=marks&&InView==FC_RAW&&r.conflict!=0?h:EMPTY_VALUE;
   RawCode[i]=r.raw;StructureCode[i]=r.structure;ValidCode[i]=r.valid;FillCode[i]=r.filled;ConflictCode[i]=r.conflict;
   // Public v3 indices stay inert. There is no watch, staging state, order or retest fill.
   Off33[i]=EMPTY_VALUE;Off34[i]=EMPTY_VALUE;MidCode[i]=r.mid;
   Off36[i]=0;Off37[i]=0;Off38[i]=0;Off39[i]=0;Off40[i]=0;Off41[i]=0;Off42[i]=0;Off43[i]=EMPTY_VALUE;FillPrice[i]=r.fillPx;
   GuardCode[i]=engine.origin;TipCode[i]=engine.extreme;BackCode[i]=engine.reaction;BiasCode[i]=engine.bias;
   EventCode[i]=closed&&engine.breakNow?engine.bias*engine.kind:0;PullCode[i]=engine.seenPullback?1:0;
}
void Remember(const int i,const datetime t,const double h,const double l,const FCBar &r){
   if(InPlans && r.filled!=0){planRing[planNext].bar=i;planRing[planNext].ep=r.fillPx;planRing[planNext].sl=r.fillSL;planRing[planNext].tp=r.fillTP;
      planNext=(planNext+1)%InKeepPlans;if(planCount<InKeepPlans)planCount++;}
   if(InMarks&&InView!=FC_RAW&&InTextCount>0&&r.display!=0){double pad=MathMax(5*_Point,(r.atr!=EMPTY_VALUE?r.atr:h-l)*0.23);
      textRing[textNext].at=t;textRing[textNext].dir=r.display;textRing[textNext].px=r.display==1?l-pad:h+pad;
      textNext=(textNext+1)%InTextCount;if(textCount<InTextCount)textCount++;}
}
void QuietObject(const string n){ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);}
void TextAt(const string n,const datetime t,const double px,const string text,const color ink,const ENUM_ANCHOR_POINT anchor){
   if(ObjectFind(0,n)<0){ObjectCreate(0,n,OBJ_TEXT,0,t,px);QuietObject(n);ObjectSetString(0,n,OBJPROP_FONT,"Segoe UI Semibold");ObjectSetInteger(0,n,OBJPROP_FONTSIZE,8);}
   else ObjectMove(0,n,0,t,px);
   ObjectSetInteger(0,n,OBJPROP_ANCHOR,anchor);ObjectSetInteger(0,n,OBJPROP_COLOR,ink);ObjectSetString(0,n,OBJPROP_TEXT,text);
}
void PlanLine(const string n,const string tag,const datetime a,const datetime b,const double px,const color ink,const ENUM_LINE_STYLE style){
   if(ObjectFind(0,n)<0){ObjectCreate(0,n,OBJ_TREND,0,a,px,b,px);QuietObject(n);ObjectSetInteger(0,n,OBJPROP_RAY_RIGHT,false);ObjectSetInteger(0,n,OBJPROP_RAY_LEFT,false);}
   else {ObjectMove(0,n,0,a,px);ObjectMove(0,n,1,b,px);}
   ObjectSetInteger(0,n,OBJPROP_COLOR,ink);ObjectSetInteger(0,n,OBJPROP_STYLE,style);
   TextAt(n+"v",b,px,tag+" "+DoubleToString(px,_Digits),ink,ANCHOR_LEFT);
}
void DrawRecent(const datetime &tm[],const int size){
   for(int j=0;j<textCount;j++)TextAt(prefix+"m"+IntegerToString(j),textRing[j].at,textRing[j].px,
      textRing[j].dir>0?"BUY":"SELL",textRing[j].dir>0?clrMediumSeaGreen:clrTomato,textRing[j].dir>0?ANCHOR_UPPER:ANCHOR_LOWER);
   for(int j=0;j<planCount;j++){
      int k=planRing[j].bar,idx=k+InPlanBars;datetime a=tm[k],b=tm[(int)MathMin(idx,size-1)];
      // Future projection only; replace projected endpoints with actual bar times as they arrive.
      for(int z=size;z<=idx;z++)b=BarEnd(b);
      string n=prefix+"p"+IntegerToString(j);
      PlanLine(n+"e","Sim entry",a,b,planRing[j].ep,clrSlateGray,STYLE_DOT);
      PlanLine(n+"s","SL",a,b,planRing[j].sl,clrTomato,STYLE_DASH);
      PlanLine(n+"t","TP",a,b,planRing[j].tp,clrMediumSeaGreen,STYLE_DASH);
   }
}
string DecText(const int n){
   switch(n){case 1:return "Veto: missing adjacent open";case 2:return "Veto: left frozen band";case 3:return "Veto: bracket/size/busy";
      case 4:return "Rejected: structure / price";case 5:return "Rejected: direction / EMA";case 6:return "Rejected: no legal stop";
      case 7:return "Rejected: position busy";case 8:return "Rejected: no chart capital";case 9:return "BUY: bullish discount";
      case 11:return "SELL: bearish premium";
      case 13:return "Rejected: source conflict";case 14:return "Rejected: alternation";case 15:return "CHoCH: context only";}return "Waiting for a cross";
}
string ViewText(){return InView==FC_RAW?"Raw crosses":InView==FC_FILLS?"Simulated fills":InView==FC_STRUCTURE?"Structure only":"Valid suggestions";}
string SourceText(){return InSource==FC_EITHER?"Either":InSource==FC_DIRECTIONAL?"Directional":InSource==FC_HIGH?"High":"Low";}
string PhaseText(){
   if(engine.kind<=1)return "Context only";
   string phase=engine.kind==2?"no trend band":engine.extreme==EMPTY_VALUE?"wait HH/LL":engine.bias==1?"discount active":"premium active";
   return (engine.kind==2?"CHoCH / ":"BOS / ")+phase;
}
void PanelLabel(const string n,const int x,const int y,const string text,const color ink,const int size){
   if(ObjectFind(0,n)<0){ObjectCreate(0,n,OBJ_LABEL,0,0,0);QuietObject(n);ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);ObjectSetInteger(0,n,OBJPROP_ANCHOR,ANCHOR_LEFT_UPPER);ObjectSetString(0,n,OBJPROP_FONT,"Segoe UI");}
   ObjectSetInteger(0,n,OBJPROP_XDISTANCE,x);ObjectSetInteger(0,n,OBJPROP_YDISTANCE,y);ObjectSetInteger(0,n,OBJPROP_FONTSIZE,size);ObjectSetInteger(0,n,OBJPROP_COLOR,ink);ObjectSetString(0,n,OBJPROP_TEXT,text);
}
void DrawPanel(){
   if(!InTable||StringLen(prefix)==0)return;
   string left[19]={"FC RANGE 4 / CHART SIM","Trades : W / L / BE","Win rate","Net / average","PF / max drawdown","Avg win / loss","Position","SL / TP","EMA / target R","Pip size","Source / length","Raw BUY / SELL","Conflict / repeat skips","Structure","Qualified / valid closes","Fills / open vetoes","Last decision","View","Protected HL / LH"};
   string v[19];int done=engine.wins+engine.losses+engine.flats;
   v[0]=_Symbol+"  "+EnumToString(_Period);v[1]=StringFormat("%d : %d / %d / %d",done,engine.wins,engine.losses,engine.flats);
   v[2]=done>0?DoubleToString(100.0*engine.wins/done,1)+"%":"-";
   v[3]=DoubleToString(engine.totalNet,2)+" / "+(done>0?DoubleToString(engine.totalNet/done,2):"-");
   v[4]=(engine.gLoss>0?DoubleToString(engine.gWin/engine.gLoss,2):"-")+" / "+DoubleToString(engine.maxDD,2);
   v[5]=(engine.wins>0?DoubleToString(engine.gWin/engine.wins,2):"-")+" / "+(engine.losses>0?DoubleToString(-engine.gLoss/engine.losses,2):"-");
   v[6]=engine.side==0?"Flat":engine.side==1?"BUY open":"SELL open";
   v[7]=engine.side==0?"-":DoubleToString(engine.stop,_Digits)+" / "+DoubleToString(engine.target,_Digits);
   v[8]=(InUseEMA?IntegerToString(InEMALen):"Off")+" / "+DoubleToString(InTargetR,2);
   v[9]=DoubleToString(InPipSize,8);v[10]=SourceText()+" / "+IntegerToString(InHull);
   v[11]=StringFormat("%d / %d",engine.rawBuys,engine.rawSells);v[12]=StringFormat("%d / %d",engine.conflicts,engine.repeats);
   v[13]=PhaseText();v[14]=StringFormat("%d / %d",engine.structN,engine.suggestN);v[15]=StringFormat("%d / %d",engine.fillN,engine.vetoN);v[16]=DecText(engine.decision);v[17]=ViewText();v[18]=engine.origin==EMPTY_VALUE?"-":DoubleToString(engine.origin,_Digits);
   int w=356,h=19*16+14,ww=(int)ChartGetInteger(0,CHART_WIDTH_IN_PIXELS,0),hh=(int)ChartGetInteger(0,CHART_HEIGHT_IN_PIXELS,0);
   int x=(InCorner==CORNER_RIGHT_LOWER||InCorner==CORNER_RIGHT_UPPER)?(int)MathMax(2,ww-w-8):8;
   int y=(InCorner==CORNER_RIGHT_LOWER||InCorner==CORNER_LEFT_LOWER)?(int)MathMax(2,hh-h-8):18;
   string box=prefix+"panel";if(ObjectFind(0,box)<0){ObjectCreate(0,box,OBJ_RECTANGLE_LABEL,0,0,0);QuietObject(box);ObjectSetInteger(0,box,OBJPROP_CORNER,CORNER_LEFT_UPPER);ObjectSetInteger(0,box,OBJPROP_BGCOLOR,C'22,40,43');ObjectSetInteger(0,box,OBJPROP_BORDER_TYPE,BORDER_FLAT);ObjectSetInteger(0,box,OBJPROP_COLOR,clrSlateGray);}
   ObjectSetInteger(0,box,OBJPROP_XDISTANCE,x);ObjectSetInteger(0,box,OBJPROP_YDISTANCE,y);ObjectSetInteger(0,box,OBJPROP_XSIZE,w);ObjectSetInteger(0,box,OBJPROP_YSIZE,h);
   for(int j=0;j<19;j++){int yy=y+5+j*16;string id=IntegerToString(j);PanelLabel(prefix+"k"+id,x+8,yy,left[j],j==0?clrPaleGreen:clrSilver,8);PanelLabel(prefix+"v"+id,x+152,yy,v[j],clrWhite,8);}
}
void OnChartEvent(const int id,const long &lparam,const double &dparam,const string &sparam){
   if(id==CHARTEVENT_CHART_CHANGE&&StringLen(prefix)>0){FillColours();DrawPanel();}
}
int OnCalculate(const int rates_total,const int prev_calculated,const datetime &time[],const double &open[],const double &high[],const double &low[],const double &close[],const long &tick_volume[],const long &volume[],const int &spread[]){
   if(rates_total<1)return 0;
   ArraySetAsSeries(time,false);ArraySetAsSeries(open,false);ArraySetAsSeries(high,false);ArraySetAsSeries(low,false);ArraySetAsSeries(close,false);
   int forming=rates_total-1;
   // A fixed-size history can drop its oldest bar on a genuine, single new candle.
   // Rebuild its indices, but allow that one fresh alert if we saw the forming bar.
   bool sliding=(prev_calculated>0 && forming>=2 && oldest>0 && time[0]>oldest && previousForm==time[forming-1] && lastClosed==time[forming-2]);
   bool reset=prev_calculated<=0||prev_calculated>rates_total||oldest!=time[0]||committed>forming;
   if(!reset&&committed>0 && time[committed-1]!=lastClosed)reset=true;
   if(reset){ResetState(engine);committed=0;oldest=time[0];lastClosed=0;planCount=0;planNext=0;textCount=0;textNext=0;
      ObjectsDeleteAll(0,prefix);Owner();}
   bool changed=reset||committed<forming;
   for(int i=committed;i<forming;i++){
      if(IsStopped())return 0;
      FCBar r;ResetBar(r);Curves(i,high,low,close,WorkH,WorkL,WorkB,r);
      datetime end=BarEnd(time[i]);StepClosed(engine,r,i,time[i],end,open[i],high,low,close,gridTick);
      PaintBar(i,high[i],low[i],r);Remember(i,time[i],high[i],low[i],r);
      // Never replay past alerts on attach, reload, backfill, or a missing adjacent bar.
      if((!reset||sliding) && i==forming-1 && time[i]==previousForm && end==time[forming] && time[i]>lastAlert &&
         ((r.display==1&&InBuyAlerts)||(r.display==-1&&InSellAlerts))){
         Alert(InView==FC_RAW?"FC Hull RAW (not an entry) ":"FC Hull ",(r.display==1?"BUY":"SELL")," | ",_Symbol," | ",EnumToString(_Period)," | ",ViewText()," | close ",DoubleToString(close[i],_Digits)," | ",TimeToString(end,TIME_DATE|TIME_SECONDS));lastAlert=time[i];}
      committed=i+1;lastClosed=time[i];
   }
   // Only preview moving lines on the live bar. No mutation of signal/structure/account state.
   FCBar live;ResetBar(live);Curves(forming,high,low,close,WorkH,WorkL,WorkB,live);
   live.ema=engine.ema==EMPTY_VALUE?close[forming]:engine.ema+2.0/(InEMALen+1.0)*(close[forming]-engine.ema);
   Bands(engine,live);PaintBar(forming,high[forming],low[forming],live,false);previousForm=time[forming];
   if(changed){DrawRecent(time,rates_total);DrawPanel();}
   return rates_total;
}
