# Hull label port: what was checked

**Goal:** preserve the user's printed Hull labels (not the chart simulator's EMA-gated queue) and filter new entries by confirmed internal structure before using the initial local-wick SL and a per-ticket USD target. New tickets now use the [70/30 profit ladder](../../docs/HULL_DEMO.md#profit-ladder-for-new-tickets); the historical result files below predate it. Never infer a fill or winning probability from a screenshot. Implementation: `src/bot/hull`. [Exact conventions and Windows setup](../../docs/HULL_DEMO.md).

## Calculation parity

`chart_reference.cjs` extracts the calculation-only JavaScript from the supplied Free Chart script; a conventional linearly weighted WMA replaces the platform-specific `bv.ma`. It uses the supplied strict crossover, masked windows, Wilder ATR seed/recursion and most-recent-equal pivot behavior. `reference.json` includes reproducible OHLC and expected `a,b,label,ATR,last confirmed low,high`. A final forming candle is excluded. Tests compare 240 closed bars × four Hull length/source settings = **960 rows**, including flat sections and warm-up. Raw direction agrees exactly on the fixture. This is not an independently exported Free Chart or live MT5 comparison: feeds, timeframes, MA rounding at equality and bar boundaries can differ. The optional EMA-plan mode uses a first-close EMA seed; EMA never affects the selected raw labels. Additional tests cover the intentionally different local-wick anchor, causal close timing, broker tick/FX conversion, fast/medium/slow counts, aggregate risk, protection, no duplicate batch and no live-money account path.

To compare your chart directly: supply a single native-timeframe CSV with `timestamp,open,high,low,close` (bar-open UTC; optionally `label` = BUY/SELL/blank for each closed candle). Export enough prior history so both computations share warm-up. Example:

```sh
python research/hull_labels/compare_chart.py --bars data/chart_M2.csv --tf 2 --last-live --out reports/hull_replay_chart/check.csv
```

Use `--last-live` only if the last row is still forming. Remove it if every row is closed. This compares chart labels, not similarity of broker fills. To regenerate the test fixture: `node research/hull_labels/chart_reference.cjs` (Node is not needed to run the bot or its Python tests).

## Structure-filtered execution

[Entry specification](../../docs/HULL_STRUCTURE.md) · [checked results](structure_check/summary.json) · [all raw labels with causal structure and entry reasons](structure_check/labels.csv). The scanner preserves the raw chart labels, not automatic entries on every label. The checked-in `structure_check/` figures are from `ec5d3db` with the then-current 1-ticket slow profile and $15 aggregate risk cap, **not** a replay with today’s scaled demo limits. To reproduce those counts and economics use that revision in a separate research checkout. To evaluate the checked-out code, run `python research/hull_labels/reproduce.py --cache data/public_xau_ticks --out reports/hull_replay_structure` against the pinned weeks 45–46. Counts at the label close are before quote, batch and risk vetoes. The replay applies the configured opposite-label exit independently of entry qualification. Compare the same feed, time window and limits; this short sample does not establish a trading edge.

Current `hull-replay` walks **ordered** executable quotes within each minute. An already-attached stop/TP is evaluated before any same-tick ladder amendment. Simulated tickets share the rate budget, so some can remain on an earlier rung. Each closed group’s CSV row records its own size, original and final prices and `ladder_rung`; `net_usd_batch` on such a row is the contribution of those legs. Batch metrics sum the groups from the same entry. This idealized replay assumes common initial fills, no amendment latency and no broker rejection. It is not evidence that trailing improves returns on a particular feed.

## Archived pre-structure ordered-quote smoke comparison

Independent public XAUUSD feed, Nov 12–15 2026 UTC, with week 45 for warm-up and week 46 through Nov 15 for evaluation. Pinned [source revision](https://github.com/Touhid-HZ/xauusd-tick-data-2026/tree/3ef0f9a5940898a3f690140481500730a3fe178a), [file hashes](results/inputs.json). No broker `XAUUSDm` data or trading account available. Shared signals; three separate flat simulated $10,000 accounts; 0.09 lot, $10 per ticket, frozen local-wick stop, 0.300 maximum spread, $15 aggregate open-risk allowance. Executable quote sides, no extra commission/slippage; stop at the first crossing quote and TP at the limit. No simulated latency, broker rejects/margin, atomic-fill guarantee, FX conversion changes or swaps. P&L and drawdown are realized dollars by batch; account guards mark equity at minute boundaries. The comparison is single-symbol, not a multi-symbol portfolio backtest.

| Evaluation | Raw closed labels | Batches | Outcome |
|---|---:|---:|---|
| Slow (1 ticket) | 460 | 2 | Two stopped sells, **−$19.152** total; PF 0 |
| Medium (3–4) | 460 | 0 | No admissible batch; win rate/PF undefined |
| Fast (6–7) | 460 | 0 | No admissible batch; win rate/PF undefined |

1,008,613 evaluation quotes, 2,760 observed M1 bars; 267 M1 labels, 130 M2, 63 M5. Labels on the same timestamp need not be separate trade opportunities. For slow, 418 decision timestamps first failed the spread cap and 16 failed the aggregate risk gate. For medium/fast, 419 spread vetoes and 18 risk vetoes each. **Do not disable guards to make one sample trade more.** These figures do not measure win rate on the user's OANDA chart or `XAUUSDm` demo feed. The chart table's 1.5R target and equity sizing in particular are not the new ticket policy.

**Trace one real rule decision:** Nov 12 10:40 UTC, M5 SELL. The selected **earlier** high was 3950.955 on the 10:20 candle. ATR buffer took the raw stop to 3951.38134; outward tick rounding gave **3951.382**. Bid entry 3950.581, per-ticket TP 3949.469 (≈$10.008), initial risk **$7.209**, not $5. The first ask to cross was 3951.395: **−$7.326**. Six tickets at that planned stop need **$43.254** of risk and therefore cannot be sent under the $15 portfolio limit.

[Summary](results/summary.json) · [every raw label/anchor](results/labels.csv) · [slow fills](results/slow.csv). Reproduce **these archived unfiltered results** from commit `9af93fd` with `python research/hull_labels/reproduce.py --cache data/public_xau_ticks --out reports/hull_replay_public` (needs the two verified files; see `results/inputs.json`). On your own instrument: `python -m bot.cli hull-replay --help`. It requires ordered UTC bid/ask ticks and explicit offline tick/contract size and conversion: `quote_usd` for gold/EURUSD quoted in USD, `base_usd` for USD-base FX. Do not pass an invented conversion for a non-USD cross. The connected demo path instead uses the terminal's profit and margin calculators.
