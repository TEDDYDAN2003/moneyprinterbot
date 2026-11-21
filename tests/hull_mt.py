"""Isolated fake MT5 terminal and desk fixtures. No terminal or network access."""

import time
from pathlib import Path
from types import SimpleNamespace as NS

from bot.hull.structure import Structure

from bot.hull.signal import Reading

T = 1794312000  # Tue 12:00 UTC
ROOT = Path(__file__).resolve().parents[1]


def scanner_clock(monkeypatch, wall, advance, mono=time.monotonic):
    """Do not replace process-global sleep: Git/subprocess waits aren't market ticks."""
    clock = NS(time=wall, sleep=advance, monotonic=mono)
    for module in ("desk", "ladder_desk", "terminal"):
        monkeypatch.setattr("bot.hull." + module + ".time", clock)


class MT:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    POSITION_TYPE_BUY = ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    TRADE_RETCODE_DONE = 10009
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_CLOSEONLY = 3
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_LONGONLY = 1
    TIMEFRAME_M1 = 1
    TIMEFRAME_M2 = 2
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_D1 = 16408

    def __init__(self):
        self.a = NS(
            connected=True,
            login=1,
            server="t",
            trade_mode=0,
            margin_mode=2,
            currency="USD",
            trade_allowed=True,
            trade_expert=True,
            equity=10000.0,
            margin_free=9000.0,
        )
        self.i = NS(
            name="XAUUSDm",
            chart_mode=0,
            trade_tick_size=0.001,
            point=0.001,
            digits=3,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_stops_level=0,
            trade_freeze_level=0,
            trade_mode=4,
            filling_mode=1,
            trade_contract_size=100,
            currency_profit="USD",
        )
        self.q = NS(time_msc=T * 1000 + 100, bid=2000.0, ask=2000.1)
        self.positions = []
        self.orders = []
        self.deals = []
        self.catalog = [self.i]
        self.requests = []
        self.retprice = None
        self.bad = False

    def account_info(self):
        return self.a

    def terminal_info(self):
        return self.a

    def symbol_info(self, n):
        return self.i

    def symbol_select(self, *args):
        return True

    def symbols_get(self):
        return self.catalog

    def symbol_info_tick(self, n):
        return self.q

    def positions_get(self, ticket=None):
        return [p for p in self.positions if ticket is None or p.ticket == ticket]

    def history_deals_get(self, start, end):
        return self.deals

    def orders_get(self):
        return self.orders

    def order_calc_profit(self, tp, n, l, a, b):
        return (1 if tp == 0 else -1) * (b - a) * l * 100

    def order_calc_margin(self, *args):
        return 80.0

    def order_check(self, r):
        return NS(retcode=0, margin_free=8900.0)

    def order_send(self, r):
        self.requests.append(dict(r))
        ret = NS(retcode=10009)
        if self.bad:
            return None
        if "position" in r:
            p = self.positions_get(ticket=r["position"])[0]
            if r["action"] == 6:
                p.sl = r["sl"]
                p.tp = r["tp"]
            else:
                self.positions.remove(p)
            return ret
        i = len(self.requests)
        fill = self.retprice or r["price"]
        p = NS(
            ticket=i,
            identifier=i,
            time_msc=self.q.time_msc,
            magic=r["magic"],
            symbol=r["symbol"],
            type=r["type"],
            sl=r["sl"],
            tp=r["tp"],
            volume=r["volume"],
            price_open=fill,
            profit=0.0,
        )
        self.positions.append(p)
        return NS(retcode=10009, volume=r["volume"], price=fill, deal=i, order=i)


def sig(sl=1999.95, side=1):
    return Reading(
        T,
        1,
        2000.0,
        2000.0,
        2001.0,
        0.4,
        side,
        side,
        "ready",
        sl + side * 0.04,
        T - 120,
        sl,
        T - 300,
        4,
        None,
        None,
        Structure(closed_s=T, bias=side),
        "low" if side == 1 else "high",
    )
