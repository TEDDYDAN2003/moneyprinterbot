// Native API stubs for synthetic calculation/adapter tests; NOT an MT5 compiler or terminal.
#include <bits/stdc++.h>
using namespace std;using datetime=long long;using color=int;using ENUM_TIMEFRAMES=int;
struct MqlRates{datetime time=0; double open=0,high=0,low=0,close=0;};struct MqlTick{datetime time=0,time_msc=0;double bid=0,ask=0;};
#define ZeroMemory(x) x={}
#define MathMax max
#define MathMin min
#define MathAbs abs
#define MathIsValidNumber isfinite
const string _Symbol="FIX";const double EMPTY_VALUE=DBL_MAX;const int PERIOD_M15=15,PERIOD_M30=30,PERIOD_D1=1440,SYMBOL_CHART_MODE_BID=0,INIT_SUCCEEDED=0,INIT_FAILED=-1,INIT_PARAMETERS_INCORRECT=-2;
map<int,vector<MqlRates>> feed;set<string> objects;int alerts=0;MqlTick tick;
template<class T>int ArraySize(const T &v){return (int)v.size();}template<class T>int ArrayResize(T &v,int n,int spare=0){v.resize(n);return n;}
template<class T>void ArrayRemove(T &v,int i,int n){v.erase(v.begin()+i,v.begin()+i+n);}template<class T>void ArrayCopy(T &v,const T &s){v=s;}template<class T>void ArraySetAsSeries(const T&,bool flag){assert(!flag);}
int Index(int tf){int n=(int)feed[tf].size();while(n>0&&feed[tf][n-1].time>tick.time)n--;return n-1;}
int CopyRates(string,int tf,int shift,int count,vector<MqlRates>&rows){assert(tf==15||tf==30);int i=Index(tf)-shift;if(i<0)return -1;auto&v=feed[tf];rows.assign(v.begin()+max(0,i-count+1),v.begin()+i+1);return rows.size();}
int CopyRates(string,int tf,datetime start,datetime end,vector<MqlRates>&rows){assert(tf==1440);for(auto x:feed[tf])if(x.time>=start&&x.time<=end)rows.push_back(x);return rows.size();}
datetime iTime(string,int tf,int shift){int k=Index(tf)-shift;return k<0?0:feed[tf][k].time;}int PeriodSeconds(int tf){return tf*60;}
template<class...T>bool SetIndexBuffer(T&&...x){return true;}template<class...T>void IndicatorSetString(T...x){}template<class...T>void ObjectSetInteger(T...x){}template<class...T>void ObjectSetString(T...x){}
template<class...T>void Print(T...x){}template<class...T>void Alert(T...x){alerts++;}template<class...T>bool ObjectCreate(int,string n,T...x){objects.insert(n);return true;}int ObjectFind(int,string n){return objects.count(n)?0:-1;}void ObjectDelete(int,string n){objects.erase(n);}void ObjectsDeleteAll(int,string p){for(auto it=objects.begin();it!=objects.end();)if(it->find(p)==0)it=objects.erase(it);else ++it;}
int GetTickCount64(){return 17;}int SymbolInfoInteger(string,int){return 0;}bool SymbolInfoTick(string,MqlTick &x){x=tick;return true;}string IntegerToString(long long x){return to_string(x);}int StringLen(string x){return x.size();}string TimeToString(datetime t,int){return to_string(t);}
