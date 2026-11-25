//+------------------------------------------------------------------+
//|                                   FCDynamicHull_AuditedWR.mq5    |
//|         FC Dynamic Hull • Audited Session Win Rates • 15/30/45   |
//|                    Exact MQL5 Desktop Port of Pine Script v6     |
//+------------------------------------------------------------------+
#property copyright "FC Dynamic Hull • MQL5 Port"
#property version   "1.02"
#property description "Dynamic Hull + Market Structure + Audited 15m/30m/45m Session Win Rates"
#property indicator_chart_window
#property indicator_buffers 20
#property indicator_plots   13

//--- Plot 0: Fill Low (DRAW_FILLING: 2 data buffers)
#property indicator_label1  "FillLowB;FillLowA"
#property indicator_type1   DRAW_FILLING
#property indicator_color1  C'76,175,80', C'242,54,69'

//--- Plot 1: Fill High (DRAW_FILLING: 2 data buffers)
#property indicator_label2  "FillHighB;FillHighA"
#property indicator_type2   DRAW_FILLING
#property indicator_color2  C'76,175,80', C'242,54,69'

//--- Plot 2: Fill Fixed (DRAW_FILLING: 2 data buffers)
#property indicator_label3  "FillFixedB;FillFixedA"
#property indicator_type3   DRAW_FILLING
#property indicator_color3  C'76,175,80', C'242,54,69'

//--- Plot 3: Hull B (DRAW_COLOR_LINE: 1 data + 1 color index)
#property indicator_label4  "Hull B"
#property indicator_type4   DRAW_COLOR_LINE
#property indicator_color4  C'146,153,164', C'76,175,80', C'242,54,69'
#property indicator_width4  1

//--- Plot 4: Hull A Low (DRAW_COLOR_LINE: 1 data + 1 color index)
#property indicator_label5  "Hull A Low"
#property indicator_type5   DRAW_COLOR_LINE
#property indicator_color5  C'146,153,164', C'76,175,80', C'242,54,69'
#property indicator_width5  1

//--- Plot 5: Hull A High (DRAW_COLOR_LINE: 1 data + 1 color index)
#property indicator_label6  "Hull A High"
#property indicator_type6   DRAW_COLOR_LINE
#property indicator_color6  C'146,153,164', C'76,175,80', C'242,54,69'
#property indicator_width6  1

//--- Plot 6: Hull A Fixed (DRAW_COLOR_LINE: 1 data + 1 color index)
#property indicator_label7  "Hull A Fixed"
#property indicator_type7   DRAW_COLOR_LINE
#property indicator_color7  C'146,153,164', C'76,175,80', C'242,54,69'
#property indicator_width7  1

//--- Plot 7: BUY Signal (data-window only; label drawn as OBJ_TEXT)
#property indicator_label8  "BUY"
#property indicator_type8   DRAW_NONE

//--- Plot 8: SELL Signal (data-window only; label drawn as OBJ_TEXT)
#property indicator_label9  "SELL"
#property indicator_type9   DRAW_NONE

//--- Plot 9: Audit Entry Dot
#property indicator_label10 "Test entry at close"
#property indicator_type10  DRAW_ARROW
#property indicator_color10 C'255,183,77'
#property indicator_width10 2

//--- Plot 10: Audit Target Dot
#property indicator_label11 "Target for test entry"
#property indicator_type11  DRAW_ARROW
#property indicator_color11 C'38,198,218'
#property indicator_width11 2

//--- Plot 11: Internal Bias (Data Window only)
#property indicator_label12 "Internal bias (+1 / 0 / -1)"
#property indicator_type12  DRAW_NONE

//--- Plot 12: Source pick (Data Window only)
#property indicator_label13 "Source: 1 Low / -1 High / 2 fixed / 0 wait"
#property indicator_type13  DRAW_NONE

//+------------------------------------------------------------------+
//| Enums                                                            |
//+------------------------------------------------------------------+
enum ENUM_HULL_A_SRC
{
   HULL_SRC_DYNAMIC = 0, // Dynamic (Low in bull, High in bear)
   HULL_SRC_LOW     = 1, // Low
   HULL_SRC_HIGH    = 2, // High
   HULL_SRC_HL2     = 3, // HL2
   HULL_SRC_OPEN    = 4, // Open
   HULL_SRC_CLOSE   = 5, // Close
   HULL_SRC_HLC3    = 6, // HLC3
   HULL_SRC_OHLC4   = 7, // OHLC4
   HULL_SRC_CUSTOM  = 8  // Custom (OHLC applied price)
};

enum ENUM_NEUTRAL_SRC
{
   NEUTRAL_HIGH = 0, // High
   NEUTRAL_WAIT = 1  // Wait
};

enum ENUM_CHART_SIGNALS
{
   SIGNALS_VALID_ONLY = 0, // Valid suggestions only
   SIGNALS_ALL_CROSS  = 1  // All Hull crosses
};

enum ENUM_INSPECT_TF
{
   INSPECT_M1 = 1, // 1 minute
   INSPECT_M5 = 5  // 5 minutes
};

enum ENUM_SERVER_CLOCK
{
   SRV_AUTO_NY7   = 0, // Auto (Live offset / NY+7 US DST fallback)
   SRV_NY7_US_DST = 1, // GMT+2/+3 (US DST: 00:00 srv = 17:00 NY)
   SRV_EET_EU_DST = 2, // GMT+2/+3 (EU DST)
   SRV_FIXED_UTC  = 3  // Fixed UTC offset (use input hours)
};

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input string             InpGroupHull       = "=== Hull ===";
input int                InpLength          = 16;                 // Length (Hull A and B, 2..500)
input ENUM_HULL_A_SRC    InpHullASource     = HULL_SRC_DYNAMIC;   // Hull A source
input ENUM_NEUTRAL_SRC   InpNeutralSrc      = NEUTRAL_HIGH;       // Dynamic before first trend
input ENUM_APPLIED_PRICE InpCustomPrice     = PRICE_MEDIAN;       // Custom source (when Hull A = Custom)

input string             InpGroupStruct     = "=== Structure ===";
input ENUM_TIMEFRAMES    InpStructTF        = PERIOD_CURRENT;     // Structure timeframe (Current = each chart/test TF)
input int                InpSwingDepth      = 3;                  // Swing bars on each side (1..50)

input string             InpGroupDisplay    = "=== Display ===";
input bool               InpShowSwings      = true;               // Show swings
input bool               InpShowBreaks      = true;               // Show BOS / MSS
input int                InpStripWidth      = 10;                 // Trend strip thickness (px, 0 = off)
input bool               InpShowBgTint      = true;               // Show structure background tint
input ENUM_CHART_SIGNALS InpMarks           = SIGNALS_VALID_ONLY; // Chart signals
input bool               InpShowOpenTP      = true;               // Show open TP lines (15m / 30m / 45m)
input bool               InpAuditMarks      = false;              // Mark counted test entries and exits (M15/M30 charts)
input int                InpMaxDrawObjects  = 500;                // Max historical swing/break labels & lines

input string             InpGroupTable      = "=== Win-rate table (1 pos/TF) ===";
input bool               InpShowTable       = false;              // Show win-rate table panel (true = on, false = off)
input double             InpLots            = 0.10;               // Lot size
input double             InpCashTarget      = 10.0;               // Target profit per trade (USD)
input double             InpUsdTick         = 0.0;                // USD tick value at 1.00 broker lot (0 = auto)
input double             InpBrokerTickSize  = 0.0;                // Price size of that broker tick (0 = chart tick)
input ENUM_INSPECT_TF    InpInspectTF       = INSPECT_M1;         // Inspect exits on
input double             InpPointIn         = 0.0;                // Broker point size (0 = chart minimum tick)
input int                InpBePct           = 0;                  // Arm break-even after close at % of TP (0 = immediate)
input datetime           InpStartAt         = D'1970.01.01 00:00:00'; // Test from (UTC)
input datetime           InpEndAt           = D'2099.12.31 23:59:00'; // Test through (UTC)

input string             InpGroupSessions   = "=== Session statistics (entry time) ===";
input string             InpAsianHours      = "0000-0700";        // Asian (UTC)
input string             InpLondonHours     = "0800-1600";        // London (Europe/London)
input string             InpNyHours         = "0800-1700";        // New York (America/New_York)
input string             InpKzHours         = "1300-1600";        // NY-London killzone (Europe/London)

input string             InpGroupMT5        = "=== MT5 Server Clock & Alerts ===";
input ENUM_SERVER_CLOCK  InpServerClock     = SRV_AUTO_NY7;       // Broker server clock mapping to UTC
input int                InpFixedUtcOffsetH = 0;                  // Fixed UTC offset hours (when Fixed UTC selected)
input int                InpMaxStudyBars    = 25000;              // Max study bars per TF (0 = all available)
input bool               InpEnableAlerts    = false;              // Enable MT5 alerts on confirmed bar closes
input bool               InpPushAlerts      = false;              // Send MT5 mobile push notifications

//+------------------------------------------------------------------+
//| Constants & Colors                                               |
//+------------------------------------------------------------------+
const color CLR_BULL    = C'76,175,80';   // #4CAF50
const color CLR_BEAR    = C'242,54,69';   // #F23645
const color CLR_NEUTRAL = C'146,153,164'; // #9299A4
const color CLR_ORANGE  = C'255,183,77';  // #FFB74D
const color CLR_CYAN    = C'38,198,218';  // #26C6DA
const color CLR_PURPLE  = C'206,147,216'; // #CE93D8
const color CLR_TP15    = C'81,211,194';  // #51D3C2
const color CLR_TP30    = C'115,174,255'; // #73AEFF
const color CLR_TP45    = C'233,172,244'; // #E9ACF4

const int OUTCOME_TAKE        =  1;
const int OUTCOME_UNCLEAR     =  2;
const int OUTCOME_BE          = -1;
const int OUTCOME_OPP         = -2;
const int OUTCOME_LOSS_EITHER = -3;

const string OBJ_PREFIX = "FCH_";

//+------------------------------------------------------------------+
//| Data Structures                                                  |
//+------------------------------------------------------------------+
struct BarData
{
   datetime time;
   datetime timeClose;
   double   open;
   double   high;
   double   low;
   double   close;
};

struct StructureFrame
{
   datetime closedAt;
   int      bias;
   int      eventCode; // Signed: 1 = BOS, 2 = MSS, 3 = initial direction
   datetime levelAt;
   double   level;
   double   guard;
   datetime pivotAt;
   double   ph;
   int      hiClass;
   bool     replaceHi;
   double   pl;
   int      loClass;
   bool     replaceLo;
};

struct StructureState
{
   double   lastH;
   datetime lastHt;
   double   lastL;
   datetime lastLt;
   int      dir;
   double   hi;
   datetime ht;
   double   lo;
   datetime lt;
   double   pullL;
   datetime pullLt;
   double   pullH;
   datetime pullHt;
   double   guard;
   datetime gt;
   double   legTip;
   datetime tipAt;
   double   lastBreak;
   datetime legStart;
   datetime legOrigin;
   datetime breakWindow;
   bool     hasLegHi;
   bool     hasLegLo;
   bool     hasLegPullH;
   bool     hasLegPullL;
};

struct RowSnapshot
{
   datetime endTime;
   int      wins;
   int      losses;
   int      beLosses;
   int      side;
   double   entry;
   double   target;
   double   points;
   bool     validTF;
   bool     priced;
   int      asianW;
   int      asianN;
   int      londonW;
   int      londonN;
   int      nyW;
   int      nyN;
   int      kzW;
   int      kzN;
   int      unknown;
   int      asianU;
   int      londonU;
   int      nyU;
   int      kzU;
   int      mixedLoss;
   int      fineBars;
   int      coarseBars;
   bool     possibleBE;
   int      heldSignals;
   datetime firstTestClose;
   datetime lastTestClose;
   bool     entered;
   int      exitCode;
   int      missedSizing;
   datetime entryAt;
};

struct ScoreState
{
   int      d;
   int      wins;
   int      losses;
   int      be;
   int      mixed;
   int      unknown;
   int      asianW;
   int      asianN;
   int      asianU;
   int      londonW;
   int      londonN;
   int      londonU;
   int      nyW;
   int      nyN;
   int      nyU;
   int      kzW;
   int      kzN;
   int      kzU;
   bool     fromAsia;
   bool     fromLondon;
   bool     fromNY;
   bool     fromKZ;
   double   entry;
   datetime entryAt;
   double   target;
   double   points;
   long     targetTicks;
   bool     away;
   bool     armed;
   bool     possibleBE;
   int      fineBars;
   int      coarseBars;
   int      heldSignals;
   datetime firstTestClose;
   datetime lastTestClose;
   bool     sawPrice;
   int      missedSizing;
};

struct StudyResult
{
   RowSnapshot cur;
   RowSnapshot prev;
   bool        hasCur;
   bool        hasPrev;
};

//+------------------------------------------------------------------+
//| Indicator Buffers                                                |
//+------------------------------------------------------------------+
double BufFillLowB[], BufFillLowA[];
double BufFillHighB[], BufFillHighA[];
double BufFillFixedB[], BufFillFixedA[];
double BufHullB[], BufHullBClr[];
double BufHullALow[], BufHullALowClr[];
double BufHullAHigh[], BufHullAHighClr[];
double BufHullAFixed[], BufHullAFixedClr[];
double BufBuyArrow[], BufSellArrow[];
double BufAuditEntry[], BufAuditTarget[];
double BufBias[], BufPick[];

//+------------------------------------------------------------------+
//| Global State                                                     |
//+------------------------------------------------------------------+
double   g_mintick           = 0.00001;
int      g_length            = 16;
int      g_depth             = 3;
int      g_bePct             = 0;
int      g_liveServerOffset  = 0;
bool     g_liveOffsetDetected= false;
string   g_fxSymbol          = "";
bool     g_fxInverted        = false;
bool     g_fxResolved        = false;
datetime g_lastStudyRunBar15 = 0;
datetime g_lastStudyRunBar30 = 0;
datetime g_lastChartDrawBar  = 0;
datetime g_lastAlertBar      = 0;
bool     g_waitingHistory    = false;
string   g_errorBanner       = "";

// Strip cache for chart scroll/zoom redraw
int      g_chartBiasCache[];
datetime g_chartTimeCache[];
int      g_chartBarsCount = 0;

// First-signal-after-MSS tracking (for BUY 1 / SELL 1 label)
int      g_lastMssBias      = 0;  // bias of last confirmed MSS: +1 bull, -1 bear, 0 none
bool     g_firstSigShown    = true; // true = first signal already shown (or no MSS yet)

// Latest closed snapshots for Open TP & Panel
RowSnapshot g_r15, g_r30, g_r45;
bool        g_ok15 = false, g_ok30 = false, g_ok45 = false;

//+------------------------------------------------------------------+
//| Utility Helpers                                                  |
//+------------------------------------------------------------------+
bool IsNa(const double v)
{
   return (v == EMPTY_VALUE || !MathIsValidNumber(v));
}

long GridRound(const double px)
{
   return (long)MathRound(px / g_mintick);
}

double RoundToMintick(const double px)
{
   return MathRound(px / g_mintick) * g_mintick;
}

color BlendColor(const color fg, const color bg, const double alpha)
{
   int fgR = (int)(fg & 0xFF);
   int fgG = (int)((fg >> 8) & 0xFF);
   int fgB = (int)((fg >> 16) & 0xFF);

   int bgR = (int)(bg & 0xFF);
   int bgG = (int)((bg >> 8) & 0xFF);
   int bgB = (int)((bg >> 16) & 0xFF);

   int r = (int)MathRound(fgR * alpha + bgR * (1.0 - alpha));
   int g = (int)MathRound(fgG * alpha + bgG * (1.0 - alpha));
   int b = (int)MathRound(fgB * alpha + bgB * (1.0 - alpha));

   return (color)((b << 16) | (g << 8) | r);
}

void DeleteAllObjectsByPrefix()
{
   int total = ObjectsTotal(0, 0, -1);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, 0, -1);
      if(StringFind(name, OBJ_PREFIX) == 0)
         ObjectDelete(0, name);
   }
}

void DeleteObjectsWithSubPrefix(const string subPrefix)
{
   string full = OBJ_PREFIX + subPrefix;
   int total = ObjectsTotal(0, 0, -1);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, 0, -1);
      if(StringFind(name, full) == 0)
         ObjectDelete(0, name);
   }
}

//+------------------------------------------------------------------+
//| Timezone & DST Conversion (UTC, London, New York, Broker Server) |
//+------------------------------------------------------------------+
datetime GetNthWeekdayUtc(const int year, const int month, const int weekday, const int nth, const int hourUtc)
{
   MqlDateTime dt;
   dt.year = year;
   dt.mon  = month;
   dt.day  = 1;
   dt.hour = hourUtc;
   dt.min  = 0;
   dt.sec  = 0;
   datetime firstDay = StructToTime(dt);
   TimeToStruct(firstDay, dt);
   int delta = (weekday - dt.day_of_week + 7) % 7;
   return firstDay + (delta + (nth - 1) * 7) * 86400;
}

datetime GetLastWeekdayUtc(const int year, const int month, const int weekday, const int hourUtc)
{
   int daysInMon = 31;
   if(month == 4 || month == 6 || month == 9 || month == 11) daysInMon = 30;
   else if(month == 2)
      daysInMon = ((year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)) ? 29 : 28;

   MqlDateTime dt;
   dt.year = year;
   dt.mon  = month;
   dt.day  = daysInMon;
   dt.hour = hourUtc;
   dt.min  = 0;
   dt.sec  = 0;
   datetime lastDay = StructToTime(dt);
   TimeToStruct(lastDay, dt);
   int back = (dt.day_of_week - weekday + 7) % 7;
   return lastDay - back * 86400;
}

bool IsEuDstUtc(const datetime utc)
{
   MqlDateTime dt;
   TimeToStruct(utc, dt);
   datetime startDst = GetLastWeekdayUtc(dt.year, 3, 0, 1);  // Last Sunday March 01:00 UTC
   datetime endDst   = GetLastWeekdayUtc(dt.year, 10, 0, 1); // Last Sunday October 01:00 UTC
   return (utc >= startDst && utc < endDst);
}

bool IsUsDstUtc(const datetime utc)
{
   MqlDateTime dt;
   TimeToStruct(utc, dt);
   if(dt.year >= 2007)
   {
      datetime startDst = GetNthWeekdayUtc(dt.year, 3, 0, 2, 7);  // 2nd Sunday March 07:00 UTC
      datetime endDst   = GetNthWeekdayUtc(dt.year, 11, 0, 1, 6); // 1st Sunday Nov 06:00 UTC
      return (utc >= startDst && utc < endDst);
   }
   datetime startOld = GetNthWeekdayUtc(dt.year, 4, 0, 1, 7);
   datetime endOld   = GetLastWeekdayUtc(dt.year, 10, 0, 6);
   return (utc >= startOld && utc < endOld);
}

void DetectLiveServerOffset()
{
   if(MQLInfoInteger(MQL_TESTER))
   {
      g_liveOffsetDetected = false;
      return;
   }
   datetime srv = TimeTradeServer();
   datetime gmt = TimeGMT();
   if(srv > 0 && gmt > 0)
   {
      int diff = (int)(srv - gmt);
      g_liveServerOffset = (int)MathRound((double)diff / 900.0) * 900;
      g_liveOffsetDetected = true;
   }
}

datetime ServerToUtc(const datetime srvTime)
{
   if(srvTime <= 0) return 0;

   ENUM_SERVER_CLOCK mode = InpServerClock;
   int fixedOff = InpFixedUtcOffsetH * 3600;

   if(mode == SRV_AUTO_NY7)
   {
      if(g_liveOffsetDetected)
      {
         if(g_liveServerOffset == 7200 || g_liveServerOffset == 10800)
            mode = SRV_NY7_US_DST;
         else
         {
            mode = SRV_FIXED_UTC;
            fixedOff = g_liveServerOffset;
         }
      }
      else
      {
         mode = SRV_NY7_US_DST;
      }
   }

   if(mode == SRV_FIXED_UTC)
      return (srvTime - fixedOff);

   if(mode == SRV_NY7_US_DST)
   {
      datetime approx = srvTime - 7200;
      int off = IsUsDstUtc(approx) ? 10800 : 7200;
      datetime utc = srvTime - off;
      int off2 = IsUsDstUtc(utc) ? 10800 : 7200;
      return (srvTime - off2);
   }

   // SRV_EET_EU_DST
   datetime approx = srvTime - 7200;
   int off = IsEuDstUtc(approx) ? 10800 : 7200;
   datetime utc = srvTime - off;
   int off2 = IsEuDstUtc(utc) ? 10800 : 7200;
   return (srvTime - off2);
}

int MinutesOfDay(const datetime t)
{
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return dt.hour * 60 + dt.min;
}

int ClockMinutesLondon(const datetime utc)
{
   int off = IsEuDstUtc(utc) ? 3600 : 0;
   return MinutesOfDay(utc + off);
}

int ClockMinutesNY(const datetime utc)
{
   int off = IsUsDstUtc(utc) ? -14400 : -18000;
   return MinutesOfDay(utc + off);
}

bool ParseSessionBounds(const string hours, int &firstMin, int &lastMin)
{
   string clean = hours;
   StringReplace(clean, " ", "");
   if(StringLen(clean) < 9)
   {
      firstMin = 0;
      lastMin  = 0;
      return false;
   }
   int h1 = (int)StringToInteger(StringSubstr(clean, 0, 2));
   int m1 = (int)StringToInteger(StringSubstr(clean, 2, 2));
   int h2 = (int)StringToInteger(StringSubstr(clean, 5, 2));
   int m2 = (int)StringToInteger(StringSubstr(clean, 7, 2));
   firstMin = h1 * 60 + m1;
   lastMin  = h2 * 60 + m2;
   return true;
}

bool InsideSession(const int localMinute, const string hours)
{
   int first = 0, last = 0;
   ParseSessionBounds(hours, first, last);
   if(first == last) return true;
   if(first < last)  return (localMinute >= first && localMinute < last);
   return (localMinute >= first || localMinute < last);
}

void GetSessionTags(const datetime utcClose, bool &asia, bool &london, bool &ny, bool &killzone)
{
   int ukMin = ClockMinutesLondon(utcClose);
   asia     = InsideSession(MinutesOfDay(utcClose), InpAsianHours);
   london   = InsideSession(ukMin, InpLondonHours);
   ny       = InsideSession(ClockMinutesNY(utcClose), InpNyHours);
   killzone = (london && ny && InsideSession(ukMin, InpKzHours));
}

string FormatUtcDateTime(const datetime utcSec)
{
   MqlDateTime dt;
   TimeToStruct(utcSec, dt);
   return StringFormat("%04d-%02d-%02d %02d:%02d", dt.year, dt.mon, dt.day, dt.hour, dt.min);
}

//+------------------------------------------------------------------+
//| FX Conversion & Broker Tick Sizing (f_ticks parity)              |
//+------------------------------------------------------------------+
void ResolveFxConversionSymbol()
{
   g_fxResolved = true;
   g_fxSymbol   = "";
   g_fxInverted = false;

   string profitCurr = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_PROFIT);
   if(profitCurr == "USD" || profitCurr == "")
      return;

   string suffix = "";
   int symLen = StringLen(_Symbol);
   if(symLen > 6)
   {
      string base6 = StringSubstr(_Symbol, 0, 6);
      if(StringFind(base6, profitCurr) >= 0)
         suffix = StringSubstr(_Symbol, 6);
   }

   string candidates[4];
   candidates[0] = profitCurr + "USD" + suffix;
   candidates[1] = profitCurr + "USD";
   candidates[2] = "USD" + profitCurr + suffix;
   candidates[3] = "USD" + profitCurr;

   for(int i = 0; i < 4; i++)
   {
      if(SymbolSelect(candidates[i], true))
      {
         g_fxSymbol   = candidates[i];
         g_fxInverted = (i >= 2);
         return;
      }
   }

   int total = SymbolsTotal(false);
   string directTarget  = profitCurr + "USD";
   string inverseTarget = "USD" + profitCurr;
   for(int i = 0; i < total; i++)
   {
      string s = SymbolName(i, false);
      if(StringFind(s, directTarget) == 0 && SymbolSelect(s, true))
      {
         g_fxSymbol   = s;
         g_fxInverted = false;
         return;
      }
      if(StringFind(s, inverseTarget) == 0 && SymbolSelect(s, true))
      {
         g_fxSymbol   = s;
         g_fxInverted = true;
         return;
      }
   }
}

double GetFxRateAt(const datetime barTimeSrv)
{
   string profitCurr = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_PROFIT);
   if(profitCurr == "USD" || profitCurr == "") return 1.0;

   if(!g_fxResolved) ResolveFxConversionSymbol();
   if(g_fxSymbol != "")
   {
      int idx = iBarShift(g_fxSymbol, PERIOD_H1, barTimeSrv, false);
      if(idx >= 0)
      {
         double c = iClose(g_fxSymbol, PERIOD_H1, idx);
         if(c > 0.0)
            return g_fxInverted ? (1.0 / c) : c;
      }
   }
   return 0.0;
}

long CalcTicksForBar(const datetime barTimeSrv)
{
   double tickUSD = InpUsdTick;
   if(tickUSD <= 0.0)
   {
      string accCurr    = AccountInfoString(ACCOUNT_CURRENCY);
      string profitCurr = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_PROFIT);
      double brokerTV   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_PROFIT);
      if(brokerTV <= 0.0) brokerTV = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);

      if(profitCurr == "USD")
      {
         if(accCurr == "USD" && brokerTV > 0.0)
         {
            tickUSD = brokerTV;
         }
         else
         {
            double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
            if(contract <= 0.0) contract = 1.0;
            tickUSD = g_mintick * contract;
         }
      }
      else
      {
         double fx = GetFxRateAt(barTimeSrv);
         double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
         if(fx > 0.0 && contract > 0.0)
         {
            tickUSD = g_mintick * contract * fx;
         }
         else if(accCurr == "USD" && brokerTV > 0.0)
         {
            tickUSD = brokerTV;
         }
         else
         {
            tickUSD = 0.0;
         }
      }
   }

   if(tickUSD <= 0.0 || InpLots <= 0.0 || InpCashTarget <= 0.0 || g_mintick <= 0.0)
      return -1;

   double cashTickSize = (InpUsdTick > 0.0 && InpBrokerTickSize > 0.0) ? InpBrokerTickSize : g_mintick;
   double rawCashTicks = NormalizeDouble(InpCashTarget / (InpLots * tickUSD), 9);
   long   cashTicks    = (long)MathMax(1.0, MathCeil(rawCashTicks));
   double rawGridTicks = NormalizeDouble((double)cashTicks * cashTickSize / g_mintick, 8);
   return (long)MathMax(1.0, MathCeil(rawGridTicks));
}

//+------------------------------------------------------------------+
//| Market Structure Engine (f_structure exact replica)              |
//+------------------------------------------------------------------+
void StructureInit(StructureState &st)
{
   st.lastH       = EMPTY_VALUE;
   st.lastHt      = 0;
   st.lastL       = EMPTY_VALUE;
   st.lastLt      = 0;
   st.dir         = 0;
   st.hi          = EMPTY_VALUE;
   st.ht          = 0;
   st.lo          = EMPTY_VALUE;
   st.lt          = 0;
   st.pullL       = EMPTY_VALUE;
   st.pullLt      = 0;
   st.pullH       = EMPTY_VALUE;
   st.pullHt      = 0;
   st.guard       = EMPTY_VALUE;
   st.gt          = 0;
   st.legTip      = EMPTY_VALUE;
   st.tipAt       = 0;
   st.lastBreak   = EMPTY_VALUE;
   st.legStart    = 0;
   st.legOrigin   = 0;
   st.breakWindow = 0;
   st.hasLegHi    = false;
   st.hasLegLo    = false;
   st.hasLegPullH = false;
   st.hasLegPullL = false;
}

void StructureStep(const BarData &bars[], const int i, const int depth, StructureState &st, StructureFrame &out)
{
   double ph = EMPTY_VALUE;
   double pl = EMPTY_VALUE;
   datetime pt = (i >= depth) ? bars[i - depth].time : 0;

   // Pine ta.pivothigh(high, depth, depth) / ta.pivotlow(low, depth, depth)
   if(i >= 2 * depth)
   {
      int c = i - depth;
      double candH = bars[c].high;
      double candL = bars[c].low;
      bool isPH = true;
      bool isPL = true;
      for(int k = 1; k <= depth; k++)
      {
         if(candH < bars[c - k].high || candH <= bars[c + k].high)
            isPH = false;
         if(candL > bars[c - k].low || candL >= bars[c + k].low)
            isPL = false;
         if(!isPH && !isPL) break;
      }
      if(isPH) ph = candH;
      if(isPL) pl = candL;
   }

   int      ev     = 0;
   datetime origin = 0;
   double   level  = EMPTY_VALUE;
   double   cls    = bars[i].close;
   datetime tm     = bars[i].time;

   if(st.dir != 0 && !IsNa(st.guard) && st.dir * (cls - st.guard) < 0.0)
   {
      ev       = -2 * st.dir;
      origin   = st.gt;
      level    = st.guard;
      st.guard = st.legTip;
      st.gt    = st.tipAt;
   }
   else if(st.dir >= 0 && !IsNa(st.hi) && cls > st.hi && (st.dir == 1 || (!IsNa(st.lastL) && st.lastL < cls)))
   {
      ev     = (st.dir == 0 ? 3 : 1);
      origin = st.ht;
      level  = st.hi;
      if(st.dir == 0 || (!IsNa(st.pullL) && (IsNa(st.guard) || st.pullL > st.guard)))
      {
         st.guard = IsNa(st.pullL) ? st.lastL  : st.pullL;
         st.gt    = IsNa(st.pullL) ? st.lastLt : st.pullLt;
      }
   }
   else if(st.dir <= 0 && !IsNa(st.lo) && cls < st.lo && (st.dir == -1 || (!IsNa(st.lastH) && st.lastH > cls)))
   {
      ev     = (st.dir == 0 ? -3 : -1);
      origin = st.lt;
      level  = st.lo;
      if(st.dir == 0 || (!IsNa(st.pullH) && (IsNa(st.guard) || st.pullH < st.guard)))
      {
         st.guard = IsNa(st.pullH) ? st.lastH  : st.pullH;
         st.gt    = IsNa(st.pullH) ? st.lastHt : st.pullHt;
      }
   }

   if(ev != 0)
   {
      st.dir         = (ev > 0 ? 1 : -1);
      st.lastBreak   = level;
      st.legStart    = tm;
      st.legOrigin   = origin;
      st.breakWindow = (i >= depth) ? bars[i - depth].time : 0;
      st.hi          = EMPTY_VALUE;
      st.lo          = EMPTY_VALUE;
      st.pullL       = EMPTY_VALUE;
      st.pullH       = EMPTY_VALUE;
      st.hasLegHi    = false;
      st.hasLegLo    = false;
      st.hasLegPullH = false;
      st.hasLegPullL = false;
      st.legTip      = (st.dir == 1 ? bars[i].high : bars[i].low);
      st.tipAt       = tm;
   }
   else if(st.dir != 0)
   {
      double x = (st.dir == 1 ? bars[i].high : bars[i].low);
      if(IsNa(st.legTip) || st.dir * (x - st.legTip) >= 0.0)
      {
         st.legTip = x;
         st.tipAt  = tm;
      }
   }

   int  hc        = 0;
   int  lc        = 0;
   bool replaceHi = false;
   bool replaceLo = false;
   bool inLeg     = (st.legStart == 0 || pt >= st.legStart);

   if(!IsNa(ph))
   {
      st.lastH  = ph;
      st.lastHt = pt;
      if(st.dir > 0 && !IsNa(st.pullL) && pt > st.pullLt && (IsNa(st.guard) || st.pullL > st.guard))
      {
         st.guard = st.pullL;
         st.gt    = st.pullLt;
      }
      bool inBreakHi = (st.dir > 0 && IsNa(st.hi) && !IsNa(st.lastBreak) && ph > st.lastBreak &&
                        st.breakWindow > 0 && pt >= st.breakWindow && (st.legOrigin == 0 || pt > st.legOrigin));
      if((inLeg || inBreakHi) && st.dir >= 0 && (IsNa(st.lastBreak) || ph > st.lastBreak) && (IsNa(st.hi) || ph >= st.hi))
      {
         hc             = (st.dir == 0 ? 3 : 1);
         replaceHi      = st.hasLegHi;
         st.hasLegHi    = true;
         st.hi          = ph;
         st.ht          = pt;
         st.pullL       = EMPTY_VALUE;
         st.hasLegPullL = false;
      }
      if(inLeg && st.dir <= 0 && !IsNa(st.lo) && pt > st.lt && (IsNa(st.pullH) || ph >= st.pullH))
      {
         if(st.dir < 0)
         {
            hc             = -1;
            replaceHi      = st.hasLegPullH;
            st.hasLegPullH = true;
            st.hasLegLo    = false;
         }
         st.pullH  = ph;
         st.pullHt = pt;
      }
   }

   if(!IsNa(pl))
   {
      st.lastL  = pl;
      st.lastLt = pt;
      if(st.dir < 0 && !IsNa(st.pullH) && pt > st.pullHt && (IsNa(st.guard) || st.pullH < st.guard))
      {
         st.guard = st.pullH;
         st.gt    = st.pullHt;
      }
      bool inBreakLo = (st.dir < 0 && IsNa(st.lo) && !IsNa(st.lastBreak) && pl < st.lastBreak &&
                        st.breakWindow > 0 && pt >= st.breakWindow && (st.legOrigin == 0 || pt > st.legOrigin));
      if((inLeg || inBreakLo) && st.dir <= 0 && (IsNa(st.lastBreak) || pl < st.lastBreak) && (IsNa(st.lo) || pl <= st.lo))
      {
         lc             = (st.dir == 0 ? 3 : -1);
         replaceLo      = st.hasLegLo;
         st.hasLegLo    = true;
         st.lo          = pl;
         st.lt          = pt;
         st.pullH       = EMPTY_VALUE;
         st.hasLegPullH = false;
      }
      if(inLeg && st.dir >= 0 && !IsNa(st.hi) && pt > st.ht && (IsNa(st.pullL) || pl <= st.pullL))
      {
         if(st.dir > 0)
         {
            lc             = 1;
            replaceLo      = st.hasLegPullL;
            st.hasLegPullL = true;
            st.hasLegHi    = false;
         }
         st.pullL  = pl;
         st.pullLt = pt;
      }
   }

   out.closedAt  = bars[i].timeClose;
   out.bias      = st.dir;
   out.eventCode = ev;
   out.levelAt   = origin;
   out.level     = level;
   out.guard     = st.guard;
   out.pivotAt   = pt;
   out.ph        = ph;
   out.hiClass   = hc;
   out.replaceHi = replaceHi;
   out.pl        = pl;
   out.loClass   = lc;
   out.replaceLo = replaceLo;
}

void BuildStructureFrames(const BarData &bars[], const int depth, StructureFrame &frames[])
{
   int n = ArraySize(bars);
   ArrayResize(frames, n);
   StructureState st;
   StructureInit(st);
   for(int i = 0; i < n; i++)
      StructureStep(bars, i, depth, st, frames[i]);
}

//+------------------------------------------------------------------+
//| Hull Calculations (f_a, f_hull exact replica)                    |
//+------------------------------------------------------------------+
void ComputeWMA(const double &src[], const int n, const int period, double &dst[])
{
   ArrayResize(dst, n);
   double denom = (double)period * (period + 1) * 0.5;
   for(int i = 0; i < n; i++)
   {
      if(i < period - 1)
      {
         dst[i] = EMPTY_VALUE;
         continue;
      }
      double sum = 0.0;
      bool valid = true;
      for(int j = 0; j < period; j++)
      {
         double v = src[i - j];
         if(IsNa(v)) { valid = false; break; }
         sum += v * (period - j);
      }
      dst[i] = valid ? (sum / denom) : EMPTY_VALUE;
   }
}

void ComputeHullA(const double &src[], const int n, const int len, const int half, const int root, double &outA[])
{
   double wHalf[], wLen[], diff[];
   ComputeWMA(src, n, half, wHalf);
   ComputeWMA(src, n, len,  wLen);
   ArrayResize(diff, n);
   for(int i = 0; i < n; i++)
   {
      if(IsNa(wHalf[i]) || IsNa(wLen[i]))
         diff[i] = EMPTY_VALUE;
      else
         diff[i] = 2.0 * wHalf[i] - wLen[i];
   }
   ComputeWMA(diff, n, root, outA);
}

void ComputeHullB(const double &cls[], const int n, const int half, double &outB[])
{
   int half3 = (int)MathMax(1.0, MathFloor((double)half / 3.0));
   int half2 = (int)MathMax(1.0, MathFloor((double)half / 2.0));
   double w3[], w2[], w1[], diff[];
   ComputeWMA(cls, n, half3, w3);
   ComputeWMA(cls, n, half2, w2);
   ComputeWMA(cls, n, half,  w1);
   ArrayResize(diff, n);
   for(int i = 0; i < n; i++)
   {
      if(IsNa(w3[i]) || IsNa(w2[i]) || IsNa(w1[i]))
         diff[i] = EMPTY_VALUE;
      else
         diff[i] = 3.0 * w3[i] - w2[i] - w1[i];
   }
   ComputeWMA(diff, n, half, outB);
}

double GetFixedPrice(const BarData &bar)
{
   switch(InpHullASource)
   {
      case HULL_SRC_OPEN:  return bar.open;
      case HULL_SRC_CLOSE: return bar.close;
      case HULL_SRC_HLC3:  return (bar.high + bar.low + bar.close) / 3.0;
      case HULL_SRC_OHLC4: return (bar.open + bar.high + bar.low + bar.close) / 4.0;
      case HULL_SRC_CUSTOM:
         switch(InpCustomPrice)
         {
            case PRICE_OPEN:     return bar.open;
            case PRICE_HIGH:     return bar.high;
            case PRICE_LOW:      return bar.low;
            case PRICE_CLOSE:    return bar.close;
            case PRICE_MEDIAN:   return (bar.high + bar.low) * 0.5;
            case PRICE_TYPICAL:  return (bar.high + bar.low + bar.close) / 3.0;
            case PRICE_WEIGHTED: return (bar.high + bar.low + bar.close * 2.0) * 0.25;
            default:             return (bar.high + bar.low) * 0.5;
         }
      default: return (bar.high + bar.low) * 0.5;
   }
}

void ComputeHullAll(const BarData &bars[], const int n, double &lowA[], double &highA[], double &fixedA[], double &hullB[])
{
   int half = (int)MathMax(1.0, MathFloor((double)g_length / 2.0));
   int root = (int)MathMax(1.0, MathRound(MathSqrt((double)g_length)));

   double lo[], hi[], cls[], fx[];
   ArrayResize(lo,  n);
   ArrayResize(hi,  n);
   ArrayResize(cls, n);
   ArrayResize(fx,  n);
   for(int i = 0; i < n; i++)
   {
      lo[i]  = bars[i].low;
      hi[i]  = bars[i].high;
      cls[i] = bars[i].close;
      fx[i]  = GetFixedPrice(bars[i]);
   }

   ComputeHullA(lo,  n, g_length, half, root, lowA);
   ComputeHullA(hi,  n, g_length, half, root, highA);
   ComputeHullA(fx,  n, g_length, half, root, fixedA);
   ComputeHullB(cls, n, half, hullB);
}

void EvalHullBar(const int i, const int bias,
                 const double &lowA[], const double &highA[], const double &fixedA[], const double &hullB[],
                 int &rawOut, double &aOut, double &bOut, int &pickOut)
{
   bOut = hullB[i];
   if(InpHullASource == HULL_SRC_DYNAMIC)
      pickOut = (bias == 1) ? 1 : ((bias == -1 || InpNeutralSrc == NEUTRAL_HIGH) ? -1 : 0);
   else if(InpHullASource == HULL_SRC_LOW)
      pickOut = 1;
   else if(InpHullASource == HULL_SRC_HIGH)
      pickOut = -1;
   else
      pickOut = 2;

   aOut = (pickOut == 1) ? lowA[i] : (pickOut == -1) ? highA[i] : (pickOut == 2) ? fixedA[i] : EMPTY_VALUE;

   rawOut = 0;
   if(i > 0)
   {
      double prevA = (pickOut == 1) ? lowA[i - 1] : (pickOut == -1) ? highA[i - 1] : (pickOut == 2) ? fixedA[i - 1] : EMPTY_VALUE;
      double prevB = hullB[i - 1];
      if(!IsNa(bOut) && !IsNa(aOut) && !IsNa(prevB) && !IsNa(prevA))
      {
         if(bOut > aOut && prevB < prevA)      rawOut =  1;
         else if(bOut < aOut && prevB > prevA) rawOut = -1;
      }
   }
}

//+------------------------------------------------------------------+
//| Audited Exit Inspection (f_piece, f_covered, f_exit, f_score)    |
//+------------------------------------------------------------------+
void EvalPiece(const int d, const long entryGrid, const long goal, const bool armed,
               const bool away, const bool possible,
               const double o, const double h, const double l, const double c,
               int &verdict, bool &awayOut, bool &maybeOut)
{
   long op    = d * (GridRound(o) - entryGrid);
   long best  = d * (GridRound(d == 1 ? h : l) - entryGrid);
   long worst = d * (GridRound(d == 1 ? l : h) - entryGrid);
   long cp    = d * (GridRound(c) - entryGrid);

   verdict  = 0;
   maybeOut = possible;

   if(op >= goal)
   {
      verdict = possible ? OUTCOME_UNCLEAR : OUTCOME_TAKE;
   }
   else if(armed && (op < 0 || (op == 0 && away)))
   {
      verdict = OUTCOME_BE;
   }
   else if(best >= goal)
   {
      bool couldStopFirst = armed && (worst < 0 || (worst == 0 && (op > 0 || away || goal > 1)));
      verdict = (possible || couldStopFirst) ? OUTCOME_UNCLEAR : OUTCOME_TAKE;
   }
   else if(armed && (worst < 0 || (worst == 0 && (op > 0 || away || (best > 0 && cp == 0)))))
   {
      verdict = OUTCOME_BE;
   }
   else if(armed && !away && op == 0 && worst == 0 && best > 0)
   {
      maybeOut = true;
   }

   awayOut = (away || best != 0 || worst != 0);
}

bool IsCovered(const MqlRates &xs[], const int mStart, const int mEnd, const int inspectSec, const BarData &bar)
{
   int count = mEnd - mStart;
   if(count <= 0 || bar.timeClose <= bar.time || IsNa(bar.open) || IsNa(bar.high) || IsNa(bar.low) || IsNa(bar.close))
      return false;

   datetime cursor = bar.time;
   long hiGrid = GridRound(bar.low);
   long loGrid = GridRound(bar.high);

   for(int k = mStart; k < mEnd; k++)
   {
      datetime t0 = xs[k].time;
      datetime t1 = t0 + inspectSec;
      if(t0 != cursor || t1 <= t0 || t1 > bar.timeClose || (t1 - t0) > inspectSec)
         return false;
      double mo = xs[k].open, mh = xs[k].high, ml = xs[k].low, mc = xs[k].close;
      if(IsNa(mo) || IsNa(mh) || IsNa(ml) || IsNa(mc) || mh < MathMax(mo, mc) || ml > MathMin(mo, mc))
         return false;
      hiGrid = MathMax(hiGrid, GridRound(mh));
      loGrid = MathMin(loGrid, GridRound(ml));
      cursor = t1;
   }

   if(cursor != bar.timeClose) return false;
   if(GridRound(xs[mStart].open) != GridRound(bar.open)) return false;
   if(GridRound(xs[mEnd - 1].close) != GridRound(bar.close)) return false;
   if(hiGrid != GridRound(bar.high) || loGrid != GridRound(bar.low)) return false;
   return true;
}

void EvalExit(const int d, const double entry, const long goal, const bool armed,
              const bool away, const bool possible, const int rawSignal,
              const BarData &bar, const MqlRates &xs[], const int mStart, const int mEnd, const int inspectSec,
              int &verdictOut, bool &leftOut, bool &maybeOut, bool &fineOut)
{
   long entryGrid = GridRound(entry);
   verdictOut = 0;
   leftOut    = away;
   maybeOut   = possible;
   fineOut    = IsCovered(xs, mStart, mEnd, inspectSec, bar);

   if(fineOut)
   {
      for(int k = mStart; k < mEnd; k++)
      {
         int v = 0; bool a = false, b = false;
         EvalPiece(d, entryGrid, goal, armed, leftOut, maybeOut, xs[k].open, xs[k].high, xs[k].low, xs[k].close, v, a, b);
         verdictOut = v;
         leftOut    = a;
         maybeOut   = b;
         if(verdictOut != 0) break;
      }
   }
   else
   {
      int v = 0; bool a = false, b = false;
      EvalPiece(d, entryGrid, goal, armed, leftOut, maybeOut, bar.open, bar.high, bar.low, bar.close, v, a, b);
      verdictOut = v;
      leftOut    = a;
      maybeOut   = b;
   }

   if(verdictOut == 0 && rawSignal == -d)
      verdictOut = maybeOut ? OUTCOME_LOSS_EITHER : OUTCOME_OPP;
}

void ScoreInit(ScoreState &sc)
{
   sc.d              = 0;
   sc.wins           = 0;
   sc.losses         = 0;
   sc.be             = 0;
   sc.mixed          = 0;
   sc.unknown        = 0;
   sc.asianW         = 0; sc.asianN  = 0; sc.asianU  = 0;
   sc.londonW        = 0; sc.londonN = 0; sc.londonU = 0;
   sc.nyW            = 0; sc.nyN     = 0; sc.nyU     = 0;
   sc.kzW            = 0; sc.kzN     = 0; sc.kzU     = 0;
   sc.fromAsia       = false;
   sc.fromLondon     = false;
   sc.fromNY         = false;
   sc.fromKZ         = false;
   sc.entry          = EMPTY_VALUE;
   sc.entryAt        = 0;
   sc.target         = EMPTY_VALUE;
   sc.points         = EMPTY_VALUE;
   sc.targetTicks    = 0;
   sc.away           = false;
   sc.armed          = false;
   sc.possibleBE     = false;
   sc.fineBars       = 0;
   sc.coarseBars     = 0;
   sc.heldSignals    = 0;
   sc.firstTestClose = 0;
   sc.lastTestClose  = 0;
   sc.sawPrice       = false;
   sc.missedSizing   = 0;
}

void ScoreStep(const BarData &bar, const int signal, const int bias, const long ticks, const bool valid,
               const MqlRates &xs[], const int mStart, const int mEnd, const int inspectSec,
               ScoreState &sc, RowSnapshot &snap)
{
   datetime utcClose = ServerToUtc(bar.timeClose);
   bool inTest = (utcClose >= InpStartAt && utcClose <= InpEndAt && valid);
   double ptSize = (InpPointIn > 0.0) ? InpPointIn : g_mintick;
   double distancePts = (ticks > 0) ? ((double)ticks * g_mintick / ptSize) : EMPTY_VALUE;

   bool entered  = false;
   int  exitCode = 0;

   if(inTest)
   {
      sc.sawPrice = sc.sawPrice || (ticks > 0);
      if(sc.firstTestClose == 0) sc.firstTestClose = bar.timeClose;
      sc.lastTestClose = bar.timeClose;
   }

   if(inTest && sc.d != 0)
   {
      int outcome = 0; bool leftNow = false, maybeNow = false, fine = false;
      EvalExit(sc.d, sc.entry, sc.targetTicks, sc.armed, sc.away, sc.possibleBE, signal,
               bar, xs, mStart, mEnd, inspectSec, outcome, leftNow, maybeNow, fine);
      sc.fineBars   += fine ? 1 : 0;
      sc.coarseBars += fine ? 0 : 1;
      exitCode = outcome;

      if(outcome != 0)
      {
         int w = (outcome == OUTCOME_TAKE) ? 1 : 0;
         int u = (outcome == OUTCOME_UNCLEAR) ? 1 : 0;
         sc.wins    += w;
         sc.losses  += (outcome < 0) ? 1 : 0;
         sc.be      += (outcome == OUTCOME_BE) ? 1 : 0;
         sc.mixed   += (outcome == OUTCOME_LOSS_EITHER) ? 1 : 0;
         sc.unknown += u;

         if(sc.fromAsia)   { sc.asianW  += w; sc.asianU  += u; sc.asianN  += 1; }
         if(sc.fromLondon) { sc.londonW += w; sc.londonU += u; sc.londonN += 1; }
         if(sc.fromNY)     { sc.nyW     += w; sc.nyU     += u; sc.nyN     += 1; }
         if(sc.fromKZ)     { sc.kzW     += w; sc.kzU     += u; sc.kzN     += 1; }

         sc.d          = 0;
         sc.possibleBE = false;
      }
      else
      {
         long gain = sc.d * (GridRound(bar.close) - GridRound(sc.entry));
         long armGoal = (long)MathCeil((double)sc.targetTicks * (double)g_bePct / 100.0);
         sc.armed      = sc.armed || (gain >= armGoal);
         sc.away       = leftNow;
         sc.possibleBE = maybeNow;
         if(sc.possibleBE && signal == sc.d && bias == sc.d && ticks > 0)
            sc.heldSignals += 1;
      }
   }

   if(inTest && sc.d == 0 && signal != 0 && signal == bias && ticks <= 0)
      sc.missedSizing += 1;

   if(inTest && sc.d == 0 && signal != 0 && signal == bias && ticks > 0)
   {
      entered = true;
      bool tagA = false, tagL = false, tagN = false, tagK = false;
      GetSessionTags(utcClose, tagA, tagL, tagN, tagK);
      sc.fromAsia   = tagA;
      sc.fromLondon = tagL;
      sc.fromNY     = tagN;
      sc.fromKZ     = tagK;

      sc.d           = signal;
      sc.entry       = bar.close;
      sc.entryAt     = bar.timeClose;
      sc.targetTicks = ticks;
      sc.target      = RoundToMintick(sc.entry + (double)sc.d * (double)sc.targetTicks * g_mintick);
      sc.points      = distancePts;
      sc.away        = false;
      sc.possibleBE  = false;
      sc.armed       = (g_bePct == 0);
   }

   snap.endTime        = bar.timeClose;
   snap.wins           = sc.wins;
   snap.losses         = sc.losses;
   snap.beLosses       = sc.be;
   snap.side           = sc.d;
   snap.entry          = sc.entry;
   snap.target         = sc.target;
   snap.points         = IsNa(sc.points) ? distancePts : sc.points;
   snap.validTF        = valid;
   snap.priced         = sc.sawPrice;
   snap.asianW         = sc.asianW;
   snap.asianN         = sc.asianN;
   snap.londonW        = sc.londonW;
   snap.londonN        = sc.londonN;
   snap.nyW            = sc.nyW;
   snap.nyN            = sc.nyN;
   snap.kzW            = sc.kzW;
   snap.kzN            = sc.kzN;
   snap.unknown        = sc.unknown;
   snap.asianU         = sc.asianU;
   snap.londonU        = sc.londonU;
   snap.nyU            = sc.nyU;
   snap.kzU            = sc.kzU;
   snap.mixedLoss      = sc.mixed;
   snap.fineBars       = sc.fineBars;
   snap.coarseBars     = sc.coarseBars;
   snap.possibleBE     = sc.possibleBE;
   snap.heldSignals    = sc.heldSignals;
   snap.firstTestClose = sc.firstTestClose;
   snap.lastTestClose  = sc.lastTestClose;
   snap.entered        = entered;
   snap.exitCode       = exitCode;
   snap.missedSizing   = sc.missedSizing;
   snap.entryAt        = sc.entryAt;
}

//+------------------------------------------------------------------+
//| Bar Loading & M45 Synthesis                                      |
//+------------------------------------------------------------------+
bool LoadNativeBars(const ENUM_TIMEFRAMES tf, const int maxBars, BarData &outBars[])
{
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int avail = Bars(_Symbol, tf);
   if(avail <= 0) return false;
   int want = (maxBars > 0 && avail > maxBars) ? maxBars : avail;
   int copied = CopyRates(_Symbol, tf, 0, want, rates);
   if(copied <= 0) return false;
   ArraySetAsSeries(rates, false);

   int sec = PeriodSeconds(tf);
   ArrayResize(outBars, copied);
   for(int i = 0; i < copied; i++)
   {
      outBars[i].time      = rates[i].time;
      outBars[i].timeClose = rates[i].time + sec;
      outBars[i].open      = rates[i].open;
      outBars[i].high      = rates[i].high;
      outBars[i].low       = rates[i].low;
      outBars[i].close     = rates[i].close;
   }
   return true;
}

bool SynthesizeM45Bars(const int maxBars45, BarData &outBars[])
{
   MqlRates m15[];
   ArraySetAsSeries(m15, false);
   int avail = Bars(_Symbol, PERIOD_M15);
   if(avail <= 0) return false;
   int want15 = (maxBars45 > 0) ? MathMin(avail, maxBars45 * 3 + 6) : avail;
   int copied = CopyRates(_Symbol, PERIOD_M15, 0, want15, m15);
   if(copied <= 0) return false;
   ArraySetAsSeries(m15, false);

   ArrayResize(outBars, 0);
   int count = 0;
   datetime curBucketOpen = 0;

   for(int i = 0; i < copied; i++)
   {
      datetime t = m15[i].time;
      datetime dayStart = t - (t % 86400);
      int secInDay = (int)(t % 86400);
      datetime bucketOpen = dayStart + (secInDay / 2700) * 2700;

      if(count == 0 || bucketOpen != curBucketOpen)
      {
         ArrayResize(outBars, count + 1, 1024);
         outBars[count].time      = bucketOpen;
         outBars[count].timeClose = bucketOpen + 2700;
         outBars[count].open      = m15[i].open;
         outBars[count].high      = m15[i].high;
         outBars[count].low       = m15[i].low;
         outBars[count].close     = m15[i].close;
         curBucketOpen = bucketOpen;
         count++;
      }
      else
      {
         int idx = count - 1;
         if(m15[i].high > outBars[idx].high) outBars[idx].high = m15[i].high;
         if(m15[i].low  < outBars[idx].low)  outBars[idx].low  = m15[i].low;
         outBars[idx].close = m15[i].close;
      }
   }
   return (count > 0);
}

//+------------------------------------------------------------------+
//| Run Independent Timeframe Study (15m / 30m / 45m)                |
//+------------------------------------------------------------------+
bool RunStudyForTF(const int signalSec, const MqlRates &inspectRates[], const int inspectCount, const int inspectSec,
                   StudyResult &res, RowSnapshot &allSnaps[], const bool keepSnaps)
{
   res.hasCur  = false;
   res.hasPrev = false;

   BarData sigBars[];
   if(signalSec == 2700)
   {
      if(!SynthesizeM45Bars(InpMaxStudyBars, sigBars)) return false;
   }
   else
   {
      ENUM_TIMEFRAMES sigTF = (signalSec == 900) ? PERIOD_M15 : PERIOD_M30;
      if(!LoadNativeBars(sigTF, InpMaxStudyBars, sigBars)) return false;
   }

   int nSig = ArraySize(sigBars);
   if(nSig <= 0) return false;

   bool useNativeStruct = (InpStructTF == PERIOD_CURRENT || PeriodSeconds(InpStructTF) == signalSec);
   int  structSec       = useNativeStruct ? signalSec : PeriodSeconds(InpStructTF);
   bool higher          = (!useNativeStruct && structSec > signalSec);
   bool lower           = (!useNativeStruct && structSec < signalSec);
   bool nested          = (!lower || (signalSec % structSec == 0));

   StructureFrame structFrames[];
   if(useNativeStruct)
   {
      BuildStructureFrames(sigBars, g_depth, structFrames);
   }
   else if(nested)
   {
      BarData structBars[];
      int structMax = InpMaxStudyBars;
      if(structMax > 0 && lower)
         structMax = InpMaxStudyBars * MathMax(1, signalSec / structSec) + 200;
      if(!LoadNativeBars(InpStructTF, structMax, structBars))
         return false;
      BuildStructureFrames(structBars, g_depth, structFrames);
   }

   int nStruct = ArraySize(structFrames);

   double lowA[], highA[], fixedA[], hullB[];
   ComputeHullAll(sigBars, nSig, lowA, highA, fixedA, hullB);

   if(keepSnaps) ArrayResize(allSnaps, nSig);

   ScoreState sc;
   ScoreInit(sc);

   int bias = 0;
   int sPtr = 0;
   int mPtr = 0;

   for(int i = 0; i < nSig; i++)
   {
      if(nested)
      {
         if(useNativeStruct)
         {
            if(structFrames[i].closedAt <= sigBars[i].timeClose)
               bias = structFrames[i].bias;
         }
         else if(higher)
         {
            while(sPtr < nStruct && structFrames[sPtr].closedAt <= sigBars[i].time)
            {
               bias = structFrames[sPtr].bias;
               sPtr++;
            }
         }
         else if(lower)
         {
            while(sPtr < nStruct && structFrames[sPtr].closedAt <= sigBars[i].timeClose)
            {
               bias = structFrames[sPtr].bias;
               sPtr++;
            }
         }
      }

      int sigRaw = 0;
      double aVal = 0.0, bVal = 0.0;
      int pick = 0;
      EvalHullBar(i, bias, lowA, highA, fixedA, hullB, sigRaw, aVal, bVal, pick);

      long ticks = CalcTicksForBar(sigBars[i].time);

      while(mPtr < inspectCount && inspectRates[mPtr].time < sigBars[i].time)
         mPtr++;
      int mStart = mPtr;
      int mEnd   = mStart;
      while(mEnd < inspectCount && inspectRates[mEnd].time < sigBars[i].timeClose)
         mEnd++;
      mPtr = mEnd;

      RowSnapshot snap;
      ScoreStep(sigBars[i], sigRaw, bias, ticks, nested, inspectRates, mStart, mEnd, inspectSec, sc, snap);

      if(keepSnaps) allSnaps[i] = snap;

      if(i == nSig - 2)
      {
         res.prev    = snap;
         res.hasPrev = true;
      }
      if(i == nSig - 1)
      {
         res.cur    = snap;
         res.hasCur = true;
      }
   }
   return true;
}

bool PickClosedSnapshot(const StudyResult &res, const datetime cutoffSrv, RowSnapshot &out)
{
   if(res.hasCur && res.cur.endTime <= cutoffSrv)
   {
      out = res.cur;
      return true;
   }
   if(res.hasPrev && res.prev.endTime <= cutoffSrv)
   {
      out = res.prev;
      return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Win-Rate Table Helpers                                           |
//+------------------------------------------------------------------+
bool RowInWindow(const bool hasRow, const RowSnapshot &r)
{
   return (hasRow && r.validTF && r.firstTestClose > 0);
}

bool RowOk(const bool hasRow, const RowSnapshot &r)
{
   return (RowInWindow(hasRow, r) && r.priced && r.missedSizing == 0);
}

void GetRowCounts(const bool hasRow, const RowSnapshot &r, const int g, bool &ok, int &w, int &nClosed, int &u)
{
   ok = RowOk(hasRow, r);
   w = 0; nClosed = 0; u = 0;
   if(!ok) return;
   if(g == 0)      { w = r.wins;    nClosed = r.wins + r.losses + r.unknown; u = r.unknown; }
   else if(g == 1) { w = r.asianW;  nClosed = r.asianN;  u = r.asianU;  }
   else if(g == 2) { w = r.londonW; nClosed = r.londonN; u = r.londonU; }
   else if(g == 3) { w = r.nyW;     nClosed = r.nyN;     u = r.nyU;     }
   else            { w = r.kzW;     nClosed = r.kzN;     u = r.kzU;     }
}

string FormatWLU(const int w, const int l, const int u)
{
   return StringFormat("%d / %d / %d", w, l, u);
}

string FormatCoverage(const int fine, const int coarse)
{
   if(fine + coarse == 0) return "-";
   return StringFormat("%.1f%%", 100.0 * (double)fine / (double)(fine + coarse));
}

double GetOpenTP(const bool hasRow, const RowSnapshot &r, const datetime cutoffUtc)
{
   if(hasRow && r.validTF && r.side != 0 && r.entryAt > 0 && cutoffUtc <= InpEndAt)
      return r.target;
   return EMPTY_VALUE;
}

void DrawCell(const int col, const int row, const int &colW[], const int &colStartX[], const int totalW,
              const int rightMargin, const int topMargin, const int rowH,
              const string text, const string tip)
{
   color bg = (row == 0) ? C'38,53,71' : (row == 1) ? C'32,59,65' : (col == 4) ? C'38,51,65' : ((row % 2 == 0) ? C'23,33,44' : C'30,42,55');
   color fg = (row == 0 || row == 1) ? clrWhite : (col == 4) ? C'166,222,221' : (col == 0) ? clrSilver : C'232,237,243';

   int rightDist = rightMargin + totalW - colStartX[col];
   int topY      = topMargin + row * rowH;

   string rName = StringFormat("%sTC_%d_%d", OBJ_PREFIX, col, row);
   if(ObjectFind(0, rName) < 0)
      ObjectCreate(0, rName, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, rName, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, rName, OBJPROP_XDISTANCE, rightDist);
   ObjectSetInteger(0, rName, OBJPROP_YDISTANCE, topY);
   ObjectSetInteger(0, rName, OBJPROP_XSIZE, colW[col] - 1);
   ObjectSetInteger(0, rName, OBJPROP_YSIZE, rowH - 1);
   ObjectSetInteger(0, rName, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, rName, OBJPROP_COLOR, bg);
   ObjectSetInteger(0, rName, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, rName, OBJPROP_BACK, false);
   ObjectSetInteger(0, rName, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, rName, OBJPROP_HIDDEN, true);
   ObjectSetString(0,  rName, OBJPROP_TOOLTIP, tip);

   string lName = StringFormat("%sTL_%d_%d", OBJ_PREFIX, col, row);
   if(ObjectFind(0, lName) < 0)
      ObjectCreate(0, lName, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, lName, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, lName, OBJPROP_ANCHOR, (col == 0) ? ANCHOR_LEFT : ANCHOR_CENTER);
   ObjectSetInteger(0, lName, OBJPROP_XDISTANCE, (col == 0) ? (rightDist - 6) : (rightDist - colW[col] / 2));
   ObjectSetInteger(0, lName, OBJPROP_YDISTANCE, topY + rowH / 2);
   ObjectSetString(0,  lName, OBJPROP_FONT, "Segoe UI");
   ObjectSetInteger(0, lName, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, lName, OBJPROP_COLOR, fg);
   ObjectSetString(0,  lName, OBJPROP_TEXT, text);
   ObjectSetInteger(0, lName, OBJPROP_BACK, false);
   ObjectSetInteger(0, lName, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, lName, OBJPROP_HIDDEN, true);
   ObjectSetString(0,  lName, OBJPROP_TOOLTIP, tip);
}

void DrawRateCell(const int col, const int row, const int &colW[], const int &colStartX[], const int totalW,
                  const int rightMargin, const int topMargin, const int rowH,
                  const bool valid, const int w, const int total, const int u, const string note)
{
   string value = "n/a";
   string detail = (col == 4) ? "Needs usable snapshots for all three timeframes."
                              : "No in-window closes, incompatible TF, or missing sizing.";
   if(valid)
   {
      value  = "- (0)";
      detail = "No finished trades.";
      if(total > 0)
      {
         string pct = StringFormat("%.1f", 100.0 * (double)w / (double)total);
         if(u > 0)
         {
            double lowerPct = MathFloor(1000.0 * (double)w / (double)total) / 10.0;
            double upperPct = MathCeil(1000.0 * (double)(w + u) / (double)total) / 10.0;
            pct = StringFormat("%.1f-%.1f", lowerPct, upperPct);
         }
         value  = pct + "% (" + IntegerToString(total) + ")";
         detail = StringFormat("TP: %d | Loss: %d | ?: %d", w, total - w - u, u);
      }
   }
   string tip = note + "\n" + detail;
   if(StringLen(tip) > 155) tip = StringSubstr(tip, 0, 155);
   DrawCell(col, row, colW, colStartX, totalW, rightMargin, topMargin, rowH, value, tip);
}

void DrawFooterCol(const int col, const int &colW[], const int &colStartX[], const int totalW,
                   const int rightMargin, const int topMargin, const int rowH,
                   const bool hasRow, const RowSnapshot &r, const datetime cutoffUtc)
{
   bool ok    = RowOk(hasRow, r);
   bool ready = RowInWindow(hasRow, r);
   string unavail = !ready ? "No in-window closes or non-nested structure TF."
                           : StringFormat("Missing TP sizing. Unpriced: %d.", r.missedSizing);

   string exitsTip = ok ? StringFormat("BE: %d | Opp: %d | BE/Opp: %d | ?: %d",
                                       r.beLosses, r.losses - r.beLosses - r.mixedLoss, r.mixedLoss, r.unknown)
                        : unavail;

   string coverTip = ok ? StringFormat("%d fine (%dm), %d coarse.\n%s -> %s UTC",
                                       r.fineBars, (int)InpInspectTF, r.coarseBars,
                                       FormatUtcDateTime(ServerToUtc(r.firstTestClose)),
                                       FormatUtcDateTime(ServerToUtc(r.lastTestClose)))
                        : unavail;

   string sideTxt = !ok ? "n/a" : (r.side == 0 ? "flat" : ((r.side == 1 ? "BUY" : "SELL") + (r.possibleBE ? "?" : "")));

   double delta = EMPTY_VALUE;
   if(ok)
   {
      if(!IsNa(r.entry)) delta = MathAbs(r.target - r.entry);
      else               delta = r.points * ((InpPointIn > 0.0) ? InpPointIn : g_mintick);
   }
   string distTxt = !ready ? "n/a"
                           : (!ok || IsNa(delta)) ? "Set tick $"
                                                  : (DoubleToString(delta, _Digits) + " / " + DoubleToString(r.points, 2));

   string pricesTip = !ok ? unavail
                          : (!IsNa(r.entry) ? StringFormat("Fill: %s | TP: %s ($%.2f @ %.2f lot)",
                                                           DoubleToString(r.entry, _Digits),
                                                           DoubleToString(r.target, _Digits),
                                                           InpCashTarget, InpLots)
                                            : "No entry in range.");

   string posTip = ok ? StringFormat("Held under ? uncertainty: %d", r.heldSignals) : unavail;

   DrawCell(col, 6, colW, colStartX, totalW, rightMargin, topMargin, rowH, ok ? FormatWLU(r.wins, r.losses, r.unknown) : "n/a", exitsTip);
   DrawCell(col, 7, colW, colStartX, totalW, rightMargin, topMargin, rowH, ok ? FormatCoverage(r.fineBars, r.coarseBars) : "n/a", coverTip);
   DrawCell(col, 8, colW, colStartX, totalW, rightMargin, topMargin, rowH, distTxt, pricesTip);
   DrawCell(col, 9, colW, colStartX, totalW, rightMargin, topMargin, rowH, sideTxt, posTip);

   double openTP = GetOpenTP(hasRow, r, cutoffUtc);
   DrawCell(col, 10, colW, colStartX, totalW, rightMargin, topMargin, rowH,
            IsNa(openTP) ? "-" : DoubleToString(openTP, _Digits),
            "Frozen target for held test slot. " + pricesTip);
}

void DrawOpenTPGuide(const string tfTag, const color tint, const bool hasRow, const RowSnapshot &r,
                     const datetime cutoffSrv, const datetime cutoffUtc)
{
   string lineName = OBJ_PREFIX + "TP_L_" + tfTag;
   string lblName  = OBJ_PREFIX + "TP_B_" + tfTag;
   double tp       = GetOpenTP(hasRow, r, cutoffUtc);

   if(InpShowOpenTP && !IsNa(tp))
   {
      datetime anchor = MathMax(r.entryAt, cutoffSrv);
      string title = "  " + tfTag + (r.side == 1 ? " BUY" : " SELL") + (r.possibleBE ? "?" : "") + " TP " + DoubleToString(tp, _Digits);

      if(ObjectFind(0, lineName) < 0)
         ObjectCreate(0, lineName, OBJ_TREND, 0, r.entryAt, tp, anchor + 60, tp);
      else
      {
         ObjectMove(0, lineName, 0, r.entryAt, tp);
         ObjectMove(0, lineName, 1, anchor + 60, tp);
      }
      ObjectSetInteger(0, lineName, OBJPROP_COLOR, tint);
      ObjectSetInteger(0, lineName, OBJPROP_STYLE, STYLE_DASH);
      ObjectSetInteger(0, lineName, OBJPROP_WIDTH, 2);
      ObjectSetInteger(0, lineName, OBJPROP_RAY_RIGHT, true);
      ObjectSetInteger(0, lineName, OBJPROP_BACK, false);
      ObjectSetInteger(0, lineName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, lineName, OBJPROP_HIDDEN, true);

      if(ObjectFind(0, lblName) < 0)
         ObjectCreate(0, lblName, OBJ_TEXT, 0, anchor, tp);
      else
         ObjectMove(0, lblName, 0, anchor, tp);
      ObjectSetString(0,  lblName, OBJPROP_TEXT, title);
      ObjectSetString(0,  lblName, OBJPROP_FONT, "Segoe UI Semibold");
      ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, lblName, OBJPROP_COLOR, tint);
      ObjectSetInteger(0, lblName, OBJPROP_ANCHOR, ANCHOR_LEFT);
      ObjectSetInteger(0, lblName, OBJPROP_BACK, false);
      ObjectSetInteger(0, lblName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, lblName, OBJPROP_HIDDEN, true);
   }
   else
   {
      if(ObjectFind(0, lineName) >= 0) ObjectDelete(0, lineName);
      if(ObjectFind(0, lblName)  >= 0) ObjectDelete(0, lblName);
   }
}

void RenderWinRatePanel(const datetime cutoffSrv)
{
   datetime cutoffUtc = ServerToUtc(cutoffSrv);

   // Always draw Open TP lines if enabled, independently of table visibility
   DrawOpenTPGuide("15m", CLR_TP15, g_ok15, g_r15, cutoffSrv, cutoffUtc);
   DrawOpenTPGuide("30m", CLR_TP30, g_ok30, g_r30, cutoffSrv, cutoffUtc);
   DrawOpenTPGuide("45m", CLR_TP45, g_ok45, g_r45, cutoffSrv, cutoffUtc);

   // If the win-rate table is disabled from settings, remove all table objects and exit
   if(!InpShowTable)
   {
      string frameName = OBJ_PREFIX + "TF_FRAME";
      if(ObjectFind(0, frameName) >= 0) ObjectDelete(0, frameName);
      DeleteObjectsWithSubPrefix("TC_");
      DeleteObjectsWithSubPrefix("TL_");
      return;
   }

   int colW[5]      = {108, 92, 92, 92, 92};
   int colStartX[5] = {0, 108, 200, 292, 384};
   int totalW       = 476;
   int rowH         = 20;
   int rightMargin  = 12;
   int topMargin    = 24;

   string frameName = OBJ_PREFIX + "TF_FRAME";
   if(ObjectFind(0, frameName) < 0)
      ObjectCreate(0, frameName, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, frameName, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, frameName, OBJPROP_XDISTANCE, rightMargin + totalW + 2);
   ObjectSetInteger(0, frameName, OBJPROP_YDISTANCE, topMargin - 2);
   ObjectSetInteger(0, frameName, OBJPROP_XSIZE, totalW + 3);
   ObjectSetInteger(0, frameName, OBJPROP_YSIZE, rowH * 11 + 3);
   ObjectSetInteger(0, frameName, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, frameName, OBJPROP_COLOR, C'70,85,103');
   ObjectSetInteger(0, frameName, OBJPROP_BGCOLOR, C'24,33,44');
   ObjectSetInteger(0, frameName, OBJPROP_BACK, false);
   ObjectSetInteger(0, frameName, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, frameName, OBJPROP_HIDDEN, true);

   string countTip = "n = finished trades (incl. ?). Bounds show min/max win rate from ? outcomes.";
   string poolTip  = "All TFs sums case counts before computing rates (independent tests).";

   DrawCell(0, 0, colW, colStartX, totalW, rightMargin, topMargin, rowH, "Win rate (n)", countTip);
   DrawCell(1, 0, colW, colStartX, totalW, rightMargin, topMargin, rowH, "15m", countTip);
   DrawCell(2, 0, colW, colStartX, totalW, rightMargin, topMargin, rowH, "30m", countTip);
   DrawCell(3, 0, colW, colStartX, totalW, rightMargin, topMargin, rowH, "45m", countTip);
   DrawCell(4, 0, colW, colStartX, totalW, rightMargin, topMargin, rowH, "All TFs", poolTip);

   for(int g = 0; g <= 4; g++)
   {
      string name = (g == 0) ? "Overall" : (g == 1) ? "Asian" : (g == 2) ? "London" : (g == 3) ? "New York" : "NY-London KZ";
      string win  = (g == 0) ? "All entries, incl. outside named sessions."
                  : (g == 1) ? InpAsianHours + " UTC"
                  : (g == 2) ? InpLondonHours + " Europe/London"
                  : (g == 3) ? InpNyHours + " America/New_York"
                             : InpKzHours + " London inside overlap.";
      int y = g + 1;
      bool ok15 = false, ok30 = false, ok45 = false;
      int w15 = 0, n15 = 0, u15 = 0;
      int w30 = 0, n30 = 0, u30 = 0;
      int w45 = 0, n45 = 0, u45 = 0;
      GetRowCounts(g_ok15, g_r15, g, ok15, w15, n15, u15);
      GetRowCounts(g_ok30, g_r30, g, ok30, w30, n30, u30);
      GetRowCounts(g_ok45, g_r45, g, ok45, w45, n45, u45);

      DrawCell(0, y, colW, colStartX, totalW, rightMargin, topMargin, rowH, name, win);
      DrawRateCell(1, y, colW, colStartX, totalW, rightMargin, topMargin, rowH, ok15, w15, n15, u15, win);
      DrawRateCell(2, y, colW, colStartX, totalW, rightMargin, topMargin, rowH, ok30, w30, n30, u30, win);
      DrawRateCell(3, y, colW, colStartX, totalW, rightMargin, topMargin, rowH, ok45, w45, n45, u45, win);
      bool allOk = (ok15 && ok30 && ok45);
      DrawRateCell(4, y, colW, colStartX, totalW, rightMargin, topMargin, rowH, allOk,
                   w15 + w30 + w45, n15 + n30 + n45, u15 + u30 + u45, win);
   }

   DrawCell(0, 6,  colW, colStartX, totalW, rightMargin, topMargin, rowH, "TP / Loss / ?", "? = trade ended, TP vs BE order unproven.");
   DrawCell(0, 7,  colW, colStartX, totalW, rightMargin, topMargin, rowH, IntegerToString((int)InpInspectTF) + "m coverage", "Share of held bars with full inspection data.");
   DrawCell(0, 8,  colW, colStartX, totalW, rightMargin, topMargin, rowH, "TP d / pts", "Price distance / points at last entry (gross).");
   DrawCell(0, 9,  colW, colStartX, totalW, rightMargin, topMargin, rowH, "Position", "One test slot per timeframe.");
   DrawCell(0, 10, colW, colStartX, totalW, rightMargin, topMargin, rowH, "Open TP price", "Frozen target on held entry.");
   DrawCell(4, 10, colW, colStartX, totalW, rightMargin, topMargin, rowH, "-", "Independent positions have no shared TP.");

   DrawFooterCol(1, colW, colStartX, totalW, rightMargin, topMargin, rowH, g_ok15, g_r15, cutoffUtc);
   DrawFooterCol(2, colW, colStartX, totalW, rightMargin, topMargin, rowH, g_ok30, g_r30, cutoffUtc);
   DrawFooterCol(3, colW, colStartX, totalW, rightMargin, topMargin, rowH, g_ok45, g_r45, cutoffUtc);

   bool full = (RowOk(g_ok15, g_r15) && RowOk(g_ok30, g_r30) && RowOk(g_ok45, g_r45));
   string wlu = full ? FormatWLU(g_r15.wins + g_r30.wins + g_r45.wins,
                                 g_r15.losses + g_r30.losses + g_r45.losses,
                                 g_r15.unknown + g_r30.unknown + g_r45.unknown) : "n/a";
   string cov = full ? FormatCoverage(g_r15.fineBars + g_r30.fineBars + g_r45.fineBars,
                                      g_r15.coarseBars + g_r30.coarseBars + g_r45.coarseBars) : "n/a";
   int slots = (g_r15.side != 0 ? 1 : 0) + (g_r30.side != 0 ? 1 : 0) + (g_r45.side != 0 ? 1 : 0);

   DrawCell(4, 6, colW, colStartX, totalW, rightMargin, topMargin, rowH, wlu, poolTip);
   DrawCell(4, 7, colW, colStartX, totalW, rightMargin, topMargin, rowH, cov, "Pooled fine vs all evaluated held bars.");
   DrawCell(4, 8, colW, colStartX, totalW, rightMargin, topMargin, rowH, (InpUsdTick > 0.0) ? "Manual $" : "Auto $",
            (InpUsdTick > 0.0) ? "Manual broker tick USD." : "MT5 live contract tick USD estimate.");
   DrawCell(4, 9, colW, colStartX, totalW, rightMargin, topMargin, rowH, full ? (IntegerToString(slots) + " held") : "n/a",
            "Held slots across 15m/30m/45m.");
}

//+------------------------------------------------------------------+
//| Chart Structure & Audit Drawings                                 |
//+------------------------------------------------------------------+
void DrawStructureEvents(const StructureFrame &events[], const int count)
{
   DeleteObjectsWithSubPrefix("SW_");
   DeleteObjectsWithSubPrefix("BRK_");

   if(!InpShowSwings && !InpShowBreaks) return;

   int maxObj = MathMax(20, InpMaxDrawObjects);
   string activeHi = "";
   string activeLo = "";

   string lblQueue[];
   string lineQueue[];
   int lblCount = 0, lineCount = 0;
   ArrayResize(lblQueue,  count * 2 + 10);
   ArrayResize(lineQueue, count + 10);

   int seq = 0;
   for(int i = 0; i < count; i++)
   {
      StructureFrame s = events[i];
      if(s.eventCode != 0)
      {
         activeHi = "";
         activeLo = "";
      }

      if(InpShowSwings && s.hiClass != 0 && s.pivotAt > 0 && !IsNa(s.ph))
      {
         if(s.replaceHi && activeHi != "")
            ObjectDelete(0, activeHi);

         string tagH = (s.hiClass == 3) ? "H" : (s.hiClass == 1) ? "HH" : "LH";
         color  clrH = (s.hiClass == 1) ? CLR_BULL : (s.hiClass == -1) ? CLR_BEAR : CLR_NEUTRAL;
         string name = OBJ_PREFIX + "SW_H_" + IntegerToString(seq++);
         if(ObjectCreate(0, name, OBJ_TEXT, 0, s.pivotAt, s.ph))
         {
            ObjectSetString(0,  name, OBJPROP_TEXT, tagH);
            ObjectSetString(0,  name, OBJPROP_FONT, "Segoe UI Semibold");
            ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
            ObjectSetInteger(0, name, OBJPROP_COLOR, clrH);
            ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LOWER);
            ObjectSetInteger(0, name, OBJPROP_BACK, false);
            ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
            ObjectSetString(0,  name, OBJPROP_TOOLTIP, "Swing high (confirmed after right-hand bars close).");
         }
         activeHi = name;
         lblQueue[lblCount++] = name;
      }

      if(InpShowSwings && s.loClass != 0 && s.pivotAt > 0 && !IsNa(s.pl))
      {
         if(s.replaceLo && activeLo != "")
            ObjectDelete(0, activeLo);

         string tagL = (s.loClass == 3) ? "L" : (s.loClass == 1) ? "HL" : "LL";
         color  clrL = (s.loClass == 1) ? CLR_BULL : (s.loClass == -1) ? CLR_BEAR : CLR_NEUTRAL;
         string name = OBJ_PREFIX + "SW_L_" + IntegerToString(seq++);
         if(ObjectCreate(0, name, OBJ_TEXT, 0, s.pivotAt, s.pl))
         {
            ObjectSetString(0,  name, OBJPROP_TEXT, tagL);
            ObjectSetString(0,  name, OBJPROP_FONT, "Segoe UI Semibold");
            ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
            ObjectSetInteger(0, name, OBJPROP_COLOR, clrL);
            ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_UPPER);
            ObjectSetInteger(0, name, OBJPROP_BACK, false);
            ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
            ObjectSetString(0,  name, OBJPROP_TOOLTIP, "Swing low (confirmed after right-hand bars close).");
         }
         activeLo = name;
         lblQueue[lblCount++] = name;
      }

      if(InpShowBreaks && s.eventCode != 0 && s.levelAt > 0 && s.closedAt > 0 && !IsNa(s.level))
      {
         int k = MathAbs(s.eventCode);
         color c = (s.eventCode > 0) ? CLR_BULL : CLR_BEAR;
         string brkTxt = ((k == 2) ? "MSS" : (k == 1) ? "BOS" : "INIT") + (s.eventCode > 0 ? " ^" : " v");

         string ln = OBJ_PREFIX + "BRK_L_" + IntegerToString(seq++);
         if(ObjectCreate(0, ln, OBJ_TREND, 0, s.levelAt, s.level, s.closedAt, s.level))
         {
            ObjectSetInteger(0, ln, OBJPROP_COLOR, c);
            ObjectSetInteger(0, ln, OBJPROP_STYLE, STYLE_SOLID);
            ObjectSetInteger(0, ln, OBJPROP_WIDTH, (k == 2) ? 2 : 1);
            ObjectSetInteger(0, ln, OBJPROP_RAY_RIGHT, false);
            ObjectSetInteger(0, ln, OBJPROP_BACK, false);
            ObjectSetInteger(0, ln, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, ln, OBJPROP_HIDDEN, true);
         }
         lineQueue[lineCount++] = ln;

         string lb = OBJ_PREFIX + "BRK_T_" + IntegerToString(seq++);
         if(ObjectCreate(0, lb, OBJ_TEXT, 0, s.closedAt, s.level))
         {
            ObjectSetString(0,  lb, OBJPROP_TEXT, brkTxt);
            ObjectSetString(0,  lb, OBJPROP_FONT, "Segoe UI Semibold");
            ObjectSetInteger(0, lb, OBJPROP_FONTSIZE, 8);
            ObjectSetInteger(0, lb, OBJPROP_COLOR, c);
            ObjectSetInteger(0, lb, OBJPROP_ANCHOR, (s.eventCode > 0) ? ANCHOR_LOWER : ANCHOR_UPPER);
            ObjectSetInteger(0, lb, OBJPROP_BACK, false);
            ObjectSetInteger(0, lb, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, lb, OBJPROP_HIDDEN, true);
            ObjectSetString(0,  lb, OBJPROP_TOOLTIP, "Confirmed structure close, not a Hull entry.");
         }
         lblQueue[lblCount++] = lb;
      }
   }

   if(lblCount > maxObj)
   {
      int trim = lblCount - maxObj;
      for(int j = 0; j < trim; j++) ObjectDelete(0, lblQueue[j]);
   }
   if(lineCount > maxObj)
   {
      int trim = lineCount - maxObj;
      for(int j = 0; j < trim; j++) ObjectDelete(0, lineQueue[j]);
   }
}

void DrawTrendBackground(const BarData &bars[], const int &biasArr[], const int n)
{
   DeleteObjectsWithSubPrefix("BG_");
   if(!InpShowBgTint || n <= 1) return;

   color chartBg  = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);
   color tintBull = BlendColor(CLR_BULL, chartBg, 0.12);
   color tintBear = BlendColor(CLR_BEAR, chartBg, 0.12);

   int segStart[];
   int segEnd[];
   int segDir[];
   ArrayResize(segStart, 0);
   ArrayResize(segEnd,   0);
   ArrayResize(segDir,   0);

   int segs     = 0;
   int curDir   = biasArr[0];
   int curStart = 0;
   for(int i = 1; i < n; i++)
   {
      if(biasArr[i] != curDir)
      {
         if(curDir != 0)
         {
            ArrayResize(segStart, segs + 1, 128);
            ArrayResize(segEnd,   segs + 1, 128);
            ArrayResize(segDir,   segs + 1, 128);
            segStart[segs] = curStart;
            segEnd[segs]   = i;
            segDir[segs]   = curDir;
            segs++;
         }
         curDir   = biasArr[i];
         curStart = i;
      }
   }
   if(curDir != 0)
   {
      ArrayResize(segStart, segs + 1, 128);
      ArrayResize(segEnd,   segs + 1, 128);
      ArrayResize(segDir,   segs + 1, 128);
      segStart[segs] = curStart;
      segEnd[segs]   = n - 1;
      segDir[segs]   = curDir;
      segs++;
   }

   int startSeg = MathMax(0, segs - 250);
   for(int s = startSeg; s < segs; s++)
   {
      datetime t1 = bars[segStart[s]].time;
      datetime t2 = bars[segEnd[s]].time;
      if(segEnd[s] == n - 1) t2 = bars[n - 1].timeClose;
      color c = (segDir[s] == 1) ? tintBull : tintBear;
      string nm = OBJ_PREFIX + "BG_" + IntegerToString(s);
      if(ObjectCreate(0, nm, OBJ_RECTANGLE, 0, t1, 0.0, t2, 1e9))
      {
         ObjectSetInteger(0, nm, OBJPROP_COLOR, c);
         ObjectSetInteger(0, nm, OBJPROP_FILL, true);
         ObjectSetInteger(0, nm, OBJPROP_BACK, true);
         ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, nm, OBJPROP_HIDDEN, true);
      }
   }
}

void DrawVisibleStrip()
{
   DeleteObjectsWithSubPrefix("ST_");
   if(InpStripWidth <= 0 || g_chartBarsCount <= 0) return;

   int firstVis = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR);
   int visCount = (int)ChartGetInteger(0, CHART_VISIBLE_BARS);
   int chartH   = (int)ChartGetInteger(0, CHART_HEIGHT_IN_PIXELS);
   int chartW   = (int)ChartGetInteger(0, CHART_WIDTH_IN_PIXELS);
   double minP  = ChartGetDouble(0, CHART_PRICE_MIN);

   if(visCount <= 0 || chartH <= InpStripWidth) return;

   int oldestChron = MathMax(0, g_chartBarsCount - 1 - firstVis);
   int newestChron = MathMin(g_chartBarsCount - 1, oldestChron + visCount + 1);

   int yTop   = chartH - InpStripWidth - 2;
   int segIdx = 0;
   int i      = oldestChron;
   while(i <= newestChron)
   {
      int d = g_chartBiasCache[i];
      int j = i + 1;
      while(j <= newestChron && g_chartBiasCache[j] == d) j++;

      int x1 = 0, y1 = 0, x2 = 0, y2 = 0;
      ChartTimePriceToXY(0, 0, g_chartTimeCache[i], minP, x1, y1);
      if(j <= g_chartBarsCount - 1)
         ChartTimePriceToXY(0, 0, g_chartTimeCache[j], minP, x2, y2);
      else
      {
         ChartTimePriceToXY(0, 0, g_chartTimeCache[g_chartBarsCount - 1], minP, x2, y2);
         x2 += 8;
      }
      x1 = MathMax(0, x1);
      x2 = MathMin(chartW, MathMax(x1 + 2, x2));
      int w = x2 - x1;
      if(w > 0)
      {
         color c = (d == 1) ? CLR_BULL : (d == -1) ? CLR_BEAR : CLR_NEUTRAL;
         string nm = OBJ_PREFIX + "ST_" + IntegerToString(segIdx++);
         if(ObjectCreate(0, nm, OBJ_RECTANGLE_LABEL, 0, 0, 0))
         {
            ObjectSetInteger(0, nm, OBJPROP_CORNER, CORNER_LEFT_UPPER);
            ObjectSetInteger(0, nm, OBJPROP_XDISTANCE, x1);
            ObjectSetInteger(0, nm, OBJPROP_YDISTANCE, yTop);
            ObjectSetInteger(0, nm, OBJPROP_XSIZE, w);
            ObjectSetInteger(0, nm, OBJPROP_YSIZE, InpStripWidth);
            ObjectSetInteger(0, nm, OBJPROP_BORDER_TYPE, BORDER_FLAT);
            ObjectSetInteger(0, nm, OBJPROP_COLOR, c);
            ObjectSetInteger(0, nm, OBJPROP_BGCOLOR, c);
            ObjectSetInteger(0, nm, OBJPROP_BACK, false);
            ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, nm, OBJPROP_HIDDEN, true);
         }
      }
      i = j;
   }
}

//+------------------------------------------------------------------+
//| Draw BUY / SELL label tags (OBJ_TEXT, like TradingView labels)  |
//| First signal after each confirmed MSS is tagged "BUY 1"/"SELL 1"|
//+------------------------------------------------------------------+
void DrawSignalLabels(const BarData &bars[], const StructureFrame &evts[], const int evtCount,
                      const int &biasArr[], const double &lowA[], const double &highA[],
                      const double &fixedA[], const double &hullB[], const int n)
{
   // Rebuild objects from scratch every full redraw
   DeleteObjectsWithSubPrefix("SIG_");

   bool allCrosses = (InpMarks == SIGNALS_ALL_CROSS);

   // Track MSS state across bars to know when to show "BUY 1" / "SELL 1"
   int   lastMssBias   = 0;
   bool  firstSigDone  = true; // start as "done" so pre-MSS bars don't get a "1" label

   int evtPtr = 0;
   int seq    = 0;

   for(int i = 0; i < n; i++)
   {
      // Advance structure events up to this bar's close to pick up MSS flags
      while(evtPtr < evtCount && evts[evtPtr].closedAt <= bars[i].timeClose)
      {
         int code = evts[evtPtr].eventCode;
         if(MathAbs(code) == 2) // MSS
         {
            int newBias = (code > 0) ? 1 : -1;
            if(newBias != lastMssBias)
            {
               lastMssBias  = newBias;
               firstSigDone = false; // reset: next aligned cross gets the "1" suffix
            }
         }
         evtPtr++;
      }

      // Evaluate cross on this bar
      int raw = 0, pick = 0;
      double a = EMPTY_VALUE, b = EMPTY_VALUE;
      EvalHullBar(i, biasArr[i], lowA, highA, fixedA, hullB, raw, a, b, pick);

      bool buySig  = allCrosses ? (raw ==  1) : (raw ==  1 && biasArr[i] ==  1);
      bool sellSig = allCrosses ? (raw == -1) : (raw == -1 && biasArr[i] == -1);

      if(!buySig && !sellSig) continue;

      bool isFirst = false;
      if(buySig && lastMssBias == 1 && !firstSigDone)
      {
         isFirst      = true;
         firstSigDone = true;
      }
      else if(sellSig && lastMssBias == -1 && !firstSigDone)
      {
         isFirst      = true;
         firstSigDone = true;
      }

      string txt  = buySig ? (isFirst ? "BUY 1" : "BUY") : (isFirst ? "SELL 1" : "SELL");
      color  clr  = buySig ? CLR_BULL : CLR_BEAR;
      double px   = buySig ? bars[i].low : bars[i].high;
      int anchor  = buySig ? ANCHOR_UPPER : ANCHOR_LOWER;

      string nm = OBJ_PREFIX + "SIG_" + IntegerToString(seq++);
      if(ObjectCreate(0, nm, OBJ_TEXT, 0, bars[i].time, px))
      {
         ObjectSetString(0,  nm, OBJPROP_TEXT,       txt);
         ObjectSetString(0,  nm, OBJPROP_FONT,       "Segoe UI Semibold");
         ObjectSetInteger(0, nm, OBJPROP_FONTSIZE,   8);
         ObjectSetInteger(0, nm, OBJPROP_COLOR,      clr);
         ObjectSetInteger(0, nm, OBJPROP_ANCHOR,     anchor);
         ObjectSetInteger(0, nm, OBJPROP_BACK,       false);
         ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, nm, OBJPROP_HIDDEN,     true);
         ObjectSetString(0,  nm, OBJPROP_TOOLTIP,    txt + " — " + TimeToString(bars[i].time));
      }
   }
}

void DrawAuditExitLabels(const BarData &bars[], const RowSnapshot &snaps[], const int n)
{
   DeleteObjectsWithSubPrefix("AUD_");
   ArrayFill(BufAuditEntry,  0, ArraySize(BufAuditEntry),  EMPTY_VALUE);
   ArrayFill(BufAuditTarget, 0, ArraySize(BufAuditTarget), EMPTY_VALUE);

   int sec = PeriodSeconds(_Period);
   if(!InpAuditMarks || (sec != 900 && sec != 1800))
      return;

   int nSnaps = ArraySize(snaps);
   int seq    = 0;
   string qNames[];
   ArrayResize(qNames, nSnaps + 1);

   int b = 0;
   for(int s = 0; s < nSnaps - 1; s++)
   {
      datetime barOpen = snaps[s].endTime - sec;
      while(b < n - 1 && bars[b].time < barOpen) b++;
      if(b >= n - 1 || bars[b].time != barOpen) continue;

      if(snaps[s].entered)
      {
         BufAuditEntry[b]  = snaps[s].entry;
         BufAuditTarget[b] = snaps[s].target;
      }

      int ec = snaps[s].exitCode;
      if(ec != 0)
      {
         string txt = (ec == OUTCOME_TAKE) ? "TP" : (ec == OUTCOME_BE) ? "BE" : (ec == OUTCOME_UNCLEAR) ? "?" : "L";
         color  clr = (ec == OUTCOME_TAKE) ? CLR_BULL : (ec == OUTCOME_UNCLEAR) ? CLR_PURPLE : CLR_BEAR;
         string nm  = OBJ_PREFIX + "AUD_" + IntegerToString(seq);
         if(ObjectCreate(0, nm, OBJ_TEXT, 0, bars[b].time, bars[b].high))
         {
            ObjectSetString(0,  nm, OBJPROP_TEXT, txt);
            ObjectSetString(0,  nm, OBJPROP_FONT, "Segoe UI Semibold");
            ObjectSetInteger(0, nm, OBJPROP_FONTSIZE, 8);
            ObjectSetInteger(0, nm, OBJPROP_COLOR, clr);
            ObjectSetInteger(0, nm, OBJPROP_ANCHOR, ANCHOR_LOWER);
            ObjectSetInteger(0, nm, OBJPROP_BACK, false);
            ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, nm, OBJPROP_HIDDEN, true);
         }
         qNames[seq++] = nm;
      }
   }

   int maxObj = MathMax(20, InpMaxDrawObjects);
   if(seq > maxObj)
   {
      for(int j = 0; j < seq - maxObj; j++)
         ObjectDelete(0, qNames[j]);
   }
}

void ShowErrorBanner(const string msg)
{
   string nm = OBJ_PREFIX + "ERR";
   if(msg == "")
   {
      if(ObjectFind(0, nm) >= 0) ObjectDelete(0, nm);
      return;
   }
   if(ObjectFind(0, nm) < 0)
      ObjectCreate(0, nm, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, nm, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, nm, OBJPROP_XDISTANCE, 12);
   ObjectSetInteger(0, nm, OBJPROP_YDISTANCE, 24);
   ObjectSetString(0,  nm, OBJPROP_FONT, "Segoe UI Semibold");
   ObjectSetInteger(0, nm, OBJPROP_FONTSIZE, 9);
   ObjectSetInteger(0, nm, OBJPROP_COLOR, CLR_BEAR);
   ObjectSetString(0,  nm, OBJPROP_TEXT, msg);
   ObjectSetInteger(0, nm, OBJPROP_HIDDEN, true);
}

//+------------------------------------------------------------------+
//| Refresh All 3 Studies + Win-Rate Table                           |
//+------------------------------------------------------------------+
bool RefreshAllStudies(const datetime cutoffSrv, const bool needChartAudit, RowSnapshot &auditSnaps[])
{
   ENUM_TIMEFRAMES inspectTF = (InpInspectTF == INSPECT_M1) ? PERIOD_M1 : PERIOD_M5;
   int inspectSec = PeriodSeconds(inspectTF);

   MqlRates inspectRates[];
   ArraySetAsSeries(inspectRates, false);
   int availInspect  = Bars(_Symbol, inspectTF);
   int copiedInspect = 0;

   if(availInspect > 0)
   {
      datetime earliestNeeded = 0;
      int bars15 = Bars(_Symbol, PERIOD_M15);
      if(bars15 > 0)
      {
         int shift15 = (InpMaxStudyBars > 0) ? MathMin(bars15 - 1, InpMaxStudyBars) : (bars15 - 1);
         datetime t15 = iTime(_Symbol, PERIOD_M15, shift15);
         if(t15 > 0) earliestNeeded = t15;
      }
      if(InpStartAt > D'1970.01.02 00:00:00')
      {
         datetime approxSrvStart = InpStartAt - 86400;
         if(approxSrvStart > earliestNeeded) earliestNeeded = approxSrvStart;
      }
      if(earliestNeeded > 0)
         copiedInspect = CopyRates(_Symbol, inspectTF, earliestNeeded, TimeCurrent() + 86400, inspectRates);
      else
         copiedInspect = CopyRates(_Symbol, inspectTF, 0, availInspect, inspectRates);

      if(copiedInspect > 0) ArraySetAsSeries(inspectRates, false);
   }

   int chartSec = PeriodSeconds(_Period);
   StudyResult r15, r30, r45;
   RowSnapshot emptySnaps[];

   bool ok15 = false;
   bool ok30 = false;
   bool ok45 = false;

   if(needChartAudit && chartSec == 900)
      ok15 = RunStudyForTF(900, inspectRates, MathMax(0, copiedInspect), inspectSec, r15, auditSnaps, true);
   else
      ok15 = RunStudyForTF(900, inspectRates, MathMax(0, copiedInspect), inspectSec, r15, emptySnaps, false);

   if(needChartAudit && chartSec == 1800)
      ok30 = RunStudyForTF(1800, inspectRates, MathMax(0, copiedInspect), inspectSec, r30, auditSnaps, true);
   else
      ok30 = RunStudyForTF(1800, inspectRates, MathMax(0, copiedInspect), inspectSec, r30, emptySnaps, false);

   ok45 = RunStudyForTF(2700, inspectRates, MathMax(0, copiedInspect), inspectSec, r45, emptySnaps, false);

   g_ok15 = ok15 && PickClosedSnapshot(r15, cutoffSrv, g_r15);
   g_ok30 = ok30 && PickClosedSnapshot(r30, cutoffSrv, g_r30);
   g_ok45 = ok45 && PickClosedSnapshot(r45, cutoffSrv, g_r45);

   RenderWinRatePanel(cutoffSrv);

   bool synced = (SeriesInfoInteger(_Symbol, PERIOD_M15, SERIES_SYNCHRONIZED) &&
                  SeriesInfoInteger(_Symbol, PERIOD_M30, SERIES_SYNCHRONIZED) &&
                  SeriesInfoInteger(_Symbol, inspectTF,  SERIES_SYNCHRONIZED));
   if(!ok15 || !ok30 || !synced)
   {
      g_waitingHistory = true;
      EventSetTimer(1);
   }
   else if(g_waitingHistory)
   {
      g_waitingHistory = false;
      EventKillTimer();
   }
   return (ok15 && ok30);
}

//+------------------------------------------------------------------+
//| Initialization & Cleanup                                         |
//+------------------------------------------------------------------+
int OnInit()
{
   g_length = MathMax(2, MathMin(500, InpLength));
   g_depth  = MathMax(1, MathMin(50, InpSwingDepth));
   g_bePct  = MathMax(0, MathMin(100, InpBePct));

   g_mintick = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(g_mintick <= 0.0) g_mintick = _Point;

   if(InpStartAt > InpEndAt)
   {
      Alert("FC Dynamic Hull: 'Test through' must not precede 'Test from'.");
      return INIT_PARAMETERS_INCORRECT;
   }

   int chartSec  = PeriodSeconds(_Period);
   int structSec = (InpStructTF == PERIOD_CURRENT) ? chartSec : PeriodSeconds(InpStructTF);
   bool lower    = (structSec < chartSec);
   bool nested   = (!lower || (chartSec % structSec == 0));
   if(!nested)
   {
      g_errorBanner = "Lower structure timeframe must nest inside chart timeframe (or set Structure TF = Current).";
      ShowErrorBanner(g_errorBanner);
      return INIT_PARAMETERS_INCORRECT;
   }
   g_errorBanner = "";

   DetectLiveServerOffset();
   ResolveFxConversionSymbol();

   color chartBg  = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);
   color fillBull = BlendColor(CLR_BULL, chartBg, 0.20);
   color fillBear = BlendColor(CLR_BEAR, chartBg, 0.20);

   SetIndexBuffer(0,  BufFillLowB,      INDICATOR_DATA);
   SetIndexBuffer(1,  BufFillLowA,      INDICATOR_DATA);
   SetIndexBuffer(2,  BufFillHighB,     INDICATOR_DATA);
   SetIndexBuffer(3,  BufFillHighA,     INDICATOR_DATA);
   SetIndexBuffer(4,  BufFillFixedB,    INDICATOR_DATA);
   SetIndexBuffer(5,  BufFillFixedA,    INDICATOR_DATA);
   SetIndexBuffer(6,  BufHullB,         INDICATOR_DATA);
   SetIndexBuffer(7,  BufHullBClr,      INDICATOR_COLOR_INDEX);
   SetIndexBuffer(8,  BufHullALow,      INDICATOR_DATA);
   SetIndexBuffer(9,  BufHullALowClr,   INDICATOR_COLOR_INDEX);
   SetIndexBuffer(10, BufHullAHigh,     INDICATOR_DATA);
   SetIndexBuffer(11, BufHullAHighClr,  INDICATOR_COLOR_INDEX);
   SetIndexBuffer(12, BufHullAFixed,    INDICATOR_DATA);
   SetIndexBuffer(13, BufHullAFixedClr, INDICATOR_COLOR_INDEX);
   SetIndexBuffer(14, BufBuyArrow,      INDICATOR_DATA);
   SetIndexBuffer(15, BufSellArrow,     INDICATOR_DATA);
   SetIndexBuffer(16, BufAuditEntry,    INDICATOR_DATA);
   SetIndexBuffer(17, BufAuditTarget,   INDICATOR_DATA);
   SetIndexBuffer(18, BufBias,          INDICATOR_DATA);
   SetIndexBuffer(19, BufPick,          INDICATOR_DATA);

   ArraySetAsSeries(BufFillLowB,      false); ArraySetAsSeries(BufFillLowA,      false);
   ArraySetAsSeries(BufFillHighB,     false); ArraySetAsSeries(BufFillHighA,     false);
   ArraySetAsSeries(BufFillFixedB,    false); ArraySetAsSeries(BufFillFixedA,    false);
   ArraySetAsSeries(BufHullB,         false); ArraySetAsSeries(BufHullBClr,      false);
   ArraySetAsSeries(BufHullALow,      false); ArraySetAsSeries(BufHullALowClr,   false);
   ArraySetAsSeries(BufHullAHigh,     false); ArraySetAsSeries(BufHullAHighClr,  false);
   ArraySetAsSeries(BufHullAFixed,    false); ArraySetAsSeries(BufHullAFixedClr, false);
   ArraySetAsSeries(BufBuyArrow,      false); ArraySetAsSeries(BufSellArrow,     false);
   ArraySetAsSeries(BufAuditEntry,    false); ArraySetAsSeries(BufAuditTarget,   false);
   ArraySetAsSeries(BufBias,          false); ArraySetAsSeries(BufPick,          false);

   for(int p = 0; p <= 2; p++)
   {
      PlotIndexSetInteger(p, PLOT_LINE_COLOR, 0, fillBull);
      PlotIndexSetInteger(p, PLOT_LINE_COLOR, 1, fillBear);
      PlotIndexSetDouble(p,  PLOT_EMPTY_VALUE, EMPTY_VALUE);
      PlotIndexSetInteger(p, PLOT_SHOW_DATA, false);
   }
   for(int p = 3; p <= 6; p++)
      PlotIndexSetDouble(p, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(7,  PLOT_ARROW, 233);
   PlotIndexSetInteger(7,  PLOT_ARROW_SHIFT, 12);
   PlotIndexSetDouble(7,   PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(8,  PLOT_ARROW, 234);
   PlotIndexSetInteger(8,  PLOT_ARROW_SHIFT, -12);
   PlotIndexSetDouble(8,   PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(9,  PLOT_ARROW, 159);
   PlotIndexSetDouble(9,   PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(10, PLOT_ARROW, 159);
   PlotIndexSetDouble(10,  PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetDouble(11, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(12, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   IndicatorSetString(INDICATOR_SHORTNAME, "FC Hull WR");

   DeleteAllObjectsByPrefix();
   g_lastStudyRunBar15 = 0;
   g_lastStudyRunBar30 = 0;
   g_lastChartDrawBar  = 0;
   g_lastAlertBar      = 0;

   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   DeleteAllObjectsByPrefix();
}

void OnTimer()
{
   if(!g_waitingHistory) return;
   RowSnapshot dummy[];
   RefreshAllStudies(TimeCurrent(), false, dummy);
   ChartRedraw(0);
}

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id == CHARTEVENT_CHART_CHANGE)
      DrawVisibleStrip();
}

//+------------------------------------------------------------------+
//| Main Indicator Calculation                                       |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   if(rates_total < 4) return 0;

   bool isSeries = ArrayGetAsSeries(time);
   int chartSec  = PeriodSeconds(_Period);

   BarData chartBars[];
   ArrayResize(chartBars, rates_total);
   for(int i = 0; i < rates_total; i++)
   {
      int idx = isSeries ? (rates_total - 1 - i) : i;
      chartBars[i].time      = time[idx];
      chartBars[i].timeClose = time[idx] + chartSec;
      chartBars[i].open      = open[idx];
      chartBars[i].high      = high[idx];
      chartBars[i].low       = low[idx];
      chartBars[i].close     = close[idx];
   }

   bool useNativeStruct = (InpStructTF == PERIOD_CURRENT || PeriodSeconds(InpStructTF) == chartSec);
   int  structSec       = useNativeStruct ? chartSec : PeriodSeconds(InpStructTF);
   bool higher          = (!useNativeStruct && structSec > chartSec);
   bool lower           = (!useNativeStruct && structSec < chartSec);
   bool nested          = (!lower || (chartSec % structSec == 0));

   if(!nested)
   {
      ShowErrorBanner("Lower structure timeframe must nest inside chart timeframe.");
      return rates_total;
   }
   ShowErrorBanner("");

   StructureFrame structFrames[];
   if(useNativeStruct)
   {
      BuildStructureFrames(chartBars, g_depth, structFrames);
   }
   else
   {
      BarData sBars[];
      int sMax = (InpMaxStudyBars > 0 && lower) ? InpMaxStudyBars * MathMax(1, chartSec / structSec) + 200 : InpMaxStudyBars;
      if(LoadNativeBars(InpStructTF, sMax, sBars))
         BuildStructureFrames(sBars, g_depth, structFrames);
   }

   int nStruct = ArraySize(structFrames);
   datetime nowSrv = TimeCurrent();

   StructureFrame drawEvents[];
   ArrayResize(drawEvents, 0);
   int drawEvtCount = 0;

   int biasArr[];
   ArrayResize(biasArr, rates_total);

   int dir = 0;
   datetime seen = 0;
   int sPtr = 0;

   for(int i = 0; i < rates_total; i++)
   {
      bool isLastBar = (i == rates_total - 1);
      datetime cutoff = isLastBar ? MathMin(nowSrv, chartBars[i].timeClose) : chartBars[i].timeClose;

      if(useNativeStruct)
      {
         if(!isLastBar && structFrames[i].closedAt <= cutoff && (seen == 0 || structFrames[i].closedAt > seen))
         {
            dir  = structFrames[i].bias;
            seen = structFrames[i].closedAt;
            if(structFrames[i].hiClass != 0 || structFrames[i].loClass != 0 || structFrames[i].eventCode != 0)
            {
               ArrayResize(drawEvents, drawEvtCount + 1, 256);
               drawEvents[drawEvtCount++] = structFrames[i];
            }
         }
      }
      else if(higher)
      {
         while(sPtr < nStruct && structFrames[sPtr].closedAt <= chartBars[i].time)
         {
            if(structFrames[sPtr].closedAt <= cutoff && (seen == 0 || structFrames[sPtr].closedAt > seen))
            {
               dir  = structFrames[sPtr].bias;
               seen = structFrames[sPtr].closedAt;
               if(structFrames[sPtr].hiClass != 0 || structFrames[sPtr].loClass != 0 || structFrames[sPtr].eventCode != 0)
               {
                  ArrayResize(drawEvents, drawEvtCount + 1, 256);
                  drawEvents[drawEvtCount++] = structFrames[sPtr];
               }
            }
            sPtr++;
         }
      }
      else if(lower)
      {
         while(sPtr < nStruct && structFrames[sPtr].closedAt <= cutoff)
         {
            if(seen == 0 || structFrames[sPtr].closedAt > seen)
            {
               dir  = structFrames[sPtr].bias;
               seen = structFrames[sPtr].closedAt;
               if(structFrames[sPtr].hiClass != 0 || structFrames[sPtr].loClass != 0 || structFrames[sPtr].eventCode != 0)
               {
                  ArrayResize(drawEvents, drawEvtCount + 1, 256);
                  drawEvents[drawEvtCount++] = structFrames[sPtr];
               }
            }
            sPtr++;
         }
      }
      biasArr[i] = dir;
   }

   double lowA[], highA[], fixedA[], hullB[];
   ComputeHullAll(chartBars, rates_total, lowA, highA, fixedA, hullB);

   bool allCrosses = (InpMarks == SIGNALS_ALL_CROSS);

   for(int i = 0; i < rates_total; i++)
   {
      int raw = 0, pick = 0;
      double a = EMPTY_VALUE, b = EMPTY_VALUE;
      EvalHullBar(i, biasArr[i], lowA, highA, fixedA, hullB, raw, a, b, pick);

      int clrIdx = 0;
      if(!IsNa(a) && !IsNa(b))
         clrIdx = (b > a) ? 1 : 2;

      BufHullB[i]    = b;
      BufHullBClr[i] = clrIdx;

      BufHullALow[i]      = EMPTY_VALUE; BufHullALowClr[i]   = clrIdx;
      BufHullAHigh[i]     = EMPTY_VALUE; BufHullAHighClr[i]  = clrIdx;
      BufHullAFixed[i]    = EMPTY_VALUE; BufHullAFixedClr[i] = clrIdx;

      BufFillLowB[i]   = EMPTY_VALUE; BufFillLowA[i]   = EMPTY_VALUE;
      BufFillHighB[i]  = EMPTY_VALUE; BufFillHighA[i]  = EMPTY_VALUE;
      BufFillFixedB[i] = EMPTY_VALUE; BufFillFixedA[i] = EMPTY_VALUE;

      if(pick == 1)
      {
         BufHullALow[i] = a;
         if(!IsNa(a) && !IsNa(b)) { BufFillLowB[i] = b; BufFillLowA[i] = a; }
      }
      else if(pick == -1)
      {
         BufHullAHigh[i] = a;
         if(!IsNa(a) && !IsNa(b)) { BufFillHighB[i] = b; BufFillHighA[i] = a; }
      }
      else if(pick == 2)
      {
         BufHullAFixed[i] = a;
         if(!IsNa(a) && !IsNa(b)) { BufFillFixedB[i] = b; BufFillFixedA[i] = a; }
      }

      bool buySig  = allCrosses ? (raw ==  1) : (raw ==  1 && biasArr[i] ==  1);
      bool sellSig = allCrosses ? (raw == -1) : (raw == -1 && biasArr[i] == -1);

      BufBuyArrow[i]  = buySig  ? chartBars[i].low  : EMPTY_VALUE;
      BufSellArrow[i] = sellSig ? chartBars[i].high : EMPTY_VALUE;

      BufBias[i] = (double)biasArr[i];
      BufPick[i] = (double)pick;
   }

   g_chartBarsCount = rates_total;
   ArrayResize(g_chartBiasCache, rates_total);
   ArrayResize(g_chartTimeCache, rates_total);
   for(int i = 0; i < rates_total; i++)
   {
      g_chartBiasCache[i] = biasArr[i];
      g_chartTimeCache[i] = chartBars[i].time;
   }

   datetime lastBarTime = chartBars[rates_total - 1].time;
   if(prev_calculated == 0 || lastBarTime != g_lastChartDrawBar)
   {
      g_lastChartDrawBar = lastBarTime;
      DrawStructureEvents(drawEvents, drawEvtCount);
      DrawSignalLabels(chartBars, drawEvents, drawEvtCount, biasArr, lowA, highA, fixedA, hullB, rates_total);
      DrawTrendBackground(chartBars, biasArr, rates_total);
      DrawVisibleStrip();

      if((InpEnableAlerts || InpPushAlerts) && rates_total >= 3 && g_lastAlertBar != chartBars[rates_total - 2].time)
      {
         int ci = rates_total - 2;
         int cRaw = 0, cPick = 0;
         double cA = 0.0, cB = 0.0;
         EvalHullBar(ci, biasArr[ci], lowA, highA, fixedA, hullB, cRaw, cA, cB, cPick);
         bool cBuy  = (cRaw ==  1 && biasArr[ci] ==  1);
         bool cSell = (cRaw == -1 && biasArr[ci] == -1);

         string alertMsg = "";
         if(cBuy)            alertMsg = StringFormat("BUY | %s | %s", _Symbol, EnumToString(_Period));
         else if(cSell)      alertMsg = StringFormat("SELL | %s | %s", _Symbol, EnumToString(_Period));
         else if(cRaw ==  1) alertMsg = StringFormat("EXIT SHORT | %s | %s", _Symbol, EnumToString(_Period));
         else if(cRaw == -1) alertMsg = StringFormat("EXIT LONG | %s | %s", _Symbol, EnumToString(_Period));

         if(alertMsg != "" && prev_calculated > 0)
         {
            if(InpEnableAlerts) Alert(alertMsg);
            if(InpPushAlerts)   SendNotification(alertMsg);
         }
         g_lastAlertBar = chartBars[ci].time;
      }
   }

   datetime curBar15 = iTime(_Symbol, PERIOD_M15, 0);
   datetime curBar30 = iTime(_Symbol, PERIOD_M30, 0);
   if(prev_calculated == 0 || curBar15 != g_lastStudyRunBar15 || curBar30 != g_lastStudyRunBar30 || g_waitingHistory)
   {
      g_lastStudyRunBar15 = curBar15;
      g_lastStudyRunBar30 = curBar30;

      RowSnapshot auditSnaps[];
      bool needAudit = (InpAuditMarks && (chartSec == 900 || chartSec == 1800));
      RefreshAllStudies(nowSrv, needAudit, auditSnaps);

      if(needAudit && ArraySize(auditSnaps) > 0)
         DrawAuditExitLabels(chartBars, auditSnaps, rates_total);
      else
      {
         DeleteObjectsWithSubPrefix("AUD_");
         ArrayFill(BufAuditEntry,  0, rates_total, EMPTY_VALUE);
         ArrayFill(BufAuditTarget, 0, rates_total, EMPTY_VALUE);
      }
   }

   return rates_total;
}
//+------------------------------------------------------------------+
