#property strict
#property version "1.010"
#property description "Independent AI operator with exact account-mode binding. Python bridge required."

// Self-contained execution wrapper: no terminal-local include dependency.
class BrokerTrade
{
private:
   ulong magic,deviation;
   ENUM_ORDER_TYPE_FILLING filling;
   MqlTradeResult result;
   bool Send(MqlTradeRequest &r)
   {
      ZeroMemory(result); MqlTradeCheckResult check={};
      if(!OrderCheck(r,check)) { result.retcode=check.retcode; result.comment="OrderCheck: "+check.comment; return false; }
      return OrderSend(r,result);
   }
public:
   void SetExpertMagicNumber(ulong value) { magic=value; }
   void SetAsyncMode(bool value) { }
   void SetDeviationInPoints(ulong value) { deviation=value; }
   void SetTypeFillingBySymbol(string symbol)
   {
      long flags=SymbolInfoInteger(symbol,SYMBOL_FILLING_MODE);
      filling=(flags&SYMBOL_FILLING_FOK)!=0?ORDER_FILLING_FOK:(flags&SYMBOL_FILLING_IOC)!=0?ORDER_FILLING_IOC:ORDER_FILLING_RETURN;
   }
   bool Entry(bool buy,double volume,string symbol,double sl,double tp,string comment)
   {
      MqlTick q={}; if(!SymbolInfoTick(symbol,q)) { ZeroMemory(result); return false; }
      MqlTradeRequest r={}; r.action=TRADE_ACTION_DEAL; r.magic=magic; r.symbol=symbol;
      r.type=buy?ORDER_TYPE_BUY:ORDER_TYPE_SELL; r.volume=volume; r.price=buy?q.ask:q.bid;
      r.sl=sl; r.tp=tp; r.deviation=deviation; r.type_filling=filling; r.comment=comment; return Send(r);
   }
   bool Buy(double volume,string symbol,double price,double sl,double tp,string comment) { return Entry(true,volume,symbol,sl,tp,comment); }
   bool Sell(double volume,string symbol,double price,double sl,double tp,string comment) { return Entry(false,volume,symbol,sl,tp,comment); }
   bool PositionClose(ulong ticket)
   {
      ZeroMemory(result); if(!PositionSelectByTicket(ticket)) return false;
      string s=PositionGetString(POSITION_SYMBOL); MqlTick q={}; if(!SymbolInfoTick(s,q)) return false;
      bool buy=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY;
      MqlTradeRequest r={}; r.action=TRADE_ACTION_DEAL; r.magic=magic; r.position=ticket; r.symbol=s;
      r.volume=PositionGetDouble(POSITION_VOLUME); r.type=buy?ORDER_TYPE_SELL:ORDER_TYPE_BUY;
      r.price=buy?q.bid:q.ask; r.deviation=deviation; r.type_filling=filling; return Send(r);
   }
   bool PositionModify(ulong ticket,double sl,double tp)
   {
      ZeroMemory(result); if(!PositionSelectByTicket(ticket)) return false;
      MqlTradeRequest r={}; r.action=TRADE_ACTION_SLTP; r.magic=magic; r.position=ticket;
      r.symbol=PositionGetString(POSITION_SYMBOL); r.sl=sl; r.tp=tp; return Send(r);
   }
   uint ResultRetcode() { return result.retcode; }
   string ResultRetcodeDescription() { return result.comment; }
};

input string InpBridge="AITrader\\demo-1";
input long InpDemoLogin=0;                 // 0 = bind to account login saved by setup window
input string InpDemoServer="";             // empty = bind to server saved by setup window
input ulong InpMagic=26092751;
input string InpSymbols="AUTO"; // AUTO = only symbols manually visible in Market Watch; legacy names are cost overrides
input string InpCommissionRoundTurn="-1"; // One value for all symbols, or comma-separated values per symbol
input string InpMaxSpreadPoints="25";
input string InpSlippagePoints="3";
input int InpBars=100;

BrokerTrade trade;
string syms[], requested[], missing="", commissions[], spreads[], slips[];
string configured_syms[], configured_commissions[], configured_spreads[], configured_slips[], shared_commission="-1",shared_spread="25",shared_slip="3";
string base, used[], status_text="Waiting for service", pending_id="none";
string bound_account="",bound_server="",bound_mode="demo";
bool live_allowed=false;
int lock_handle=INVALID_HANDLE, n=0, policy_version=0, configured_n=0, reset_nonce=0,resume_nonce=0;
bool symbols_ready=false;
int panel_page=0,panel_pages=1;
long policy_expiry=0, daykey=0;
double highwater=0,daybase=0,risk_pct=0.5,total_pct=1.5,daily_pct=2,dd_pct=5;
bool state_ok=false,daily_halt=false,total_halt=false,local_pause=true,enabled=false;
string direction="BOTH",policy_symbols="";
ulong last_snapshot=0,last_panel=0,last_history=0,last_catalog=0;
string prefix="AIT_";

long Now() { return (long)TimeGMT(); }
string Account() { return (string)AccountInfoInteger(ACCOUNT_LOGIN); }
string Server() { return AccountInfoString(ACCOUNT_SERVER); }
string J(string v)
{
   StringReplace(v,"\\","\\\\"); StringReplace(v,"\"","\\\"");
   StringReplace(v,"\r","\\r"); StringReplace(v,"\n","\\n"); StringReplace(v,"\t","\\t");
   return "\""+v+"\"";
}
string Num(double x) { return MathIsValidNumber(x)?DoubleToString(x,8):"0"; }
string Bool(bool x) { return x?"true":"false"; }
bool DigitsOnly(string s)
{
   if(StringLen(s)<1) return false;
   for(int i=0;i<StringLen(s);i++) { ushort c=StringGetCharacter(s,i); if(c<'0'||c>'9') return false; }
   return true;
}
bool SafeToken(string s)
{
   if(StringLen(s)<1||StringLen(s)>100) return false;
   for(int i=0;i<StringLen(s);i++) { ushort c=StringGetCharacter(s,i); if(!((c>='0'&&c<='9')||(c>='a'&&c<='z')||(c>='A'&&c<='Z')||c=='.'||c=='_'||c=='-'||c=='#')) return false; }
   return true;
}
bool DecimalNumber(string s,bool negative=false)
{
   int dots=0,digits=0;
   for(int i=0;i<StringLen(s);i++)
   {
      ushort c=StringGetCharacter(s,i);
      if(c>='0'&&c<='9') digits++;
      else if(c=='.'&&++dots<=1) continue;
      else if(c=='-'&&i==0&&negative) continue;
      else return false;
   }
   return digits>0;
}
string Read(string name)
{
   int h=FileOpen(base+name,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
   if(h==INVALID_HANDLE) return "";
   string s=FileReadString(h); FileClose(h); return s;
}
bool WriteAtomic(string name,string value)
{
   int h=FileOpen(base+name+".tmp",FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
   if(h==INVALID_HANDLE) return false;
   bool ok=FileWriteString(h,value+"\n")>0; FileFlush(h); FileClose(h);
   return ok && FileMove(base+name+".tmp",FILE_COMMON,base+name,FILE_COMMON|FILE_REWRITE);
}
bool Append(string name,string value)
{
   int h=FileOpen(base+name,FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,0,CP_UTF8);
   if(h==INVALID_HANDLE) return false;
   FileSeek(h,0,SEEK_END); bool ok=FileWriteString(h,value+"\n")>0; FileFlush(h); FileClose(h); return ok;
}
void SaveState()
{
   string value="1,"+Account()+","+Server()+","+(string)InpMagic+","+Num(highwater)+","+Num(daybase)+","+(string)daykey+","+(string)(int)daily_halt+","+(string)(int)total_halt+","+(string)reset_nonce+","+(string)resume_nonce+","+(string)(int)local_pause+","+(string)policy_version+","+Num(risk_pct)+","+Num(total_pct)+","+Num(daily_pct)+","+Num(dd_pct);
   if(!WriteAtomic("risk.csv",value)) { state_ok=false; enabled=false; }
}
bool OwnedSelected() { return (ulong)PositionGetInteger(POSITION_MAGIC)==InpMagic; }
bool AnyOwned()
{
   for(int i=PositionsTotal()-1;i>=0;i--) if(PositionGetTicket(i)>0&&OwnedSelected()) return true;
   return false;
}
void LoadState()
{
   if(!FileIsExist(base+"risk.csv",FILE_COMMON))
   {
      if(AnyOwned()||FileIsExist(base+"used.txt",FILE_COMMON)) return;
      highwater=daybase=AccountInfoDouble(ACCOUNT_EQUITY); daykey=Now()/86400; state_ok=true; SaveState(); return;
   }
   string a[]; if(StringSplit(Read("risk.csv"),',',a)!=17) return;
   if(!DecimalNumber(a[4])||!DecimalNumber(a[5])||!DigitsOnly(a[6])) return;
   if(a[0]!="1"||a[1]!=Account()||a[2]!=Server()||(ulong)StringToInteger(a[3])!=InpMagic) return;
   highwater=StringToDouble(a[4]); daybase=StringToDouble(a[5]); daykey=StringToInteger(a[6]);
   if(highwater<=0||daybase<=0||daykey<=0||daykey>Now()/86400||!MathIsValidNumber(highwater)||!MathIsValidNumber(daybase)) return;
   for(int i=7;i<13;i++) if(!DigitsOnly(a[i])) return;
   for(int i=13;i<17;i++) if(!DecimalNumber(a[i])) return;
   if((a[7]!="0"&&a[7]!="1")||(a[8]!="0"&&a[8]!="1")||(a[11]!="0"&&a[11]!="1")) return;
   risk_pct=StringToDouble(a[13]); total_pct=StringToDouble(a[14]); daily_pct=StringToDouble(a[15]); dd_pct=StringToDouble(a[16]);
   if(!(risk_pct>0&&risk_pct<=0.5&&total_pct>=risk_pct&&total_pct<=1.5&&daily_pct>0&&daily_pct<=2&&dd_pct>0&&dd_pct<=5)) return;
   daily_halt=(a[7]=="1"); total_halt=(a[8]=="1"); reset_nonce=(int)StringToInteger(a[9]); resume_nonce=(int)StringToInteger(a[10]);
   local_pause=(a[11]=="1"); policy_version=(int)StringToInteger(a[12]); state_ok=true;
}
void LoadUsed()
{
   int h=FileOpen(base+"used.txt",FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,0,CP_UTF8);
   if(h==INVALID_HANDLE) return;
   while(!FileIsEnding(h)) { string id=FileReadString(h); if(id=="") continue; int size=ArraySize(used); ArrayResize(used,size+1); used[size]=id; }
   FileClose(h);
}
bool MarkUsed(string id)
{
   for(int i=0;i<ArraySize(used);i++) if(used[i]==id) return false;
   if(!Append("used.txt",id)) { state_ok=false; return false; }
   int size=ArraySize(used); ArrayResize(used,size+1); used[size]=id; return true;
}
void Result(string id,string status,uint code,string detail,long latency_ms=0)
{
   status_text=status+": "+detail;
   string row="{\"id\":"+J(id)+",\"status\":"+J(status)+",\"retcode\":"+(string)code+",\"detail\":"+J(detail)+",\"time\":"+(string)Now()+",\"account\":"+J(Account())+",\"server\":"+J(Server())+",\"latency_ms\":"+(string)latency_ms+"}";
   if(!Append("results.jsonl",row)) state_ok=false;
}
int SymbolIndex(string symbol) { for(int i=0;i<n;i++) if(syms[i]==symbol) return i; return -1; }
bool PolicySymbol(string symbol) { return StringFind("|"+policy_symbols+"|","|"+symbol+"|")>=0; }
bool RefreshWatchlist()
{
   ArrayResize(syms,0); ArrayResize(requested,0); ArrayResize(commissions,0);
   ArrayResize(spreads,0); ArrayResize(slips,0);
   n=0; missing="";
   int total=SymbolsTotal(true);
   for(int i=0;i<total;i++)
   {
      string symbol=SymbolName(i,true);
      if(!SafeToken(symbol)||!SymbolInfoInteger(symbol,SYMBOL_VISIBLE)||
         SymbolInfoInteger(symbol,SYMBOL_TRADE_MODE)==SYMBOL_TRADE_MODE_DISABLED) continue;
      if(n>=10)
      {
         ArrayResize(syms,0); ArrayResize(requested,0); ArrayResize(commissions,0);
         ArrayResize(spreads,0); ArrayResize(slips,0); n=0;
         missing=J("Market Watch has more than 10 visible tradable symbols; hide extras");
         return false;
      }
      int source=-1;
      for(int j=0;j<configured_n;j++) if(configured_syms[j]==symbol) { source=j; break; }
      ArrayResize(syms,n+1); ArrayResize(requested,n+1); ArrayResize(commissions,n+1);
      ArrayResize(spreads,n+1); ArrayResize(slips,n+1);
      syms[n]=symbol; requested[n]=symbol;
      commissions[n]=source>=0?configured_commissions[source]:shared_commission;
      spreads[n]=source>=0?configured_spreads[source]:shared_spread;
      slips[n]=source>=0?configured_slips[source]:shared_slip;
      n++;
   }
   return n>0;
}
bool ConfigurePolicySymbols()
{
   string selected[]; int count=StringSplit(policy_symbols,'|',selected);
   if(count<1||count>10||n<1) { symbols_ready=false; return false; }
   bool complete=true;
   for(int i=0;i<count;i++)
   {
      string symbol=selected[i];
      if(!SafeToken(symbol)||SymbolIndex(symbol)<0)
      {
         complete=false;
         if(missing!="") missing+=",";
         missing+=J(symbol+": not visible in Market Watch");
      }
   }
   symbols_ready=complete;
   return symbols_ready;
}
bool AccountMatched()
{
   if(Account()!=bound_account||Server()!=bound_server) return false;
   long mode=AccountInfoInteger(ACCOUNT_TRADE_MODE);
   return (bound_mode=="demo"&&mode==ACCOUNT_TRADE_MODE_DEMO)||(bound_mode=="real"&&mode==ACCOUNT_TRADE_MODE_REAL);
}
bool IsDemo() { return AccountMatched()&&bound_mode=="demo"; }
bool TradeAllowed()
{
   return AccountMatched() && TerminalInfoInteger(TERMINAL_CONNECTED) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_EXPERT);
}
void ReadPolicy()
{
   enabled=false;
   string a[]; if(StringSplit(Read("policy.csv"),',',a)!=15) return;
   if(!DigitsOnly(a[3])||!DigitsOnly(a[4])||!DigitsOnly(a[5])||!DigitsOnly(a[13])||!DigitsOnly(a[14])||(a[6]!="0"&&a[6]!="1")) return;
   for(int i=8;i<=11;i++) if(!DecimalNumber(a[i])) return;
   if(a[0]!="1"||a[1]!=Account()||a[2]!=Server()||(ulong)StringToInteger(a[3])!=InpMagic) return;
   int version=(int)StringToInteger(a[4]); long expiry=StringToInteger(a[5]);
   if(version<policy_version||version<=0||expiry<Now()||expiry>Now()+60) return;
   double r=StringToDouble(a[8]),t=StringToDouble(a[9]),d=StringToDouble(a[10]),dd=StringToDouble(a[11]);
   if(!(r>0&&r<=0.5&&t>=r&&t<=1.5&&d>0&&d<=2&&dd>0&&dd<=5)) return;
   if(a[7]!="BUY"&&a[7]!="SELL"&&a[7]!="BOTH") return;
   bool changed=(policy_version!=version); policy_version=version; policy_expiry=expiry;
   direction=a[7]; policy_symbols=a[12]; risk_pct=r; total_pct=t; daily_pct=d; dd_pct=dd;
   int rn=(int)StringToInteger(a[13]),sn=(int)StringToInteger(a[14]);
   if(rn>reset_nonce&&state_ok&&!AnyOwned()) { reset_nonce=rn; total_halt=false; highwater=AccountInfoDouble(ACCOUNT_EQUITY); local_pause=true; changed=true; }
   if(sn>resume_nonce&&state_ok&&!total_halt&&!daily_halt) { resume_nonce=sn; local_pause=false; changed=true; }
   bool symbols_ok=ConfigurePolicySymbols();
   enabled=a[6]=="1"&&!local_pause&&symbols_ok&&(bound_mode=="demo"||live_allowed);
   if(changed) SaveState();
}
ulong FindPosition(string symbol,string id)
{
   for(int i=PositionsTotal()-1;i>=0;i--)
   {
      ulong ticket=PositionGetTicket(i);
      if(ticket>0&&OwnedSelected()&&PositionGetString(POSITION_SYMBOL)==symbol&&(string)PositionGetInteger(POSITION_IDENTIFIER)==id) return ticket;
   }
   return 0;
}
bool Occupied(string symbol)
{
   for(int i=PositionsTotal()-1;i>=0;i--) if(PositionGetTicket(i)>0&&PositionGetString(POSITION_SYMBOL)==symbol) return true;
   for(int i=OrdersTotal()-1;i>=0;i--) if(OrderGetTicket(i)>0&&OrderGetString(ORDER_SYMBOL)==symbol) return true;
   return false;
}
string RiskFile(long id) { return "position-"+(string)id+".risk"; }
bool SaveInitialRisk(long id,double loss) { return WriteAtomic(RiskFile(id),Num(loss)); }
double TotalRisk()
{
   double total=0;
   for(int i=PositionsTotal()-1;i>=0;i--)
   {
      if(PositionGetTicket(i)==0) continue;
      string s=PositionGetString(POSITION_SYMBOL); double sl=PositionGetDouble(POSITION_SL),loss=0;
      if(sl<=0) return -1;
      ENUM_ORDER_TYPE type=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
      if(!OrderCalcProfit(type,s,PositionGetDouble(POSITION_VOLUME),PositionGetDouble(POSITION_PRICE_OPEN),sl,loss)) return -1;
      if(OwnedSelected())
      {
         double initial=StringToDouble(Read(RiskFile(PositionGetInteger(POSITION_IDENTIFIER))));
         if(initial<=0) return -1;
         total+=MathMax(initial,MathMax(0,-loss));
      }
      else total+=MathMax(0,-loss); // unrelated exposure can block new risk, never managed
   }
   return total;
}
void Protect()
{
   if(!AccountMatched()) return;
   double equity=AccountInfoDouble(ACCOUNT_EQUITY); bool changed=false;
   if(state_ok)
   {
      if(Now()/86400!=daykey) { daykey=Now()/86400; daybase=equity; daily_halt=false; changed=true; }
      if(equity>highwater) { highwater=equity; changed=true; }
      if(!daily_halt&&equity<=daybase*(1-daily_pct/100)) { daily_halt=true; changed=true; }
      if(!total_halt&&equity<=highwater*(1-dd_pct/100)) { total_halt=true; changed=true; }
      if(changed) SaveState();
   }
   if(!TradeAllowed()) return;
   for(int i=PositionsTotal()-1;i>=0;i--)
   {
      ulong ticket=PositionGetTicket(i); if(ticket==0||!OwnedSelected()) continue;
      if(daily_halt||total_halt||!state_ok||PositionGetDouble(POSITION_SL)<=0)
      {
         trade.SetTypeFillingBySymbol(PositionGetString(POSITION_SYMBOL));
         trade.PositionClose(ticket);
         Print("AITrader protection close: ",ticket," ret=",trade.ResultRetcode());
      }
   }
}
bool FreshQuote(string symbol,MqlTick &q)
{
   if(!SymbolInfoTick(symbol,q)||q.ask<=q.bid||q.bid<=0) return false;
   // Broker quote time and TimeTradeServer share the broker time domain.
   return MathAbs((long)TimeTradeServer()-(long)q.time)<=10;
}
double SnapPrice(string s,double value)
{
   double tick=SymbolInfoDouble(s,SYMBOL_TRADE_TICK_SIZE);
   if(tick<=0) return 0;
   return NormalizeDouble(MathRound(value/tick)*tick,(int)SymbolInfoInteger(s,SYMBOL_DIGITS));
}
double BrokerCommission(string symbol)
{
   // Only the simple per-lot, deposit-currency rule maps to a stable round-trip input.
   // Empty, tiered, per-trade, percentage and deferred rules remain unknown.
   MqlCommission rules[];
   if(SymbolInfoCommissions(symbol,rules)!=1) return -1;
   if(rules[0].mode_range!=SYMBOL_COMMISSION_RANGE_VOLUME ||
      rules[0].mode_charge!=SYMBOL_COMMISSION_CHARGE_INSTANT ||
      rules[0].mode_direction!=SYMBOL_COMMISSION_DIRECTION_BOTH ||
      rules[0].mode_profit!=SYMBOL_COMMISSION_PROFIT_ALL || ArraySize(rules[0].tiers)!=1) return -1;
   MqlCommissionTier tier=rules[0].tiers[0];
   if(tier.mode!=SYMBOL_COMMISSION_MONEY_DEPOSIT ||
      tier.volume_type!=SYMBOL_COMMISSION_VOLUME_TYPE_VOLUME ||
      tier.min_value>0 || tier.max_value>0 || tier.value<0) return -1;
   double minlot=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN);
   double maxlot=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX);
   if(tier.range_from>minlot || (tier.range_to>0 && tier.range_to<maxlot)) return -1;
   if(rules[0].currency!="" && rules[0].currency!=AccountInfoString(ACCOUNT_CURRENCY)) return -1;
   double multiplier=rules[0].mode_entry==SYMBOL_COMMISSION_ENTRY_INOUT?2.0:1.0;
   if(rules[0].mode_entry!=SYMBOL_COMMISSION_ENTRY_INOUT &&
      rules[0].mode_entry!=SYMBOL_COMMISSION_ENTRY_IN &&
      rules[0].mode_entry!=SYMBOL_COMMISSION_ENTRY_OUT) return -1;
   return tier.value*multiplier;
}
double Commission(int idx)
{
   double value=StringToDouble(commissions[idx]);
   double broker=BrokerCommission(syms[idx]);
   if(value>=0 && broker>=0) return MathMax(value,broker);
   return value>=0?value:broker;
}
bool SendEntry(string symbol,string action,double sl,double tp,string id)
{
   int idx=SymbolIndex(symbol); MqlTick q;
   if(!enabled||(bound_mode=="real"&&!live_allowed)||local_pause||!state_ok||daily_halt||total_halt||policy_expiry<Now()||!PolicySymbol(symbol)||Occupied(symbol)) { Result(id,"REJECTED",0,"entry blocked: state/policy/exposure"); return false; }
   if(direction!="BOTH"&&direction!=action) { Result(id,"REJECTED",0,"direction forbidden"); return false; }
   if(idx<0||!FreshQuote(symbol,q)) { Result(id,"REJECTED",0,"stale quote"); return false; }
   double point=SymbolInfoDouble(symbol,SYMBOL_POINT),commission=Commission(idx),slip=StringToDouble(slips[idx])*point;
   if(point<=0||commission<0||q.ask-q.bid>StringToDouble(spreads[idx])*point) { Result(id,"REJECTED",0,"cost configuration or spread"); return false; }
   sl=SnapPrice(symbol,sl); tp=SnapPrice(symbol,tp);
   double distance=(double)SymbolInfoInteger(symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;
   bool buy=action=="BUY";
   if(sl<=0||tp<=0||(buy&&!(sl<q.bid-distance&&tp>q.ask+distance))||(!buy&&!(sl>q.ask+distance&&tp<q.bid-distance))) { Result(id,"REJECTED",0,"invalid SL/TP"); return false; }
   ENUM_ORDER_TYPE type=buy?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
   double unitloss=0;
   if(!OrderCalcProfit(type,symbol,1,buy?q.ask+slip:q.bid-slip,buy?sl-slip:sl+slip,unitloss)||unitloss>=0) { Result(id,"REJECTED",0,"risk calculation failed"); return false; }
   unitloss=-unitloss+commission;
   double equity=AccountInfoDouble(ACCOUNT_EQUITY),budget=equity*risk_pct/100;
   double current=TotalRisk(); if(current<0) { Result(id,"REJECTED",0,"unknown existing risk"); return false; }
   budget=MathMin(budget,equity*total_pct/100-current);
   double step=SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP),minimum=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN),maximum=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX);
   if(step<=0||budget<=0) { Result(id,"REJECTED",0,"risk budget exhausted"); return false; }
   double volume=NormalizeDouble(MathFloor(MathMin(budget/unitloss,maximum)/step)*step,8);
   if(volume<minimum||volume*unitloss>budget+0.000001) { Result(id,"REJECTED",0,"minimum lot exceeds budget"); return false; }
   double margin=0;
   if(!OrderCalcMargin(type,symbol,volume,buy?q.ask:q.bid,margin)||margin>AccountInfoDouble(ACCOUNT_MARGIN_FREE)*0.9) { Result(id,"REJECTED",0,"insufficient margin"); return false; }
   trade.SetTypeFillingBySymbol(symbol); trade.SetDeviationInPoints((ulong)StringToInteger(slips[idx]));
   ulong sent_at=GetMicrosecondCount();
   bool ok=buy?trade.Buy(volume,symbol,0,sl,tp,"AIT:"+StringSubstr(id,0,20)):trade.Sell(volume,symbol,0,sl,tp,"AIT:"+StringSubstr(id,0,20));
   long latency_ms=(long)MathMin((double)((GetMicrosecondCount()-sent_at)/1000),60000.0);
   uint code=trade.ResultRetcode();
   // Persist full reserved risk even for a partial fill (conservative until flat).
   if(ok&&(code==TRADE_RETCODE_DONE||code==TRADE_RETCODE_DONE_PARTIAL))
   {
      for(int i=PositionsTotal()-1;i>=0;i--)
         if(PositionGetTicket(i)>0&&OwnedSelected()&&PositionGetString(POSITION_SYMBOL)==symbol)
            if(!SaveInitialRisk(PositionGetInteger(POSITION_IDENTIFIER),volume*unitloss)) state_ok=false;
   }
   string status=code==TRADE_RETCODE_DONE?"DONE":code==TRADE_RETCODE_DONE_PARTIAL?"PARTIAL":(code==TRADE_RETCODE_TIMEOUT||code==TRADE_RETCODE_CONNECTION||code==TRADE_RETCODE_PLACED)?"UNCERTAIN":"REJECTED";
   if(status=="UNCERTAIN") { local_pause=true; SaveState(); }
   Result(id,status,code,trade.ResultRetcodeDescription(),latency_ms); return status=="DONE";
}
void Command()
{
   string a[]; if(StringSplit(Read("command.csv"),',',a)!=12) return;
   string id=a[1]; if(!SafeToken(id)) return;
   if(!MarkUsed(id)) return; // journal flushed before any broker operation
   if(!DigitsOnly(a[4])||!DigitsOnly(a[5])||!DigitsOnly(a[6])||!DecimalNumber(a[10])||!DecimalNumber(a[11])) { Result(id,"REJECTED",0,"malformed numeric fields"); return; }
   if(a[0]!="1"||a[2]!=Account()||a[3]!=Server()||(ulong)StringToInteger(a[4])!=InpMagic) { Result(id,"REJECTED",0,"identity mismatch"); return; }
   long expiry=StringToInteger(a[6]);
   if(expiry<Now()||expiry>Now()+120||(int)StringToInteger(a[5])!=policy_version||policy_version<=0) { Result(id,"REJECTED",0,"expired/version mismatch"); return; }
   if(!TradeAllowed()) { Result(id,"REJECTED",0,"account/trading permissions"); return; }
   string action=a[7],symbol=a[8];
   if(!SafeToken(symbol)||!DigitsOnly(a[9])) { Result(id,"REJECTED",0,"invalid command"); return; }
   double sl=StringToDouble(a[10]),tp=StringToDouble(a[11]);
   if(!MathIsValidNumber(sl)||!MathIsValidNumber(tp)) { Result(id,"REJECTED",0,"invalid prices"); return; }
   if(action=="BUY"||action=="SELL") { SendEntry(symbol,action,sl,tp,id); return; }
   ulong ticket=FindPosition(symbol,a[9]);
   if(ticket==0) { Result(id,"REJECTED",0,"position not owned or no longer exists"); return; }
   trade.SetTypeFillingBySymbol(symbol);
   ulong sent_at=GetMicrosecondCount();
   if(action=="CLOSE") trade.PositionClose(ticket);
   else if(action=="TIGHTEN")
   {
      MqlTick q; if(!FreshQuote(symbol,q)) { Result(id,"REJECTED",0,"stale quote"); return; }
      double old=PositionGetDouble(POSITION_SL); bool buy=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY;
      sl=SnapPrice(symbol,sl);
      double minimum=(double)MathMax(SymbolInfoInteger(symbol,SYMBOL_TRADE_STOPS_LEVEL),SymbolInfoInteger(symbol,SYMBOL_TRADE_FREEZE_LEVEL))*SymbolInfoDouble(symbol,SYMBOL_POINT);
      if(old<=0||sl<=0||(buy&&!(sl>old&&sl<q.bid-minimum))||(!buy&&!(sl<old&&sl>q.ask+minimum))) { Result(id,"REJECTED",0,"stop must tighten outside freeze level"); return; }
      trade.PositionModify(ticket,sl,PositionGetDouble(POSITION_TP));
   }
   else { Result(id,"REJECTED",0,"unsupported action"); return; }
   uint code=trade.ResultRetcode();
   string status=code==TRADE_RETCODE_DONE?"DONE":code==TRADE_RETCODE_DONE_PARTIAL?"PARTIAL":(code==TRADE_RETCODE_TIMEOUT||code==TRADE_RETCODE_CONNECTION||code==TRADE_RETCODE_PLACED)?"UNCERTAIN":"REJECTED";
   if(status=="UNCERTAIN") { local_pause=true; SaveState(); }
   long latency_ms=(long)MathMin((double)((GetMicrosecondCount()-sent_at)/1000),60000.0);
   Result(id,status,code,trade.ResultRetcodeDescription(),latency_ms);
}
string Bars(string symbol,ENUM_TIMEFRAMES tf,bool &ready)
{
   MqlRates rates[]; int count=CopyRates(symbol,tf,1,InpBars,rates);
   if(count<InpBars) ready=false;
   string out="[";
   for(int i=0;i<count;i++)
   {
      if(i>0) out+=",";
      out+="{\"time\":"+(string)(long)rates[i].time+",\"open\":"+Num(rates[i].open)+",\"high\":"+Num(rates[i].high)+",\"low\":"+Num(rates[i].low)+",\"close\":"+Num(rates[i].close)+",\"volume\":"+(string)rates[i].tick_volume+"}";
   }
   return out+"]";
}
void Snapshot()
{
   string out="{\"schema\":1,\"ea_version\":\"1.010\",\"account\":"+J(Account())+",\"server\":"+J(Server())+",\"magic\":"+(string)InpMagic+",\"demo\":"+Bool(IsDemo())+",\"account_mode\":"+J(bound_mode)+",\"live_enabled\":"+Bool(live_allowed)+",\"time\":"+(string)Now()+",\"equity\":"+Num(AccountInfoDouble(ACCOUNT_EQUITY))+",\"balance\":"+Num(AccountInfoDouble(ACCOUNT_BALANCE))+",\"currency\":"+J(AccountInfoString(ACCOUNT_CURRENCY))+",\"state_ok\":"+Bool(state_ok)+",\"halted\":"+Bool(daily_halt||total_halt||!state_ok)+",\"local_pause\":"+Bool(local_pause)+",\"missing_symbols\":["+missing+"],\"positions\":[";
   int count=0;
   for(int i=0;i<PositionsTotal();i++)
   {
      ulong ticket=PositionGetTicket(i); if(ticket==0) continue;
      if(count++>0) out+=",";
      out+="{\"ticket\":"+J((string)ticket)+",\"id\":"+J((string)PositionGetInteger(POSITION_IDENTIFIER))+",\"symbol\":"+J(PositionGetString(POSITION_SYMBOL))+",\"owned\":"+Bool(OwnedSelected())+",\"side\":"+J(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL")+",\"volume\":"+Num(PositionGetDouble(POSITION_VOLUME))+",\"open\":"+Num(PositionGetDouble(POSITION_PRICE_OPEN))+",\"sl\":"+Num(PositionGetDouble(POSITION_SL))+",\"tp\":"+Num(PositionGetDouble(POSITION_TP))+",\"profit\":"+Num(PositionGetDouble(POSITION_PROFIT))+"}";
   }
   out+="],\"symbols\":{";
   count=0;
   for(int i=0;i<n;i++)
   {
      string symbol=syms[i]; if(symbol=="") continue;
      MqlTick q={}; bool ready=FreshQuote(symbol,q);
      string bars="{\"M5\":"+Bars(symbol,PERIOD_M5,ready)+",\"M15\":"+Bars(symbol,PERIOD_M15,ready)+",\"H1\":"+Bars(symbol,PERIOD_H1,ready)+",\"H4\":"+Bars(symbol,PERIOD_H4,ready)+"}";
      double commission=Commission(i);
      if(commission<0) ready=false;
      if(count++>0) out+=",";
      double point=SymbolInfoDouble(symbol,SYMBOL_POINT);
      double spread_points=point>0?(q.ask-q.bid)/point:0;
      out+=J(symbol)+":{\"ready\":"+Bool(ready)+",\"bid\":"+Num(q.bid)+",\"ask\":"+Num(q.ask)+",\"point\":"+Num(point)+",\"spread_points\":"+Num(spread_points)+",\"max_spread_points\":"+spreads[i]+",\"slippage_points\":"+slips[i]+",\"tick_size\":"+Num(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE))+",\"volume_min\":"+Num(SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN))+",\"volume_step\":"+Num(SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP))+",\"commission_round_turn\":"+Num(commission)+",\"commission_source\":"+J(StringToDouble(commissions[i])>=0?"manual_or_broker_max":commission>=0?"broker_rule":"unknown")+",\"bars\":"+bars+"}";
   }
   out+="}}"; WriteAtomic("snapshot.json",out);
}
void Catalog()
{
   string out="{\"account\":"+J(Account())+",\"server\":"+J(Server())+",\"magic\":"+(string)InpMagic+",\"demo\":"+Bool(IsDemo())+",\"account_mode\":"+J(bound_mode)+",\"time\":"+(string)Now()+",\"symbols\":[";
   int added=0;
   for(int i=0;i<n;i++)
   {
      string symbol=syms[i];
      if(added++>0) out+=",";
      out+=J(symbol);
   }
   out+="]}"; WriteAtomic("catalog.json",out);
}
void ExportDeals()
{
   // Overlap/replay is intentional; service deduplicates broker deal IDs.
   if(!HistorySelect((datetime)0,TimeCurrent()+60)) return;
   long owned_ids[];
   for(int j=0;j<HistoryDealsTotal();j++)
   {
      ulong d=HistoryDealGetTicket(j);
      if((ulong)HistoryDealGetInteger(d,DEAL_MAGIC)==InpMagic)
      { int size=ArraySize(owned_ids); ArrayResize(owned_ids,size+1); owned_ids[size]=HistoryDealGetInteger(d,DEAL_POSITION_ID); }
   }
   for(int i=0;i<HistoryDealsTotal();i++)
   {
      ulong deal=HistoryDealGetTicket(i);
      if(deal==0) continue;
      bool ours=false;
      for(int j=0;j<ArraySize(owned_ids);j++) if(owned_ids[j]>0&&owned_ids[j]==HistoryDealGetInteger(deal,DEAL_POSITION_ID)) { ours=true; break; }
      if(!ours) continue;
      string marker="deal-"+(string)deal+".seen";
      if(FileIsExist(base+marker,FILE_COMMON)) continue;
      string row="{\"account\":"+J(Account())+",\"server\":"+J(Server())+",\"deal\":"+J((string)deal)+",\"position_id\":"+J((string)HistoryDealGetInteger(deal,DEAL_POSITION_ID))+",\"time_server\":"+(string)HistoryDealGetInteger(deal,DEAL_TIME)+",\"observed_utc\":"+(string)Now()+",\"entry\":"+(string)HistoryDealGetInteger(deal,DEAL_ENTRY)+",\"symbol\":"+J(HistoryDealGetString(deal,DEAL_SYMBOL))+",\"volume\":"+Num(HistoryDealGetDouble(deal,DEAL_VOLUME))+",\"profit\":"+Num(HistoryDealGetDouble(deal,DEAL_PROFIT))+",\"commission\":"+Num(HistoryDealGetDouble(deal,DEAL_COMMISSION))+",\"swap\":"+Num(HistoryDealGetDouble(deal,DEAL_SWAP))+",\"fee\":"+Num(HistoryDealGetDouble(deal,DEAL_FEE))+"}";
      if(Append("deals.jsonl",row)) WriteAtomic(marker,"1");
   }
}
void Button(string name,string label,int x,int y,int width=100)
{
   string id=prefix+name;
   if(ObjectFind(0,id)<0) ObjectCreate(0,id,OBJ_BUTTON,0,0,0);
   ObjectSetInteger(0,id,OBJPROP_XDISTANCE,x); ObjectSetInteger(0,id,OBJPROP_YDISTANCE,y);
   ObjectSetInteger(0,id,OBJPROP_XSIZE,width); ObjectSetInteger(0,id,OBJPROP_YSIZE,38);
   ObjectSetInteger(0,id,OBJPROP_ZORDER,10);
   ObjectSetString(0,id,OBJPROP_TEXT,label); ObjectSetInteger(0,id,OBJPROP_FONTSIZE,11);
}
void ChatInput()
{
   string id=prefix+"chat_input";
   if(ObjectFind(0,id)<0)
   {
      ObjectCreate(0,id,OBJ_EDIT,0,0,0);
      ObjectSetString(0,id,OBJPROP_TEXT,"");
   }
   ObjectSetInteger(0,id,OBJPROP_XDISTANCE,10); ObjectSetInteger(0,id,OBJPROP_YDISTANCE,480);
   ObjectSetInteger(0,id,OBJPROP_XSIZE,780); ObjectSetInteger(0,id,OBJPROP_YSIZE,32);
   ObjectSetInteger(0,id,OBJPROP_ZORDER,10);
   ObjectSetInteger(0,id,OBJPROP_COLOR,clrBlack); ObjectSetInteger(0,id,OBJPROP_BGCOLOR,clrWhite);
   ObjectSetInteger(0,id,OBJPROP_FONTSIZE,10);
   ObjectSetString(0,id,OBJPROP_FONT,"Microsoft JhengHei");
}
void Panel()
{
   string info="";
   int h=FileOpen(base+"panel.txt",FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
   if(h!=INVALID_HANDLE) { while(!FileIsEnding(h)) info+=FileReadString(h)+"\n"; FileClose(h); }
   string next_pending=Read("pending.csv"); if(next_pending!=pending_id) panel_page=0; pending_id=next_pending;
   string summary="AI Trader | "+(bound_mode=="real"?"REAL ACCOUNT":"DEMO ACCOUNT")+" | "+Account()+" | "+Server()+"\n"+
           "Risk "+DoubleToString(risk_pct,2)+"% / total "+DoubleToString(total_pct,2)+"% | policy "+(string)policy_version+"\n"+
           "EA state="+Bool(state_ok)+" daily halt="+Bool(daily_halt)+" total halt="+Bool(total_halt)+" local pause="+Bool(local_pause)+"\n"+
           "Bridge fresh="+Bool(policy_expiry>=Now())+" | "+status_text+"\n"+info;
   string raw[],lines[]; int count=StringSplit(summary,'\n',raw);
   for(int i=0;i<count;i++)
      for(int start=0;start<MathMax(1,StringLen(raw[i]));start+=65)
      { int size=ArraySize(lines); ArrayResize(lines,size+1); lines[size]=StringSubstr(raw[i],start,65); }
   panel_pages=MathMax(1,(ArraySize(lines)+19)/20); panel_page=MathMin(panel_page,panel_pages-1);
   string bg=prefix+"background";
   if(ObjectFind(0,bg)<0) ObjectCreate(0,bg,OBJ_RECTANGLE_LABEL,0,0,0);
   ObjectSetInteger(0,bg,OBJPROP_XDISTANCE,5); ObjectSetInteger(0,bg,OBJPROP_YDISTANCE,5);
   ObjectSetInteger(0,bg,OBJPROP_XSIZE,920); ObjectSetInteger(0,bg,OBJPROP_YSIZE,525);
   ObjectSetInteger(0,bg,OBJPROP_BGCOLOR,clrBlack); ObjectSetInteger(0,bg,OBJPROP_BACK,false);
   ObjectSetInteger(0,bg,OBJPROP_ZORDER,0);
   for(int i=0;i<20;i++)
   {
      string label=prefix+"line"+(string)i;
      if(ObjectFind(0,label)<0) ObjectCreate(0,label,OBJ_LABEL,0,0,0);
      ObjectSetInteger(0,label,OBJPROP_XDISTANCE,14); ObjectSetInteger(0,label,OBJPROP_YDISTANCE,106+i*18);
      ObjectSetInteger(0,label,OBJPROP_COLOR,clrWhite); ObjectSetInteger(0,label,OBJPROP_FONTSIZE,9);
      ObjectSetString(0,label,OBJPROP_FONT,"Microsoft JhengHei");
      int idx=panel_page*20+i; ObjectSetString(0,label,OBJPROP_TEXT,idx<ArraySize(lines)?lines[idx]:" ");
   }
   Button("pause","暫停新單",10,12,145); Button("resume","啟動提案",165,12,145);
   Button("close","平倉提案",320,12,145); Button("sync","同步",475,12,120);
   Button("confirm","確認待辦",10,56,145); Button("reset","重設回撤",165,56,145);
   Button("prev","上頁",320,56,120); Button("next","下頁 "+(string)(panel_page+1)+"/"+(string)panel_pages,450,56,160);
   ChatInput(); Button("send","送出訊息",800,480,105);
   Comment("");
   ChartRedraw();
}
void OnChartEvent(const int event,const long &lparam,const double &dparam,const string &sparam)
{
   if(event!=CHARTEVENT_OBJECT_CLICK||StringFind(sparam,prefix)!=0) return;
   string action=StringSubstr(sparam,StringLen(prefix));
   if(action=="chat_input") return;
   ObjectSetInteger(0,sparam,OBJPROP_STATE,false);
   if(action=="prev") { panel_page=MathMax(0,panel_page-1); Panel(); return; }
   if(action=="next") { panel_page=MathMin(panel_pages-1,panel_page+1); Panel(); return; }
   if(action=="send")
   {
      string message=ObjectGetString(0,prefix+"chat_input",OBJPROP_TEXT);
      StringTrimLeft(message); StringTrimRight(message);
      if(StringLen(message)<1||StringLen(message)>1000) { status_text="請輸入 1 至 1000 字"; Panel(); return; }
      for(int i=0;i<StringLen(message);i++) if(StringGetCharacter(message,i)<32)
      { status_text="訊息不能包含換行或控制字元"; Panel(); return; }
      if(!AccountMatched()) { status_text="帳號或模式不符，訊息未送出"; Panel(); return; }
      string chat_id=(string)Now()+"-"+(string)GetMicrosecondCount();
      bool sent=Append("ui.jsonl","{\"id\":"+J(chat_id)+",\"account\":"+J(Account())+",\"server\":"+J(Server())+",\"magic\":"+(string)InpMagic+",\"time\":"+(string)Now()+",\"action\":\"chat\",\"text\":"+J(message)+"}");
      if(sent) { ObjectSetString(0,prefix+"chat_input",OBJPROP_TEXT,""); status_text="訊息已送出，等待回覆"; panel_page=0; }
      else status_text="訊息寫入失敗";
      Panel(); return;
   }
   if(action=="confirm"&&panel_page<panel_pages-1) { status_text="Please read all proposal pages before confirming"; Panel(); return; }
   if(action!="pause"&&action!="resume"&&action!="close"&&action!="sync"&&action!="confirm"&&action!="reset") return;
   if(action=="pause") { local_pause=true; enabled=false; SaveState(); }
   string id=(string)Now()+"-"+(string)GetMicrosecondCount();
   Append("ui.jsonl","{\"id\":"+J(id)+",\"account\":"+J(Account())+",\"server\":"+J(Server())+",\"time\":"+(string)Now()+",\"action\":"+J(action)+",\"proposal\":"+J(pending_id)+"}");
   Panel();
}
int InitFailure(string reason)
{
   string message="AITrader setup error: "+reason;
   Print(message);
   Alert(message);
   return INIT_PARAMETERS_INCORRECT;
}
int OnInit()
{
   if(MQLInfoInteger(MQL_TESTER)) { Print("Use recorded service replay for AI decisions; this EA requires an MT5 bridge."); return INIT_FAILED; }
   if(InpMagic==0||InpBars<30||InpBars>300) return InitFailure("Invalid magic or InpBars (30..300)");
   string selected_bridge=InpBridge, selected_symbols=InpSymbols, selected_commissions=InpCommissionRoundTurn;
   string selected_spreads=InpMaxSpreadPoints, selected_slips=InpSlippagePoints;
   if(InpBridge=="AITrader\\demo-1" && InpDemoLogin==0 && InpDemoServer=="" &&
      FileIsExist("AITrader\\accounts.txt",FILE_COMMON))
   {
      int index=FileOpen("AITrader\\accounts.txt",FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,0,CP_UTF8);
      if(index==INVALID_HANDLE) return InitFailure("Cannot read AITrader accounts index");
      int matches=0;
      while(!FileIsEnding(index))
      {
         string row=FileReadString(index);
         string cells[];
         int field_count=StringSplit(row,'|',cells);
         if((field_count!=9&&field_count!=11) || cells[0]!="1") continue;
         if(cells[1]==Account() && cells[2]==Server() && (ulong)StringToInteger(cells[3])==InpMagic)
         {
            matches++;
            selected_bridge=cells[4]; selected_symbols=cells[5]; selected_commissions=cells[6];
            selected_spreads=cells[7]; selected_slips=cells[8];
            bound_mode=field_count==11?cells[9]:"demo";
            live_allowed=field_count==11&&cells[10]=="1";
         }
      }
      FileClose(index);
      if(matches!=1) return InitFailure("Account login/server not uniquely registered; save all accounts in setup");
   }
   if(StringFind(selected_bridge,"..")>=0||StringFind(selected_bridge,":")>=0||StringSubstr(selected_bridge,0,1)=="\\") return InitFailure("InpBridge must be a relative Common Files path");
   base=selected_bridge+"\\";
   string folder=""; string parts[]; int count=StringSplit(selected_bridge,'\\',parts);
   for(int i=0;i<count;i++)
   {
      if(parts[i]==""||parts[i]=="."||parts[i]=="..") return InitFailure("InpBridge contains an empty or dot component");
      folder+=(i>0?"\\":"")+parts[i];
      FolderCreate(folder,FILE_COMMON); // existing folder may return false; checked by following file operations
   }
   string binding[];
   if(FileIsExist(base+"binding.csv",FILE_COMMON))
   {
      int field_count=StringSplit(Read("binding.csv"),',',binding);
      if((field_count!=4&&field_count!=6) || binding[0]!="1" ||
         !DigitsOnly(binding[1]) || binding[2]=="" || !DigitsOnly(binding[3]) ||
         (ulong)StringToInteger(binding[3])!=InpMagic)
      return InitFailure("Invalid binding.csv; save settings again in AI Trader");
      bound_account=binding[1]; bound_server=binding[2];
      string binding_mode=field_count==6?binding[4]:"demo";
      bool binding_live=field_count==6&&binding[5]=="1";
      if(binding_mode!=bound_mode||binding_live!=live_allowed) return InitFailure("Account mode differs between index and binding; save settings again");
      if((InpDemoLogin!=0 && (string)InpDemoLogin!=bound_account) ||
         (InpDemoServer!="" && InpDemoServer!=bound_server))
      return InitFailure("EA login/server inputs disagree with binding.csv");
   }
   else if(InpDemoLogin>0 && InpDemoServer!="")
   { bound_account=(string)InpDemoLogin; bound_server=InpDemoServer; }
   else
   return InitFailure("No binding.csv: enter BOTH account login and server in EA inputs, or save settings in AI Trader");
   if((bound_mode!="demo"&&bound_mode!="real")||(bound_mode=="demo"&&live_allowed)||!AccountMatched())
      return InitFailure("MT5 account mode or login/server differs: login="+Account()+", server="+Server());
   long server_hash=0;
   for(int c=0;c<StringLen(Server());c++) server_hash=(server_hash*131+StringGetCharacter(Server(),c))%2147483647;
   lock_handle=FileOpen("AITrader-account-"+Account()+"-"+(string)server_hash+"-"+(string)InpMagic+".lock",FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(lock_handle==INVALID_HANDLE) { Print("Another AITrader EA owns this account/magic"); return INIT_FAILED; }
   configured_n=selected_symbols=="AUTO"?0:StringSplit(selected_symbols,',',requested);
   int commission_count=StringSplit(selected_commissions,',',commissions);
   int spread_count=StringSplit(selected_spreads,',',spreads);
   int slip_count=StringSplit(selected_slips,',',slips);
   if(configured_n<0||configured_n>10||(commission_count!=1&&commission_count!=configured_n)||
      (spread_count!=1&&spread_count!=configured_n)||(slip_count!=1&&slip_count!=configured_n)||
      (configured_n==0&&(commission_count!=1||spread_count!=1||slip_count!=1)))
      return InitFailure("Cost override counts: symbols="+(string)configured_n+", commission="+(string)commission_count+", spread="+(string)spread_count+", slippage="+(string)slip_count);
   for(int i=0;i<commission_count;i++) if(!DecimalNumber(commissions[i],true)||StringToDouble(commissions[i])<-1) return InitFailure("Invalid commission override");
   for(int i=0;i<spread_count;i++) if(!DecimalNumber(spreads[i])||StringToDouble(spreads[i])<=0) return InitFailure("Invalid max spread override");
   for(int i=0;i<slip_count;i++) if(!DigitsOnly(slips[i])) return InitFailure("Invalid slippage override");
   if(commission_count==1) shared_commission=commissions[0];
   shared_spread=spreads[0]; shared_slip=slips[0];
   for(int i=1;i<spread_count;i++) if(StringToDouble(spreads[i])<StringToDouble(shared_spread)) shared_spread=spreads[i];
   for(int i=1;i<slip_count;i++) if(StringToDouble(slips[i])<StringToDouble(shared_slip)) shared_slip=slips[i];
   if(configured_n>0)
   {
      if(commission_count==1) { string value=commissions[0]; ArrayResize(commissions,configured_n); for(int i=0;i<configured_n;i++) commissions[i]=value; }
      if(spread_count==1) { string value=spreads[0]; ArrayResize(spreads,configured_n); for(int i=0;i<configured_n;i++) spreads[i]=value; }
      if(slip_count==1) { string value=slips[0]; ArrayResize(slips,configured_n); for(int i=0;i<configured_n;i++) slips[i]=value; }
   }
   ArrayResize(configured_syms,configured_n);
   for(int i=0;i<configured_n;i++)
   {
      string wanted=requested[i]; StringTrimLeft(wanted); StringTrimRight(wanted);
      if(!SafeToken(wanted)) return InitFailure("Invalid cost override symbol at item "+(string)(i+1));
      int matches=0; string match=""; bool custom=false;
      if(SymbolExist(wanted,custom)) { match=wanted; matches=1; }
      else for(int j=0;j<SymbolsTotal(false);j++) { string candidate=SymbolName(j,false); if(StringFind(candidate,wanted)==0) { matches++; match=candidate; } }
      configured_syms[i]=matches==1&&SafeToken(match)?match:"";
      for(int j=0;j<i;j++) if(configured_syms[i]!=""&&configured_syms[i]==configured_syms[j]) return InitFailure("Two cost overrides map to one broker symbol: "+configured_syms[i]);
   }
   ArrayCopy(configured_commissions,commissions);
   ArrayCopy(configured_spreads,spreads); ArrayCopy(configured_slips,slips);
   trade.SetExpertMagicNumber(InpMagic); trade.SetAsyncMode(false); trade.SetDeviationInPoints(10);
   LoadState(); LoadUsed(); RefreshWatchlist(); ReadPolicy(); Protect(); Snapshot(); Catalog(); Panel(); ExportDeals();
   EventSetTimer(1); return INIT_SUCCEEDED;
}
void OnTimer()
{
   if(!AccountMatched()) { enabled=false; return; }
   RefreshWatchlist(); ReadPolicy(); Protect(); Command();
   ulong now=GetTickCount64();
   if(now-last_snapshot>=5000) { Snapshot(); last_snapshot=now; }
   if(now-last_catalog>=60000) { Catalog(); last_catalog=now; }
   if(now-last_panel>=2000) { Panel(); last_panel=now; }
   if(now-last_history>=30000) { ExportDeals(); last_history=now; }
}
void OnTick() { Protect(); }
void OnTradeTransaction(const MqlTradeTransaction &trans,const MqlTradeRequest &request,const MqlTradeResult &result)
{
   last_snapshot=0; last_history=0;
}
void OnDeinit(const int reason)
{
   EventKillTimer(); if(state_ok) SaveState();
   if(lock_handle!=INVALID_HANDLE) FileClose(lock_handle);
   ObjectsDeleteAll(0,prefix); Comment("");
}
