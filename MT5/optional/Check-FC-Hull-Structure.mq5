// Optional read-only smoke check of a user-compiled v3 indicator using its default inputs.
// Install as a SCRIPT, not an EA. No broker/account/order calls.
#property version "3.00"
#property script_show_inputs
input int InCheckBars=1000; // Recent closed candles (100 to 10000)
bool SideCode(const double v){return v==0||v==1||v==-1;}
void OnStart(){
   if(InCheckBars<100||InCheckBars>10000){Print("Choose 100..10000 bars.");return;}
   int h=iCustom(_Symbol,_Period,"FC-Hull-Either-16-Structure");
   if(h==INVALID_HANDLE){Print("Cannot load indicator; compile it in MQL5/Indicators first. Error ",GetLastError());return;}
   int ready=0;
   for(int k=0;k<60&&!IsStopped();k++){ready=BarsCalculated(h);if(ready>=100)break;Sleep(100);}
   int n=(int)MathMin(InCheckBars,ready-1);if(n<2){Print("Not enough calculated history. Load chart bars and rerun.");IndicatorRelease(h);return;}
   double raw[],zone[],valid[],fill[],conflict[],guard[],bias[],event[],level[],mid[],watch[],arm[],confirm[],stage[],touch[],rf[],fp[],live[1];
   bool ok=CopyBuffer(h,22,1,n,raw)==n && CopyBuffer(h,23,1,n,zone)==n && CopyBuffer(h,24,1,n,valid)==n && CopyBuffer(h,25,1,n,fill)==n && CopyBuffer(h,26,1,n,conflict)==n &&
      CopyBuffer(h,27,1,n,guard)==n && CopyBuffer(h,30,1,n,bias)==n && CopyBuffer(h,31,1,n,event)==n && CopyBuffer(h,34,1,n,level)==n &&
      CopyBuffer(h,35,1,n,mid)==n && CopyBuffer(h,36,1,n,watch)==n && CopyBuffer(h,37,1,n,arm)==n && CopyBuffer(h,38,1,n,confirm)==n &&
      CopyBuffer(h,39,1,n,stage)==n && CopyBuffer(h,40,1,n,touch)==n && CopyBuffer(h,41,1,n,rf)==n && CopyBuffer(h,44,1,n,fp)==n;
   int errors=0,armed=0,limits=0,retests=0,fills=0;
   if(ok){
      // CopyBuffer starts with the oldest requested candle in non-series destination arrays.
      for(int i=0;i<n;i++){
         if(!SideCode(raw[i])||!SideCode(zone[i])||!SideCode(valid[i])||!SideCode(fill[i])||!SideCode(watch[i])||!SideCode(arm[i])||!SideCode(stage[i])||!SideCode(touch[i])||!SideCode(rf[i]))errors++;
         if(conflict[i]!=0 && raw[i]!=0)errors++;
         if(bias[i]!=0 && (!SideCode(bias[i])||guard[i]==EMPTY_VALUE))errors++;
         if(event[i]!=0 && event[i]!=2*bias[i] && event[i]!=3*bias[i])errors++;
         if(zone[i]!=0 && touch[i]==0 && (zone[i]!=raw[i]||raw[i]!=bias[i]||mid[i]==EMPTY_VALUE))errors++;
         if(arm[i]!=0){armed++;if(arm[i]!=watch[i]||arm[i]!=raw[i]||arm[i]!=-bias[i]||valid[i]==arm[i]||stage[i]==arm[i])errors++;}
         if(confirm[i]!=0 && (event[i]!=2*confirm[i]||watch[i]!=0))errors++;
         if(stage[i]!=0){limits++;if(stage[i]!=confirm[i]||level[i]==EMPTY_VALUE||valid[i]==stage[i]||rf[i]==stage[i])errors++;
            if(i>0 && guard[i-1]!=level[i])errors++;}
         if(rf[i]!=0){retests++;if(rf[i]!=valid[i]||rf[i]!=fill[i]||rf[i]!=touch[i])errors++;
            if(i>0 && (level[i-1]==EMPTY_VALUE||rf[i]*(fp[i]-level[i-1])>_Point*0.001))errors++;}
         if(valid[i]!=0 && rf[i]==0 && (valid[i]!=raw[i]||valid[i]!=bias[i]||mid[i]==EMPTY_VALUE))errors++;
         if(fill[i]!=0){fills++;if(fp[i]==EMPTY_VALUE)errors++;
            if(rf[i]==0 && i>0 && (valid[i-1]!=fill[i]||rf[i-1]!=0))errors++;}
         else if(fp[i]!=EMPTY_VALUE)errors++;
      }
      int events[12]={22,23,24,25,26,31,37,38,39,40,41,42};
      for(int k=0;k<ArraySize(events);k++)if(CopyBuffer(h,events[k],0,1,live)!=1||live[0]!=0)errors++;
   }
   if(!ok)Print("Cannot read v3 buffers. Check the indicator version and loaded history. Error ",GetLastError());
   else PrintFormat("Hull Range Retest v3 native smoke: %s | %s %s | %d closed bars | watches %d | limits staged %d | retest fills %d | all simulated fills %d | consistency errors %d",errors==0?"PASS":"CHECK",_Symbol,EnumToString(_Period),n,armed,limits,retests,fills,errors);
   IndicatorRelease(h);
}
