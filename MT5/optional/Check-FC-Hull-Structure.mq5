// Optional read-only, native MT5 buffer smoke check. Run as a SCRIPT, not an EA.
#property version "2.00"
#property script_show_inputs
input int InCheckBars=1000; // Recent closed candles (100 to 10000)
void OnStart(){
   if(InCheckBars<100||InCheckBars>10000){Print("Choose 100..10000 bars.");return;}
   int h=iCustom(_Symbol,_Period,"FC-Hull-Either-16-Structure");
   if(h==INVALID_HANDLE){Print("Cannot load indicator; compile it in MQL5/Indicators first. Error ",GetLastError());return;}
   int ready=0;
   for(int k=0;k<60&&!IsStopped();k++){ready=BarsCalculated(h);if(ready>=100)break;Sleep(100);}
   int n=(int)MathMin(InCheckBars,ready-1);if(n<2){Print("Not enough calculated history. Load chart bars and rerun.");IndicatorRelease(h);return;}
   double raw[],zone[],valid[],fill[],conflict[],guard[],candidate[],bias[],event[],seen[],live[1];bool ok=true;int fills=0,suggestions=0,errors=0;
   if(CopyBuffer(h,22,1,n,raw)!=n||CopyBuffer(h,23,1,n,zone)!=n||CopyBuffer(h,24,1,n,valid)!=n||CopyBuffer(h,25,1,n,fill)!=n||CopyBuffer(h,26,1,n,conflict)!=n ||
      CopyBuffer(h,27,1,n,guard)!=n || CopyBuffer(h,29,1,n,candidate)!=n || CopyBuffer(h,30,1,n,bias)!=n ||
      CopyBuffer(h,31,1,n,event)!=n || CopyBuffer(h,32,1,n,seen)!=n)ok=false;
   // CopyBuffer writes oldest requested value first in normal (non-series) destination arrays.
   if(ok){for(int i=0;i<n;i++){
      if((raw[i]!=0&&raw[i]!=1&&raw[i]!=-1)||(zone[i]!=0&&zone[i]!=raw[i])||(valid[i]!=0&&valid[i]!=zone[i])||(conflict[i]!=0&&raw[i]!=0))errors++;
      if(bias[i]!=0 && ((bias[i]!=1 && bias[i]!=-1) || guard[i]==EMPTY_VALUE))errors++;
      if(event[i]!=0 && event[i]!=2*bias[i] && event[i]!=3*bias[i])errors++;
      if((seen[i]!=0 && seen[i]!=1) || (candidate[i]!=EMPTY_VALUE)!=(seen[i]==1))errors++;
      if(valid[i]!=0)suggestions++;
      if(fill[i]!=0){fills++;if(i>0&&fill[i]!=valid[i-1])errors++;}
   }
   if(CopyBuffer(h,31,0,1,live)!=1 || live[0]!=0)errors++;
   for(int k=22;k<=26;k++){if(CopyBuffer(h,k,0,1,live)!=1 || live[0]!=0)errors++;}}
   if(!ok)Print("Unable to copy all indicator buffers. Error ",GetLastError());
   else PrintFormat("Hull Protected v2 native smoke: %s | %s %s | %d closed bars | %d valid closes | %d simulated fills | %d consistency errors",errors==0?"PASS":"CHECK",_Symbol,EnumToString(_Period),n,suggestions,fills,errors);
   IndicatorRelease(h);
}
