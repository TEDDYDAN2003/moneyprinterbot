from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bot.models import Candle, StrategyConfig, StructureSnapshot


@dataclass
class _StructureState:
    length: int
    zone_len: int
    is_internal: bool
    trend: int = 0
    last_high: Optional[float] = None
    last_high_bar: Optional[int] = None
    last_low: Optional[float] = None
    last_low_bar: Optional[int] = None
    last_hh: Optional[float] = None
    last_hl: Optional[float] = None
    last_ll: Optional[float] = None
    last_lh: Optional[float] = None
    hi_type: int = 0
    lo_type: int = 0
    bos_hi: Optional[float] = None
    bos_hi_bar: Optional[int] = None
    bos_hi_x: bool = True
    bos_lo: Optional[float] = None
    bos_lo_bar: Optional[int] = None
    bos_lo_x: bool = True
    rl: Optional[float] = None
    rl_bar: Optional[int] = None
    rh: Optional[float] = None
    rh_bar: Optional[int] = None
    prot_low: Optional[float] = None
    prot_low_bar: Optional[int] = None
    prot_high: Optional[float] = None
    prot_high_bar: Optional[int] = None
    ev_bar: int = -1
    choch_dir: int = 0
    choch_lvl: Optional[float] = None
    choch_bar: Optional[int] = None


class _WilderATR:
    """Matches TradingView ta.atr(14) using Wilder RMA."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: Optional[float] = None
        self._tr_seed: list[float] = []
        self.value: float = 0.0

    def update(self, candle: Candle) -> float:
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        self._prev_close = candle.close

        if len(self._tr_seed) < self.period:
            self._tr_seed.append(tr)
            self.value = sum(self._tr_seed) / len(self._tr_seed)
        else:
            alpha = 1.0 / self.period
            self.value = alpha * tr + (1.0 - alpha) * self.value
        return self.value


class StructureTracker:
    """
    Exact Python port of f_structure(len, zoneLen, isInt) from the Pine Script v6
    'Wickless Structure Strategy'.
    """

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg
        self.internal = _StructureState(cfg.internal_len, cfg.choch_zone_bars, True)
        self.external = _StructureState(cfg.external_len, cfg.choch_zone_bars, False)
        self.atr = _WilderATR(14)
        self.candles: list[Candle] = []

    @staticmethod
    def _pivot_high(candles: list[Candle], bar_index: int, length: int) -> Optional[float]:
        pj = bar_index - length
        if pj - length < 0:
            return None
        val = candles[pj].high
        # Must be >= left bars and strictly > right bars (standard TV pivot uniqueness)
        for idx in range(pj - length, pj):
            if candles[idx].high >= val:
                return None
        for idx in range(pj + 1, pj + length + 1):
            if candles[idx].high > val:
                return None
        return val

    @staticmethod
    def _pivot_low(candles: list[Candle], bar_index: int, length: int) -> Optional[float]:
        pj = bar_index - length
        if pj - length < 0:
            return None
        val = candles[pj].low
        for idx in range(pj - length, pj):
            if candles[idx].low <= val:
                return None
        for idx in range(pj + 1, pj + length + 1):
            if candles[idx].low < val:
                return None
        return val

    def _step_state(self, st: _StructureState, bar_index: int) -> Optional[str]:
        length = st.length
        candles = self.candles
        c = candles[bar_index]
        ph = self._pivot_high(candles, bar_index, length)
        pl = self._pivot_low(candles, bar_index, length)
        event_note: Optional[str] = None
        prefix = "INT" if st.is_internal else "EXT"

        pj = bar_index - length
        if ph is not None:
            if st.last_high is not None:
                if ph > st.last_high:
                    st.last_hh = ph
                    st.hi_type = 1
                elif ph < st.last_high:
                    st.last_lh = ph
                    st.hi_type = -1
            st.last_high = ph
            st.last_high_bar = pj
            if pj >= st.ev_bar and (st.bos_hi is None or st.bos_hi_x or ph > st.bos_hi):
                st.bos_hi = ph
                st.bos_hi_bar = pj
                st.bos_hi_x = False
                st.rl = None
                st.rl_bar = None
                if length > 1:
                    for k in range(1, length):
                        idx = bar_index - k
                        if idx >= 0 and (st.rl is None or candles[idx].low < st.rl):
                            st.rl = candles[idx].low
                            st.rl_bar = idx

        if pl is not None:
            if st.last_low is not None:
                if pl > st.last_low:
                    st.last_hl = pl
                    st.lo_type = 1
                elif pl < st.last_low:
                    st.last_ll = pl
                    st.lo_type = -1
            st.last_low = pl
            st.last_low_bar = pj
            if pj >= st.ev_bar and (st.bos_lo is None or st.bos_lo_x or pl < st.bos_lo):
                st.bos_lo = pl
                st.bos_lo_bar = pj
                st.bos_lo_x = False
                st.rh = None
                st.rh_bar = None
                if length > 1:
                    for k in range(1, length):
                        idx = bar_index - k
                        if idx >= 0 and (st.rh is None or candles[idx].high > st.rh):
                            st.rh = candles[idx].high
                            st.rh_bar = idx

        done = False
        if st.trend == -1 and st.prot_high is not None and c.close > st.prot_high:
            leg_low: Optional[float] = None
            leg_low_bar: Optional[int] = None
            cnt = 0 if st.prot_high_bar is None else min(bar_index - 1 - st.prot_high_bar, 1500)
            if cnt >= 1:
                for k in range(1, cnt + 1):
                    idx = bar_index - k
                    if idx >= 0 and (leg_low is None or candles[idx].low < leg_low):
                        leg_low = candles[idx].low
                        leg_low_bar = idx
            st.choch_dir = 1
            st.choch_bar = bar_index
            st.choch_lvl = st.last_low if leg_low is None else leg_low
            st.trend = 1
            st.prot_low = leg_low
            st.prot_low_bar = leg_low_bar
            st.prot_high = None
            st.prot_high_bar = None
            st.last_ll = None
            st.last_lh = None
            st.hi_type = 0
            st.lo_type = 0
            st.bos_hi = None
            st.bos_hi_x = True
            st.bos_lo = None
            st.bos_lo_x = True
            st.ev_bar = bar_index
            done = True
            event_note = f"{prefix} MSS UP"

        elif st.trend == 1 and st.prot_low is not None and c.close < st.prot_low:
            leg_high: Optional[float] = None
            leg_high_bar: Optional[int] = None
            cnt = 0 if st.prot_low_bar is None else min(bar_index - 1 - st.prot_low_bar, 1500)
            if cnt >= 1:
                for k in range(1, cnt + 1):
                    idx = bar_index - k
                    if idx >= 0 and (leg_high is None or candles[idx].high > leg_high):
                        leg_high = candles[idx].high
                        leg_high_bar = idx
            st.choch_dir = -1
            st.choch_bar = bar_index
            st.choch_lvl = st.last_high if leg_high is None else leg_high
            st.trend = -1
            st.prot_high = leg_high
            st.prot_high_bar = leg_high_bar
            st.prot_low = None
            st.prot_low_bar = None
            st.last_hh = None
            st.last_hl = None
            st.hi_type = 0
            st.lo_type = 0
            st.bos_hi = None
            st.bos_hi_x = True
            st.bos_lo = None
            st.bos_lo_x = True
            st.ev_bar = bar_index
            done = True
            event_note = f"{prefix} MSS DOWN"

        if not done:
            if st.trend != -1 and st.bos_hi is not None and not st.bos_hi_x and c.close > st.bos_hi:
                st.bos_hi_x = True
                if st.rl_bar is not None:
                    st.prot_low = st.rl
                    st.prot_low_bar = st.rl_bar
                elif st.prot_low is None and st.last_low is not None and st.last_low < c.close:
                    st.prot_low = st.last_low
                    st.prot_low_bar = st.last_low_bar
                st.trend = 1
                if st.choch_dir == 1:
                    st.choch_dir = 0
                st.ev_bar = bar_index
                event_note = f"{prefix} BOS UP"

            elif st.trend != 1 and st.bos_lo is not None and not st.bos_lo_x and c.close < st.bos_lo:
                st.bos_lo_x = True
                if st.rh_bar is not None:
                    st.prot_high = st.rh
                    st.prot_high_bar = st.rh_bar
                elif st.prot_high is None and st.last_high is not None and st.last_high > c.close:
                    st.prot_high = st.last_high
                    st.prot_high_bar = st.last_high_bar
                st.trend = -1
                if st.choch_dir == -1:
                    st.choch_dir = 0
                st.ev_bar = bar_index
                event_note = f"{prefix} BOS DOWN"

        if st.bos_hi is not None and (st.rl is None or c.low < st.rl):
            st.rl = c.low
            st.rl_bar = bar_index
        if st.bos_lo is not None and (st.rh is None or c.high > st.rh):
            st.rh = c.high
            st.rh_bar = bar_index

        return event_note

    def prov_high(self, from_bar: Optional[int], bar_index: int) -> Optional[float]:
        if from_bar is None:
            return None
        n = bar_index - 1 - from_bar
        if n < 1:
            return None
        h: Optional[float] = None
        for k in range(1, min(n, 300) + 1):
            idx = bar_index - k
            if idx >= 0:
                val = self.candles[idx].high
                h = val if h is None else max(h, val)
        return h

    def prov_low(self, from_bar: Optional[int], bar_index: int) -> Optional[float]:
        if from_bar is None:
            return None
        n = bar_index - 1 - from_bar
        if n < 1:
            return None
        l: Optional[float] = None
        for k in range(1, min(n, 300) + 1):
            idx = bar_index - k
            if idx >= 0:
                val = self.candles[idx].low
                l = val if l is None else min(l, val)
        return l

    def update(self, candle: Candle) -> StructureSnapshot:
        self.candles.append(candle)
        bar_index = len(self.candles) - 1
        atr_val = self.atr.update(candle)

        int_event = self._step_state(self.internal, bar_index)
        ext_event = self._step_state(self.external, bar_index)

        choch_on = (
            self.internal.choch_dir != 0
            and self.internal.choch_bar is not None
            and (bar_index - self.internal.choch_bar) <= self.internal.zone_len
        )

        # Consolidation context (identical to Pine)
        consol_range = False
        if self.cfg.consol_range_atr and len(self.candles) >= self.cfg.consol_bars and atr_val > 0:
            window = self.candles[-self.cfg.consol_bars :]
            range_n = max(x.high for x in window) - min(x.low for x in window)
            consol_range = range_n < atr_val * self.cfg.consol_mult

        consol_mixed = self.cfg.consol_mixed_swings and (
            self.internal.hi_type * self.internal.lo_type == -1
        )
        consol_micro = False
        if self.cfg.use_consolidation and len(self.candles) >= self.cfg.consol_bars:
            window = self.candles[-self.cfg.consol_bars :]
            small_bodies = 0
            directions: list[int] = []
            for item in window:
                candle_range = item.high - item.low
                body = abs(item.close - item.open)
                if candle_range > 0 and body / candle_range <= self.cfg.consol_small_body_ratio:
                    small_bodies += 1
                if item.close > item.open:
                    directions.append(1)
                elif item.close < item.open:
                    directions.append(-1)
            alternations = sum(
                left != right for left, right in zip(directions, directions[1:])
            )
            alternation_fraction = (
                alternations / (len(directions) - 1) if len(directions) > 1 else 0.0
            )
            consol_micro = (
                small_bodies / len(window) >= self.cfg.consol_small_body_fraction
                and alternation_fraction >= self.cfg.consol_alternation_fraction
            )
        consolidating = self.cfg.use_consolidation and (
            consol_range or consol_mixed or consol_micro
        )

        notes = [n for n in (int_event, ext_event) if n]
        return StructureSnapshot(
            bar_index=bar_index,
            internal_trend=self.internal.trend,
            external_trend=self.external.trend,
            last_hh=self.internal.last_hh,
            last_hl=self.internal.last_hl,
            last_ll=self.internal.last_ll,
            last_lh=self.internal.last_lh,
            last_high=self.internal.last_high,
            last_low=self.internal.last_low,
            last_high_bar=self.internal.last_high_bar,
            last_low_bar=self.internal.last_low_bar,
            choch_dir=self.internal.choch_dir,
            choch_lvl=self.internal.choch_lvl,
            choch_on=choch_on,
            prot_high=self.internal.prot_high,
            prot_low=self.internal.prot_low,
            hi_type=self.internal.hi_type,
            lo_type=self.internal.lo_type,
            atr_14=atr_val,
            consolidating=consolidating,
            event_note=" | ".join(notes) if notes else None,
        )
