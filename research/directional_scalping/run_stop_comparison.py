"""Freeze prior experimental entries and run paired structural-stop study."""
from __future__ import annotations
import hashlib, json, math
from pathlib import Path
import numpy as np
import pandas as pd
from structure_engine import structure, resample_m1, validate_causality_and_semantics

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
INPUT=HERE/"inputs"; OUT=HERE/"results"
DATA=ROOT/"data/bars/xauusdm_m1_3y_20261117.parquet"
TF_HORIZON={"M1":15,"M5":30,"M15":60}
TP=1.50; FIXED_SL=.75; COST_PRICE=.50+2*.10+7/100
CONTRACT=100.0; INIT=10000.0; BUFFER_ATR=.10

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def metrics(df, col="net_pnl"):
    x=df[col].to_numpy(float); wins=x[x>0]; losses=-x[x<0]
    eq=INIT+np.cumsum(x); peaks=np.maximum.accumulate(np.r_[INIT,eq])[1:]
    dd=(peaks-eq)/peaks if len(eq) else np.array([])
    return {"trades":int(len(x)),"win_rate":float(np.mean(x>0)) if len(x) else None,
        "gross_profit":float(wins.sum()),"gross_loss":float(losses.sum()),
        "net_profit":float(x.sum()),"profit_factor":float(wins.sum()/losses.sum()) if losses.sum()>0 else None,
        "expectancy":float(x.mean()) if len(x) else None,"max_drawdown_pct":float(dd.max()) if len(dd) else None,
        "costs":float(df.cost.sum()) if len(df) else 0.0,
        "avg_mfe_price":float(df.mfe_price.mean()) if len(df) else None,
        "avg_mae_price":float(df.mae_price.mean()) if len(df) else None,
        "partial_tp_before_stop_rate":float(df.partial_tp_before_stop.mean()) if len(df) else None}

def simulate(raw, row, stop, horizon):
    e=int(row.entry_idx); side=int(row.side); entry=float(raw.open.iloc[e]); lots=float(row.lots)
    target=entry+side*TP; xidx=min(e+horizon-1,len(raw)-1); exitp=float(raw.close.iloc[xidx]); reason="timeout"; partial=False
    hi=raw.high.to_numpy(); lo=raw.low.to_numpy(); op=raw.open.to_numpy(); cl=raw.close.to_numpy()
    # Missing bars inside the holding path invalidate the pair upstream.
    for j in range(e,xidx+1):
        if j>e and ((op[j]<=stop if side==1 else op[j]>=stop)):
            exitp=float(op[j]); xidx=j; reason="stop_gap"; break
        slhit=(lo[j]<=stop if side==1 else hi[j]>=stop)
        tphit=(hi[j]>=target if side==1 else lo[j]<=target)
        if tphit and slhit: exitp=float(stop); xidx=j; reason="stop_same_bar"; break
        if slhit: exitp=float(stop); xidx=j; reason="stop"; break
        partial_hit=(hi[j]>=entry+TP*.5 if side==1 else lo[j]<=entry-TP*.5)
        if partial_hit: partial=True
        if tphit: exitp=float(target); xidx=j; reason="target"; break
    path_hi=hi[e:xidx+1]; path_lo=lo[e:xidx+1]
    mfe=float(np.max(path_hi-entry) if side==1 else np.max(entry-path_lo))
    mae=float(np.max(entry-path_lo) if side==1 else np.max(path_hi-entry))
    gross=side*(exitp-entry)*CONTRACT*lots
    cost=COST_PRICE*CONTRACT*lots
    return {"exit_time":raw.index[xidx].isoformat(),"exit_reason":reason,"exit_price":exitp,
        "gross_pnl":gross,"cost":cost,"net_pnl":gross-cost,"mfe_price":mfe,"mae_price":mae,
        "partial_tp_before_stop":bool(partial),"bars_held":xidx-e+1,"exit_idx":xidx}

def paired_bootstrap(df, variant, reps=3000, seed=41):
    # Resample UTC days as blocks, preserving within-day clustering. Use daily
    # sums/counts so the bootstrap is vectorized rather than row-wise.
    d=df.copy()
    if "entry_time" not in d.columns: d=d.rename_axis("entry_time").reset_index()
    d["day"]=pd.to_datetime(d.entry_time,utc=True).dt.floor("D")
    daily=(d.assign(delta=d[variant]-d["fixed"]).groupby("day").delta.agg(["sum","count"]))
    n=len(daily)
    if n<2: return {"mean_delta":None,"ci95":[None,None],"bootstrap_days":n}
    rng=np.random.default_rng(seed); picks=rng.integers(0,n,size=(reps,n))
    sums=daily["sum"].to_numpy()[picks].sum(axis=1)
    counts=daily["count"].to_numpy()[picks].sum(axis=1)
    vals=sums/counts
    return {"mean_delta":float((d[variant]-d.fixed).mean()),"ci95":[float(np.quantile(vals,.025)),float(np.quantile(vals,.975))],"bootstrap_days":int(n),"replicates":reps}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    trade_file=INPUT/"final_selected_trades.csv"
    trades=pd.read_csv(trade_file,parse_dates=["entry_time","exit_time"])
    trades=trades[trades.tf.isin(TF_HORIZON)].copy().sort_values(["tf","entry_time"])
    # Freeze exact executed decisions and the original research threshold/config.
    trades["signal_threshold"]=.30; trades["baseline_tp_price"]=1.50; trades["baseline_sl_price"]=.75
    trades["signal_source"]="research_m1_m15_events.py final selected-trade log; fixed-barrier-trained p_long/p_short argmax"
    trades["p_long_raw_available"]=False; trades["p_short_raw_available"]=False
    frozen=OUT/"frozen_baseline_entries.csv"; trades.to_csv(frozen,index=False)

    raw=pd.read_parquet(DATA).sort_index(); raw=raw.loc[~raw.index.duplicated()].copy()
    if raw.index.tz is None: raw.index=raw.index.tz_localize("UTC")
    else: raw.index=raw.index.tz_convert("UTC")
    # Normalize to numeric OHLC; index is source M1 bar open timestamp.
    for c in ("open","high","low","close"): raw[c]=pd.to_numeric(raw[c],errors="coerce")
    raw=raw.dropna(subset=["open","high","low","close"])
    tf_bars={"M1":resample_m1(raw,1),"M5":resample_m1(raw,5),"M15":resample_m1(raw,15)}
    all_states={}; all_events={}
    validation=validate_causality_and_semantics()
    if not all(validation.values()):
        raise RuntimeError(f"Structure validation failed; stop comparison blocked: {validation}")
    cached=all((OUT/f"structure_state_{tf}.parquet").exists() and (OUT/f"structure_events_{tf}.csv").exists() for tf in TF_HORIZON)
    for tf,bars in tf_bars.items():
        if cached:
            all_states[tf]=pd.read_parquet(OUT/f"structure_state_{tf}.parquet")
            all_states[tf].index=pd.DatetimeIndex(all_states[tf].index)
            all_events[tf]=pd.read_csv(OUT/f"structure_events_{tf}.csv")
        else:
            s,e=structure(bars,tf,k=1.0); all_states[tf]=s; all_events[tf]=e
    # Structure diagnostics are stored independently; all state timestamps are
    # the completed-candle times at which each field first became available.
    for tf,s in all_states.items():
        s.to_parquet(OUT/f"structure_state_{tf}.parquet",compression="zstd")
        all_events[tf].to_csv(OUT/f"structure_events_{tf}.csv",index=False)

    # Map each frozen executed entry to the latest structure state known at that
    # timestamp; the recorded entry is next-M1-open after that close.
    raw_pos=raw.index.get_indexer(trades.entry_time)
    trades["entry_idx"]=raw_pos
    candidates=[]; coverage=[]
    for r in trades.itertuples(index=False):
        tf=r.tf; ts=pd.Timestamp(r.entry_time); side=int(r.side); s=all_states[tf]
        j=s.index.searchsorted(ts,side="right")-1
        if r.entry_idx<0 or j<0:
            coverage.append({"tf":tf,"entry_time":ts.isoformat(),"status":"missing_entry_or_structure_timestamp"}); continue
        st=s.iloc[j]; entry=float(raw.open.iloc[r.entry_idx]); atrv=float(st.atr14)
        pivot=st.latest_swing_low if side==1 else st.latest_swing_high
        prot=st.protected_low if side==1 else st.protected_high
        buff=float(.10*atrv) if np.isfinite(atrv) else np.nan
        cand={"fixed":entry-side*FIXED_SL,
              "confirmed_swing":float(pivot) if np.isfinite(pivot) else np.nan,
              "protected":float(prot) if np.isfinite(prot) else np.nan,
              "protected_buffered":(float(prot-side*buff) if np.isfinite(prot) and np.isfinite(buff) else np.nan)}
        valid={k:(np.isfinite(v) and ((v<entry) if side==1 else (v>entry))) for k,v in cand.items()}
        status="valid" if all(valid.values()) else "invalid_stop_candidate"
        coverage.append({"tf":tf,"entry_time":ts.isoformat(),"status":status,"atr14":atrv,
            "entry_price":entry,"side":side,"swing_stop":cand["confirmed_swing"],"protected_stop":cand["protected"],
            "buffered_stop":cand["protected_buffered"],"validity":json.dumps({k:bool(v) for k,v in valid.items()}),
            "structure_asof":s.index[j].isoformat(),"structure_trend":int(st.trend),"last_bos":st.last_bos,"last_mss":st.last_mss})
        candidates.append({"row":r,"cand":cand,"valid":valid,"state":st})
    pd.DataFrame(coverage).to_csv(OUT/"structural_stop_candidates.csv",index=False)

    output=[]; attrition=[]
    for tf in TF_HORIZON:
        subset=[z for z in candidates if z["row"].tf==tf]
        # Paired cohort: every stop exists, and the complete maximum horizon is
        # contiguous in M1 bars. Same cohort is used for all four stop variants.
        eligible=[]; prev_furthest=-1; exclusion={"unavailable_level":0,"gap_or_overlap":0}
        for z in subset:
            r=z["row"]; e=int(r.entry_idx); hor=TF_HORIZON[tf]
            if not all(z["valid"].values()): exclusion["unavailable_level"]+=1; continue
            if e+hor>len(raw): exclusion["gap_or_overlap"]+=1; continue
            expected=pd.date_range(raw.index[e],periods=hor,freq="min")
            if not raw.index[e:e+hor].equals(expected): exclusion["gap_or_overlap"]+=1; continue
            if e<=prev_furthest: exclusion["gap_or_overlap"]+=1; continue
            sims={v:simulate(raw,r,z["cand"][v],hor) for v in ("fixed","confirmed_swing","protected","protected_buffered")}
            prev_furthest=max(x["exit_idx"] for x in sims.values())
            eligible.append((z,sims))
        attrition.append({"tf":tf,"frozen_entries":len(subset),"paired_entries":len(eligible),**exclusion})
        rows=[]
        for z,sims in eligible:
            r=z["row"]; base={"tf":tf,"entry_time":r.entry_time.isoformat(),"side":int(r.side),"prob":float(r.prob),
                "lots":float(r.lots),"session":r.session,"trend_state":r.trend_state,"vol_state":r.vol_state,
                "regime":r.regime,"news":r.news,"signal_threshold":.30,"entry_price":float(raw.open.iloc[int(r.entry_idx)])}
            for variant,res in sims.items():
                rows.append({**base,"variant":variant,"stop_price":z["cand"][variant],**res})
        output.extend(rows)
    result=pd.DataFrame(output)
    result.to_csv(OUT/"paired_stop_trade_results.csv",index=False)
    # Paired summaries (one line per timeframe/variant); portfolio drawdown is
    # sequential on the common non-overlapping entry cohort.
    summary=[]; paired=[]; byreg=[]
    if len(result):
        for tf in TF_HORIZON:
            tfdata=result[result.tf.eq(tf)]
            wide=tfdata.pivot(index="entry_time",columns="variant",values="net_pnl")
            for v in ("fixed","confirmed_swing","protected","protected_buffered"):
                d=tfdata[tfdata.variant.eq(v)].copy().sort_values("entry_time")
                summary.append({"tf":tf,"variant":v,**metrics(d)})
                if v!="fixed": paired.append({"tf":tf,"variant_vs_fixed":v,**paired_bootstrap(wide,v)})
                for regime,g in d.groupby(["trend_state","vol_state"],dropna=False):
                    byreg.append({"tf":tf,"variant":v,"trend_state":regime[0],"vol_state":regime[1],**metrics(g)})
    pd.DataFrame(summary).to_csv(OUT/"paired_stop_summary.csv",index=False)
    pd.DataFrame(paired).to_csv(OUT/"paired_expectancy_deltas.csv",index=False)
    pd.DataFrame(byreg).to_csv(OUT/"paired_stop_by_regime.csv",index=False)

    manifest={"status":"completed stop comparison; research only; no deployment",
        "entry_freeze":{"source_file":"inputs/final_selected_trades.csv","sha256":sha(trade_file),
        "signal_script_sha256":sha(INPUT/"research_m1_m15_events.py"),"threshold":.30,"side":"argmax(p_long,p_short), recorded side frozen",
        "raw_probabilities":"p_long/p_short not preserved in trade log; exact executed selected probability and side retained",
        "entry_timestamp":"exact prior final_selected_trades entry_time; next M1 open after completed TF decision candle",
        "counts_by_tf":trades.groupby("tf").size().to_dict()},
        "dataset":{"file":"data/bars/xauusdm_m1_3y_20261117.parquet","sha256":sha(DATA),"rows":len(raw),"start":raw.index[0].isoformat(),"end":raw.index[-1].isoformat(),"resolution":"M1 OHLC; no tick archive"},
        "assumptions":{"tp_price_distance":TP,"fixed_stop_distance":FIXED_SL,"buffer":"0.10 x ATR14 at entry","lots":"frozen original trade lots across variants","cost_price_equivalent":COST_PRICE,"spread_price_round_turn":.50,"slippage_each_side":.10,"commission_round_turn_per_lot":7,"contract_oz_per_lot":100,"horizon_minutes":TF_HORIZON,"same_bar_tp_sl":"stop first","entry":"next M1 open, as previous study","one_at_a_time":"paired common cohort excludes overlaps across stop variants","gap":"exclude if any missing M1 bar occurs in full fixed horizon"},
        "structure":{"method":"ATR-scaled directional change; k=1.0, per timeframe ATR14 frozen at candidate extreme","validation":validation,"events_by_tf":{k:len(v) for k,v in all_events.items()},"state_rows_by_tf":{k:len(v) for k,v in all_states.items()}},
        "implementation_hashes":{"structure_engine_sha256":sha(HERE/"structure_engine.py"),"comparison_runner_sha256":sha(HERE/"run_stop_comparison.py")},
        "cohort_attrition":attrition,"outputs":["frozen_baseline_entries.csv","structural_stop_candidates.csv","paired_stop_trade_results.csv","paired_stop_summary.csv","paired_expectancy_deltas.csv","paired_stop_by_regime.csv","structure_state_M1.parquet","structure_state_M5.parquet","structure_state_M15.parquet","structure_events_M1.csv","structure_events_M5.csv","structure_events_M15.csv"],
        "limitations":["The frozen experimental classifier was trained for fixed-barrier outcomes, not TP-before-structural-SL.","p_long and p_short raw components were not saved; only selected side and max probability in executed trade records are available.","The baseline trade log contains final holdout selected trades only; it is not the full signal stream or a new walk-forward test.","Prior entries were selected using the old simulator's risk and cost rules. Frozen lot sizing is preserved, so cash risk differs by stop distance.","MT5 archive contains M1 candles only; intraminute price path, true variable bid/ask and tick execution are unavailable.","The structure method is an OHLC directional-change adaptation; the Pine source does not fully specify an objectively causal swing confirmation formula, so this is a documented approximation, not pixel-identical behavior.","Small final-holdout trade count means paired confidence intervals are imprecise and no strategy efficacy claim is justified."]}
    (OUT/"stop_comparison_manifest.json").write_text(json.dumps(manifest,indent=2,default=str),encoding="utf-8")
    print(json.dumps({"manifest":manifest,"summary":summary,"paired":paired},indent=2,default=str))

if __name__=="__main__": main()
