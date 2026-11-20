# Which session moves each Hull market?

**Reference period: 1 September–31 October 2026, UTC.** This is a price study, not a strategy backtest. The 11 configured Exness markets contributed **112,493,616 bid/ask ticks and 132,439 complete five-minute bars**. No private terminal, orders, fills or journal records were used. `DE40` and `DE30` resolve to one `DE30m` contract. `1HZ100V` is a 24/7 Deriv synthetic with no home exchange session; it has no Exness benchmark here.

## Findings

Typical five-minute **mid-price high–low range**, in basis points of price (1 bp = 0.01%). Values compare equal-duration bars regardless of lot size, contract multiplier, or index level. “Open” means the **first hour** on the indicated local cash-market clock. Broad buckets and opening hours answer different questions and must not be added together.

| Config profile (market) | Widest of four broad sessions | Median M5 range | Widest of three first hours | Median M5 range |
|---|---|---:|---|---:|
| JP225 (Japan) | London + New York | 13.13 bp | Tokyo | **23.70 bp** |
| DE40 / DE30 (Germany) | London before NY | 9.12 bp | Frankfurt | **12.23 bp** |
| US500 (US) | London + New York | 7.04 bp | New York | **9.84 bp** |
| NAS100USD (US) | London + New York | 10.96 bp | New York | 17.22 bp |
| US30 (US) | London + New York | 8.39 bp | New York | 13.09 bp |
| XAUUSD (gold) | London + New York | 13.61 bp | New York | 16.64 bp |
| GBPJPY | London + New York | 3.93 bp | New York | 4.63 bp |
| EURUSD | London + New York | 3.39 bp | New York | 3.85 bp |
| GBPUSD | London + New York | 3.76 bp | New York | 4.12 bp |
| USDJPY | London + New York | 4.00 bp | New York | 4.64 bp |
| AUDUSD | London + New York | 3.84 bp | New York | 4.45 bp |

- **JP225:** Tokyo 09:00–10:00 JST (03:00–04:00 Nairobi) narrowly phrased is the useful hypothesis: 23.70 bp versus 18.85 bp in the US opening hour. Its **whole** Asia-Pacific bucket was 12.84 bp, slightly below 13.13 bp in the London–NY overlap. Japan’s cash lunch break is 11:30–12:30 JST. Home hours are not an always-on edge.
- **Two research-only comparators not in Hull config:** HK50m’s widest broad bucket was Asia-Pacific (7.52 bp). XAGUSDm’s was the London–NY overlap (23.56 bp); its median quoted spread then was about 4.58 bp. Neither was added to the dashboard ranking, scanner or symbol config.

## What counts as a session?

The same weekday city windows as Hull Ledger: Sydney 08:00–17:00, Tokyo 09:00–18:00, London 08:00–17:00 and New York 08:00–17:00, interpreted **in each local time zone on the given date**. Assign each observed M5 bar by UTC bar start: (1) London and NY both on → overlap; (2) otherwise London or NY if on; (3) otherwise Tokyo or Sydney → Asia-Pacific; (4) otherwise Off-session. This avoids giving one bar to two broad buckets. The dashboard ranks the four named sessions; retains off-session evidence separately. Sydney Monday can start on Sunday UTC.

An independent comparison flags the first 60 minutes from **09:00 Tokyo, 09:00 Frankfurt or 09:30 New York**, on a local weekday. For Nairobi those starts are respectively **03:00**, **10:00 (CEST) / 11:00 (CET)**, **16:30 (EDT) / 17:30 (EST)**. Holiday-specific opens, half-days, news days and lunch breaks aren't modelled separately. A CFD can still quote when its underlying exchange is closed. These times do not enable or disable trading.

## Data & measurement

- [Exness historical quotes](https://get.exness.help/hc/en-us/articles/360021547851-Tick-history) and [contract identities](https://get.exness.help/hc/en-us/articles/17854383867548-Indices). The 26 pinned monthly files in `archives.json` cover 11 configured markets plus two comparison markets. Archive servers may differ slightly from your MT5 demo. This snapshot does not claim that all markets quoted on the same dates (40–44 UTC dates per opening-hour cell).
- Exchange clocks: [JPX cash hours](https://www.jpx.co.jp/english/equities/trading/domestic/01.html), [Deutsche Börse: Xetra core hours](https://www.deutsche-boerse.com/dbg-en/media/news-stories/press-releases/Deutsche-B-rse-Group-Launches-24-7-Trading-for-DAX-40-Equities-on-360X-5664632), [NYSE core session](https://www.nyse.com/trade/hours-calendars). Context: [RBA’s intraday FX study](https://www.rba.gov.au/publications/bulletin/2007/dec/1.html) also found elevated volatility around London–NY and a Tokyo-open effect for USD/JPY. Its 1993–2007 observations are **not** inputs to these numbers. [Deriv’s synthetic indices](https://deriv.com/derived-indices/volatility-indices) do not have an exchange open.
- Check file identity, symbol, timestamp order and UTC month. Exclude non-finite/negative/crossed quotes; none were found in this snapshot. Preserve equal-time ticks in archive order. For each non-overlapping UTC five-minute interval require at least one valid quote in **every** constituent minute. No forward-fill, zero bars for closures, tick weighting or overnight return. This coverage rule can omit thin periods and does not prove continuous liquidity within a minute.
- `mid = (bid + ask) / 2`; `range_bp = 10,000 × (max_mid − min_mid) / first_mid`. Spread: on the first valid quote of each minute compute `10,000 × (ask − bid) / mid`; take the median of those five minute samples per bar. Report the **pooled median across complete bars** for each market/window, observation count and number of UTC dates. This range proxy is not return standard deviation. Range is unsigned; neither statistic estimates profit or slippage. The UI requires ≥100 bars on ≥10 UTC dates to rank a cell; no bars means missing, not quiet.

## Reproduce or use for later research

From the repository root, with Python 3.11+, NumPy, pandas 2+ and PyYAML installed (plus `tzdata` on Windows):

```text
python research/session_volatility/study.py /path/outside/repo/exness-zips --download
python HullJournal/ui/build_dashboard.py
```

Use your own folder path. The optional download fetches missing monthly archives over HTTPS; **without `--download`, the study runs offline** on those files. Hash or input-count mismatches fail rather than substituting a different series. `windows.csv` is the full period table; `windows_by_utc_date.csv` gives one row per observed **UTC date × broker symbol × window**. Its day medians should not be averaged to reconstruct the pooled median. Raw tick files are not in the repo or the kit.

For a strategy study, use **completed setups**, the actual first-fill UTC time, count, realized net after costs and a consistent risk/size basis. Match exact broker symbols and window definitions; keep no-trade dates distinct from losing days. Use later journal dates as an out-of-sample test of a *fixed hypothesis*, not as though September–October quotes were contemporary with them. To study market-to-market return correlation instead, compare synchronized returns on common timestamps, not these unsigned ranges or a set of ladder tickets. A large move might be a whipsaw; it says nothing by itself about the direction or outcome of a Hull setup.
