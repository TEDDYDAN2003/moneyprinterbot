"""Causal research-only OHLC market-structure engine.

Swing pivots use an ATR-scaled directional-change rule rather than a fixed
left/right candle count. See STRUCTURE_METHOD.md for timing and semantics.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr14(bars: pd.DataFrame) -> pd.Series:
    prev = bars.close.shift(1)
    tr = pd.concat([bars.high-bars.low, (bars.high-prev).abs(),
                    (bars.low-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=14).mean()


def resample_m1(raw: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Match the research convention: close-time index, complete bars only."""
    x = raw.copy()
    x.index = x.index + pd.Timedelta(minutes=1)
    rule = f"{minutes}min"
    bars = x.resample(rule, label="right", closed="right").agg(
        {"open":"first", "high":"max", "low":"min", "close":"last",
         "volume":"sum", "spread":"max"})
    counts = x.close.resample(rule, label="right", closed="right").count()
    return bars.loc[counts.eq(minutes)].dropna(subset=["open","high","low","close"])


def structure(bars: pd.DataFrame, tf: str, k: float = 1.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return causal per-close state and confirmed structural events."""
    a = atr14(bars).to_numpy(float)
    o, h, l, c = (bars[x].to_numpy(float) for x in ("open","high","low","close"))
    ix = bars.index
    n = len(bars)
    states = {"atr14":np.full(n,np.nan),"trend":np.zeros(n,dtype=np.int8),
        "latest_swing_high":np.full(n,np.nan),"latest_swing_low":np.full(n,np.nan),
        "latest_swing_high_time":np.full(n,np.datetime64("NaT", "ns"),dtype="datetime64[ns]"),
        "latest_swing_low_time":np.full(n,np.datetime64("NaT", "ns"),dtype="datetime64[ns]"),
        "protected_high":np.full(n,np.nan),"protected_low":np.full(n,np.nan),
        "last_bos":np.full(n,"",dtype="U8"),"last_mss":np.full(n,"",dtype="U8"),
        "consolidation":np.zeros(n,dtype=bool),"range_compressed":np.zeros(n,dtype=bool),
        "mixed_swings":np.zeros(n,dtype=bool),"confirmed_swing_high_class":np.full(n,"",dtype="U2"),
        "confirmed_swing_low_class":np.full(n,"",dtype="U2")}
    events = []
    mode = 0  # 0 initializing, +1 tracking high, -1 tracking low
    extreme = np.nan; ext_idx = -1; ext_atr = np.nan
    prev_hi = prev_lo = np.nan
    latest_hi = latest_lo = np.nan
    latest_hi_time = latest_lo_time = pd.NaT
    latest_hi_idx = latest_lo_idx = -1
    trend = 0
    prot_hi = prot_lo = np.nan
    wait_up = wait_dn = np.nan
    armed_hi = armed_lo = np.nan
    pending_up_low = pending_dn_high = np.nan
    bull_leg_high = bear_leg_low = np.nan
    last_bos = last_mss = ""
    swing_types = []
    hi_class = lo_class = ""

    def emit(kind, direction, level, when, pivot_time=None, details=None):
        events.append({"time": when, "timeframe": tf, "event": kind,
                       "direction": direction, "level": float(level) if np.isfinite(level) else np.nan,
                       "pivot_time": pivot_time, **(details or {})})

    for i in range(n):
        if i and np.isfinite(a[i]) and np.isfinite(a[i-1]):
            # Update/confirm the running directional-change extreme using this
            # completed candle. Confirmation is timestamped at i, never i-ext.
            if mode == 0:
                # Bootstrap with a high-side candidate; it is not exposed until
                # a later completed close confirms the ATR-sized reversal.
                mode = 1; extreme, ext_idx, ext_atr = h[i], i, a[i]
            elif mode == 1:
                if h[i] > extreme:
                    extreme, ext_idx, ext_atr = h[i], i, a[i]
                if c[i] <= extreme-k*ext_atr:
                    pivot = extreme; pidx = ext_idx
                    latest_hi, latest_hi_time, latest_hi_idx = pivot, ix[pidx], pidx
                    cls = "HH" if not np.isfinite(prev_hi) or pivot > prev_hi else "LH"
                    prev_hi = pivot; swing_types.append(cls); hi_class = cls
                    emit("swing_confirmed","high",pivot,ix[i],ix[pidx],{"class":cls})
                    mode = -1; extreme, ext_idx, ext_atr = l[i], i, a[i]
            else:
                if l[i] < extreme:
                    extreme, ext_idx, ext_atr = l[i], i, a[i]
                if c[i] >= extreme+k*ext_atr:
                    pivot = extreme; pidx = ext_idx
                    latest_lo, latest_lo_time, latest_lo_idx = pivot, ix[pidx], pidx
                    cls = "HL" if not np.isfinite(prev_lo) or pivot > prev_lo else "LL"
                    prev_lo = pivot; swing_types.append(cls); lo_class = cls
                    emit("swing_confirmed","low",pivot,ix[i],ix[pidx],{"class":cls})
                    mode = 1; extreme, ext_idx, ext_atr = h[i], i, a[i]

        # BOS is a strict close beyond the most recent confirmed opposite pivot.
        # Protected levels are accumulated pullback extremes since that pivot.
        if np.isfinite(latest_hi):
            if not np.isfinite(armed_hi) or latest_hi != armed_hi:
                wait_up = latest_hi; armed_hi = latest_hi
                # Once the pivot is confirmed, the already completed pullback
                # bars since its extreme are known and seed the protected low.
                pending_up_low = np.min(l[latest_hi_idx:i+1])
            elif np.isfinite(wait_up):
                pending_up_low = min(pending_up_low, l[i])
        if np.isfinite(latest_lo):
            if not np.isfinite(armed_lo) or latest_lo != armed_lo:
                wait_dn = latest_lo; armed_lo = latest_lo
                pending_dn_high = np.max(h[latest_lo_idx:i+1])
            elif np.isfinite(wait_dn):
                pending_dn_high = max(pending_dn_high, h[i])

        # MSS has priority over continuation BOS if the close invalidates the
        # established protected level on this bar.
        if trend == 1 and np.isfinite(prot_lo) and c[i] < prot_lo:
            last_mss = "bearish"
            emit("MSS","bearish",prot_lo,ix[i],details={"previous_trend":"bullish"})
            trend = -1
            prot_hi = bull_leg_high if np.isfinite(bull_leg_high) else latest_hi
            prot_lo = np.nan
            bear_leg_low = l[i]
            wait_dn = latest_lo
        elif trend == -1 and np.isfinite(prot_hi) and c[i] > prot_hi:
            last_mss = "bullish"
            emit("MSS","bullish",prot_hi,ix[i],details={"previous_trend":"bearish"})
            trend = 1
            prot_lo = bear_leg_low if np.isfinite(bear_leg_low) else latest_lo
            prot_hi = np.nan
            bull_leg_high = h[i]
            wait_up = latest_hi
        else:
            if np.isfinite(wait_up) and c[i] > wait_up and (trend <= 0 or wait_up >= (latest_hi if np.isfinite(latest_hi) else wait_up)):
                oldtrend = trend
                trend = 1; last_bos = "bullish"
                prot_lo = pending_up_low
                prot_hi = np.nan
                bull_leg_high = h[i]
                emit("BOS","bullish",wait_up,ix[i],ix[latest_hi_idx],{"previous_trend":oldtrend,"protected_low":prot_lo})
                wait_up = np.nan
            elif np.isfinite(wait_dn) and c[i] < wait_dn and (trend >= 0 or wait_dn <= (latest_lo if np.isfinite(latest_lo) else wait_dn)):
                oldtrend = trend
                trend = -1; last_bos = "bearish"
                prot_hi = pending_dn_high
                prot_lo = np.nan
                bear_leg_low = l[i]
                emit("BOS","bearish",wait_dn,ix[i],ix[latest_lo_idx],{"previous_trend":oldtrend,"protected_high":prot_hi})
                wait_dn = np.nan

        if trend == 1: bull_leg_high = max(bull_leg_high, h[i]) if np.isfinite(bull_leg_high) else h[i]
        if trend == -1: bear_leg_low = min(bear_leg_low, l[i]) if np.isfinite(bear_leg_low) else l[i]

        # Initial neutral state can be seeded by the first close-confirmed break.
        rng20 = (np.nanmax(h[max(0,i-19):i+1])-np.nanmin(l[max(0,i-19):i+1]))
        compressed = bool(np.isfinite(a[i]) and rng20 < 3.5*a[i])
        mixed = len(swing_types)>=4 and len(set(swing_types[-4:]))>=3
        states["atr14"][i]=a[i]; states["trend"][i]=trend
        states["latest_swing_high"][i]=latest_hi; states["latest_swing_low"][i]=latest_lo
        if not pd.isna(latest_hi_time): states["latest_swing_high_time"][i]=np.datetime64(latest_hi_time.tz_localize(None))
        if not pd.isna(latest_lo_time): states["latest_swing_low_time"][i]=np.datetime64(latest_lo_time.tz_localize(None))
        states["protected_high"][i]=prot_hi; states["protected_low"][i]=prot_lo
        states["last_bos"][i]=last_bos; states["last_mss"][i]=last_mss
        states["consolidation"][i]=compressed or mixed; states["range_compressed"][i]=compressed
        states["mixed_swings"][i]=mixed; states["confirmed_swing_high_class"][i]=hi_class
        states["confirmed_swing_low_class"][i]=lo_class
    return pd.DataFrame(states,index=ix).rename_axis("time"), pd.DataFrame(events)


def validate_causality_and_semantics() -> dict:
    """Small deterministic checks for timestamping and strict close breaks."""
    idx = pd.date_range("2025-01-01", periods=90, freq="min", tz="UTC")
    close = np.array([100 + .03*np.sin(i) for i in range(90)], float)
    close[20:42] = np.linspace(100.1,103.0,22)
    close[42:50] = np.linspace(102.7,99.5,8)
    close[50:70] = np.linspace(99.8,104.5,20)
    close[70:78] = np.linspace(104.1,98.8,8)
    close[78:] = np.linspace(99.2,106.0,12)
    bars = pd.DataFrame({"open":close, "high":close+.02,"low":close-.02,"close":close},index=idx)
    _, ev = structure(bars,"M1",k=1.0)
    checks = {"engine_runs": True,
              "event_times_not_before_confirmation": bool((ev.time >= ev.pivot_time.fillna(ev.time)).all()) if len(ev) else False,
              "swing_records_have_confirmation": bool((ev.loc[ev.event.eq("swing_confirmed"),"time"].notna()).all()),
              "bullish_bos_seen":bool(((ev.event=="BOS")&(ev.direction=="bullish")).any()),
              "bearish_bos_seen":bool(((ev.event=="BOS")&(ev.direction=="bearish")).any()),
              "bullish_mss_seen":bool(((ev.event=="MSS")&(ev.direction=="bullish")).any()),
              "bearish_mss_seen":bool(((ev.event=="MSS")&(ev.direction=="bearish")).any()),
              "bos_records_protected_level":bool(ev.loc[ev.event.eq("BOS"),["protected_low","protected_high"]].notna().any(axis=1).all()) if (ev.event=="BOS").any() else False}
    # Protected level on a BOS equals the pullback extreme from the pivot to
    # its confirming close. This confirms the retrospective scan is causal:
    # only bars already closed at the BOS timestamp are included.
    swinghighs=ev.loc[ev.event.eq("swing_confirmed")&ev.direction.eq("high")]
    bull_bos=ev.loc[ev.event.eq("BOS")&ev.direction.eq("bullish")]
    tests=[]
    for b in bull_bos.itertuples():
        prior=swinghighs[swinghighs.level.eq(b.level)].tail(1)
        if len(prior):
            ptime=pd.Timestamp(prior.iloc[0].pivot_time)
            end=idx.get_loc(pd.Timestamp(b.time))
            start=idx.get_loc(ptime)
            tests.append(abs(float(b.protected_low)-float(bars.low.iloc[start:end+1].min()))<1e-9)
    checks["protected_low_includes_confirmed_pullback"] = bool(tests and all(tests))
    # A wick-only high beyond a confirmed level is explicitly followed by a
    # close that stays below it; no bullish BOS may be attributed to that bar.
    wickbars=bars.copy(); wickbars.iloc[20,wickbars.columns.get_loc("high")]=104.0
    wickbars.iloc[20,wickbars.columns.get_loc("close")]=100.0
    _, wev=structure(wickbars,"M1",k=1.0)
    checks["wick_only_does_not_create_bos"] = not bool(((wev.event=="BOS")&(wev.direction=="bullish")&(wev.time==idx[20])).any())
    return checks
