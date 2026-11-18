# Entry signal audit — directional-structure research

**Status: no new backtest was run.** The current production Python bot does not contain an integrated BUY/SELL signal generator. I did not substitute the Pine wickless entry condition.

## What the code currently does

| Component | Evidence | Finding |
|---|---|---|
| Directional signal | src/bot/backtest/engine.py | run_backtest accepts a caller-provided signal(timestamp, features) callback returning OrderRequest or None. The repository does not provide that callback implementation. |
| Signal fusion | src/bot/decision/signal_fusion.py | decide checks a supplied probability, spread, risk and expected value; it returns allow/reject. It does not choose BUY versus SELL or calculate the probability. |
| CLI model training | src/bot/cli.py, src/bot/labeling/triple_barrier.py | The CLI labels an upper barrier versus lower barrier using close/high/low and ATR, then trains LogisticBaseline. There is no directional side feature, prediction/inference command, or conversion from class probability to an order. Timeouts are NA in this labeler. |
| Model artifacts | models/registry/*.npz, src/bot/models/registry.py | Artifacts exist, but the registry module only writes metadata; no production runner loads them to generate orders. |
| Pine asset | public/assets/aistudio/Paste_November_16__2026_-_2_31PM_ba61040a | Contains a Wickless candle strategy and market-structure logic. It is a separate reference artifact; its Wickless condition is not wired into the Python bot and is not assumed to be the approved research entry signal. |

## Existing research-only directional baselines

The untracked research runner scripts/research_m1_m15_events.py has its own experimental entry rule. It evaluates candidate bars, duplicates each candidate for long and short, trains a LightGBM classifier on fixed-price TP-before-SL labels with a side feature, and saves p_long and p_short. Its simulator chooses the larger probability and trades when the maximum exceeds a development threshold. That is an experimental model rule, not a production signal generator. Its labels used fixed TP/SL distances; they do not answer TP-before-structural-stop.

The separate reports/study_20261117/study_report.json records another cost-aware model target and 30-minute sampling. These are research outputs, not a single wired baseline. The README describes a planned probability-driven decision engine, but the current Python package does not implement that plan end to end.

## Consequence for the requested experiment

The current request can cleanly test market structure only after the entry stream is fixed. The choices are materially different:

1. **Production baseline:** none exists in the Python code to replay; implementing one would add a new strategy, which this study should not silently do.
2. **Prior research baseline:** freeze the existing research runner's candidate times, p_long/p_short scores and threshold, then compare stop methods on those same entries. This preserves that study's entry decisions, but those probabilities were learned for fixed TP/SL outcomes and are not calibrated for structural stops.
3. **New structure-stop prediction target:** retrain the same side-probability architecture against TP-before-protected-stop labels. This preserves the model form and signal schedule but changes the target and therefore creates a new experimental signal; report it separately from the frozen baseline.

The Wickless condition must not be substituted without explicit direction to do so. Likewise BOS/MSS, timeframe alignment, momentum, pullback or consolidation must not become new entry filters in the stop comparison. They can be saved as context and analyzed after the unchanged baseline signals are generated.

## Hold point

No M1/M5/M15 structural-stop results are available from this new experiment yet. The only completed evidence remains the prior fixed-distance TP/SL research. The next run must identify which of the three baselines above is the control; until then, any claimed entry accuracy or profitability would be attributable to a newly chosen signal rather than the market-structure stop experiment.
