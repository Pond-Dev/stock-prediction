//+------------------------------------------------------------------+
//|                                          TgxmEmaRsiAtrDemo.mq5   |
//|  Demo-only Expert Advisor for the EMA/RSI/ATR rule set.           |
//|                                                                  |
//|  Same rules as pine/ema_rsi_atr_advisory.pine and the Python      |
//|  `tgxm autotrade` worker: an EMA crossover on a CLOSED bar,       |
//|  confirmed by RSI and by a higher timeframe, with ATR-derived     |
//|  protection.  It runs inside the terminal, so no external process |
//|  is required, but it keeps the same hard limits:                  |
//|                                                                  |
//|    * refuses to run on anything but a Demo account                |
//|    * one fixed lot under a hard cap that it never raises          |
//|    * a numeric Stop Loss on every entry, never removed            |
//|    * one position per symbol, a cooldown, and a per-day cap       |
//|    * a magic number of its own, so it can only ever manage the    |
//|      positions it opened                                          |
//|                                                                  |
//|  It does not read Telegram and shares no state with the Python    |
//|  bot; run one or the other on a given symbol, not both.           |
//+------------------------------------------------------------------+
#property copyright "tgxm"
#property version   "1.00"
#property description "EMA/RSI/ATR crossover strategy, Demo accounts only."

#include <Trade\Trade.mqh>

//--- rule parameters: keep these identical to the Pine inputs -------
input group                "Rule (matches the Pine script)"
input int                  EmaFastPeriod        = 20;      // EMA Fast Period
input int                  EmaSlowPeriod        = 50;      // EMA Slow Period
input int                  RsiPeriod            = 14;      // RSI Period
input int                  RsiOverbought        = 70;      // RSI Overbought
input int                  RsiOversold          = 30;      // RSI Oversold
input int                  AtrPeriod            = 14;      // ATR Period
input double               AtrStopLossMult      = 1.5;     // ATR Stop-Loss Multiplier
input double               AtrTakeProfit1Mult   = 1.5;     // ATR Take-Profit 1 (breakeven trigger)
input double               AtrTakeProfit2Mult   = 3.0;     // ATR Take-Profit 2 (order target)
input double               TakeProfitMoney      = 0.0;     // Fixed profit target in account currency (0 = use ATR)
input bool                 UseRsiFilter         = true;    // Require RSI confirmation
input bool                 UseHigherTimeframe   = true;    // Require higher-timeframe agreement
input ENUM_TIMEFRAMES      HigherTimeframe      = PERIOD_CURRENT; // Higher timeframe (CURRENT = preset ladder)
input bool                 MoveToBreakeven      = true;    // Move Stop Loss to breakeven after TP1
input bool                 CloseOnOppositeCross = true;    // Close when the EMAs cross back

input group                "Risk and safety"
input bool                 DemoAccountsOnly     = true;    // Refuse to run on a non-Demo account
input long                 AllowedAccountLogin  = 0;       // Exact account login (0 = any Demo account)
input double               FixedLot             = 0.01;    // Fixed volume
input double               HardLotCap           = 0.01;    // Hard cap; volume is never raised above this
input int                  MaxOpenPositions     = 1;       // Own positions allowed on this symbol
input bool                 BlockOnForeignTrades = true;    // Block while a position this EA does not own is open
input int                  CooldownBars         = 3;       // Bars to wait after an entry
input int                  MaxTradesPerDay      = 10;      // Entries per server-time day
input int                  MaxSpreadPoints      = 60;      // Skip an entry above this spread
input int                  DeviationPoints      = 20;      // Maximum slippage
input ulong                MagicNumber          = 26082703; // Ownership tag for this EA

//--- state ---------------------------------------------------------
CTrade         g_trade;
int            g_emaFastHandle    = INVALID_HANDLE;
int            g_emaSlowHandle    = INVALID_HANDLE;
int            g_rsiHandle        = INVALID_HANDLE;
int            g_atrHandle        = INVALID_HANDLE;
int            g_htfFastHandle    = INVALID_HANDLE;
int            g_htfSlowHandle    = INVALID_HANDLE;
ENUM_TIMEFRAMES g_higherTimeframe = PERIOD_CURRENT;
datetime       g_lastEvaluatedBar = 0;
datetime       g_lastCloseErrorBar = 0;

//+------------------------------------------------------------------+
//| The Pine preset ladder: the higher timeframe a trader checks next |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES LadderRung(const ENUM_TIMEFRAMES chart)
  {
   switch(chart)
     {
      case PERIOD_M1:
      case PERIOD_M2:
      case PERIOD_M3:  return(PERIOD_M15);
      case PERIOD_M5:
      case PERIOD_M10:
      case PERIOD_M15: return(PERIOD_H1);
      case PERIOD_M30:
      case PERIOD_H1:  return(PERIOD_H4);
      case PERIOD_H2:
      case PERIOD_H3:
      case PERIOD_H4:  return(PERIOD_D1);
      case PERIOD_D1:  return(PERIOD_W1);
      case PERIOD_W1:  return(PERIOD_MN1);
      default:         return(PERIOD_CURRENT);
     }
  }

//+------------------------------------------------------------------+
//| Account identity gate: this EA is for Demo accounts only         |
//+------------------------------------------------------------------+
bool AccountIsAllowed(string &reason)
  {
   const long tradeMode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(DemoAccountsOnly && tradeMode != ACCOUNT_TRADE_MODE_DEMO)
     {
      reason = "this EA runs on Demo accounts only";
      return(false);
     }
   if(AllowedAccountLogin > 0 && AccountInfoInteger(ACCOUNT_LOGIN) != AllowedAccountLogin)
     {
      reason = "active account login is not the allowed one";
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Trading permission gate, re-checked before every entry           |
//+------------------------------------------------------------------+
bool TradingIsPermitted(string &reason)
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
     {
      reason = "Algo Trading is switched off in the terminal";
      return(false);
     }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      reason = "algorithmic trading is not allowed for this chart";
      return(false);
     }
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
     {
      reason = "the account does not allow Expert Advisor trading";
      return(false);
     }
   if(SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
     {
      reason = "this symbol does not allow full trading right now";
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Volume the EA is allowed to send: never rounded up past the cap  |
//+------------------------------------------------------------------+
bool ResolveVolume(double &volume, string &reason)
  {
   const double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   const double minimum= SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double maximum= SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(FixedLot <= 0.0 || HardLotCap <= 0.0)
     {
      reason = "FixedLot and HardLotCap must be positive";
      return(false);
     }
   if(FixedLot > HardLotCap)
     {
      reason = "FixedLot exceeds HardLotCap";
      return(false);
     }
   if(step <= 0.0)
     {
      reason = "broker volume step is unavailable";
      return(false);
     }
   // Round DOWN onto the broker's step: rounding up could exceed the cap.
   double candidate = MathFloor(FixedLot / step + 0.0000001) * step;
   candidate = NormalizeDouble(candidate, 8);
   if(candidate < minimum || candidate > maximum || candidate > HardLotCap)
     {
      reason = StringFormat("volume %.8f is outside broker/cap limits", candidate);
      return(false);
     }
   volume = candidate;
   return(true);
  }

//+------------------------------------------------------------------+
//| Price distance that is worth `money` on `volume` of this symbol   |
//|                                                                  |
//| Used when the target is stated in account currency instead of in  |
//| ATR multiples.                                                    |
//|                                                                  |
//| Measured with OrderCalcProfit rather than SYMBOL_TRADE_TICK_VALUE |
//| on purpose: that field disagrees with the terminal's own profit   |
//| calculation on some servers (MetaQuotes-Demo reports 0.1 for      |
//| XAUUSD where the calculation gives 1.0), which would put the      |
//| target ten times too far away.  The calculator is what actually   |
//| settles the trade, so it is the only figure worth trusting.       |
//+------------------------------------------------------------------+
bool MoneyTargetDistance(const double volume, const double money, double &distance)
  {
   distance = 0.0;
   if(volume <= 0.0 || money <= 0.0)
      return(false);
   const double price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(price <= 0.0)
      return(false);
   const double probe = 1.0;
   double profit = 0.0;
   if(!OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, volume, price, price + probe, profit))
      return(false);
   if(profit <= 0.0)
      return(false);
   distance = money * probe / profit;
   return(distance > 0.0);
  }

//+------------------------------------------------------------------+
//| Indicator reads, all on CLOSED bars                              |
//+------------------------------------------------------------------+
bool ReadPair(const int handle, const int count, double &values[])
  {
   ArraySetAsSeries(values, true);
   // start_pos 1 skips the bar that is still forming.
   return(CopyBuffer(handle, 0, 1, count, values) == count);
  }

//+------------------------------------------------------------------+
//| Own positions on this symbol                                     |
//+------------------------------------------------------------------+
int CountPositions(int &foreign)
  {
   int owned = 0;
   foreign = 0;
   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      const ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         owned++;
      else
         foreign++;
     }
   return(owned);
  }

//+------------------------------------------------------------------+
//| Entry history for the cooldown and the per-day cap               |
//|                                                                  |
//| Read back from deal history rather than kept in memory, so a     |
//| restart cannot reset either limit.                               |
//+------------------------------------------------------------------+
bool ScanEntryHistory(datetime &lastEntry, int &todayCount)
  {
   lastEntry  = 0;
   todayCount = 0;

   MqlDateTime parts;
   TimeToStruct(TimeCurrent(), parts);
   parts.hour = 0;
   parts.min  = 0;
   parts.sec  = 0;
   const datetime dayStart = StructToTime(parts);
   const datetime from     = dayStart - 14 * 86400;

   if(!HistorySelect(from, TimeCurrent() + 60))
      return(false);

   for(int index = HistoryDealsTotal() - 1; index >= 0; index--)
     {
      const ulong ticket = HistoryDealGetTicket(index);
      if(ticket == 0)
         continue;
      if((ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_IN)
         continue;
      const datetime dealTime = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      if(dealTime > lastEntry)
         lastEntry = dealTime;
      if(dealTime >= dayStart)
         todayCount++;
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Protective prices the broker will accept                         |
//+------------------------------------------------------------------+
bool ProtectionIsValid(const bool isBuy, const double stopLoss, const double takeProfit)
  {
   const double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const long   stops  = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   const double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   const double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   const double quote  = isBuy ? bid : ask;
   const double minimum= stops * point;

   if(stopLoss <= 0.0)
      return(false);
   if(isBuy && !(stopLoss < quote))
      return(false);
   if(!isBuy && !(stopLoss > quote))
      return(false);
   if(takeProfit > 0.0)
     {
      if(isBuy && !(takeProfit > quote))
         return(false);
      if(!isBuy && !(takeProfit < quote))
         return(false);
     }
   if(minimum > 0.0)
     {
      if(MathAbs(quote - stopLoss) < minimum)
         return(false);
      if(takeProfit > 0.0 && MathAbs(quote - takeProfit) < minimum)
         return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| In-trade rules: breakeven after TP1, close on the opposite cross |
//+------------------------------------------------------------------+
void ManageOpenPositions(const bool crossedUp, const bool crossedDown)
  {
   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      const ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber)
         continue;  // never touch a position this EA did not open

      const bool   isBuy      = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      const double entry      = PositionGetDouble(POSITION_PRICE_OPEN);
      const double stopLoss   = PositionGetDouble(POSITION_SL);
      const double takeProfit = PositionGetDouble(POSITION_TP);

      if(CloseOnOppositeCross && ((isBuy && crossedDown) || (!isBuy && crossedUp)))
        {
         // The cross stays reported for the whole bar after it happened, so a
         // failed close is retried within that bar and then left alone: the
         // position keeps its broker-side Stop Loss either way.  The failure
         // is logged once per bar rather than once per tick.
         if(!g_trade.PositionClose(ticket, DeviationPoints))
           {
            const datetime bar = iTime(_Symbol, PERIOD_CURRENT, 0);
            if(bar != g_lastCloseErrorBar)
              {
               g_lastCloseErrorBar = bar;
               PrintFormat("close on reverse failed for #%I64u: %s (%u)",
                           ticket, g_trade.ResultRetcodeDescription(), g_trade.ResultRetcode());
              }
           }
         continue;
        }

      if(!MoveToBreakeven || takeProfit <= 0.0 || AtrTakeProfit2Mult <= 0.0)
         continue;

      const double digitsEntry = NormalizeDouble(entry, _Digits);
      if(MathAbs(NormalizeDouble(stopLoss, _Digits) - digitsEntry) < SymbolInfoDouble(_Symbol, SYMBOL_POINT))
         continue;  // already at breakeven

      // The breakeven trigger is derived from the order's own target as a
      // fraction of the distance to it, so it survives a restart with no extra
      // state and stays correct whether that target came from the ATR
      // multiples or from a fixed money amount.
      const double span   = MathAbs(takeProfit - entry) * (AtrTakeProfit1Mult / AtrTakeProfit2Mult);
      const double target = isBuy ? entry + span : entry - span;
      const double quote  = isBuy ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                                  : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      const bool   reached= isBuy ? (quote >= target) : (quote <= target);
      if(!reached)
         continue;
      if(!ProtectionIsValid(isBuy, digitsEntry, takeProfit))
         continue;  // price came back; the original stop stays in place
      if(!g_trade.PositionModify(ticket, digitsEntry, takeProfit))
         PrintFormat("breakeven stop failed for #%I64u: %s (%u)",
                     ticket, g_trade.ResultRetcodeDescription(), g_trade.ResultRetcode());
      else
         PrintFormat("#%I64u reached the first target; stop moved to breakeven %.*f",
                     ticket, _Digits, digitsEntry);
     }
  }

//+------------------------------------------------------------------+
//| Initialisation                                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   string reason = "";
   if(!AccountIsAllowed(reason))
     {
      PrintFormat("refusing to start: %s", reason);
      return(INIT_FAILED);
     }
   if(EmaFastPeriod < 1 || EmaSlowPeriod < 2 || EmaFastPeriod >= EmaSlowPeriod)
     {
      Print("refusing to start: EmaFastPeriod must be below EmaSlowPeriod");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(RsiPeriod < 2 || AtrPeriod < 2)
     {
      Print("refusing to start: RSI and ATR periods must be at least 2");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(RsiOverbought <= 50 || RsiOversold >= 50)
     {
      Print("refusing to start: the RSI bands must sit either side of 50");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(AtrStopLossMult <= 0.0 || AtrTakeProfit1Mult <= 0.0 || AtrTakeProfit2Mult <= 0.0)
     {
      Print("refusing to start: ATR multipliers must be positive");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(TakeProfitMoney < 0.0)
     {
      Print("refusing to start: TakeProfitMoney cannot be negative");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(MoveToBreakeven && AtrTakeProfit1Mult >= AtrTakeProfit2Mult)
     {
      Print("refusing to start: the breakeven trigger must sit below the order target");
      return(INIT_PARAMETERS_INCORRECT);
     }
   double volume = 0.0;
   if(!ResolveVolume(volume, reason))
     {
      PrintFormat("refusing to start: %s", reason);
      return(INIT_PARAMETERS_INCORRECT);
     }

   g_higherTimeframe = (HigherTimeframe == PERIOD_CURRENT)
                       ? LadderRung((ENUM_TIMEFRAMES)Period())
                       : HigherTimeframe;
   if(UseHigherTimeframe)
     {
      if(g_higherTimeframe == PERIOD_CURRENT ||
         PeriodSeconds(g_higherTimeframe) <= PeriodSeconds((ENUM_TIMEFRAMES)Period()))
        {
         Print("refusing to start: the higher timeframe must be strictly above the chart");
         return(INIT_PARAMETERS_INCORRECT);
        }
     }

   g_emaFastHandle = iMA(_Symbol, PERIOD_CURRENT, EmaFastPeriod, 0, MODE_EMA, PRICE_CLOSE);
   g_emaSlowHandle = iMA(_Symbol, PERIOD_CURRENT, EmaSlowPeriod, 0, MODE_EMA, PRICE_CLOSE);
   g_rsiHandle     = iRSI(_Symbol, PERIOD_CURRENT, RsiPeriod, PRICE_CLOSE);
   g_atrHandle     = iATR(_Symbol, PERIOD_CURRENT, AtrPeriod);
   if(UseHigherTimeframe)
     {
      g_htfFastHandle = iMA(_Symbol, g_higherTimeframe, EmaFastPeriod, 0, MODE_EMA, PRICE_CLOSE);
      g_htfSlowHandle = iMA(_Symbol, g_higherTimeframe, EmaSlowPeriod, 0, MODE_EMA, PRICE_CLOSE);
     }
   if(g_emaFastHandle == INVALID_HANDLE || g_emaSlowHandle == INVALID_HANDLE ||
      g_rsiHandle == INVALID_HANDLE || g_atrHandle == INVALID_HANDLE ||
      (UseHigherTimeframe && (g_htfFastHandle == INVALID_HANDLE || g_htfSlowHandle == INVALID_HANDLE)))
     {
      Print("refusing to start: an indicator handle could not be created");
      return(INIT_FAILED);
     }

   g_trade.SetExpertMagicNumber(MagicNumber);
   g_trade.SetDeviationInPoints(DeviationPoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   string target = StringFormat("ATR x %.2f", AtrTakeProfit2Mult);
   if(TakeProfitMoney > 0.0)
     {
      double distance = 0.0;
      if(!MoneyTargetDistance(volume, TakeProfitMoney, distance))
        {
         Print("refusing to start: TakeProfitMoney cannot be converted to a price on this symbol");
         return(INIT_PARAMETERS_INCORRECT);
        }
      target = StringFormat("%.2f %s (%.*f of price)", TakeProfitMoney,
                            AccountInfoString(ACCOUNT_CURRENCY), _Digits, distance);
     }
   PrintFormat("started on %s %s | RSI filter %s | higher timeframe %s | volume %.2f | target %s | magic %I64u | account %I64d (%s)",
               _Symbol, EnumToString((ENUM_TIMEFRAMES)Period()),
               UseRsiFilter ? "on" : "off",
               UseHigherTimeframe ? EnumToString(g_higherTimeframe) : "off",
               volume, target, MagicNumber, AccountInfoInteger(ACCOUNT_LOGIN),
               AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO ? "Demo" : "NOT DEMO");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Shutdown                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_emaFastHandle != INVALID_HANDLE) IndicatorRelease(g_emaFastHandle);
   if(g_emaSlowHandle != INVALID_HANDLE) IndicatorRelease(g_emaSlowHandle);
   if(g_rsiHandle     != INVALID_HANDLE) IndicatorRelease(g_rsiHandle);
   if(g_atrHandle     != INVALID_HANDLE) IndicatorRelease(g_atrHandle);
   if(g_htfFastHandle != INVALID_HANDLE) IndicatorRelease(g_htfFastHandle);
   if(g_htfSlowHandle != INVALID_HANDLE) IndicatorRelease(g_htfSlowHandle);
   PrintFormat("stopped (reason %d); open positions keep their broker-side Stop Loss", reason);
  }

//+------------------------------------------------------------------+
//| Main loop                                                        |
//+------------------------------------------------------------------+
void OnTick()
  {
   double emaFast[], emaSlow[];
   if(!ReadPair(g_emaFastHandle, 2, emaFast) || !ReadPair(g_emaSlowHandle, 2, emaSlow))
      return;  // history is still loading

   // Index 0 is the last CLOSED bar, index 1 the one before it: exactly the
   // two points ta.crossover() compares.
   const bool crossedUp   = (emaFast[1] <= emaSlow[1]) && (emaFast[0] > emaSlow[0]);
   const bool crossedDown = (emaFast[1] >= emaSlow[1]) && (emaFast[0] < emaSlow[0]);

   ManageOpenPositions(crossedUp, crossedDown);

   // Entries are decided once per closed bar, never intrabar.
   const datetime currentBar = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(currentBar == 0 || currentBar == g_lastEvaluatedBar)
      return;

   if(!crossedUp && !crossedDown)
     {
      g_lastEvaluatedBar = currentBar;
      return;
     }

   string reason = "";
   if(!AccountIsAllowed(reason) || !TradingIsPermitted(reason))
     {
      PrintFormat("skipping the crossover on this bar: %s", reason);
      g_lastEvaluatedBar = currentBar;
      return;
     }

   double rsi[], atr[];
   if(!ReadPair(g_rsiHandle, 1, rsi) || !ReadPair(g_atrHandle, 1, atr))
      return;
   if(atr[0] <= 0.0)
     {
      g_lastEvaluatedBar = currentBar;
      return;
     }

   const bool isBuy = crossedUp;
   if(UseRsiFilter)
     {
      const bool rsiOk = isBuy ? (rsi[0] > 50.0 && rsi[0] < RsiOverbought)
                               : (rsi[0] < 50.0 && rsi[0] > RsiOversold);
      if(!rsiOk)
        {
         PrintFormat("%s crossover blocked: RSI %.2f is outside the band",
                     isBuy ? "up" : "down", rsi[0]);
         g_lastEvaluatedBar = currentBar;
         return;
        }
     }

   if(UseHigherTimeframe)
     {
      double htfFast[], htfSlow[];
      if(!ReadPair(g_htfFastHandle, 1, htfFast) || !ReadPair(g_htfSlowHandle, 1, htfSlow))
         return;  // an unjudgeable higher timeframe blocks, it does not pass
      const bool htfBullish = htfFast[0] > htfSlow[0];
      const bool htfBearish = htfFast[0] < htfSlow[0];
      if((isBuy && !htfBullish) || (!isBuy && !htfBearish))
        {
         PrintFormat("%s crossover blocked: %s disagrees",
                     isBuy ? "up" : "down", EnumToString(g_higherTimeframe));
         g_lastEvaluatedBar = currentBar;
         return;
        }
     }

   g_lastEvaluatedBar = currentBar;

   const long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(MaxSpreadPoints > 0 && spread > MaxSpreadPoints)
     {
      PrintFormat("entry skipped: spread %d points is above the %d limit",
                  (int)spread, MaxSpreadPoints);
      return;
     }

   int foreign = 0;
   const int owned = CountPositions(foreign);
   if(owned >= MaxOpenPositions)
     {
      PrintFormat("entry skipped: %d position(s) of this EA are already open", owned);
      return;
     }
   if(BlockOnForeignTrades && foreign > 0)
     {
      PrintFormat("entry skipped: %d position(s) on %s are not owned by this EA",
                  foreign, _Symbol);
      return;
     }

   datetime lastEntry = 0;
   int todayCount = 0;
   if(!ScanEntryHistory(lastEntry, todayCount))
     {
      Print("entry skipped: deal history could not be read");
      return;
     }
   if(MaxTradesPerDay > 0 && todayCount >= MaxTradesPerDay)
     {
      PrintFormat("entry skipped: %d entries already taken today", todayCount);
      return;
     }
   if(CooldownBars > 0 && lastEntry > 0)
     {
      const int cooldownSeconds = CooldownBars * PeriodSeconds((ENUM_TIMEFRAMES)Period());
      if(currentBar - lastEntry < cooldownSeconds)
        {
         PrintFormat("entry skipped: still inside the %d-bar cooldown", CooldownBars);
         return;
        }
     }

   double volume = 0.0;
   if(!ResolveVolume(volume, reason))
     {
      PrintFormat("entry skipped: %s", reason);
      return;
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.bid <= 0.0 || tick.ask <= 0.0)
     {
      Print("entry skipped: no usable quote");
      return;
     }

   // Protection is measured from the signal bar's close, exactly as the Pine
   // script draws it, then validated against the live quote.
   const double reference  = iClose(_Symbol, PERIOD_CURRENT, 1);
   const double stopLoss   = NormalizeDouble(isBuy ? reference - atr[0] * AtrStopLossMult
                                                   : reference + atr[0] * AtrStopLossMult, _Digits);
   double targetDistance = atr[0] * AtrTakeProfit2Mult;
   if(TakeProfitMoney > 0.0 && !MoneyTargetDistance(volume, TakeProfitMoney, targetDistance))
     {
      Print("entry skipped: the money target could not be converted to a price");
      return;
     }
   const double takeProfit = NormalizeDouble(isBuy ? reference + targetDistance
                                                   : reference - targetDistance, _Digits);
   if(!ProtectionIsValid(isBuy, stopLoss, takeProfit))
     {
      PrintFormat("entry skipped: price left the zone where SL %.*f / TP %.*f are valid",
                  _Digits, stopLoss, _Digits, takeProfit);
      return;
     }

   const string comment = StringFormat("tgxm-ea-%s", isBuy ? "buy" : "sell");
   const bool sent = isBuy
                     ? g_trade.Buy(volume, _Symbol, tick.ask, stopLoss, takeProfit, comment)
                     : g_trade.Sell(volume, _Symbol, tick.bid, stopLoss, takeProfit, comment);
   if(!sent)
     {
      PrintFormat("order rejected: %s (%u); nothing is retried on this bar",
                  g_trade.ResultRetcodeDescription(), g_trade.ResultRetcode());
      return;
     }
   // State the risk and reward in money, so the log shows what was actually
   // committed rather than only the prices it was derived from.
   const double filled = g_trade.ResultPrice();
   const ENUM_ORDER_TYPE side = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double atRisk = 0.0, atTarget = 0.0;
   if(!OrderCalcProfit(side, _Symbol, volume, filled, stopLoss, atRisk))
      atRisk = 0.0;    // reporting only; the order itself is already placed
   if(!OrderCalcProfit(side, _Symbol, volume, filled, takeProfit, atTarget))
      atTarget = 0.0;
   PrintFormat("%s %.2f %s at %.*f | SL %.*f (%.2f %s) | TP %.*f (%+.2f %s) | RSI %.2f | ATR %.*f | order #%I64u",
               isBuy ? "BUY" : "SELL", volume, _Symbol,
               _Digits, filled, _Digits, stopLoss, atRisk, AccountInfoString(ACCOUNT_CURRENCY),
               _Digits, takeProfit, atTarget, AccountInfoString(ACCOUNT_CURRENCY),
               rsi[0], _Digits, atr[0], g_trade.ResultOrder());
  }
//+------------------------------------------------------------------+
