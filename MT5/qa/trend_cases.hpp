// Host-only mirrored fixtures. Real structure/stop/execution functions; controlled raw
// Hull direction to test trend-band boundaries not dependent on chance in a long price tape.
void TrendCases(){
  InSwing=1;InATRLen=2;InATRBuffer=0;InPullback=7;InUseEMA=false;InSource=FC_EITHER;InAlternate=false;
  InLong=true;InShort=true;InStop=FC_LOCAL;InExtraPips=0;InSlipTicks=0;InTargetR=1.5;
  vector<string> cases={"old_zone","fresh_choch","seen_and_fresh","close_equal","wick","no_cross_on_touch","fresh_on_touch","no_cross_in_range","band_mid","outside_half","guard_equal","open_mid","open_outside","open_guard","missing_open","one_tick_inside","opposite_exit_only","hold_if_exit_off"};
  for(int m:{1,-1})for(const string &k:cases){
    auto X=[&](double p){return m==1?p:220.-p;};
    vector<double> cp={100,96,102,99,105,110,106,114,118,115,108,105,104,102,101,100};
    vector<double> op=cp,hh=cp,ll=cp;for(size_t j=0;j<cp.size();j++){hh[j]++;ll[j]--;}
    ll[10]=104;op[12]=106;hh[12]=107;ll[12]=103;op[13]=103;hh[13]=104.8;ll[13]=101;op[14]=102;hh[14]=105;ll[14]=98;
    vector<int> raw(cp.size(),0);
    bool band=k=="band_mid"||k=="outside_half"||k=="guard_equal"||k=="open_mid"||k=="open_outside"||k=="open_guard"||k=="missing_open"||k=="one_tick_inside";
    if(band){raw[10]=1;cp[10]=op[10]=k=="outside_half"?112.001:k=="guard_equal"?105:k=="one_tick_inside"?111.999:k=="open_mid"?111:112;hh[10]=cp[10]+1;ll[10]=cp[10]-1;
      op[11]=k=="open_outside"?112.001:k=="open_guard"?105:k=="band_mid"?111.999:112;hh[11]=113;ll[11]=104;}
    if(k=="old_zone"||k=="seen_and_fresh"||k=="fresh_on_touch"||k=="opposite_exit_only"||k=="hold_if_exit_off")raw[9]=-1;
    if(k=="close_equal")raw[11]=-1; // close exactly at the guard
    if(k=="wick")raw[10]=-1; // wick through the guard, close back inside
    if(k=="no_cross_on_touch"){raw[12]=-1;hh[13]=105;hh[14]=105;} // two exact later touches, no new cross
    if(k=="fresh_choch"||k=="seen_and_fresh"||k=="opposite_exit_only"||k=="hold_if_exit_off")raw[12]=-1;
    if(k=="fresh_on_touch")raw[14]=-1;
    vector<double> h,l,c;vector<FCBar> rr;vector<FCState> ss;FCState st;ResetState(st);InOpposite=k!="hold_if_exit_off";
    for(int i=0;i<(int)cp.size();i++){
      h.push_back(X(m==1?hh[i]:ll[i]));l.push_back(X(m==1?ll[i]:hh[i]));c.push_back(X(cp[i]));
      if(i==12&&(k=="opposite_exit_only"||k=="hold_if_exit_off")){st.side=m;st.units=1;st.entry=X(112);st.stop=X(85);st.target=X(145);}
      FCBar r;ResetBar(r);int d=raw[i]*m;r.hi=2;r.lo=1;st.oldH=2;st.oldL=1;st.oldB=d==1?0:3;r.b=d==-1?0:3;
      datetime t=1800000000+i*60+(k=="missing_open"&&i>=11?120:0);StepClosed(st,r,i,t,t+60,X(op[i]),h,l,c,.001);rr.push_back(r);ss.push_back(st);
      assert(r.raw==d);if(i<9)assert(!r.structure&&r.mid==EMPTY_VALUE);
      if(!band)assert(!r.valid&&!r.filled&&!r.structure&&!st.pending);
    }
    assert(ss[7].kind==3&&ss[7].breakNow&&ss[7].origin==X(105));assert(rr[9].mid==X(112)&&ss[9].extreme==X(119));
    if(band){bool qual=k!="outside_half"&&k!="guard_equal";assert(rr[10].structure==(qual?m:0)&&rr[10].valid==(qual?m:0));
      bool fill=qual&&k!="missing_open"&&k!="open_guard"&&k!="open_outside";assert(rr[11].filled==(fill?m:0));continue;}
    assert(!ss[10].breakNow&&!ss[11].breakNow);assert(ss[12].breakNow&&ss[12].kind==2&&ss[12].broken==X(105));
    if(k=="wick")assert(rr[10].raw==-m&&ss[10].kind==3&&m*(c[10]-ss[10].origin)>0);
    if(k=="close_equal")assert(rr[11].raw==-m&&ss[11].kind==3&&c[11]==ss[11].origin);
    if(k=="fresh_on_touch"||k=="no_cross_on_touch")assert(X(hh[14])==ss[12].broken&&rr[14].raw==(k=="fresh_on_touch"?-m:0));
    for(int i=12;i<16;i++)assert(rr[i].mid==EMPTY_VALUE&&!ss[i].pending&&!rr[i].valid&&!rr[i].filled);
    if(k=="opposite_exit_only")assert(ss[12].exitNext&&ss[12].side==m&&!ss[13].side);
    if(k=="hold_if_exit_off")assert(!ss[12].exitNext&&ss[13].side==m);
  }
  cerr<<"PASS "<<2*cases.size()<<" mirrored BOS half-range / CHoCH context-only / later retest rejection / exit paths\n";
}
