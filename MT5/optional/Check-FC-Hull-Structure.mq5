// Read-only optional SCRIPT for a locally compiled v4 chart indicator (not an EA).
#property version "4.00"
#property script_show_inputs
input int InCheckBars=1000; // Recent closed candles (100 to 10000)
bool SideCode(const double v){return v==0||v==1||v==-1;}
void OnStart(){
   if(InCheckBars<100||InCheckBars>10000){Print("Choose 100..10000 bars.");return;}
   int h=iCustom(_Symbol,_Period,"FC-Hull-Either-16-Structure");
   if(h==INVALID_HANDLE){Print("Compile v4 in MQL5/Indicators first. Error ",GetLastError());return;}
   int ready=0;
   for(int k=0;k<60&&!IsStopped();k++){ready=BarsCalculated(h);if(ready>=100)break;Sleep(100);}
   int n=(int)MathMin(InCheckBars,ready-1);if(n<2){Print("Insufficient history");IndicatorRelease(h);return;}
   double raw[],zone[],valid[],fill[],guard[],bias[],event[],mid[],price[],arr[],cl[],live[1];
   bool ok=CopyBuffer(h,22,1,n,raw)==n && CopyBuffer(h,23,1,n,zone)==n && CopyBuffer(h,24,1,n,valid)==n && CopyBuffer(h,25,1,n,fill)==n &&
           CopyBuffer(h,27,1,n,guard)==n && CopyBuffer(h,30,1,n,bias)==n && CopyBuffer(h,31,1,n,event)==n && CopyBuffer(h,35,1,n,mid)==n && CopyBuffer(h,44,1,n,price)==n && CopyClose(_Symbol,_Period,1,n,cl)==n;
   int errors=0,choch=0,signals=0,fills=0;
   if(ok){ // Normal destination arrays: oldest requested candle first.
      for(int i=0;i<n;i++){
         if(!SideCode(raw[i])||!SideCode(zone[i])||!SideCode(valid[i])||!SideCode(fill[i]))errors++;
         if(MathAbs(event[i])==2){choch++;if(zone[i]||valid[i]||mid[i]!=EMPTY_VALUE)errors++;}
         if(zone[i]){
            if(mid[i]==EMPTY_VALUE||guard[i]==EMPTY_VALUE||zone[i]!=bias[i]||zone[i]!=raw[i])errors++;
            else if(zone[i]*(cl[i]-guard[i])<=0||zone[i]*(cl[i]-mid[i])>_Point*0.0001)errors++;
         }
         if(valid[i]){signals++;if(valid[i]!=zone[i])errors++;}
         if(fill[i]){fills++;if(price[i]==EMPTY_VALUE||(i>0&&fill[i]!=valid[i-1]))errors++;}
         else if(price[i]!=EMPTY_VALUE)errors++;
      }
      for(int k=33;k<=43;k++){if(k==35)continue;double expected=(k==33||k==34||k==43)?EMPTY_VALUE:0.0;
         if(CopyBuffer(h,k,1,n,arr)!=n){ok=false;break;}for(int i=0;i<n;i++)if(arr[i]!=expected)errors++;}
      int codes[6]={22,23,24,25,26,31};for(int k=0;k<ArraySize(codes);k++)if(CopyBuffer(h,codes[k],0,1,live)!=1||live[0]!=0)errors++;
   }
   if(!ok)Print("Cannot read v4 buffers/chart history. Error ",GetLastError());
   else PrintFormat("Hull BOS half-range v4: %s | %s %s | %d closed candles | %d context-only CHoCH | %d valid closes | %d simulated fills | %d consistency errors",errors==0?"PASS":"CHECK",_Symbol,EnumToString(_Period),n,choch,signals,fills,errors);
   IndicatorRelease(h);
}
