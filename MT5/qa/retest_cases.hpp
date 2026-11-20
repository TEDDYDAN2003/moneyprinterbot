// Host-only deterministic acceptance tests, never included by the MT5 indicator.
// Supply controlled Hull A/B values to select known +1/-1/0 raw crosses while feeding
// real OHLC bars to the unmodified StepClosed. Repeat every path reflected about 110.
void RetestCases(){
  InSwing=1;InATRLen=2;InATRBuffer=0;InPullback=7;InUseEMA=false;InSource=FC_EITHER;InAlternate=false;
  InLong=true;InShort=true;InOpposite=true;InStop=FC_LOCAL;InExtraPips=0;InSlipTicks=0;InTargetR=1.5;
  vector<string> cases={"armed","direct","no_arm","wrong_zone","old_zone_equal","cancel_before","cancel_tie","cancel_after","touch_then_opposite","gap","obsolete_bos","band_mid","band_wrong_half","guard_equal","pass_open","gap_through_stop","limit_improvement","ambiguous_tp","stop_first","direction_off","retest_veto_stop","strict_edge","roll_no_touch","watch_exits_old","busy_no_exit","raw_conflict","post_conflict","gap_prior","gap_fresh","rearmed","exact_tip"};
  for(int mir:{1,-1})for(const string &name:cases){
    int trend=mir,rev=-mir;
    auto X=[&](double v){return mir==1?v:220.-v;};
    // A known swing high/low seeds context, then close 114 breaks HH 111. At bar 9
    // the outer HH is 119 and the protected HL is 105; the midpoint is exactly 112.
    vector<double> cp={100,96,102,99,105,110,106,114,118,115,108,105,104,102,101,100};
    vector<double> op=cp,hh=cp,ll=cp;for(size_t j=0;j<cp.size();j++){hh[j]++;ll[j]--;}
    ll[10]=104; // wick through HL, but close on the bullish side
    op[12]=106;hh[12]=107;ll[12]=103; // CHoCH close traded through its own future limit
    op[13]=103;hh[13]=104.8;ll[13]=101;op[14]=102;hh[14]=105;ll[14]=98;
    vector<int> raw(cp.size(),0);raw[9]=-1;
    if(name=="direct" || name=="no_arm" || name=="wrong_zone" || name=="band_mid" || name=="band_wrong_half" || name=="guard_equal" || name=="pass_open")raw[9]=0;
    if(name=="direct"||name=="gap_fresh")raw[12]=-1;
    if(name=="exact_tip"){raw[9]=0;raw[10]=-1;op[10]=117;cp[10]=hh[10]=119;ll[10]=115;}
    if(name=="rearmed"){raw[10]=1;raw[11]=-1;cp[11]=op[11]=115;hh[11]=116;ll[11]=104;}
    if(name=="wrong_zone")raw[10]=-1;
    if(name=="old_zone_equal"){cp[9]=op[9]=111;hh[9]=112;ll[9]=110;}
    if(name=="strict_edge"){cp[9]=op[9]=hh[9]=119;ll[9]=114; // equal high prevents confirmation of the earlier pivot; wait for the new one
      raw[9]=0;cp[10]=118;op[10]=117;hh[10]=118.5;ll[10]=115;raw[10]=-1;}
    if(name=="cancel_before")raw[10]=1;if(name=="cancel_tie")raw[12]=1;
    if(name=="cancel_after")raw[13]=1;if(name=="touch_then_opposite")raw[14]=1;
    if(name=="obsolete_bos"){op[10]=118;hh[10]=122;ll[10]=117;cp[10]=121;}
    if(name=="band_mid" || name=="pass_open"){cp[10]=op[10]=112;hh[10]=113;ll[10]=111;raw[10]=1;op[11]=name=="pass_open"?112:112.01;hh[11]=113;ll[11]=104;}
    if(name=="band_wrong_half"){raw[10]=1;cp[10]=op[10]=112.01;hh[10]=114;ll[10]=111;}
    if(name=="guard_equal")raw[11]=1;
    if(name=="gap_through_stop"){op[14]=130;hh[14]=131;}
    if(name=="limit_improvement"){op[14]=106;hh[14]=107;}
    if(name=="ambiguous_tp" || name=="stop_first"){ll[14]=60;if(name=="stop_first")hh[14]=140;}
    if(name=="roll_no_touch"){hh[14]=103;hh[15]=101;}
    vector<double> o,h,l,c;vector<FCBar> rr;vector<FCState> ss;FCState st;ResetState(st);
    for(int i=0;i<(int)cp.size();i++){
       if(name=="direction_off"){InLong=rev!=1;InShort=rev!=-1;}
       else {InLong=true;InShort=true;}
       o.push_back(X(op[i]));c.push_back(X(cp[i]));h.push_back(X(mir==1?hh[i]:ll[i]));l.push_back(X(mir==1?ll[i]:hh[i]));
       assert(l[i]<=min(o[i],c[i]) && h[i]>=max(o[i],c[i]));
       InOpposite=name!="busy_no_exit";
       if(i==9&&(name=="busy_no_exit"||name=="watch_exits_old")){
          st.side=trend;st.units=1;st.entry=X(112);st.stop=X(85);st.target=X(145);
       }
       FCBar r;ResetBar(r);int d=raw[i]*mir; r.hi=2;r.lo=1;st.oldH=2;st.oldL=1;st.oldB=d==1?0:3;r.b=d==-1?0:3;
       if((i==10&&name=="raw_conflict")||(i==13&&name=="post_conflict")){
          st.oldH=1;st.oldL=3;st.oldB=2;r.hi=3;r.lo=1;r.b=2;
       }
       datetime t=1800000000+i*60+((name=="gap"&&i>=13)||((name=="gap_prior"||name=="gap_fresh")&&i>=12)?120:0);
       // Invalid stop when staging a watched CHoCH: last pivot can be unavailable.
       if(name=="retest_veto_stop" && i==12){InStop=FC_PIVOT;st.knownH=EMPTY_VALUE;st.knownL=EMPTY_VALUE;}else InStop=FC_LOCAL;
       StepClosed(st,r,i,t,t+60,o[i],h,l,c,.001);rr.push_back(r);ss.push_back(st);
       assert(r.raw==d);
       if(i<9){assert(r.mid==EMPTY_VALUE && !r.trend);}
       if(r.trend){assert(r.trend==st.bias && st.kind==3);}
       if(r.placed){assert(st.breakNow && st.kind==2 && st.retOK && !r.retFill && !r.valid);}
    }
    auto need=[&](bool cond,const char* msg){if(!cond){cerr<<"REGRESSION "<<name<<" mir="<<mir<<" "<<msg<<"\n";abort();}};
    need(ss[7].kind==3 && ss[7].breakNow && ss[7].origin==X(105),"BOS-proven guard");
    if(name!="strict_edge")need(rr[9].mid==X(112) && ss[9].extreme==X(119),"known range");
    if(name=="band_mid" || name=="pass_open"){need(rr[10].trend==trend&&rr[10].valid==trend,"midpoint inclusive");need(rr[11].filled==(name=="pass_open"?trend:0),"frozen band at open");continue;}
    if(name=="band_wrong_half"){need(!rr[10].trend&&!rr[10].valid,"wrong half");continue;}
    if(name=="guard_equal"){need(!rr[11].trend&&!rr[11].valid&&!ss[11].breakNow,"guard equality");continue;}
    if(name=="no_arm"||name=="wrong_zone"||name=="old_zone_equal"||name=="direction_off"){
       need(!rr[9].armed&&!rr[10].armed&&!rr[12].confirmed&&!rr[14].retFill,"no unsolicited reversal");continue;}
    if(name!="direct"){int j=(name=="strict_edge"||name=="exact_tip")?10:9;need(rr[j].armed==rev&&ss[j].watchGuard==X(105)&&!rr[j].valid&&!rr[j].structure&&!ss[j].retOK,"watch NOT entry");}
    if(name=="watch_exits_old")need(ss[9].exitNext&&ss[9].side==trend&&!rr[9].valid&&!ss[10].side,"old raw opposite exits independently");
    if(name=="rearmed")need(rr[10].cancel==rev&&rr[11].armed==rev,"fresh zone cross arms again after cancellation");
    if(name=="gap_prior"||name=="raw_conflict"){int j=name=="gap_prior"?12:10;need(rr[j].cancel==rev&&!rr[12].placed&&!rr[14].retFill,"no stale watch after gap or conflicting crosses");continue;}
    if(name=="obsolete_bos"){need(ss[10].breakNow&&ss[10].kind==3&&rr[10].cancel==rev&&!ss[10].watch&&!rr[12].placed,"old range expired");continue;}
    if(name=="cancel_before"||name=="cancel_tie"){need(rr[name=="cancel_before"?10:12].cancel==rev&&!rr[12].placed&&!rr[14].retFill,"cancel before confirmation");continue;}
    need(!rr[10].confirmed&&!rr[11].confirmed&&!ss[11].breakNow,"sweep / equality cannot confirm");
    need(ss[12].breakNow&&ss[12].kind==2&&rr[12].confirmed==rev&&!rr[12].retFill&&!rr[12].valid,"confirm is NOT a fill");
    if(name=="busy_no_exit"){need(!ss[9].exitNext&&ss[12].side==trend&&!rr[12].placed&&rr[14].touch==rev&&!rr[14].retFill,"watched CHoCH never manufactures an exit / slot");continue;}
    if(name=="retest_veto_stop"){need(!rr[12].placed&&!rr[14].retFill,"invalid stop cannot become an executable limit");continue;}
    need(rr[12].placed==rev&&ss[12].retPx==X(105)&&!ss[12].watch,"freeze the broken guard");need(!rr[13].touch&&!rr[13].valid,"no retest no entry");
    if(name=="gap"||name=="cancel_after"||name=="post_conflict"){need(rr[13].cancel==rev&&!ss[13].retOK&&!rr[14].retFill,"cancel outstanding limit");continue;}
    if(name=="roll_no_touch"){need(ss[15].retOK&&!rr[14].filled&&!rr[15].filled,"never chase a breakout");continue;}
    if(name=="gap_through_stop"){need(rr[14].touch==rev&&!rr[14].retFill&&!rr[14].valid,"opening beyond stop vetoes fill");continue;}
    need(rr[14].touch==rev&&rr[14].retFill==rev&&rr[14].filled==rev&&rr[14].valid==rev&&!ss[14].retOK,"later touch fills once");
    need(rr[14].fillPx==X(name=="limit_improvement"?106:105)&&!rr[15].retFill,"limit or better, no duplicate");
    if(name=="ambiguous_tp")need(ss[14].side==rev && !ss[14].wins,"target wick might predate fill");
    if(name=="stop_first")need(!ss[14].side && ss[14].losses==1,"stop wins ambiguous double touch");
  }
  cerr<<"PASS "<<2*cases.size()<<" mirrored, hand-specified BOS / half-range / CHoCH / retest / cancellation paths\n";
}
