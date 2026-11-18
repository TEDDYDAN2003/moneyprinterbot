"""Build the study write-up and a standalone visual review from checked-in evidence."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
W = {
    "untouched_aug": "Aug 6–Sep 2",
    "replication": "Sep 3–30",
    "early": "Oct 1–28",
    "later": "Oct 29–Nov 11",
    "forward": "Nov 12–15",
}
N = {
    "baseline": "A · Immediate / cash SL",
    "return_cash": "B · Band retest / cash SL",
    "return_wick": "C · Same retest / wick SL",
    "pattern_wick": "D · C + candle filter",
    "pattern_zone": "E · D + next-zone TP",
    "pattern_managed": "F · E + monitored exits",
    "return_managed": "Band retest / wick + managed",
    "sweep_wick": "Swept pocket / wick + $10 TP",
    "sweep_managed": "Swept pocket / wick + managed",
    "sweep_pattern_managed": "Swept pocket / pattern + managed",
    "reaction_managed": "Combined / wick + managed",
}


def load(p):
    return json.loads(p.read_text())


def rows(p):
    with p.open() as f:
        return list(csv.DictReader(f))


def num(x):
    if x is None:
        return "—"
    q = Decimal(str(round(x, 8))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f}".replace("-", "−")


def pct(w, n):
    return f"{100 * w / n:.1f}%" if n else "—"


def mm(k, w, phase):
    return load(HERE / "evidence" / phase / w / "summary.json")["arms"][k]["metrics"]


def pooled(k, phase):
    base = HERE / "evidence" / phase
    data = [r for w in W if (base / w).exists() for r in rows(base / w / (k + ".csv"))]
    values = [float(r["net_usd"]) for r in data]
    n = len(values)
    wins = sum(x > 0 for x in values)
    gp = sum(x for x in values if x > 0)
    gl = -sum(x for x in values if x < 0)
    return [
        N[k],
        f"{wins} / {n}",
        pct(wins, n),
        num(gp / gl if gl else None),
        num(sum(values)),
        num(sum(values) / n if n else None),
        num(max(mm(k, w, phase)["max_drawdown"] for w in W if (base / w).exists())),
    ]


HEAD = [
    "Policy",
    "Wins / trades",
    "Win rate",
    "PF",
    "Net USD",
    "USD / trade",
    "Worst window DD¹",
]


def table(headers, data):
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "|" + ":---|" + "---:|" * (len(headers) - 1),
            *["| " + " | ".join(map(str, r)) + " |" for r in data],
        ]
    )


def htable(headers, data):
    td = lambda x: escape(str(x))
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join('<th scope="col">' + td(v) + "</th>" for v in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>"
            + "".join(
                '<th scope="row">' + td(v) + "</th>"
                if i == 0
                else "<td>" + td(v) + "</td>"
                for i, v in enumerate(row)
            )
            + "</tr>"
            for row in data
        )
        + "</tbody></table></div>"
    )


def by_window(w, phase):
    doc = load(HERE / "evidence" / phase / w / "summary.json")
    result = []
    for k, a in doc["arms"].items():
        m = a["metrics"]
        n = m["trades"]
        win = m["wins"]
        result.append(
            [
                N[k],
                f"{win} / {n}",
                pct(win, n),
                num(m["profit_factor"]),
                num(m["net_profit"]),
                num(m["expectancy_usd"]),
                num(m["max_drawdown"]),
            ]
        )
    return result


def report():
    primary = [pooled(k, "primary") for k in list(N)[:6]]
    full = [pooled(k, "followup") for k in N]
    stress = [
        pooled(k, "sensitivity")
        for k in (
            "baseline",
            "return_cash",
            "return_wick",
            "sweep_wick",
            "reaction_managed",
        )
    ]
    section = "\n\n".join(
        "### "
        + W[w]
        + " UTC\n\n"
        + table(HEAD[:-1] + ["Realized DD"], by_window(w, "followup"))
        for w in W
    )
    text = (
        """# Gold: does a candle stop fix the entries?

**Decision, 18 November 2026: keep the scanner in shadow.** Waiting for a second quote made a real wick stop affordable on some trades. It reduced dollars lost versus a fixed stop on the *same retests* but did not improve their win count. The combined pattern-aware, actively managed observer lost in **each of five windows**. Do not widen the stop, increase size, or promote the three-trade sweep result to a proven strategy.

## What was actually compared

- **One XAUUSD leg**, 0.09 × 100 oz = 9 oz. **$10 target / $5 maximum modeled initial risk**, not a guaranteed realized stop. Bid/ask spread is retained once; no commission or added slippage. The 0.300 maximum entry spread and session settings were not loosened. An allowed 0.300 spread already costs $2.70, leaving only about 0.256 price units between the initial executable mark and a full $5 stop.
- [Rules and phase chronology](PROTOCOL.md). Primary: four separately reset $10,000 simulated accounts per policy (Sep 3–Nov 15). The Oct windows were previously examined; Sep was a fresh historical replication; Nov 12–15 is a short later-dated slice, **not live forward trading**. After seeing no affordable sweep entries inside the original S/R band, a separately specified experiment also allowed returns into the previously swept pocket, and added **Aug 6–Sep 2**, unused before that choice. No thresholds were selected from that August window.
- [Public source](https://github.com/Touhid-HZ/xauusd-tick-data-2026/tree/3ef0f9a5940898a3f690140481500730a3fe178a) and [15 file hashes](inputs.json). **22,673,430 ordered bid/ask ticks, 99,148 M1 bars** in the expanded comparison; **17,570,325 ticks, 71,548 bars** in the primary four. These are **not the user's XAUUSDm prices**. The symbol suffix in our config does not change where replay prices came from. Screenshots were not training labels. No other symbols were performance-tested.
- Use only completed M1/M5/M15 support/resistance and pullback reactions. Keep candle, previous-candle-at-level and finished source-frame tags. Freeze the level, extreme and objective before the watch. Cancel on expiry (180 s), invalidation/objective at any tick, opposite structure, gaps or market gates. Sample the first tick per two-second UTC bucket for discretionary entry and giveback. The initial bar-close signal must arrive on time. Fill at ask for a long, bid for a short. Never apply a pre-entry tick to a new position. The replay's hard stop and target inspect every ordered quote, even if P&L changes faster than its two-second observer.
- Ideal barriers unless marked *first crossing*. Next-zone targets can be an opposing level or the configured round-number objective and must offer at least $10. Active exits use contrary completed-candle evidence or sampled giveback; they never loosen an SL. See [operating rules](../../docs/CANDLE_RETEST.md) for exact definitions and the difference between a two-second sample and a broker terminal poll. No order-book inference beyond price, latency model, margin rejections, swap or broker acceptance in replay. The guard marks equity at minute boundaries; reported DD is realized, not worst open-equity DD.

## Primary ladder · four windows, Sept 3–Nov 15

"""
        + table(HEAD, primary)
        + """

¹ Maximum of the *separate* window realized drawdowns, in USD. Pooled net and PF are descriptive sums of dollar outcomes, **not** a continuous portfolio, average of period PFs, or 104 independent experiments. No-trade PF, win rate and expectancy are undefined. Cash controls rounded to about +$10.008 / −$4.995 on this tick grid. Exact metrics and ledgers including empty files: [primary](evidence/primary).

**Stop vs entry vs exit:** B and C filled at the same 26 prices/times: 3 wins, 23 losses in both. Actual wick stops reduced the net loss by **$21.726**, but C was still worse than the immediate control in aggregate. Within C, 18/26 trades ended in less than a second; median holding time was **0.102 s**. A second visit to a level is not necessarily confirmation of renewed demand or supply. The mandatory candle filter removed all three winners: zero wins among eight fills. On Oct 22 a real bullish body engulf at a respected pullback still lost: long 4022.045, SL 4021.491, −$4.986. F saved $1.395 on one September loss by exiting on an adverse candle; it produced no winners. This is evidence against making that pattern filter mandatory *here*, not evidence that engulfing candles never matter.

**Research-only wider stop:** freeze A's 32 entry times, directions, 0.09 lots and $10 targets. Ignoring the cap raised wins from **7/32 to 18/32**, but net went from **−$54.819 to −$66.681** (PF 0.561 → 0.730). **32/32** wick risks breached $5: median $19.638, max $66.717. These trades can overlap; they are not a funded account or a permitted configuration. With August included the figures are 18/34 and −$109.089 for the uncapped diagnostic versus 7/34 and −$64.809 for the immediate control. More winners did not pay for the larger losers.

## Follow-up · test the swept area itself

In the primary ladder no filled second-band entry was classified as a sweep/reclaim. For a confirmed sweep only, the follow-up allows a return between the swept-side edge and the already-known wick; no catch of a falling/rising price without the earlier reclaim. For other setups the combined observer uses the original band. The pattern is logged; the optional filtered arm requires a directional engulf/rejection or eligible predecessor with follow-through. Combined is a separately replayed account with one watch at a time, not a sum of arms.

"""
        + table(HEAD, full)
        + """

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

"""
        + table(HEAD, stress)
        + """

The combined observer's worst crossing loss was **−$6.498** and **14/33** trades lost more than $5, despite modeled initial risks ≤$5 throughout. Broker fills are not guaranteed at either assumption. This is why the limit is a **pre-entry** risk rule, not a guarantee about realized drawdown.

## Per-window results, expanded run · ideal barriers

Each window starts flat with fresh risk guards. Prior market data can warm structure. Identical date windows are not counted twice in pooled results. All periods are 2026 UTC, dates inclusive below. The August replication is earlier history, not out-of-sample future trading. The final period covers only the available trading sessions in four calendar days.

"""
        + section
        + """

## Reproduce or replace the feed

```sh
python -m pip install -e ".[test]"
python research/candle_retest/reproduce.py --download --out reports/candle_replay_public
python research/candle_retest/quote_audit.py --evidence reports/candle_replay_public --out reports/candle_replay_public/audit
python research/candle_retest/verify.py
python -m pytest -q
```

The driver hashes every quote file, records exact windows, policies, config and research-code digests, and writes a separate CSV per policy/window. `verify.py` checks the **committed** 104 policy/window ledgers, risk arithmetic, immediate cohorts and both original Oct/Nov control ledgers, without downloading data. These 104 ledgers are correlated comparisons using overlapping inputs across phases, not 104 independent samples. See [evidence](evidence), [protocol](PROTOCOL.md), and [read-only broker export + exact replay commands](../../docs/CANDLE_RETEST.md). Next step: replay the user's actual `XAUUSDm` UTC bid/ask ticks on the same dates and keep subsequent demo shadow windows separate. No credential, live order or connected broker validation was used for this study.
"""
    )
    (HERE / "REPORT.md").write_text(text, encoding="utf-8")
    return primary, full, stress


def svg_chart():
    case = load(HERE / "examples/nov11_case.json")
    bars = case["candles_t_o_h_l_c"]
    quotes = case["quotes_from_entry_ms_bid_ask"]

    def panel(values, lines, draw):
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.09
        lo -= pad
        hi += pad
        y = lambda p: 20 + (hi - p) * 184 / (hi - lo)
        fig = [
            '<svg viewBox="0 0 680 240" role="img" aria-label="'
            + escape(lines[0])
            + '"><title>'
            + escape(lines[0])
            + "</title>"
        ]
        for j in range(5):
            v = lo + j * (hi - lo) / 4
            yy = y(v)
            fig.append(
                f'<path d="M60,{yy:.1f} H655" stroke="#dce3de"/><text x="52" y="{yy + 4:.1f}" text-anchor="end">{v:.2f}</text>'
            )
        fig += draw(y)
        return "".join(fig) + "</svg>"

    def candles(y):
        f = []
        xs = lambda i: 78 + i * 40
        f.append(
            f'<rect x="60" width="595" y="{y(4043.0124):.2f}" height="{y(4042.6816) - y(4043.0124):.2f}" fill="#dce8e6"/>'
        )
        for i, (t, o, h, l, c) in enumerate(bars):
            x = xs(i)
            color = "#267869" if c >= o else "#a35040"
            color = "#603c04" if i == 10 else color
            if i == 10:
                f.append(
                    f'<rect x="{x - 16}" width="32" y="12" height="197" fill="#f5e6b9" opacity=".65"/>'
                )
            f.append(
                f'<path d="M{x},{y(h):.2f} V{y(l):.2f}" stroke="{color}" stroke-width="1.6"/><rect x="{x - 7}" y="{min(y(o), y(c)):.2f}" width="14" height="{max(1.5, abs(y(o) - y(c))):.2f}" fill="{color}"/>'
            )
            if i in (0, 5, 10, 14):
                f.append(
                    f'<text x="{x}" y="226" text-anchor="middle">{datetime.fromtimestamp(t, UTC):%H:%M}</text>'
                )
        return f

    def tape(y):
        xmax = quotes[-1][0]
        x = lambda t: 60 + 585 * t / xmax
        f = []
        for val, clr in ((4043.345, "#7c8270"), (4043.643, "#ad593b")):
            f.append(
                f'<path d="M60,{y(val):.2f} H646" stroke="{clr}" stroke-dasharray="4 4"/>'
            )
        for idx, col in [(1, "#1d6d66"), (2, "#a24f38")]:
            points = " ".join(f"{x(r[0]):.2f},{y(r[idx]):.2f}" for r in quotes)
            f.append(
                f'<polyline points="{points}" fill="none" stroke="{col}" stroke-width="1.6"/>'
            )
            f.append(
                f'<circle cx="{x(xmax)}" cy="{y(quotes[-1][idx]):.2f}" r="3.5" fill="{col}"/>'
            )
        for t in [0, 10000, 20000, xmax]:
            f.append(
                f'<text x="{x(t):.1f}" y="226" text-anchor="middle">{t / 1000:g} s</text>'
            )
        return f

    return panel(
        [v for b in bars for v in b[1:]],
        ["Fifteen one-minute bid candles, November 11"],
        candles,
    ), panel(
        [v for q in quotes for v in q[1:]],
        ["Bid and ask while this short was open"],
        tape,
    )


CSS = """*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:#182b29;background:#f4f3ec;font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}main{max-width:1140px;margin:auto;padding:24px clamp(13px,4vw,52px) 50px}header{display:flex;justify-content:space-between;gap:12px;align-items:center;border-bottom:1px solid #aab6aa;padding-bottom:15px;font-size:.77rem;font-weight:650;letter-spacing:.08em;text-transform:uppercase}.status{white-space:nowrap;color:#205f53;background:#dceae1;border:1px solid #9fbcb0;padding:.22em .65em;border-radius:3em}.eyebrow{color:#686842;font-size:.76rem;letter-spacing:.15em;font-weight:720;text-transform:uppercase;margin:0 0 9px}h1,h2,h3,p{margin:0 0 13px}h1,h2,.num{font:normal clamp(2.1rem,5.8vw,4.8rem)/1.03 Georgia,serif;letter-spacing:-.045em}h2{font-size:clamp(1.75rem,3.2vw,2.8rem)}h3{font-size:1.08rem}.hero{padding:40px 0 18px;max-width:930px}.hero p:not(.eyebrow){font-size:1.1rem;max-width:83ch}.kpis,.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}.card,section{background:#fffefa;border:1px solid #cdd4c8;border-radius:8px;padding:clamp(15px,2.5vw,25px)}section{margin-top:18px}.kpis .num{display:block;font-size:2.3rem;margin:5px 0 6px}.label,.note,figcaption{font-size:.85rem;color:#405853}.cols{grid-template-columns:1fr 1fr}.minor{font-size:.83rem;color:#53625b}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.87rem;font-variant-numeric:tabular-nums;white-space:nowrap;margin:8px 0 12px}td,th{padding:8px 9px;text-align:right;border-bottom:1px solid #dce1d8}th:first-child{text-align:left;font-weight:560}thead th{background:#edf1e9;color:#304a43}details{border-top:1px solid #d8dfd5;padding:10px 0}summary{cursor:pointer;font-weight:640}figure{padding:12px 8px;margin:0;background:#f8f9f3;border:1px solid #dce1d9;border-radius:6px;min-width:0}svg{display:block;width:100%;height:auto}svg text{font:11px system-ui;fill:#405953}code,pre{font-family:ui-monospace,Consolas,monospace;font-size:.88em}pre{color:#eef6ef;background:#203933;border-radius:5px;padding:15px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}.call{border-left:3px solid #ac8437;background:#faf4e4;padding:12px 16px;margin:15px 0}.flow{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.flow b{display:block}.neg{color:#954d3f}a{color:#24695e}footer{margin:18px 0;font-size:.78rem;color:#45605a}p:last-child{margin-bottom:0}@media(max-width:760px){.kpis,.flow{grid-template-columns:1fr}.cols{grid-template-columns:1fr}.hero{padding-top:25px}.kpis .card{padding:14px 18px}.kpis .num{display:inline-block;margin-right:12px}.card .minor{display:block}header{font-size:.63rem}.scroll{max-width:calc(100vw - 62px)}}@media print{body{background:white}main{max-width:none;padding:5px}.hero{padding-top:10px}section{break-inside:avoid;box-shadow:none}details{break-inside:avoid}.scroll{overflow:visible}}"""


def html(path, primary, full, stress):
    c, q = svg_chart()
    sel = [full[i] for i in (0, 1, 2, 3, 7, 8, 10)]
    window_tables = "".join(
        "<details><summary>"
        + w
        + " UTC</summary>"
        + htable(HEAD[:-1] + ["Realized DD"], by_window(k, "followup"))
        + "</details>"
        for k, w in W.items()
    )
    out = (
        """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Gold · The stop behind the wick</title><style>"""
        + CSS
        + """</style></head><body><main><header><span>Moneyprinterbot &nbsp; / &nbsp; Research note 02</span><span class="status">Shadow only · 18 Nov 2026</span></header>
<div class="hero"><p class="eyebrow">XAUUSD · market structure &amp; execution</p><h1>The stop behind the wick</h1><p>A second visit sometimes put the rejection candle inside a $5 risk budget. On identical retests, a wick stop cut the size of the losses. <strong>It did not turn a single additional trade into a winner.</strong></p></div>
<div class="kpis"><div class="card"><span class="label">01 / Expanded study</span><span class="num">22.67m</span><span class="minor">ordered bid/ask quotes · five date windows</span></div><div class="card"><span class="label">02 / Matched retest entries</span><span class="num">4 / 30</span><span class="minor">wins with a cash stop and with a wick stop</span></div><div class="card"><span class="label">03 / Combined managed rule</span><span class="num neg">−$69.47</span><span class="minor">4 / 33 wins · profit factor 0.37</span></div></div>
<section><p class="eyebrow">01 / Decision</p><h2>Use the level. Keep the risk limit.</h2><div class="flow"><p><b>Read the candle.</b> Only classify an entry at a known M1/M5/M15 reaction. Log engulfing, wick rejection, the previous candle at that level and the finished source timeframe.</p><p><b>Wait for the return.</b> Freeze the band or swept pocket for up to 180 seconds. Use the farther of the reaction wick and known invalidation. No affordable second visit means no entry.</p><p><b>Test the hold.</b> Keep broker-side protection in the design. In the managed rule, aim at the next zone and monitor adverse closes and profit giveback. Do not widen SL after entry.</p></div><p class="call"><b>Operating decision:</b> paper observation only. Keep 0.09 lot, a $10 reference target, and $5 maximum <em>modeled initial</em> loss. Spread is included once; commission and added slippage are zero. These are independent XAUUSD prices, not your broker’s XAUUSDm.</p></section>
<section><p class="eyebrow">02 / Comparable outcomes</p><h2>Separate the entry, the stop and the exit.</h2><p>Five non-overlapping windows, Aug 6–Nov 15. B and C entered at the same 30 times and prices. Requiring a directional candle removed all four winning retests. The three-trade swept-pocket result is not enough to promote.</p>"""
        + htable(HEAD, sel)
        + """<p class="note">PF = gross wins ÷ gross losses. ¹ Largest separate-window realized drawdown (USD), not a continuous portfolio. Each policy starts flat in each window. A dash means no defined statistic.</p><details><summary>Primary four-window comparison · all six frozen rules</summary><p>Sep 3–Nov 15. Only after this comparison produced zero affordable sweep entries inside the original band did we test the swept pocket separately and add unused August history.</p>"""
        + htable(HEAD, primary)
        + """</details><details><summary>All expanded policies and all five windows, including no fills</summary>"""
        + htable(HEAD, full)
        + window_tables
        + """</details></section>
<section><p class="eyebrow">03 / One trade, quote by quote</p><h2>The bid never broke the high. The ask hit the stop.</h2><p>Nov 11, 12:49 UTC: a bearish rejection after a doji at M1 resistance. The bot waited <b>80.011 seconds</b> for a return to the swept area. This is the one eligible swept-pocket trade in the later October–November window.</p><div class="cols"><figure>"""
        + c
        + """<figcaption><b>Bid candles, M1.</b> Shaded band: frozen resistance around 4042.847. Highlight: the 12:48 candle, known at 12:49. Its bid high was 4043.345.</figcaption></figure><figure>"""
        + q
        + """<figcaption><b>All 125 held quotes.</b> Teal = bid, rust = ask. Dashed lines: bid wick (4043.345) and attached ask stop (4043.643). Only the ask crossed its stop.</figcaption></figure></div>
<p class="call"><b>Entry 4043.328 bid / 4043.625 ask.</b> Risk to stop was $2.835, of which $2.673 was already the spread. After 27.358 s the quote was 4043.095 / 4043.665. Spread widened from 0.297 to 0.570. The chart bid was lower; the stop still executed in the model.</p><p>Using even the reaction candle’s observed ask high plus one tick would have required $5.004 at that entry. Across the combined rule’s 13 stopped shorts, 11 hit the ask stop without the held bid reaching the buffered structural invalidation. That does not mean staying in those trades would have made money.</p></section>
<section><p class="eyebrow">04 / Three checks against a misleading win rate</p><div class="flow"><p><b>Uncapped stop ≠ solution.</b> On the original 32 immediate entries, the same $10 targets and wider wick stops raised wins from 7 to 18, yet net worsened from −$54.82 to −$66.68. Median risk $19.64, max $66.72. Not an executable $5 rule.</p><p><b>A hold can give profit back.</b> The Aug 27 sweep short made $10.008 at the fixed target, versus $6.12 at the managed exit. Both direct sweep rules won just one of three. With August included, 18 of 33 combined trades ended in under one second.</p><p><b>A stop is not a fill guarantee.</b> The first observed quote through the stop, rather than the ideal level, put the combined rule at −$93.00. Fourteen trades lost more than $5; worst loss $6.498. No added friction was imposed.</p></div><details><summary>First executable stop quote · sensitivity results</summary>"""
        + htable(HEAD, stress)
        + """</details></section>
<section><p class="eyebrow">05 / Missing evidence</p><h2>Use the actual XAUUSDm feed next.</h2><p>In the later period the independent spread blocked 1,211 of 1,246 immediate candidates. The monitored combined rule armed 1,208 watches, but 692 hit the objective before an affordable entry and only seven filled. A broker’s bid/ask history can materially change both entry availability and stop behavior.</p><p><b>On the Windows MT5 host</b>, from the project directory, export Oct 28 as warm-up plus Oct 29–Nov 11. This is read-only and contains no account credentials.</p><pre>python -m bot.cli export-ticks --symbol XAUUSD --start 2026-10-28 --end 2026-11-12 --out data/XAUUSDm_ticks.csv.gz</pre><p>Share the compressed file and its generated <code>.manifest.json</code>. In the bundle: <code>docs/CANDLE_RETEST.md</code> for setup, <code>research/candle_retest/REPORT.md</code> for the study, <code>evidence/</code> for individual ledgers and <code>examples/</code> for this quote audit.</p></section><footer>Source: independent public XAUUSD quote mirror, revision 3ef0f9a · 2026 UTC · illustrative replay, no broker orders · paper by default</footer></main></body></html>"""
    )
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(out, encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--html",
        type=Path,
        default=Path("reports/candle_replay_public/Gold-candle-retest-review.html"),
    )
    args = ap.parse_args()
    html(args.html, *report())
