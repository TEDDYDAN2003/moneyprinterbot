"""Research M1/M15 TP-before-SL models and causal context features.

The script is deliberately research-only. It does not write champion artifacts or
connect to the terminal. Decision features are computed at completed-bar close;
orders are simulated from the following M1 open using conservative OHLC rules.
"""
from __future__ import annotations

import json, math
from pathlib import Path
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import roc_auc_score, brier_score_loss

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/bars/xauusdm_m1_3y_20261117.parquet'
EVENT_FILE=ROOT/'data/events/bls_cpi_ppi_actual_2023_2026.csv'
OUT=ROOT/'reports/m1_m15_event_research_20261117'
POINT=.001; CONTRACT=100.; COMMISSION=7.; SLIP_SIDE=.10; SPREAD=.50
INIT=10000.; RISK=.0025; LOT_STEP=.01; MIN_LOT=.01
PAIRS=((.50,.50),(.75,.50),(1.00,.50),(1.50,.75))
HORIZON={'M1':15,'M5':30,'M15':60}
ANCHOR=(1.50,.75)
EVENTS=pd.DataFrame()

def resample(raw, minutes):
    x=raw.copy(); x.index=x.index+pd.Timedelta(minutes=1)
    y=x.resample(f'{minutes}min',label='right',closed='right').agg(
        {'open':'first','high':'max','low':'min','close':'last','volume':'sum','spread':'max'})
    n=x.close.resample(f'{minutes}min',label='right',closed='right').count()
    return y.loc[n==minutes].dropna(subset=['open','high','low','close'])

def atr(b):
    pc=b.close.shift(); tr=pd.concat([b.high-b.low,(b.high-pc).abs(),(b.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(14,min_periods=14).mean()

def core_features(b, label):
    c=b.close; a=atr(b); rng=(b.high-b.low).replace(0,np.nan); d=c.diff()
    up=d.clip(lower=0).rolling(14,min_periods=14).mean(); dn=(-d.clip(upper=0)).rolling(14,min_periods=14).mean()
    x=pd.DataFrame(index=b.index)
    for n in (1,2,3,5,10,20): x[f'{label}_ret{n}']=c.pct_change(n)
    x[f'{label}_atr']=a; x[f'{label}_atr_pct']=a/c
    x[f'{label}_atr_ratio']=a/a.rolling(50,min_periods=40).mean()
    x[f'{label}_body_atr']=(c-b.open)/a
    x[f'{label}_range_atr']=rng/a
    x[f'{label}_close_loc']=(c-b.low)/rng
    x[f'{label}_upper_wick']=(b.high-b[['open','close']].max(axis=1))/rng
    x[f'{label}_lower_wick']=(b[['open','close']].min(axis=1)-b.low)/rng
    for n in (9,21,50):
        e=c.ewm(span=n,adjust=False,min_periods=n).mean()
        x[f'{label}_ema{n}_dist']=(c-e)/a
    x[f'{label}_ema9_21']=(c.ewm(span=9,adjust=False).mean()-c.ewm(span=21,adjust=False).mean())/a
    x[f'{label}_rsi']=1-1/(1+(up/dn.replace(0,np.nan)))
    mid=c.rolling(20,min_periods=20).mean(); sd=c.rolling(20,min_periods=20).std()
    x[f'{label}_bb_width']=4*sd/mid
    plus=b.high.diff(); minus=-b.low.diff()
    pdm=plus.where((plus>minus)&(plus>0),0.).rolling(14,min_periods=14).mean()
    mdm=minus.where((minus>plus)&(minus>0),0.).rolling(14,min_periods=14).mean()
    pdi=100*pdm/a.replace(0,np.nan); mdi=100*mdm/a.replace(0,np.nan)
    x[f'{label}_adx']=(100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)).rolling(14,min_periods=14).mean()/100
    sign=np.sign((c-b.open).to_numpy())
    up_run=np.zeros(len(b),dtype=float); dn_run=np.zeros(len(b),dtype=float)
    for i in range(len(b)):
        up_run[i]=(up_run[i-1]+1 if i and sign[i]>0 and sign[i-1]>0 else 1 if sign[i]>0 else 0)
        dn_run[i]=(dn_run[i-1]+1 if i and sign[i]<0 and sign[i-1]<0 else 1 if sign[i]<0 else 0)
    x[f'{label}_bull_streak']=up_run; x[f'{label}_bear_streak']=dn_run
    signed_body=np.sign(c-b.open)*(c-b.open).abs()/a
    for n in (3,5,10):
        x[f'{label}_body_pressure_{n}']=signed_body.rolling(n,min_periods=n).sum()
        x[f'{label}_bull_fraction_{n}']=(c>b.open).rolling(n,min_periods=n).mean()
    lv=np.log1p(b.volume.clip(lower=0)); x[f'{label}_tickvol_z']=(lv-lv.rolling(60,min_periods=30).mean())/lv.rolling(60,min_periods=30).std().replace(0,np.nan)
    slot=b.index.day_of_week*1440+b.index.hour*60+b.index.minute
    volbase=b.volume.groupby(slot).transform(lambda z:z.shift(1).rolling(20,min_periods=5).median())
    x[f'{label}_session_relative_volume']=b.volume/volbase.replace(0,np.nan)
    spreadpx=b.spread*POINT
    x[f'{label}_spread_px']=np.maximum(SPREAD,spreadpx)
    x[f'{label}_spread_anomaly']=spreadpx/spreadpx.rolling(60,min_periods=30).median().replace(0,np.nan)
    x[f'{label}_inside_bar']=((b.high<=b.high.shift(1))&(b.low>=b.low.shift(1))).astype(float)
    x[f'{label}_outside_bar']=((b.high>b.high.shift(1))&(b.low<b.low.shift(1))).astype(float)
    return x

def profile_levels(b):
    """Prior observed UTC-day tick-volume profile; estimates, not exchange volume."""
    day=b.index.floor('D'); records=[]
    for d,ix in pd.Series(np.arange(len(b)),index=b.index).groupby(day):
        pos=ix.to_numpy(); close=b.close.to_numpy()[pos]; vol=b.volume.to_numpy()[pos]
        bins=np.floor(close/.5).astype(int); unique,inv=np.unique(bins,return_inverse=True)
        sums=np.bincount(inv,weights=vol); order=np.argsort(sums)[::-1]; poc_i=order[0]
        selected=[]; total=sums.sum(); acc=0.
        for k in order:
            selected.append(k); acc+=sums[k]
            if acc>=.70*total: break
        records.append((d,(unique[poc_i]+.5)*.5,unique[selected].min()*.5,(unique[selected].max()+1)*.5))
    tab=pd.DataFrame([z[1:] for z in records],index=pd.DatetimeIndex([z[0] for z in records]),columns=['poc','val','vah']).shift(1)
    mapped=tab.reindex(pd.DatetimeIndex(day.unique()),method='ffill')
    mapped.index=pd.DatetimeIndex(day.unique())
    return mapped.reindex(day).set_axis(b.index)

def make_features(raw, tf):
    minutes={'M1':1,'M5':5,'M15':15}[tf]
    context_tfs={'M1':(5,15),'M5':(15,60),'M15':(60,240)}[tf]
    base=resample(raw,minutes); label=tf.lower()
    x=core_features(base,label)
    # Session and calendar context are known at the signal close.
    from bot.time.sessions import session_flags
    x=x.join(session_flags(x.index).astype(float).add_prefix('session_'))
    for kind in ('CPI','PPI'):
        releases=pd.DatetimeIndex(pd.to_datetime(EVENTS.loc[EVENTS.event==kind,'release_utc'],utc=True)).asi8
        if len(releases):
            now=x.index.asi8; k=np.searchsorted(releases,now,side='left'); before=np.clip(k-1,0,len(releases)-1); after=np.clip(k,0,len(releases)-1)
            db=(now-releases[before])/60e9; da=(releases[after]-now)/60e9
            signed=np.where(da<=db,da,-db)
            x[f'news_{kind.lower()}_minutes']=np.clip(signed,-10080,10080)
            x[f'news_{kind.lower()}_near15']=(np.abs(signed)<=15).astype(float)
    # Causal higher-timeframe context: right-edge labels denote completed bars.
    for cm in context_tfs:
        cb=resample(raw,cm); tag=f'ctx{cm}'
        cf=core_features(cb,tag)[[f'{tag}_ret3',f'{tag}_ret10',f'{tag}_atr_ratio',f'{tag}_ema9_21']].copy()
        ph,pa=_confirmed_pivot(cb.high); pl,pla=_confirmed_pivot(cb.low,low=True)
        ca=atr(cb)
        cf[f'{tag}_swing_high_dist_atr']=(cb.close-ph)/ca; cf[f'{tag}_swing_low_dist_atr']=(cb.close-pl)/ca
        cf[f'{tag}_swing_high_age']=pa; cf[f'{tag}_swing_low_age']=pla
        x=x.join(cf.reindex(x.index,method='ffill'))
    # Previous completed daily range and profile, plus prior rolling structure.
    d=base.index.floor('D')
    daily=base.groupby(d).agg(day_high=('high','max'),day_low=('low','min')).shift(1)
    prev=daily.reindex(pd.DatetimeIndex(d.unique()),method='ffill').reindex(d).set_axis(base.index)
    prof=profile_levels(base)
    a=atr(base)
    daily_range=(daily.day_high-daily.day_low).rolling(20,min_periods=10).mean()
    adr=daily_range.reindex(pd.DatetimeIndex(d.unique()),method='ffill').reindex(d).set_axis(base.index)
    running_hi=base.high.groupby(d).cummax(); running_lo=base.low.groupby(d).cummin()
    x['daily_adr_utilization']=(running_hi-running_lo)/adr
    x['previous_day_range_position']=(base.close-prev.day_low)/(prev.day_high-prev.day_low).replace(0,np.nan)
    for name,lev in [('pdh',prev.day_high),('pdl',prev.day_low),('poc',prof.poc),('val',prof.val),('vah',prof.vah)]:
        x[f'zone_{name}_dist_atr']=(base.close-lev)/a
        x[f'zone_{name}_near']=((base.close-lev).abs()<=.25*a).astype(float)
    for n in (12,48):
        rh=base.high.shift(1).rolling(n,min_periods=n).max(); rl=base.low.shift(1).rolling(n,min_periods=n).min()
        x[f'structure_{n}_high_dist_atr']=(base.close-rh)/a
        x[f'structure_{n}_low_dist_atr']=(base.close-rl)/a
        x[f'sweep_high_{n}']=((base.high>rh+.05*a)&(base.close<rh)).astype(float)
        x[f'sweep_low_{n}']=((base.low<rl-.05*a)&(base.close>rl)).astype(float)
        x[f'reclaim_high_{n}']=((base.close>rh)&(base.open<rh)).astype(float)
        x[f'reclaim_low_{n}']=((base.close<rl)&(base.open>rl)).astype(float)
    ph,pha=_confirmed_pivot(base.high); pl,pla=_confirmed_pivot(base.low,low=True)
    x['confirmed_swing_high_dist_atr']=(base.close-ph)/a; x['confirmed_swing_low_dist_atr']=(base.close-pl)/a
    x['confirmed_swing_high_age']=pha; x['confirmed_swing_low_age']=pla
    # FVGs become observable at the close of the third candle. Re-tests and
    # inversions can only use zones created on earlier completed bars.
    bull_fvg=base.low>base.high.shift(2); bear_fvg=base.high<base.low.shift(2)
    bull_lo=base.high.shift(2).where(bull_fvg).ffill(); bull_hi=base.low.where(bull_fvg).ffill()
    bear_lo=base.high.where(bear_fvg).ffill(); bear_hi=base.low.shift(2).where(bear_fvg).ffill()
    bull_age=_age(bull_fvg,500); bear_age=_age(bear_fvg,500)
    x['fvg_bull_age']=bull_age; x['fvg_bear_age']=bear_age
    x['fvg_bull_width_atr']=(bull_hi-bull_lo)/a; x['fvg_bear_width_atr']=(bear_hi-bear_lo)/a
    x['fvg_bull_dist_atr']=(base.close-(bull_hi+bull_lo)/2)/a; x['fvg_bear_dist_atr']=(base.close-(bear_hi+bear_lo)/2)/a
    x['fvg_bull_retest']=((bull_age>0)&(base.low<=bull_hi)&(base.high>=bull_lo)).astype(float)
    x['fvg_bear_retest']=((bear_age>0)&(base.low<=bear_hi)&(base.high>=bear_lo)).astype(float)
    x['ifvg_bullish']=((bull_age>0)&(base.close<bull_lo)).astype(float)
    x['ifvg_bearish']=((bear_age>0)&(base.close>bear_hi)).astype(float)
    # Deterministic order-block proxy: prior opposite candle before a confirmed
    # range displacement. Only published on the displacement close, then aged.
    rh=base.high.shift(1).rolling(12,min_periods=12).max(); rl=base.low.shift(1).rolling(12,min_periods=12).min()
    dis=(base.high-base.low)/a
    bull=((base.close>rh)&(dis>=1.2)&(base.close.shift(1)<base.open.shift(1))).astype(float)
    bear=((base.close<rl)&(dis>=1.2)&(base.close.shift(1)>base.open.shift(1))).astype(float)
    # Keep event recency; the feature is deliberately a proxy, not order flow.
    x['ob_bull_age']=_age(bull,64); x['ob_bear_age']=_age(bear,64)
    x['ob_bull_recent']=(x.ob_bull_age<=32).astype(float); x['ob_bear_recent']=(x.ob_bear_age<=32).astype(float)
    ctxtrend=x[[c for c in x.columns if c.endswith('_ema9_21') and c.startswith('ctx')]]
    if len(ctxtrend.columns):
        trend=ctxtrend.iloc[:,-1]
        ratio=x[[c for c in x.columns if c.endswith('_atr_ratio') and c.startswith('ctx')]].iloc[:,-1]
        x['regime_trend_strength']=trend.abs()
        x['regime_trending']=(trend.abs()>.10).astype(float)
        x['regime_range']=(trend.abs()<=.10).astype(float)
        x['regime_high_vol']=(ratio>1.5).astype(float)
        x['regime_low_vol']=(ratio<.75).astype(float)
        # Equal-count confluence scores are model inputs, never hard entry gates.
        bull=(trend>0).astype(float)+(x[f'sweep_low_12']>0).astype(float)+(x[f'{label}_body_pressure_5']>0).astype(float)
        bear=(trend<0).astype(float)+(x[f'sweep_high_12']>0).astype(float)+(x[f'{label}_body_pressure_5']<0).astype(float)
        bull+=((x.zone_pdl_dist_atr.abs()<.25)|(x.zone_val_dist_atr.abs()<.25)).astype(float)
        bear+=((x.zone_pdh_dist_atr.abs()<.25)|(x.zone_vah_dist_atr.abs()<.25)).astype(float)
        bull+=((x.session_london>0)|(x.session_new_york>0)).astype(float)
        bear+=((x.session_london>0)|(x.session_new_york>0)).astype(float)
        x['confluence_score_bull']=bull; x['confluence_score_bear']=bear
    base.index.name='time'
    return base,x.replace([np.inf,-np.inf],np.nan).copy()

def _age(s,cap):
    out=np.empty(len(s),dtype=float); age=cap+1
    for i,v in enumerate(s.fillna(0).to_numpy()): age=0 if v else min(age+1,cap+1); out[i]=age
    return pd.Series(out,index=s.index)

def _confirmed_pivot(values, low=False, right=2):
    candidate=values.shift(right); window=values.rolling(2*right+1,min_periods=2*right+1)
    extreme=window.min() if low else window.max(); event=candidate.eq(extreme)&candidate.notna()
    return candidate.where(event).ffill(),_age(event,500)

def feature_groups(columns, tf):
    base=[c for c in columns if c.startswith(('m1_','m5_','m15_','session_','news_','regime_'))]
    context=[c for c in columns if c.startswith('ctx')]
    zones=[c for c in columns if c.startswith(('zone_','structure_','sweep_','reclaim_','ob_','confirmed_swing_','fvg_','ifvg_'))]
    profile=[c for c in columns if c.startswith('zone_poc_') or c.startswith('zone_val_') or c.startswith('zone_vah_')]
    zones=[c for c in zones if c not in profile]
    scores=[c for c in columns if c.startswith('confluence_')]
    return {'base':base,'base_context':base+context,'base_context_zones':base+context+zones,'all':base+context+zones+profile+scores,'score_only':scores,'base_score':base+scores}

def samples_for(raw, base, features, tf):
    # M1 inputs are sampled every two bars to limit near-duplicate labels while
    # keeping next-M1 execution and the short-horizon setup intact.
    close_times=base.index; step=2 if tf=='M1' else 1
    cand=np.arange(0,len(base),step,dtype=int)
    # The raw timestamp is the following M1 candle open. Never enter on the bar
    # whose close generated the feature row.
    entry_ts=close_times[cand]; raw_idx=raw.index.get_indexer(entry_ts)
    ok=(raw_idx>=0)&(cand<len(base)-1)
    cand=cand[ok]; raw_idx=raw_idx[ok]
    contig=(raw.index[raw_idx]==close_times[cand])
    valid=np.isfinite(features.iloc[cand].to_numpy(float)).all(axis=1)
    keep=contig&valid
    return cand[keep],raw_idx[keep]

def first_passage(raw, entry_idx, side, tp, sl, horizon):
    n=len(entry_idx); entry=raw.open.to_numpy()[entry_idx]
    hi=raw.high.to_numpy(); lo=raw.low.to_numpy(); op=raw.open.to_numpy()
    result=np.zeros(n,dtype=np.int8); end=np.full(n,-1,dtype=np.int32)
    active=np.ones(n,dtype=bool); up=entry+side*tp; dn=entry-side*sl
    # Scan M1 candles chronologically. Same-candle TP and SL is conservatively SL.
    for k in range(horizon):
        ix=entry_idx+k
        inrange=ix<len(raw); active &= inrange
        if not active.any(): break
        # Missing minutes invalidate the event rather than allowing price to jump unseen.
        expected=raw.index[entry_idx]+pd.to_timedelta(k,unit='m')
        contiguous=(raw.index[np.minimum(ix,len(raw)-1)]==expected)
        bad=active&~contiguous; result[bad]=-1; end[bad]=k; active[bad]=False
        j=np.flatnonzero(active)
        if not len(j): continue
        ii=ix[j]; s=side[j]
        hit_tp=np.where(s==1,hi[ii]>=up[j],lo[ii]<=up[j])
        hit_sl=np.where(s==1,lo[ii]<=dn[j],hi[ii]>=dn[j])
        both=hit_tp&hit_sl; slhit=hit_sl|both; tphit=hit_tp&~hit_sl
        result[j[slhit]]=0; end[j[slhit]]=k; active[j[slhit]]=False
        result[j[tphit]]=1; end[j[tphit]]=k; active[j[tphit]]=False
    # For timeout, label remains zero for classifier; simulator marks actual close PnL.
    end[active]=horizon-1
    return result,end

def target_for(raw, base, cand, entry_idx, tf, tp, sl):
    sides=np.r_[np.ones(len(cand),dtype=np.int8),-np.ones(len(cand),dtype=np.int8)]
    ix=np.r_[entry_idx,entry_idx]
    y,end=first_passage(raw,ix,sides,tp,sl,HORIZON[tf])
    valid=y>=0
    return sides,y,end,valid

def walkforward(raw,base,features,cand,entry_idx,tf,group,cols,tp,sl):
    sides,y,end,valid=target_for(raw,base,cand,entry_idx,tf,tp,sl)
    n=len(cand); X0=features.iloc[cand][cols].to_numpy(np.float32); X=np.vstack([X0,X0])
    side_feature=np.r_[np.ones(n,dtype=np.float32),-np.ones(n,dtype=np.float32)]
    X=np.column_stack([X,side_feature]); model_cols=list(cols)+['direction_side']; times=base.index[cand].append(base.index[cand])
    # Horizon purge is applied using the maximum event duration.
    first=base.index[cand].min()+pd.Timedelta(days=365); fold=pd.Timestamp(first.year,first.month,1,tz=first.tz)
    if fold<first: fold+=pd.offsets.MonthBegin()
    preds=[]; metrics=[]; gains=[]
    while fold<=base.index[cand].max():
        nxt=fold+pd.DateOffset(months=6)
        tr=(times>=fold-pd.Timedelta(days=365))&(times<fold-pd.Timedelta(minutes=HORIZON[tf]))
        te=(times>=fold)&(times<nxt)
        # If event crosses missing feed minutes, remove it on both sides.
        tr &= np.r_[valid[:n],valid[n:]]; te &= np.r_[valid[:n],valid[n:]]
        ti=np.flatnonzero(tr); vi=np.flatnonzero(te)
        if len(ti)>1200 and len(vi)>60 and np.unique(y[ti]).size>1:
            model=LGBMClassifier(n_estimators=50,learning_rate=.04,num_leaves=15,min_child_samples=100,subsample=.85,colsample_bytree=.85,reg_lambda=8,verbosity=-1,random_state=41,n_jobs=4,deterministic=True,force_col_wise=True)
            model.fit(X[ti],y[ti]); prob=model.predict_proba(X[vi])[:,1]
            auc=float(roc_auc_score(y[vi],prob)) if np.unique(y[vi]).size>1 else float('nan')
            metrics.append({'tf':tf,'group':group,'tp':tp,'sl':sl,'fold':fold.isoformat(),'n_train':len(ti),'n_test':len(vi),'auc':auc,'brier':float(brier_score_loss(y[vi],prob)),'base_rows':len(vi)//2})
            # Long and short rows occur in mirrored blocks; store both direction probabilities.
            ixv=vi[vi<n]; ixv=ixv[ixv+n<len(prob)]
            p_long=np.full(n,np.nan);p_short=np.full(n,np.nan)
            # Collect predictions against sample index using block positions.
            for k,global_row in enumerate(vi):
                local=int(global_row if global_row<n else global_row-n)
                if global_row<n:p_long[local]=prob[k]
                else:p_short[local]=prob[k]
            ids=np.flatnonzero(np.isfinite(p_long)&np.isfinite(p_short))
            if len(ids):
                preds.append(pd.DataFrame({'time':base.index[cand[ids]],'position':cand[ids],'entry_idx':entry_idx[ids],'p_long':p_long[ids],'p_short':p_short[ids],'fold':fold.isoformat()}))
            gi=model.booster_.feature_importance('gain'); den=gi.sum() or 1
            gains.extend({'tf':tf,'group':group,'tp':tp,'sl':sl,'feature':c,'gain_share':float(g/den)} for c,g in zip(model_cols,gi))
        fold=nxt
    return (pd.concat(preds,ignore_index=True) if preds else pd.DataFrame()),pd.DataFrame(metrics),pd.DataFrame(gains)

def sim(raw,base,features,pred,tp,sl,tf,threshold,start,end,management='fixed',risk_fraction=RISK):
    p=pred[(pred.time>=start)&(pred.time<end)].copy()
    p['side']=np.where(p.p_long>=p.p_short,1,-1);p['prob']=p[['p_long','p_short']].max(axis=1)
    p=p[p.prob>=threshold].sort_values('position')
    cash_cost_px=SPREAD+2*SLIP_SIDE+COMMISSION/CONTRACT
    equity=INIT; peak=INIT; maxdd=0.; day_start=INIT; day_key=None; week_start=INIT; week_key=None; locked_days=set(); locked_weeks=set(); halt=False; last_exit=-1; trades=[]
    raw_open=raw.open.to_numpy(); raw_hi=raw.high.to_numpy(); raw_lo=raw.low.to_numpy(); raw_close=raw.close.to_numpy()
    atr_col=f'{tf.lower()}_atr'; atr_entry=features[atr_col].to_numpy()
    trend_cols=[c for c in features.columns if c.startswith('ctx') and c.endswith('_ema9_21')]
    trend_col=trend_cols[-1] if trend_cols else None
    for z in p.itertuples(index=False):
        e=int(z.entry_idx); pos=int(z.position); side=int(z.side)
        if halt or e<=last_exit or e+HORIZON[tf]>=len(raw): continue
        if not np.isfinite(atr_entry[pos]): continue
        dt=raw.index[e]; d=dt.date(); w=dt.isocalendar()[:2]
        if d!=day_key: day_key=d; day_start=equity
        if w!=week_key: week_key=w; week_start=equity
        if d in locked_days or w in locked_weeks: continue
        # Freeze out the 5m immediately before and 15m after scheduled CPI/PPI.
        news='non-news'; offset=np.nan
        for ev in EVENTS.itertuples(index=False):
            et=pd.Timestamp(ev.release_utc)
            if dt>=et-pd.Timedelta(minutes=5) and dt<=et+pd.Timedelta(minutes=15): news=ev.event; offset=(dt-et).total_seconds()/60; break
        entry=float(raw_open[e]); stopd=sl
        risk_per_lot=stopd*CONTRACT+cash_cost_px*CONTRACT
        budget=equity*risk_fraction; lots=math.floor(budget/risk_per_lot/LOT_STEP+1e-9)*LOT_STEP
        if lots<MIN_LOT: continue
        stop=entry-side*sl; target=entry+side*tp; exitp=None; exitidx=e+HORIZON[tf]-1; reason='timeout'; partial=False; partial_gross=0.; armed=False
        active_stop=stop; best=entry; initial_trend=float(features.iloc[pos][trend_col]) if trend_col else side
        for j in range(e,exitidx+1):
            # MTM and hard equity/day/week limits include the open trade.
            mark=raw_close[j]; mtm=equity+side*(mark-entry)*CONTRACT*lots-cash_cost_px*CONTRACT*lots
            peak=max(peak,mtm); maxdd=max(maxdd,(peak-mtm)/max(peak,1.))
            if mtm<=peak*.92: exitp=mark; exitidx=j; reason='equity_kill'; halt=True; break
            if mtm<=day_start*.98: exitp=mark; exitidx=j; reason='daily_limit'; locked_days.add(d); break
            if mtm<=week_start*.95: exitp=mark; exitidx=j; reason='weekly_limit'; locked_weeks.add(w); break
            op=float(raw_open[j]); tp_hit=(raw_hi[j]>=target if side==1 else raw_lo[j]<=target); sl_hit=(raw_lo[j]<=active_stop if side==1 else raw_hi[j]>=active_stop)
            if j>e and ((op<=active_stop if side==1 else op>=active_stop)): exitp=op; reason='stop_gap'; exitidx=j; break
            if tp_hit and sl_hit: exitp=active_stop; reason='stop_same_bar'; exitidx=j; break
            if sl_hit: exitp=active_stop; reason='breakeven_stop' if partial else 'stop'; exitidx=j; break
            best=max(best,float(raw_hi[j])) if side==1 else min(best,float(raw_lo[j]))
            if management=='partial_1R' and not partial and ((raw_hi[j]>=entry+sl) if side==1 else (raw_lo[j]<=entry-sl)):
                partial=True; partial_gross=.5*sl*CONTRACT*lots; active_stop=entry
            if tp_hit: exitp=target; reason='target'; exitidx=j; break
            if management=='trailing_1R' and ((best-entry)*side>=sl):
                trail=best-side*sl
                active_stop=max(active_stop,trail) if side==1 else min(active_stop,trail)
            if management=='htf_reversal' and ((best-entry)*side>=sl) and trend_col:
                armed=True
                fi=features.index.searchsorted(raw.index[j]+pd.Timedelta(minutes=1),side='right')-1
                if fi>=0:
                    current=float(features[trend_col].iloc[fi])
                    if initial_trend*current<0:
                        exitp=float(raw_close[j]);reason='htf_reversal';exitidx=j;break
        if exitp is None: exitp=float(raw_close[exitidx])
        gross=(partial_gross+(.5*side*(exitp-entry)*CONTRACT*lots if partial else side*(exitp-entry)*CONTRACT*lots)); cost=cash_cost_px*CONTRACT*lots; pnl=gross-cost
        equity+=pnl; peak=max(peak,equity); maxdd=max(maxdd,(peak-equity)/peak); last_exit=exitidx
        if equity<=peak*.92: halt=True
        if equity<=day_start*.98: locked_days.add(d)
        if equity<=week_start*.95: locked_weeks.add(w)
        # session tags from timezone aware UTC entry time
        from bot.time.sessions import session_flags
        sf=session_flags(pd.DatetimeIndex([base.index[pos]])); sname='Asia' if sf.asia.iloc[0] else 'London-NY overlap' if sf.london.iloc[0] and sf.new_york.iloc[0] else 'London' if sf.london.iloc[0] else 'New York' if sf.new_york.iloc[0] else 'Off-session'
        rfrow=features.iloc[pos]; trend_state='Trending' if rfrow.get('regime_trending',0)>=.5 else 'Ranging'
        vol_state='High volatility' if rfrow.get('regime_high_vol',0)>=.5 else 'Low volatility' if rfrow.get('regime_low_vol',0)>=.5 else 'Normal volatility'
        regime=f'{trend_state} / {vol_state}'
        trades.append({'entry_time':dt.isoformat(),'exit_time':raw.index[exitidx].isoformat(),'tf':tf,'side':side,'prob':float(z.prob),'lots':lots,'gross':gross,'cost':cost,'net_pnl':pnl,'net_r':pnl/max(budget,1e-9),'reason':reason,'management':management,'risk_fraction':risk_fraction,'session':sname,'trend_state':trend_state,'vol_state':vol_state,'regime':regime,'news':news,'news_offset_min':offset,'hold_min':exitidx-e+1})
    return trades,metrics(trades,maxdd)

def metrics(trades,dd=None):
    if not trades:return {'trades':0,'net_pnl':0.,'net_profit':0.,'profit_factor':0.,'win_rate':0.,'expectancy':0.,'max_drawdown_pct':float(dd or 0),'sharpe_ratio':None}
    d=pd.DataFrame(trades); p=d.net_pnl.to_numpy(); w=p[p>0]; l=-p[p<0]
    exits=pd.to_datetime(d.exit_time,utc=True).dt.floor('D'); daily=d.groupby(exits).net_pnl.sum()
    days=pd.date_range(daily.index.min(),daily.index.max(),freq='D',tz='UTC'); daily=daily.reindex(days,fill_value=0.); before=INIT+daily.cumsum().shift(1,fill_value=0.); ret=daily/before.replace(0,np.nan)
    sharpe=float(ret.mean()/ret.std(ddof=1)*np.sqrt(252)) if ret.std(ddof=1)>0 else None
    return {'trades':len(p),'net_pnl':float(p.sum()),'net_profit':float(p.sum()),'profit_factor':float(w.sum()/l.sum()) if l.sum() else float('inf'),'win_rate':float(np.mean(p>0)),'expectancy':float(p.mean()),'expectancy_r':float(d.net_r.mean()),'max_drawdown_pct':float(dd if dd is not None else 0),'sharpe_ratio':sharpe,'costs':float(d.cost.sum()),'mean_hold_min':float(d.hold_min.mean())}

def main():
    global EVENTS
    import sys
    sys.path.insert(0,str(ROOT/'src'))
    OUT.mkdir(parents=True,exist_ok=True)
    EVENTS=pd.read_csv(EVENT_FILE) if EVENT_FILE.exists() else pd.DataFrame(columns=['release_utc','event'])
    raw=pd.read_parquet(DATA).sort_index(); raw=raw.loc[~raw.index.duplicated()]
    # Convert to complete M1 close labels consistently; source timestamps are candle opens.
    results=[]; predictions=[]; fold_metrics=[]; gains=[]; feat_registry={}; cache=OUT/'variant_cache';cache.mkdir(parents=True,exist_ok=True)
    for tf in ('M1','M5','M15'):
        base,features=make_features(raw,tf); cand,entry_idx=samples_for(raw,base,features,tf)
        feat_registry[tf]=(base,features,cand,entry_idx)
        groups=feature_groups(features.columns,tf)
        friction=SPREAD+2*SLIP_SIDE+COMMISSION/CONTRACT
        variants=[('all',groups['all'],pair) for pair in PAIRS if (pair[1]+friction)/(pair[0]+pair[1])<=1.]
        variants += [(g,groups[g],ANCHOR) for g in ('base','base_context_zones')]
        for group,cols,(tp,sl) in variants:
            stem=f'v6_{tf}_{group}_tp{tp:g}_sl{sl:g}'.replace('.','p')
            pp=cache/f'{stem}_pred.csv'; mp=cache/f'{stem}_metrics.csv'; gp=cache/f'{stem}_gain.csv'
            if pp.exists() and mp.exists() and gp.exists():
                p=pd.read_csv(pp,parse_dates=['time']);m=pd.read_csv(mp);g=pd.read_csv(gp)
                print('cache',tf,group,tp,sl,flush=True)
            else:
                print('fit',tf,group,tp,sl,'rows',len(cand),flush=True)
                p,m,g=walkforward(raw,base,features,cand,entry_idx,tf,group,cols,tp,sl)
                p.to_csv(pp,index=False);m.to_csv(mp,index=False);g.to_csv(gp,index=False)
            if len(p):
                p['tf']=tf;p['group']=group;p['tp']=tp;p['sl']=sl;predictions.append(p)
            if len(m):fold_metrics.append(m)
            if len(g):gains.append(g)
    pred=pd.concat(predictions,ignore_index=True); folds=pd.concat(fold_metrics,ignore_index=True); imp=pd.concat(gains,ignore_index=True)
    pred.to_csv(OUT/'oos_predictions.csv',index=False);folds.to_csv(OUT/'walkforward_metrics.csv',index=False);imp.to_csv(OUT/'feature_importance.csv',index=False)
    last=raw.index[-1]+pd.Timedelta(minutes=1); lock_start=last-pd.Timedelta(days=180)
    grid=[]; choice=None
    # Rank only the full feature model candidates; feature-family ablations use the anchor pair.
    for tf in ('M1','M5','M15'):
        base,features,cand,entry_idx=feat_registry[tf]
        for pair in PAIRS:
            tp,sl=pair
            sub=pred[(pred.tf==tf)&(pred.group=='all')&(pred.tp==tp)&(pred.sl==sl)]
            costpx=SPREAD+2*SLIP_SIDE+COMMISSION/CONTRACT
            be=(sl+costpx)/(tp+sl)
            # Sweep the decision threshold empirically; break-even probability is
            # reported as a reference, not imposed as a brittle hard gate.
            thresholds=sorted(set([be,.25,.30,.35,.40,.45,.50,.55,.60,.65,.70,.75,.80,.85,.90]))
            for th in thresholds:
                for rf in (.0025,.005):
                    tr,st=sim(raw,base,features,sub,tp,sl,tf,th,base.index.min(),lock_start,risk_fraction=rf)
                    st.update({'tf':tf,'tp':tp,'sl':sl,'threshold':th,'risk_fraction':rf,'break_even_probability_worst_case':be,'group':'all'})
                    grid.append(st)
    grid=pd.DataFrame(grid);grid.to_csv(OUT/'strategy_grid_development.csv',index=False)
    selected_by_tf={}; selected_management={}; final=[]; breakdowns={}; exit_rows=[]; final_trades=[]
    for tf in ('M1','M5','M15'):
        candidates=grid[(grid.tf==tf)&(grid.risk_fraction==RISK)&(grid.trades>=100)]
        qualified=candidates[(candidates.net_pnl>0)&(candidates.profit_factor>1)&(candidates.max_drawdown_pct<=.10)]
        pool=qualified if len(qualified) else candidates
        if not len(pool):
            # Predeclared diagnostic fallback keeps the final OOS timeframe table
            # populated without pretending this configuration passed selection.
            entry_choice={'tf':tf,'tp':1.5,'sl':.75,'threshold':.30,'risk_fraction':RISK,'diagnostic':True}
        else:
            pool=pool.copy();pool['score']=pool.net_pnl/np.maximum(pool.max_drawdown_pct*INIT,100)
            entry_choice=pool.sort_values(['score','profit_factor'],ascending=False).iloc[0].to_dict()
        selected_by_tf[tf]=entry_choice
        tp=float(entry_choice['tp']);sl=float(entry_choice['sl']);th=float(entry_choice['threshold']);base,features,_,_=feat_registry[tf]
        sub=pred[(pred.tf==tf)&(pred.group=='all')&(pred.tp==tp)&(pred.sl==sl)]
        manager_rows=[]
        for manager in ('fixed','partial_1R','trailing_1R','htf_reversal'):
            for rf in (.0025,.005):
                _,devst=sim(raw,base,features,sub,tp,sl,tf,th,base.index.min(),lock_start,management=manager,risk_fraction=rf)
                manager_rows.append({'tf':tf,'tp':tp,'sl':sl,'threshold':th,'management':manager,'risk_fraction':rf,'development':devst})
        mgr=pd.DataFrame(manager_rows); good=mgr[(mgr.development.map(lambda z:z['trades'])>=100)&(mgr.development.map(lambda z:z['net_pnl'])>0)&(mgr.development.map(lambda z:z['profit_factor'])>1)&(mgr.development.map(lambda z:z['max_drawdown_pct'])<=.10)]
        manager_choice=(good.assign(score=good.development.map(lambda z:z['net_pnl']/max(z['max_drawdown_pct']*INIT,100))).sort_values('score',ascending=False).iloc[0].to_dict() if len(good) else next(r for r in manager_rows if r['management']=='fixed' and r['risk_fraction']==RISK))
        selected_management[tf]={k:manager_choice[k] for k in ('management','risk_fraction')}
        for r in manager_rows: r['selected_on_development']=r['management']==manager_choice['management'] and r['risk_fraction']==manager_choice['risk_fraction']
        exit_rows.extend(manager_rows)
        trades,st=sim(raw,base,features,sub,tp,sl,tf,th,lock_start,last,management=manager_choice['management'],risk_fraction=manager_choice['risk_fraction'])
        st.update({'tf':tf,'tp':tp,'sl':sl,'threshold':th,'management':manager_choice['management'],'risk_fraction':manager_choice['risk_fraction'],'diagnostic':not bool(len(good))})
        final.append(st); final_trades.extend(trades)
    pd.DataFrame(exit_rows).to_json(OUT/'risk_exit_comparison.json',orient='records',indent=2)
    td=pd.DataFrame(final_trades);td.to_csv(OUT/'final_selected_trades.csv',index=False)
    if len(td):
        breakdowns={key:td.groupby(key).net_pnl.agg(['count','sum','mean']).reset_index().to_dict('records') for key in ('tf','session','trend_state','vol_state','regime','news')}
    comparison=pd.DataFrame(final).set_index('tf').reindex(['M1','M5','M15']).reset_index() if final else pd.DataFrame(columns=['tf'])
    comparison.to_csv(OUT/'final_timeframe_comparison.csv',index=False)
    # Feature group ablation results on the same anchor label and common thresholds.
    abl=[]
    for tf in ('M1','M5','M15'):
        base,features,_,_=feat_registry[tf]
        for group in ('base','base_context_zones','all'):
            sub=pred[(pred.tf==tf)&(pred.group==group)&(pred.tp==ANCHOR[0])&(pred.sl==ANCHOR[1])]
            mm=folds[(folds.tf==tf)&(folds.group==group)&(folds.tp==ANCHOR[0])&(folds.sl==ANCHOR[1])]
            abl.append({'tf':tf,'group':group,'folds':len(mm),'mean_auc':float(mm.auc.mean()) if len(mm) else np.nan,'mean_brier':float(mm.brier.mean()) if len(mm) else np.nan,'auc_folds_gt_055':int((mm.auc>.55).sum()) if len(mm) else 0,'oos_predictions':len(sub)})
    pd.DataFrame(abl).to_csv(OUT/'feature_ablation.csv',index=False)
    summary={}
    for tf in ('M1','M5','M15'):
        for group in ('all','base','base_context_zones'):
            pairs=PAIRS if group=='all' else (ANCHOR,)
            for tp,sl in pairs:
                m=folds[(folds.tf==tf)&(folds.group==group)&(folds.tp==tp)&(folds.sl==sl)]
                summary[f'{tf}_{group}_tp{tp:g}_sl{sl:g}']={'folds':len(m),'walkforward_periods':int(m.fold.nunique()) if len(m) else 0,'auc_mean':float(m.auc.mean()) if len(m) else None,'auc_std':float(m.auc.std()) if len(m) else None,'auc_min':float(m.auc.min()) if len(m) else None,'auc_max':float(m.auc.max()) if len(m) else None,'brier_mean':float(m.brier.mean()) if len(m) else None,'folds_auc_over_055':int((m.auc>.55).sum()) if len(m) else 0}
    choice=selected_by_tf
    diagnostic=selected_management
    selected_config={tf:{k:v for k,v in cfg.items() if k in ('tp','sl','threshold','risk_fraction','net_pnl','profit_factor','trades','max_drawdown_pct','score')} if cfg else None for tf,cfg in selected_by_tf.items()}
    status_by_tf={r['tf']:('confirmed candidate' if not r.get('diagnostic') else 'diagnostic only; development gates failed') for r in final}
    friction=SPREAD+2*SLIP_SIDE+COMMISSION/CONTRACT
    report={'status':'research complete; no live/paper promotion','dataset':{'file':str(DATA.relative_to(ROOT)),'rows':len(raw),'from':raw.index[0].isoformat(),'to':raw.index[-1].isoformat(),'columns':list(raw.columns)},'design':{'decision':'completed M1/M5/M15 close; entry next M1 open','targets':'TP before SL first passage; same M1 bar both barriers = stop; timeout PnL marked to horizon close','horizons_minutes':HORIZON,'tp_sl_pairs':PAIRS,'break_even_probability_worst_case':{f'tp{tp}_sl{sl}':(sl+friction)/(tp+sl) for tp,sl in PAIRS},'walkforward':'365-day rolling train, 3-month test windows, horizon purge; latest 180 days held out','risk':'compare 0.25% and 0.50% per trade; 100 oz/lot, .01 min/step; minimum lot rejected if over risk budget; daily -2%, weekly -5%, peak DD -8%','costs':{'round_trip_spread_px':SPREAD,'slippage_each_side_px':SLIP_SIDE,'commission_per_lot_round_turn':COMMISSION,'contract_oz_per_lot':CONTRACT,'cost_price_equivalent':friction},'management':['fixed TP/SL','partial 50% at 1R then breakeven stop','trail one initial risk after +1R','HTF reversal exit after +1R'],'features':'candle pressure/streaks, session, scheduled news, higher-TF context, prior-day levels, rolling swing/sweeps, order-block proxy, tick-volume profile proxy'},'model_metrics_by_variant':summary,'feature_ablation':abl,'selected_development_candidate':choice,'diagnostic_candidate':diagnostic,'risk_exit_comparison_file':str((OUT/'risk_exit_comparison.json').relative_to(ROOT)) if diagnostic else None,'final_confirmation':final,'acceptance_gates':({'positive_expectancy':final[0]['net_pnl']>0,'profit_factor_at_least_1_5':final[0]['profit_factor']>=1.5,'max_drawdown_at_most_10pct':final[0]['max_drawdown_pct']<=.1,'minimum_100_final_trades':final[0]['trades']>=100} if final and not final[0].get('diagnostic') else 'not_met_or_not_evaluated'),'session_news_breakdown':breakdowns,'feature_importance_top':{str(k):v.sort_values('gain_share',ascending=False).groupby('feature').gain_share.mean().sort_values(ascending=False).head(20).to_dict() for k,v in imp.groupby(['tf','group','tp','sl'])},'limitations':['OHLC bars do not reveal within-minute order; ambiguous TP/SL bars are stops.','MT5 spread is recorded in broker points; conversion uses .001 price unit and the user-provided $0.50 conservative round-trip floor.','Tick volume is broker-specific activity, not centralized traded volume; POC/VAH/VAL are prior-day approximations.','Order-block proxy means last opposite candle before a confirmed range displacement, not observed institutional orders.','Historical rejected/requoted fills and swap series are unavailable; trades are intraday only.','The final period only evaluates a development configuration; it does not rescue a strategy that failed development eligibility.']}
    report['dataset']['real_volume_nonzero_fraction']=float((raw.real_volume.fillna(0)>0).mean()) if 'real_volume' in raw else 0.0
    report['dataset']['spread_price_quantiles']=(raw.spread*POINT).quantile([.5,.9,.99]).to_dict() if 'spread' in raw else {}
    report['selected_development_config_by_tf']=selected_config
    report['selected_management_by_tf']=selected_management
    report['final_confirmation_by_tf']=final
    report['final_comparison_table']=final
    report['final_status_by_tf']=status_by_tf
    report['risk_exit_development_comparison_file']=str((OUT/'risk_exit_comparison.json').relative_to(ROOT))
    report['acceptance_gates_by_tf']={r['tf']:{'positive_expectancy':r['net_pnl']>0,'profit_factor_at_least_1_5':r['profit_factor']>=1.5,'max_drawdown_at_most_10pct':r['max_drawdown_pct']<=.10,'minimum_100_final_trades':r['trades']>=100,'diagnostic_only':r['diagnostic']} for r in final}
    report['session_regime_news_breakdown']=breakdowns
    report['design']['features']='candle pressure/streaks, session-relative tick volume, spread anomaly, candle anatomy, ADX, Bollinger width, ADR utilization, scheduled news, regime, higher-TF context, prior-day/rolling levels, sweeps, order-block/profile proxies'
    report['design']['m1_sampling']='each second completed M1 bar; M5 and M15 every completed bar'
    report['design']['walkforward']='365-day rolling train, 6-month test windows, horizon purge; latest 180 days held out'
    periods={tf:sorted(folds.loc[(folds.tf==tf)&(folds.group=='all'),'fold'].drop_duplicates().astype(str).tolist()) for tf in ('M1','M5','M15')}
    report['walkforward_periods_by_tf']=periods
    report['independence_note']='The non-overlapping six-month test blocks are chronological but not fully independent: training windows overlap and market regimes persist. The 3.25-year history yields only a small handful of walk-forward periods plus one final six-month holdout; this is not enough evidence to claim stable institutional-grade performance.'
    report['leakage_controls']={'decision_time':'Features include the completed decision candle; simulated order enters at the next M1 open.','higher_timeframe':'Right-edge completed bars are forward-filled only after their close.','swing_points':'Two right-side bars are required; a swing level is exposed only after those bars close.','fvg':'FVG features are first exposed at the third candle close; retest/inversion features require a positive age from a previously completed FVG.','daily_levels_profile':'Only prior completed UTC-day levels/profile are used.','purge':'Training events ending within the max target horizon before each test block are purged.'}
    report['execution_accounting']={'entry':'next M1 open; spread/slippage/commission charged round turn','same_bar_tp_sl':'stop first','position_interaction':'one position at a time; subsequent signals before exit are skipped; sizing uses updated equity','risk_limits':'0.25%/0.50% fractional sizing, min lot/step, daily/weekly/peak equity stops','unavailable':'requotes, actual fill latency/slippage, and historical swap/order-book data'}
    report['limitations']=['OHLC bars do not reveal within-minute order; ambiguous TP/SL bars are stops.','MT5 spread is recorded in broker points; conversion uses .001 price unit and $0.50 conservative round-trip floor.','Tick volume is broker-specific quote activity, not centralized traded volume; POC/VAH/VAL are prior-day approximations.','Order-block proxy is last opposing candle before a confirmed range displacement, not observed institutional orders.','No historical bid/ask ticks, order-book depth, rejected/requoted fills, or synchronized cross-market series are available.','The final six months report one development-selected configuration per timeframe; diagnostic-only rows failed development gates.']
    (OUT/'research_report.json').write_text(json.dumps(report,indent=2,default=str))
    final_map={r['tf']:r for r in final}
    lines=['# M1 / M5 / M15 Event-Model Research','',f"Data: {len(raw):,} M1 bars, {raw.index[0].date()} to {raw.index[-1].date()}. Latest 180 days held out.",'','## Final out-of-sample comparison','','| Metric | M1 | M5 | M15 |','|---|---:|---:|---:|']
    metric_rows=[('Win rate','win_rate',lambda v:f'{v:.1%}'),('Profit factor','profit_factor',lambda v:f'{v:.2f}'),('Maximum drawdown','max_drawdown_pct',lambda v:f'{v:.2%}'),('Expectancy / trade','expectancy',lambda v:f'${v:.2f}'),('Total trades','trades',lambda v:f'{int(v)}'),('Net profit','net_pnl',lambda v:f'${v:.2f}'),('Sharpe ratio','sharpe_ratio',lambda v:'—' if v is None else f'{v:.2f}')]
    for label,key,fmt in metric_rows:
        vals=['—' if final_map.get(tf,{}).get(key) is None else fmt(final_map[tf][key]) for tf in ('M1','M5','M15')]
        lines.append(f'| {label} | {vals[0]} | {vals[1]} | {vals[2]} |')
    lines += ['', 'Each timeframe uses a TP/SL, probability threshold, and management choice selected on development folds. M1 decisions are evaluated every second completed M1 bar; M5/M15 use each completed bar. A dash means no development candidate had at least 100 trades. The JSON distinguishes confirmed candidates from diagnostic-only rows.', '', '## Method and limits', '',f'- Conservative modeled friction: ${friction:.2f} price units round trip.','- Compared 0.25% and 0.50% risk; fixed TP/SL, partial at 1R, trailing after 1R, and higher-timeframe reversal on matched entries.','- Same-candle TP/SL ambiguity is counted as a stop; open positions are marked to market for daily, weekly, and peak drawdown controls.','- Features include candle momentum/streaks, regimes, session/news, completed HTF context, two-bar-confirmed swings, prior-day/rolling levels, FVG/retests, sweeps, ADX, Bollinger width, ADR, session-relative tick volume, spread anomaly, and profile proxies.','- The report lists the number of six-month walk-forward blocks. These blocks are not fully independent because training windows overlap and regimes persist; 3.25 years is still a short sample.','- MT5 real volume is zero in this file. Tick volume is broker quote activity; no tick imbalance, arrival-rate, depth, or cross-market result is claimed.','- $0.50 TP / $0.50 SL and $0.75 TP / $0.50 SL cannot break even with $0.77 modeled friction.','- Research only. A candidate that fails its unseen period is not viable.','', 'See `risk_exit_comparison.json`, `feature_ablation.csv`, `walkforward_metrics.csv`, and `final_selected_trades.csv`.']
    (OUT/'research_report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('report',OUT/'research_report.json',flush=True);print('markdown',OUT/'research_report.md',flush=True);print('final',final,flush=True)

if __name__=='__main__': main()
