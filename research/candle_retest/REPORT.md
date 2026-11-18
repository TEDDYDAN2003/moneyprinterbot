# Gold: does a candle stop fix the entries?

**Decision, 18 November 2026: keep the scanner in shadow.** Waiting for a second quote made a real wick stop affordable on some trades. It reduced dollars lost versus a fixed stop on the *same retests* but did not improve their win count. The combined pattern-aware, actively managed observer lost in **each of five windows**. Do not widen the stop, increase size, or promote the three-trade sweep result to a proven strategy.

## What was actually compared

- **One XAUUSD leg**, 0.09 × 100 oz = 9 oz. **$10 target / $5 maximum modeled initial risk**, not a guaranteed realized stop. Bid/ask spread is retained once; no commission or added slippage. The 0.300 maximum entry spread and session settings were not loosened. An allowed 0.300 spread already costs $2.70, leaving only about 0.256 price units between the initial executable mark and a full $5 stop.
- [Rules and phase chronology](PROTOCOL.md). Primary: four separately reset $10,000 simulated accounts per policy (Sep 3–Nov 15). The Oct windows were previously examined; Sep was a fresh historical replication; Nov 12–15 is a short later-dated slice, **not live forward trading**. After seeing no affordable sweep entries inside the original S/R band, a separately specified experiment also allowed returns into the previously swept pocket, and added **Aug 6–Sep 2**, unused before that choice. No thresholds were selected from that August window.
- [Public source](https://github.com/Touhid-HZ/xauusd-tick-data-2026/tree/3ef0f9a5940898a3f690140481500730a3fe178a) and [15 file hashes](inputs.json). **22,673,430 ordered bid/ask ticks, 99,148 M1 bars** in the expanded comparison; **17,570,325 ticks, 71,548 bars** in the primary four. These are **not the user's XAUUSDm prices**. The symbol suffix in our config does not change where replay prices came from. Screenshots were not training labels. No other symbols were performance-tested.
- Use only completed M1/M5/M15 support/resistance and pullback reactions. Keep candle, previous-candle-at-level and finished source-frame tags. Freeze the level, extreme and objective before the watch. Cancel on expiry (180 s), invalidation/objective at any tick, opposite structure, gaps or market gates. Sample the first tick per two-second UTC bucket for discretionary entry and giveback. The initial bar-close signal must arrive on time. Fill at ask for a long, bid for a short. Never apply a pre-entry tick to a new position. The replay's hard stop and target inspect every ordered quote, even if P&L changes faster than its two-second observer.
- Ideal barriers unless marked *first crossing*. Next-zone targets can be an opposing level or the configured round-number objective and must offer at least $10. Active exits use contrary completed-candle evidence or sampled giveback; they never loosen an SL. See [operating rules](../../docs/CANDLE_RETEST.md) for exact definitions and the difference between a two-second sample and a broker terminal poll. No order-book inference beyond price, latency model, margin rejections, swap or broker acceptance in replay. The guard marks equity at minute boundaries; reported DD is realized, not worst open-equity DD.

## Primary ladder · four windows, Sept 3–Nov 15

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Worst window DD¹ |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 7 / 32 | 21.9% | 0.56 | −54.82 | −1.71 | 44.96 |
| B · Band retest / cash SL | 3 / 26 | 11.5% | 0.26 | −84.86 | −3.26 | 39.96 |
| C · Same retest / wick SL | 3 / 26 | 11.5% | 0.32 | −63.14 | −2.43 | 32.14 |
| D · C + candle filter | 0 / 8 | 0.0% | 0.00 | −32.56 | −4.07 | 19.43 |
| E · D + next-zone TP | 0 / 8 | 0.0% | 0.00 | −32.56 | −4.07 | 19.43 |
| F · E + monitored exits | 0 / 8 | 0.0% | 0.00 | −31.17 | −3.90 | 19.43 |

¹ Maximum of the *separate* window realized drawdowns, in USD. Pooled net and PF are descriptive sums of dollar outcomes, **not** a continuous portfolio, average of period PFs, or 104 independent experiments. No-trade PF, win rate and expectancy are undefined. Cash controls rounded to about +$10.008 / −$4.995 on this tick grid. Exact metrics and ledgers including empty files: [primary](evidence/primary).

**Stop vs entry vs exit:** B and C filled at the same 26 prices/times: 3 wins, 23 losses in both. Actual wick stops reduced the net loss by **$21.726**, but C was still worse than the immediate control in aggregate. Within C, 18/26 trades ended in less than a second; median holding time was **0.102 s**. A second visit to a level is not necessarily confirmation of renewed demand or supply. The mandatory candle filter removed all three winners: zero wins among eight fills. On Oct 22 a real bullish body engulf at a respected pullback still lost: long 4022.045, SL 4021.491, −$4.986. F saved $1.395 on one September loss by exiting on an adverse candle; it produced no winners. This is evidence against making that pattern filter mandatory *here*, not evidence that engulfing candles never matter.

**Research-only wider stop:** freeze A's 32 entry times, directions, 0.09 lots and $10 targets. Ignoring the cap raised wins from **7/32 to 18/32**, but net went from **−$54.819 to −$66.681** (PF 0.561 → 0.730). **32/32** wick risks breached $5: median $19.638, max $66.717. These trades can overlap; they are not a funded account or a permitted configuration. With August included the figures are 18/34 and −$109.089 for the uncapped diagnostic versus 7/34 and −$64.809 for the immediate control. More winners did not pay for the larger losers.

## Follow-up · test the swept area itself

In the primary ladder no filled second-band entry was classified as a sweep/reclaim. For a confirmed sweep only, the follow-up allows a return between the swept-side edge and the already-known wick; no catch of a falling/rising price without the earlier reclaim. For other setups the combined observer uses the original band. The pattern is logged; the optional filtered arm requires a directional engulf/rejection or eligible predecessor with follow-through. Combined is a separately replayed account with one watch at a time, not a sum of arms.

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Worst window DD¹ |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 7 / 34 | 20.6% | 0.52 | −64.81 | −1.91 | 44.96 |
| B · Band retest / cash SL | 4 / 30 | 13.3% | 0.31 | −89.84 | −2.99 | 39.96 |
| C · Same retest / wick SL | 4 / 30 | 13.3% | 0.38 | −65.74 | −2.19 | 32.14 |
| D · C + candle filter | 0 / 8 | 0.0% | 0.00 | −32.56 | −4.07 | 19.43 |
| E · D + next-zone TP | 0 / 8 | 0.0% | 0.00 | −32.56 | −4.07 | 19.43 |
| F · E + monitored exits | 0 / 8 | 0.0% | 0.00 | −31.17 | −3.90 | 19.43 |
| Band retest / wick + managed | 3 / 30 | 10.0% | 0.33 | −69.42 | −2.31 | 30.74 |
| Swept pocket / wick + $10 TP | 1 / 3 | 33.3% | 1.62 | 3.83 | 1.28 | 3.34 |
| Swept pocket / wick + managed | 1 / 3 | 33.3% | 0.99 | −0.05 | −0.02 | 3.34 |
| Swept pocket / pattern + managed | 0 / 1 | 0.0% | 0.00 | −2.84 | −2.84 | 2.84 |
| Combined / wick + managed | 4 / 33 | 12.1% | 0.37 | −69.47 | −2.11 | 30.74 |

- The 30 identical B/C returns in the expanded sample won 4 each. The wick reduced losses by **$24.102**, but still returned −$65.736. Requiring the candle discarded all four winning retests. Lower absolute loss from fewer trades is not evidence of a better per-trade expectancy.
- The direct swept-pocket policy produced only **three** trades: Aug 19 buy −$3.339; Aug 27 sell +$10.008 fixed target / +$6.120 managed; Nov 11 sell −$2.835. Thus fixed TP pooled PF **1.62**, net **+$3.834**; monitored PF **0.99**, net **−$0.054**. Both are 1/3, entirely dependent on one winner. Sep, Oct 1–28 and Nov 12–15 had **no** such fills. Requiring a directional pattern left only the losing Nov 11 trade.
- On Aug 27 the last M1 high was 4592.205, already **below** the eventual short fill at 4592.875. The earlier sweep extreme was 4593.085, so using just the latest candle would misplace the stop. The bot used the farther extreme: ask SL 4593.376, initial risk $4.509. Sampled peak got to $10.44; exit on giveback locked in $6.12, **less** than the fixed target, not an improvement.
- Combined exits: **26 hard stops, 3 adverse candles, 3 profit givebacks, 1 next-zone target**. 18/33 trades ended in under a second. Actual initial risk ranged from $2.196 to $4.986. Active exits sometimes helped and sometimes gave back a winner. No justification for a universal “hold longer” rule.
- In Oct 29–Nov 11, **1,211/1,246 immediate zone candidates** first failed the spread check. The combined rule armed 1,208 watches; 692 saw the objective first, 284 were invalidated, 190 expired, 35 saw contrary structure, **7 entered**. These are lifecycles, not quote counts. Quote checks can fail many times within one watch. Many correctly anticipated moves simply did not offer an affordable second entry.

### November 11 · a bearish candle that did *not* keep an ask stop safe

At 12:49 UTC an M1 resistance sweep at 4042.847 had reclaimed the level, after a doji, with a bearish upper-wick rejection. Reaction **bid high 4043.345**; buffered bid invalidation **4043.346**. At **12:50:20.011** the second visit gave **bid 4043.328 / ask 4043.625**. The actual short stop was **4043.643**: one tick beyond the bid high plus 0.297 entry spread. Modeled risk was `(4043.643 − 4043.328) × 9 = $2.835`. **$2.673** of it was already the entry spread. From the current ask there was just **0.018** to the stop.

At **12:50:47.369** the quote was **4043.095 / 4043.665**. The spread had widened to **0.570**. The bid moved *down* and never exceeded 4043.328 while the short was open; the ask nevertheless hit the attached SL. Barrier loss −$2.835; first executable quote loss **−$3.033**. Even using the reaction candle's actual ask high (4043.883) + one tick instead would have required **$5.004 at that fill**, just over budget; that alternative was **not** used to trade. Across the combined observer's **13 stopped sells**, **11** hit the ask stop without the bid ever reaching the buffered structural invalidation; five did not even reach the last reaction candle's high. This describes recorded paths, not proof that waiting longer would have been profitable. [All short-stop audits](examples/short_stops.csv) · [case trace](examples/nov11_case.json).

## Sensitivity: observed quote through the stop

Same decisions, spread and zero additional friction; stop at the first executable crossing quote rather than awarding an exact barrier fill. This is not a second spread charge. Targets remain at their limits.

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Worst window DD¹ |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 7 / 34 | 20.6% | 0.44 | −88.20 | −2.59 | 55.08 |
| B · Band retest / cash SL | 4 / 30 | 13.3% | 0.27 | −105.71 | −3.52 | 45.50 |
| C · Same retest / wick SL | 4 / 30 | 13.3% | 0.31 | −90.92 | −3.03 | 42.83 |
| Swept pocket / wick + $10 TP | 1 / 3 | 33.3% | 1.47 | 3.20 | 1.07 | 3.78 |
| Combined / wick + managed | 4 / 33 | 12.1% | 0.30 | −93.00 | −2.82 | 39.32 |

The combined observer's worst crossing loss was **−$6.498** and **14/33** trades lost more than $5, despite modeled initial risks ≤$5 throughout. Broker fills are not guaranteed at either assumption. This is why the limit is a **pre-entry** risk rule, not a guarantee about realized drawdown.

## Per-window results, expanded run · ideal barriers

Each window starts flat with fresh risk guards. Prior market data can warm structure. Identical date windows are not counted twice in pooled results. All periods are 2026 UTC, dates inclusive below. The August replication is earlier history, not out-of-sample future trading. The final period covers only the available trading sessions in four calendar days.

### Aug 6–Sep 2 UTC

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Realized DD |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 0 / 2 | 0.0% | 0.00 | −9.99 | −5.00 | 9.99 |
| B · Band retest / cash SL | 1 / 4 | 25.0% | 0.67 | −4.98 | −1.24 | 9.99 |
| C · Same retest / wick SL | 1 / 4 | 25.0% | 0.79 | −2.60 | −0.65 | 8.42 |
| D · C + candle filter | 0 / 0 | — | — | 0.00 | — | 0.00 |
| E · D + next-zone TP | 0 / 0 | — | — | 0.00 | — | 0.00 |
| F · E + monitored exits | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Band retest / wick + managed | 0 / 4 | 0.0% | 0.00 | −12.34 | −3.08 | 12.34 |
| Swept pocket / wick + $10 TP | 1 / 2 | 50.0% | 3.00 | 6.67 | 3.33 | 3.34 |
| Swept pocket / wick + managed | 1 / 2 | 50.0% | 1.83 | 2.78 | 1.39 | 3.34 |
| Swept pocket / pattern + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Combined / wick + managed | 1 / 6 | 16.7% | 0.39 | −9.56 | −1.59 | 9.56 |

### Sep 3–30 UTC

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Realized DD |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 3 / 7 | 42.9% | 1.50 | 10.04 | 1.43 | 9.99 |
| B · Band retest / cash SL | 0 / 8 | 0.0% | 0.00 | −39.96 | −5.00 | 39.96 |
| C · Same retest / wick SL | 0 / 8 | 0.0% | 0.00 | −32.14 | −4.02 | 32.14 |
| D · C + candle filter | 0 / 2 | 0.0% | 0.00 | −8.95 | −4.47 | 8.95 |
| E · D + next-zone TP | 0 / 2 | 0.0% | 0.00 | −8.95 | −4.47 | 8.95 |
| F · E + monitored exits | 0 / 2 | 0.0% | 0.00 | −7.55 | −3.78 | 7.55 |
| Band retest / wick + managed | 0 / 8 | 0.0% | 0.00 | −30.74 | −3.84 | 30.74 |
| Swept pocket / wick + $10 TP | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Swept pocket / wick + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Swept pocket / pattern + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Combined / wick + managed | 0 / 8 | 0.0% | 0.00 | −30.74 | −3.84 | 30.74 |

### Oct 1–28 UTC

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Realized DD |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 2 / 13 | 15.4% | 0.36 | −34.93 | −2.69 | 44.96 |
| B · Band retest / cash SL | 2 / 11 | 18.2% | 0.45 | −24.94 | −2.27 | 29.97 |
| C · Same retest / wick SL | 2 / 11 | 18.2% | 0.57 | −15.15 | −1.38 | 22.07 |
| D · C + candle filter | 0 / 5 | 0.0% | 0.00 | −19.43 | −3.89 | 19.43 |
| E · D + next-zone TP | 0 / 5 | 0.0% | 0.00 | −19.43 | −3.89 | 19.43 |
| F · E + monitored exits | 0 / 5 | 0.0% | 0.00 | −19.43 | −3.89 | 19.43 |
| Band retest / wick + managed | 2 / 11 | 18.2% | 0.69 | −11.06 | −1.01 | 22.07 |
| Swept pocket / wick + $10 TP | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Swept pocket / wick + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Swept pocket / pattern + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Combined / wick + managed | 2 / 11 | 18.2% | 0.69 | −11.06 | −1.01 | 22.07 |

### Oct 29–Nov 11 UTC

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Realized DD |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 1 / 9 | 11.1% | 0.25 | −29.95 | −3.33 | 34.97 |
| B · Band retest / cash SL | 1 / 6 | 16.7% | 0.40 | −14.97 | −2.49 | 24.98 |
| C · Same retest / wick SL | 1 / 6 | 16.7% | 0.47 | −11.17 | −1.86 | 21.18 |
| D · C + candle filter | 0 / 1 | 0.0% | 0.00 | −4.19 | −4.19 | 4.19 |
| E · D + next-zone TP | 0 / 1 | 0.0% | 0.00 | −4.19 | −4.19 | 4.19 |
| F · E + monitored exits | 0 / 1 | 0.0% | 0.00 | −4.19 | −4.19 | 4.19 |
| Band retest / wick + managed | 1 / 6 | 16.7% | 0.50 | −10.59 | −1.77 | 21.18 |
| Swept pocket / wick + $10 TP | 0 / 1 | 0.0% | 0.00 | −2.84 | −2.84 | 2.84 |
| Swept pocket / wick + managed | 0 / 1 | 0.0% | 0.00 | −2.84 | −2.84 | 2.84 |
| Swept pocket / pattern + managed | 0 / 1 | 0.0% | 0.00 | −2.84 | −2.84 | 2.84 |
| Combined / wick + managed | 1 / 7 | 14.3% | 0.44 | −13.43 | −1.92 | 21.18 |

### Nov 12–15 UTC

| Policy | Wins / trades | Win rate | PF | Net USD | USD / trade | Realized DD |
|:---|---:|---:|---:|---:|---:|---:|
| A · Immediate / cash SL | 1 / 3 | 33.3% | 1.00 | 0.02 | 0.01 | 9.99 |
| B · Band retest / cash SL | 0 / 1 | 0.0% | 0.00 | −5.00 | −5.00 | 5.00 |
| C · Same retest / wick SL | 0 / 1 | 0.0% | 0.00 | −4.68 | −4.68 | 4.68 |
| D · C + candle filter | 0 / 0 | — | — | 0.00 | — | 0.00 |
| E · D + next-zone TP | 0 / 0 | — | — | 0.00 | — | 0.00 |
| F · E + monitored exits | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Band retest / wick + managed | 0 / 1 | 0.0% | 0.00 | −4.68 | −4.68 | 4.68 |
| Swept pocket / wick + $10 TP | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Swept pocket / wick + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Swept pocket / pattern + managed | 0 / 0 | — | — | 0.00 | — | 0.00 |
| Combined / wick + managed | 0 / 1 | 0.0% | 0.00 | −4.68 | −4.68 | 4.68 |

## Reproduce or replace the feed

```sh
python -m pip install -e ".[test]"
python research/candle_retest/reproduce.py --download --out reports/candle_replay_public
python research/candle_retest/quote_audit.py --evidence reports/candle_replay_public --out reports/candle_replay_public/audit
python research/candle_retest/verify.py
python -m pytest -q
```

The driver hashes every quote file, records exact windows, policies, config and research-code digests, and writes a separate CSV per policy/window. `verify.py` checks the **committed** 104 policy/window ledgers, risk arithmetic, immediate cohorts and both original Oct/Nov control ledgers, without downloading data. These 104 ledgers are correlated comparisons using overlapping inputs across phases, not 104 independent samples. See [evidence](evidence), [protocol](PROTOCOL.md), and [read-only broker export + exact replay commands](../../docs/CANDLE_RETEST.md). Next step: replay the user's actual `XAUUSDm` UTC bid/ask ticks on the same dates and keep subsequent demo shadow windows separate. No credential, live order or connected broker validation was used for this study.
