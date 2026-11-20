"""Semantic invariants on the actual v3 PineTS output; no broker connection or profitability claims."""
import json
import math
import sys
from collections import Counter
from pathlib import Path


def check(path):
    tape = json.loads(Path(path).read_text()); keys = tape['keys']; totals = Counter()
    for case in tape['cases']:
        prev = None; data = tape['fixtures'][case['fixture']]; stats = Counter()
        for i, (bar, values) in enumerate(zip(data, case['rows'], strict=True)):
            r = dict(zip(keys, values, strict=True)); t, o, h, l, c = bar
            bias, d, mid = r['bias'], r['selected'], r['midRange']; origin, tip = r['origin'], r['extreme']
            ready = r['eventKind'] == 3 and None not in (origin,tip) and bias*(tip-origin)>0
            assert (mid is not None)==ready
            if ready: assert math.isclose(mid,(origin+tip)/2,rel_tol=0,abs_tol=5.1e-10)
            bz=ready and bias==1; sz=ready and bias==-1
            assert (r['buyLo'],r['buyHi'])==((origin,mid) if bz else (None,None))
            assert (r['sellLo'],r['sellHi'])==((mid,origin) if sz else (None,None))
            tr=1 if bz and d==1 and origin<c<=mid else -1 if sz and d==-1 and mid<=c<origin else 0
            assert r['trendSide']==tr
            assert r['structureSide']==(r['touched'] or tr)
            if not r['retestFill'] and r['suggestion']:assert r['suggestion']==tr==bias
            if r['armedNow']:
                sign=r['armedNow'];assert ready and d==sign==-bias and r['watchGuard']==origin
                assert (tip<=c<r['broken']) if sign==1 else (r['broken']<c<=tip)
                assert r['suggestion']!=sign and r['placedNow']!=sign
                stats['watch'+str(sign)]+=1
            if r['confirmNow']:
                sign=r['confirmNow'];assert r['guardEvent']==2*sign and bias==sign and sign*(c-r['broken'])>0
                assert prev and r['broken']==prev['origin'] and r['watchSide']==0
                assert d==sign or (prev['watchSide']==sign and prev['watchGuard']==r['broken'])
                assert d!=-sign and r['retLevel']==r['broken']
                if d==0:stats['confirm_from_stored_watch']+=1
            if r['placedNow']:
                sign=r['placedNow'];assert sign==r['confirmNow']==r['retSide'] and r['qRetOK'] and sign*(r['retLevel']-r['retSL'])>0
                # A separately existing, opposite plan can theoretically touch on this bar; the NEW plan cannot fill.
                assert r['retestFill']!=sign and r['suggestion']!=sign
            if prev and prev['watchSide'] and d==-prev['watchSide']:assert r['confirmNow']!=prev['watchSide'] and r['watchSide']!=prev['watchSide']
            if r['touched']:
                sign=r['touched']; assert prev and prev['retSide']==sign and prev['retLevel'] is not None and t==data[i-1][0]+60
                assert (l<=prev['retLevel']) if sign==1 else (h>=prev['retLevel'])
                stats['touched'+str(sign)]+=1
            if r['retestFill']:
                sign=r['retestFill'];assert sign==r['touched']==r['filledSide']==r['suggestion'] and prev['qRetOK']
                assert sign*(r['simFillPx']-prev['retLevel'])<=1e-9
                assert r['simFillSL']==prev['retSL'] and r['simFillTP']==prev['retTP']
                stats['retFill'+str(sign)]+=1
            elif r['filledSide']:
                assert prev and prev['pendingSide']==r['filledSide']==prev['suggestion']
            if r['retSide'] and not r['confirmNow']:assert prev and r['retLevel']==prev['retLevel']
            stats['bars']+=1;prev=r
        if case['name'] in ('watch-buy','watch-sell'):
            sign=1 if case['name']=='watch-buy' else -1
            price=92.281 if sign==1 else 107.719
            get=lambda i,k:case['rows'][i][keys.index(k)]
            assert get(410,'armedNow')==sign and get(410,'suggestion')==0
            assert get(416,'watchSide')==sign and get(416,'guardEvent')==0
            assert get(418,'selected')==0 and get(418,'confirmNow')==get(418,'placedNow')==sign
            assert get(418,'retLevel')==price and get(418,'filledSide')==get(418,'suggestion')==0
            assert get(419,'touched')==0 and get(419,'qRetOK')==1
            assert get(420,'touched')==get(420,'retestFill')==get(420,'suggestion')==sign
            assert get(420,'simFillPx')==price
        totals.update(stats)
    assert all(totals[k]>0 for k in ('watch-1','watch1','retFill1','retFill-1','confirm_from_stored_watch'))
    print('PASS chart-only v3 invariants:',dict(totals))

if __name__=='__main__':check(sys.argv[1])
