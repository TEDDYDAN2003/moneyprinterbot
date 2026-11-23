import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize()
positions = mt5.positions_get()
if positions:
    for p in positions:
        print(f"Ticket: {p.ticket}  Symbol: {p.symbol}  Type: {'BUY' if p.type==0 else 'SELL'}  Lots: {p.volume}  Open: {p.price_open}  Profit: {p.profit:.2f}  Magic: {p.magic}")
else:
    print("No open positions")
mt5.shutdown()
