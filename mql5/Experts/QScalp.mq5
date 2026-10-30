//+------------------------------------------------------------------+
//| QScalp.mq5 - micro-account scalping EA skeleton for MetaTrader 5 |
//|                                                                  |
//| Flow per tick: spread sample -> day roll / daily halt / drawdown |
//| kill -> time stop -> on a new bar: signal -> risk guard -> CTrade |
//| market order with SL/TP attached on the broker's server.         |
//|                                                                  |
//| The guard mirrors qtrade/risk.py (same reason codes, same        |
//| defaults). The signal is the DEMO rule of pine/ and              |
//| qtrade/strategies.py: plumbing load, NOT an edge.                |
//|                                                                  |
//| Back-test: Strategy Tester, "Every tick based on real ticks",    |
//| with a non-zero execution delay. The economic calendar is not    |
//| available in the tester, so the news filter only works live.     |
//+------------------------------------------------------------------+
#property copyright "q-trade"
#property version   "0.10"
#property description "Fixed-USD risk, daily stop, drawdown kill, rate limits, spread guard, session/news filters."

#include <Trade\Trade.mqh>

input group "Risk (USD; converted to the account currency: USC = x100)"
input double InpRiskUSD            = 1.0;    // Risk per trade
input double InpDailyMaxLossUSD    = 4.0;    // Daily worst case: closed + floating + open risk + new trade
input double InpMaxDrawdownUSD     = 20.0;   // From peak equity: stop for good (0 = off)
input int    InpMaxTradesPerDay    = 20;
input int    InpMaxTradesPerHour   = 6;
input int    InpMinIntervalSec     = 60;
input int    InpMaxConsecLosses    = 3;
input int    InpCooldownMin        = 60;
input int    InpMaxPositions       = 1;
input double InpCommissionPerLotRT = 7.0;    // Account currency per 1.0 lot, round trip (not available via the API)
input double InpSizeOvershoot      = 0.0;    // Accept the minimum lot up to risk x (1 + this); 0 = never exceed
input double InpAccountPerUSD      = 0.0;    // Account units per USD (0 = auto: USD 1, USC 100)

input group "Spread and execution"
input int    InpMaxSpreadPoints = 15;        // Absolute cap, points (0 = off)
input double InpMaxSpreadToStop = 0.20;      // Spread / stop distance
input double InpSpreadSpikeMult = 2.0;       // Spread vs rolling median
input int    InpSpreadWindow    = 300;       // Ticks in the rolling median
input int    InpDeviationPoints = 10;        // Max slippage accepted on entry
input int    InpMaxHoldMin      = 30;        // Time stop (0 = off)

input group "Time filters (SERVER time; on most brokers server midnight = 17:00 New York)"
input int  InpSessionStartHour  = 9;         // ~07:00 UTC on a GMT+2/+3 server
input int  InpSessionEndHour    = 19;
input int  InpRolloverBeforeMin = 5;         // Block entries around server midnight (spreads blow out)
input int  InpRolloverAfterMin  = 65;
input bool InpNewsFilter        = true;      // MT5 economic calendar, high importance, live only
input int  InpNewsBeforeMin     = 5;
input int  InpNewsAfterMin      = 10;

input group "Demo signal: EMA cross + ATR on closed bars"
input ENUM_TIMEFRAMES InpTF   = PERIOD_M1;
input int    InpFast    = 9;
input int    InpSlow    = 21;
input int    InpAtrLen  = 14;
input double InpAtrMult = 1.5;
input double InpRR      = 1.5;

input group "Housekeeping"
input ulong InpMagic   = 20261030;
input bool  InpJournal = true;               // CSV in MQL5\Files (tester: the agent's sandbox)
input bool  InpNotify  = true;               // Push to the MT5 mobile app on halt / kill

CTrade   trade;
int      hFast = INVALID_HANDLE, hSlow = INVALID_HANDLE, hAtr = INVALID_HANDLE;
datetime g_lastBar = 0;
double   g_spr[];
int      g_sprCount = 0, g_sprPos = 0;
string   g_prefix;

//--- state that must survive restarts lives in terminal global variables (shared by every chart with this magic number)
double GV(const string k)              { return GlobalVariableCheck(g_prefix + k) ? GlobalVariableGet(g_prefix + k) : 0.0; }
void   GVSet(const string k, double v) { GlobalVariableSet(g_prefix + k, v); }

double AccountPerUSD()
  {
   if(InpAccountPerUSD > 0)
      return InpAccountPerUSD;
   string c = AccountInfoString(ACCOUNT_CURRENCY);
   return (c == "USC" || c == "USc") ? 100.0 : 1.0;
  }

double EquityUSD() { return AccountInfoDouble(ACCOUNT_EQUITY) / AccountPerUSD(); }

void Alarm(const string msg)
  {
   Print("QScalp: ", msg);
   if(InpNotify && !MQLInfoInteger(MQL_TESTER))
      SendNotification("QScalp " + _Symbol + ": " + msg);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpSlow <= InpFast || InpAtrLen < 2 || InpRiskUSD <= 0 || InpSpreadWindow < 10)
      return INIT_PARAMETERS_INCORRECT;
   string ccy = AccountInfoString(ACCOUNT_CURRENCY);
   if(InpAccountPerUSD <= 0 && ccy != "USD" && ccy != "USC" && ccy != "USc")
     {
      Print("QScalp: account currency ", ccy, ": set InpAccountPerUSD");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_prefix = "QS_" + IntegerToString((long)InpMagic) + "_";
   hFast = iMA(_Symbol, InpTF, InpFast, 0, MODE_EMA, PRICE_CLOSE);
   hSlow = iMA(_Symbol, InpTF, InpSlow, 0, MODE_EMA, PRICE_CLOSE);
   hAtr  = iATR(_Symbol, InpTF, InpAtrLen);
   if(hFast == INVALID_HANDLE || hSlow == INVALID_HANDLE || hAtr == INVALID_HANDLE)
      return INIT_FAILED;
   ArrayResize(g_spr, InpSpreadWindow);
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpDeviationPoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   g_lastBar = iTime(_Symbol, InpTF, 0);     // never act on a cross that happened before the EA started
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("QScalp: Algo Trading is OFF in the terminal: every order will fail (10027)");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   IndicatorRelease(hFast);
   IndicatorRelease(hSlow);
   IndicatorRelease(hAtr);
   GlobalVariablesFlush();
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk) || tk.bid <= 0)
      return;
   PushSpread((tk.ask - tk.bid) / _Point);
   UpdateDay();
   TimeStop();
   datetime bar = iTime(_Symbol, InpTF, 0);
   if(bar == 0 || bar == g_lastBar)
      return;
   g_lastBar = bar;                          // first tick of a new bar: the bar before it is final (no look-ahead)
   double slDist = 0;
   int dir = Signal(slDist);
   if(dir != 0)
      TryEnter(dir, slDist, tk);
  }

//--- day roll-over, daily halt, drawdown kill (equity based: includes floating P/L, commission and swap)
void UpdateDay()
  {
   double eq  = EquityUSD();
   double day = (double)(long)(TimeCurrent() / 86400);
   if(GV("day") != day)
     {
      GVSet("day", day);
      GVSet("dayEq", eq);
      GlobalVariablesFlush();
     }
   if(eq > GV("peak"))
      GVSet("peak", eq);
   if(GV("halt") != day && GV("dayEq") - eq >= InpDailyMaxLossUSD)
     {
      GVSet("halt", day);
      GlobalVariablesFlush();
      Alarm(StringFormat("daily loss limit hit (%.2f USD): no entries until the next server day", GV("dayEq") - eq));
     }
   if(InpMaxDrawdownUSD > 0 && GV("killed") == 0 && GV("peak") - eq >= InpMaxDrawdownUSD)
     {
      GVSet("killed", 1);
      GlobalVariablesFlush();
      Alarm(StringFormat("max drawdown hit (peak %.2f, equity %.2f): stopped. Delete global variable %skilled to re-arm",
                         GV("peak"), eq, g_prefix));
     }
  }

//--- rolling median spread (points)
void PushSpread(double pts)
  {
   g_spr[g_sprPos] = pts;
   g_sprPos = (g_sprPos + 1) % InpSpreadWindow;
   if(g_sprCount < InpSpreadWindow)
      g_sprCount++;
  }

double MedianSpread()
  {
   if(g_sprCount < 50)
      return -1;
   double tmp[];
   ArrayResize(tmp, g_sprCount);
   ArrayCopy(tmp, g_spr, 0, 0, g_sprCount);
   ArraySort(tmp);
   int m = g_sprCount / 2;
   return (g_sprCount % 2 == 1) ? tmp[m] : 0.5 * (tmp[m - 1] + tmp[m]);
  }

//--- demo signal: +1 buy, -1 sell, 0 none; slDist in price units
int Signal(double &slDist)
  {
   double f[], s[], a[];
   ArraySetAsSeries(f, true);
   ArraySetAsSeries(s, true);
   ArraySetAsSeries(a, true);
   if(CopyBuffer(hFast, 0, 1, 2, f) != 2 || CopyBuffer(hSlow, 0, 1, 2, s) != 2 || CopyBuffer(hAtr, 0, 1, 1, a) != 1)
      return 0;
   slDist = InpAtrMult * a[0];
   if(slDist <= 0)
      return 0;
   if(f[1] <= s[1] && f[0] > s[0])           // index 0 = last closed bar, 1 = the bar before it
      return +1;
   if(f[1] >= s[1] && f[0] < s[0])
      return -1;
   return 0;
  }

//--- our open positions (all symbols, this magic) and the money they can still lose at their stops, USD
int OurPositions(double &openRiskUSD)
  {
   int n = 0;
   openRiskUSD = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_MAGIC) != (long)InpMagic)
         continue;
      n++;
      ENUM_ORDER_TYPE type = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double vol = PositionGetDouble(POSITION_VOLUME), sl = PositionGetDouble(POSITION_SL), p = 0;
      if(sl > 0 && OrderCalcProfit(type, PositionGetString(POSITION_SYMBOL), vol, PositionGetDouble(POSITION_PRICE_OPEN), sl, p))
         openRiskUSD += MathMax(0.0, -p + InpCommissionPerLotRT * vol) / AccountPerUSD();
      else
         openRiskUSD += InpRiskUSD;           // no stop on the server?! count a full R
     }
   return n;
  }

//--- counts come from the account history, so they survive restarts without any state of our own
int EntriesSince(datetime from, datetime &last)
  {
   last = 0;
   int n = 0;
   if(!HistorySelect(from, TimeCurrent() + 60))
      return 0;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong d = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != (long)InpMagic || HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_IN)
         continue;
      n++;
      datetime t = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
      if(t > last)
         last = t;
     }
   return n;
  }

// Losing exits in a row (latest first). Stateless, so slightly stricter than qtrade/risk.py: once the streak is long enough,
// EVERY further loss restarts the cooldown until a win resets it.
int LosingStreak(datetime &lastLoss)
  {
   lastLoss = 0;
   int n = 0;
   if(!HistorySelect(TimeCurrent() - 3 * 86400, TimeCurrent() + 60))
      return 0;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong d = HistoryDealGetTicket(i);
      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != (long)InpMagic || (entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY))
         continue;
      double pnl = HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP) + HistoryDealGetDouble(d, DEAL_COMMISSION);
      if(pnl >= 0)
         break;
      if(lastLoss == 0)
         lastLoss = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
      n++;
     }
   return n;
  }

bool InSession(const MqlDateTime &dt)
  {
   if(InpSessionStartHour == InpSessionEndHour)
      return true;
   if(InpSessionStartHour < InpSessionEndHour)
      return dt.hour >= InpSessionStartHour && dt.hour < InpSessionEndHour;
   return dt.hour >= InpSessionStartHour || dt.hour < InpSessionEndHour;  // window across midnight
  }

bool InRollover(const MqlDateTime &dt)
  {
   int m = dt.hour * 60 + dt.min;
   return m < InpRolloverAfterMin || m >= 1440 - InpRolloverBeforeMin;
  }

bool NewsBlocked()
  {
   if(!InpNewsFilter || MQLInfoInteger(MQL_TESTER))
      return false;                           // the calendar does not exist in the tester
   datetime now = TimeTradeServer();          // calendar times are trade-server times
   string ccys[2];
   ccys[0] = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_BASE);
   ccys[1] = SymbolInfoString(_Symbol, SYMBOL_CURRENCY_PROFIT);
   for(int c = 0; c < 2; c++)
     {
      MqlCalendarValue v[];
      if(!CalendarValueHistory(v, now - InpNewsAfterMin * 60, now + InpNewsBeforeMin * 60, NULL, ccys[c]))
         continue;
      for(int i = 0; i < ArraySize(v); i++)
        {
         MqlCalendarEvent ev;
         if(CalendarEventById(v[i].event_id, ev) && ev.importance == CALENDAR_IMPORTANCE_HIGH)
            return true;
        }
     }
   return false;
  }

//--- largest volume whose loss at the stop (commission included) fits the budget; 0 when even the minimum does not fit
double VolumeForRisk(ENUM_ORDER_TYPE type, double price, double sl, double riskAcc, double &riskOutAcc)
  {
   double p = 0;
   riskOutAcc = 0;
   if(!OrderCalcProfit(type, _Symbol, 1.0, price, sl, p) || p >= 0)
      return 0;
   double perLot = -p + InpCommissionPerLotRT;
   double vmin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vstep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double vol = MathMin(MathFloor(riskAcc / perLot / vstep + 1e-9) * vstep, vmax);
   if(vol < vmin - 1e-9)
     {
      riskOutAcc = vmin * perLot;              // NEVER round up to the minimum past the budget
      return riskOutAcc <= riskAcc * (1 + InpSizeOvershoot) + 1e-9 ? vmin : 0;
     }
   riskOutAcc = vol * perLot;
   int digits = (int)MathMax(0, MathCeil(-MathLog10(vstep) - 1e-9));
   return NormalizeDouble(vol, digits);
  }

//--- the gate: every failing check is recorded, then one market order with SL/TP on the server
void TryEnter(int dir, double slDist, const MqlTick &tk)
  {
   datetime now = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(now, dt);
   double k = AccountPerUSD(), eq = EquityUSD(), day = (double)(long)(now / 86400);
   string why = "";

   if(FileIsExist("QScalp.KILL", FILE_COMMON))
      why += "KILL_SWITCH,";                  // create Common\Files\QScalp.KILL to stop all entries
   if(GV("killed") > 0)
      why += "KILLED,";
   if(GV("halt") == day)
      why += "DAILY_HALT,";
   datetime last = 0, lastLoss = 0;
   if(EntriesSince((datetime)((long)day * 86400), last) >= InpMaxTradesPerDay)
      why += "MAX_TRADES_DAY,";
   if(EntriesSince(now - 3600, last) >= InpMaxTradesPerHour)
      why += "RATE_WINDOW,";
   if(last > 0 && now - last < InpMinIntervalSec)
      why += "MIN_INTERVAL,";
   if(LosingStreak(lastLoss) >= InpMaxConsecLosses && now < lastLoss + InpCooldownMin * 60)
      why += "COOLDOWN,";
   double openRisk = 0;
   if(OurPositions(openRisk) >= InpMaxPositions)
      why += "MAX_POSITIONS,";

   double spread = tk.ask - tk.bid, spreadPts = spread / _Point, med = MedianSpread();
   if(InpMaxSpreadPoints > 0 && spreadPts > InpMaxSpreadPoints)
      why += "SPREAD_ABS,";
   if(spread > InpMaxSpreadToStop * slDist)
      why += "SPREAD_TO_STOP,";
   if(med > 0 && spreadPts > InpSpreadSpikeMult * med)
      why += "SPREAD_SPIKE,";
   long stopsLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsLevel > 0 && slDist < stopsLevel * _Point)
      why += "STOPS_LEVEL,";
   if(!InSession(dt))
      why += "OFF_SESSION,";
   if(InRollover(dt))
      why += "ROLLOVER,";
   if(NewsBlocked())
      why += "NEWS,";

   ENUM_ORDER_TYPE type = dir > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double price  = dir > 0 ? tk.ask : tk.bid;
   double anchor = dir > 0 ? tk.bid : tk.ask; // stops measured from the exit side, as in qtrade/router.py
   double sl = NormalizeDouble(anchor - dir * slDist, _Digits);
   double tp = NormalizeDouble(anchor + dir * slDist * InpRR, _Digits);
   double riskAcc = 0;
   double vol = VolumeForRisk(type, price, sl, InpRiskUSD * k, riskAcc);
   if(vol <= 0)
      why += "MIN_LOT_RISK,";                 // the journal's risk column shows what the minimum lot would risk
   if((GV("dayEq") - eq) + openRisk + riskAcc / k > InpDailyMaxLossUSD + 1e-9)
      why += "DAILY_LOSS,";

   if(why != "")
     {
      Journal("REJECT", dir, vol, price, sl, tp, spreadPts, riskAcc / k, 0, 0, why);
      return;
     }
   ulong t0 = GetMicrosecondCount();
   bool sent = dir > 0 ? trade.Buy(vol, _Symbol, price, sl, tp, "qscalp") : trade.Sell(vol, _Symbol, price, sl, tp, "qscalp");
   double us = (double)(GetMicrosecondCount() - t0);
   uint rc = trade.ResultRetcode();
   if(sent && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_DONE_PARTIAL))
     {
      ulong order = trade.ResultOrder();      // POSITION_IDENTIFIER = ticket of the order that opened the position
      if(order > 0)
         GVSet("r_" + IntegerToString((long)order), riskAcc / k);  // for R at the close
      Journal("PLACED", dir, trade.ResultVolume(), trade.ResultPrice(), sl, tp, spreadPts, riskAcc / k, 0, us,
              IntegerToString(rc) + " slip " + DoubleToString((trade.ResultPrice() - price) * dir / _Point, 1) + "pt");
     }
   else
      Journal("FAILED", dir, vol, price, sl, tp, spreadPts, riskAcc / k, 0, us, IntegerToString(rc) + " " + trade.ResultRetcodeDescription());
  }

//--- time stop
void TimeStop()
  {
   if(InpMaxHoldMin <= 0)
      return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_MAGIC) != (long)InpMagic || PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(TimeCurrent() - (datetime)PositionGetInteger(POSITION_TIME) >= InpMaxHoldMin * 60)
         trade.PositionClose(t);
     }
  }

//--- closes: realised P/L of the whole position (all its deals), and R against the risk recorded at entry
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || !HistoryDealSelect(trans.deal))
      return;
   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != (long)InpMagic)
      return;
   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY)
      return;
   long   pos   = HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   double vol   = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
   double price = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
   int    dir   = HistoryDealGetInteger(trans.deal, DEAL_TYPE) == DEAL_TYPE_SELL ? +1 : -1;  // a sell closes a buy
   double pnl = 0;
   if(HistorySelectByPosition(pos))
      for(int i = 0; i < HistoryDealsTotal(); i++)
        {
         ulong d = HistoryDealGetTicket(i);
         pnl += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_COMMISSION) +
                HistoryDealGetDouble(d, DEAL_SWAP) + HistoryDealGetDouble(d, DEAL_FEE);
        }
   string key = "r_" + IntegerToString(pos);
   double risk = GV(key);
   GlobalVariableDel(g_prefix + key);
   Journal("CLOSE", dir, vol, price, 0, 0, 0, risk, pnl / AccountPerUSD(), 0, IntegerToString(pos));
  }

//--- append-only CSV journal: the same columns the forward judge needs (R per closed trade)
void Journal(const string ev, int dir, double vol, double price, double sl, double tp, double spreadPts, double riskUSD,
             double pnlUSD, double us, const string note)
  {
   if(!InpJournal)
      return;
   int h = FileOpen("QScalp_" + _Symbol + ".csv", FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, ',');
   if(h == INVALID_HANDLE)
      return;
   if(FileSize(h) == 0)
      FileWrite(h, "time", "event", "side", "volume", "price", "sl", "tp", "spread_pts", "risk_usd", "pnl_usd", "r", "exec_us", "note");
   FileSeek(h, 0, SEEK_END);
   double r = (ev == "CLOSE" && riskUSD > 0) ? pnlUSD / riskUSD : 0;
   FileWrite(h, TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS), ev, dir > 0 ? "buy" : "sell", DoubleToString(vol, 2),
             DoubleToString(price, _Digits), DoubleToString(sl, _Digits), DoubleToString(tp, _Digits), DoubleToString(spreadPts, 1),
             DoubleToString(riskUSD, 4), DoubleToString(pnlUSD, 4), DoubleToString(r, 4), DoubleToString(us, 0), note);
   FileClose(h);
  }
//+------------------------------------------------------------------+
