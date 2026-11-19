"""Synthetic MT5-shaped records only; never a price or execution feed."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledger import MAGIC

T = int(datetime(2026, 11, 2, 7, tzinfo=UTC).timestamp())
BIG = 90071992547409901


def deal(
    id,
    pid,
    at,
    typ,
    ent,
    vol,
    px,
    *,
    sy="XAUUSDm",
    magic=MAGIC,
    comment="",
    profit=0,
    commission=0,
    swap=0,
    fee=0,
    reason=3,
    ms=0,
):
    return NS(
        ticket=id,
        position_id=pid,
        order=id + 100,
        symbol=sy,
        magic=magic,
        type=typ,
        entry=ent,
        volume=vol,
        price=px,
        time=at,
        time_msc=at * 1000 + ms,
        profit=profit,
        commission=commission,
        swap=swap,
        fee=fee,
        reason=reason,
        comment=comment,
    )


class FakeMT:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

    def __init__(self, ds, at):
        self.ds = ds
        self.at = at
        self.login = 726384
        self.online = True
        self.missing = None
        self.ps_override = None
        self.currency = "USD"
        self.host = "Synthetic-Demo"

    def terminal_info(self):
        return NS(connected=self.online)

    def account_info(self):
        return NS(
            login=self.login,
            server=self.host,
            currency=self.currency,
            currency_digits=2,
            trade_mode=0,
            margin_mode=2,
        )

    def history_deals_get(self, *span, position=None):
        if self.missing == ("position" if position is not None else "range"):
            return None
        ds = [d for d in self.ds if d.time <= self.at]
        if position is not None:
            return tuple(d for d in ds if d.position_id == position)
        return tuple(
            d for d in ds if span[0].timestamp() <= d.time <= span[1].timestamp()
        )

    def positions_get(self):
        if self.missing == "positions":
            return None
        if self.ps_override is not None:
            return self.ps_override
        grouped = {}
        for d in self.ds:
            if d.type not in (0, 1) or d.entry not in (0, 1, 3) or d.time > self.at:
                continue
            a = grouped.setdefault(d.position_id, [0, None])
            a[0] += d.volume * (1 if d.entry == 0 else -1)
            if d.entry == 0:
                a[1] = d
        return tuple(
            NS(identifier=pid, ticket=pid + 777, magic=a[1].magic, symbol=a[1].symbol)
            for pid, a in grouped.items()
            if a[0] > 1e-7 and a[1]
        )


def sample():
    import random

    rs = random.Random(1603)
    ds = []
    models = {}
    id = BIG
    symbols = [
        ("XAUUSDm", 3678.41, 2, 0.10, 100.0),
        ("GBPJPYm", 201.945, 3, 0.20, 630.0),
        ("EURUSDm", 1.12841, 5, 0.50, 100000.0),
        ("USDJPYm", 152.815, 3, 0.20, 655.0),
        ("US30m", 43165.5, 1, 0.10, 1.0),
    ]
    outcomes = [
        1,
        1,
        -1,
        1,
        -1,
        -1,
        1,
        1,
        -1,
        1,
        1,
        -1,
        1,
        1,
        -1,
        1,
        1,
        -1,
        1,
        -1,
        1,
        1,
        -1,
        1,
    ]
    for i, direction in enumerate(outcomes):
        sy, ref, digits, lot, mult = symbols[[0, 1, 2, 0, 3, 4, 0, 1, 2, 0, 4][i % 11]]
        tf = [1, 2, 5, 2, 1, 5][i % 6]
        typ = int((i % 5) in (1, 2, 4))
        side = ("BUY", "SELL")[typ]
        stamp = (
            T
            - 7 * 3600
            + 86400 * (i // 2)
            + [1, 14, 8, 19, 5, 13, 10, 22][i % 8] * 3600
            + (12 if i % 2 == 0 else 36) * 60
        )
        n = [3, 4, 7, 3, 4, 3, 6, 3][i % 8]
        base = round(ref + rs.uniform(-0.0007, 0.0007) * ref, digits)
        models[(sy, tf, stamp, side)] = (
            ("bull_discount_buy" if i % 3 else "buy_below_bear_bos")
            if typ == 0
            else ("bear_premium_sell" if i % 3 else "sell_above_bull_bos")
        )
        if i == 6:
            models.pop((sy, tf, stamp, side))  # sample with no saved entry reason
        for j in range(n):
            pid = 720003081 + i * 12 + j
            px = round(base + rs.uniform(-1.5, 1.5) * 10**-digits, digits)
            gross = round(
                rs.uniform(7.8, 14.5)
                * (direction if j == 0 or rs.random() > 0.16 else -direction),
                2,
            )
            if i == 19 and j == 0:
                gross = 0.38
            close = round(px + gross / (lot * mult) * (1 if typ == 0 else -1), digits)
            end = stamp + rs.randint(75, 4100)
            ds.append(
                deal(
                    id,
                    pid,
                    stamp + 1 + j // 4,
                    typ,
                    0,
                    lot,
                    px,
                    sy=sy,
                    comment=f"h{tf}_{stamp}_{j + 1}",
                    commission=-0.18,
                    ms=93 + j * 34,
                )
            )
            id += 1
            ds.append(
                deal(
                    id,
                    pid,
                    end,
                    1 - typ,
                    1,
                    lot,
                    close,
                    sy=sy,
                    profit=gross,
                    commission=-0.18,
                    fee=-0.02,
                    reason=(0 if i == 3 else 3 if i == 9 else 5 if gross > 0 else 4),
                    comment=("hull_exit" if i == 9 else ""),
                    ms=128 + j * 33,
                )
            )
            id += 1
    return ds, models
