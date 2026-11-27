"""Producer → account JSONL → existing SQLite → actual CSV exporter, on fabricated MT5 IO."""

import csv
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from hull_cross_tape import run_tape
from hull_mt import ROOT, T, sig

from bot.hull.terminal import sync_deals

NAME = "XAUUSDm"


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize(("tf", "side"), [(15, 1), (30, -1), (45, 1)])
def test_upgrade_appends_separate_logs_and_reopens_same_journal(
    tmp_path, monkeypatch, tf, side
):
    # The journal is deliberately usable standalone without installing the bot.
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from journal import Desktop, Reporter
    from ledger import MAGIC, db_open, get, identity, iso, plain, put

    old = T - 3 * 3600
    live_ms = (old + 2) * 1000 + 140
    history = [
        deal(701, 901, old + 3, 0, 0, 0.1, 100, comment=f"i15_{live_ms}_1"),
        deal(702, 901, old + 100, 1, 1, 0.1, 101, profit=2.5, magic=0),
        deal(703, 902, old - 1800, 1, 0, 0.1, 101, comment=f"h30_{old - 1800}_1"),
        deal(704, 902, old - 1710, 0, 1, 0.1, 103, profit=-2, magic=0),
    ]
    broker = FakeMT(history.copy(), T - 30)
    broker.login, broker.host = 1, "t"
    key = identity(broker, MAGIC)["key"]
    audit = tmp_path / "reports/hull_runtime" / key
    audit.mkdir(parents=True)
    armed = audit / "demo.jsonl"
    shadow = audit / "shadow.jsonl"
    armed.write_text(
        json.dumps(
            {
                "event": "live_hull_cross",
                "symbol": NAME,
                "entry_side": 1,
                "tf": 15,
                "close_s": old + 900,
                "trigger_msc": live_ms,
                "trigger": {"ms": live_ms, "bid": 99.9, "ask": 100},
                "entry_setup": "trend_hull_cross",
            }
        )
        + "\n"
    )
    old_armed = armed.read_bytes()
    shadow.write_text('{"event":"started","summary":{"marker":"previous observer"}}\n')
    old_shadow = shadow.read_bytes()
    folder = tmp_path / "old-journal"
    folder.mkdir()
    db = folder / "journal.sqlite3"

    def exported(kind="tickets"):
        with (folder / (kind + ".csv")).open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    with closing(db_open(db)) as c:
        Reporter(c, folder, Desktop()).poll(broker, broker.at, repo=tmp_path)
        historical = exported()
        assert len(historical) == 2 and not any(
            r["initial_stop_policy"] for r in historical
        )
        with (
            c
        ):  # emulate the pre-update checkpoint/index, without resetting the deal cursor
            c.execute("DROP TABLE cross_receipts")
            put(c, "log_cursor", get(c, "log_cursor") | {"format": 3})
    observer = run_tape(tmp_path, monkeypatch, tf, side, armed=False)[0]
    assert (
        not observer.requests
        and shadow.read_bytes().startswith(old_shadow)
        and armed.read_bytes() == old_armed
    )
    shadow_events = records(shadow)
    assert any(
        r.get("outcome") == "shadow_plan_only" for r in shadow_events
    ) and not any(r["event"] == "demo_result" for r in shadow_events)
    assert Path(shadow_events[1]["summary"]["audit_log"]) == shadow
    before = shadow.read_bytes()
    mt = run_tape(tmp_path, monkeypatch, tf, side, history=history)[0]
    assert (
        len(mt.positions) == 4
        and shadow.read_bytes() == before
        and armed.read_bytes().startswith(old_armed)
    )
    logs = records(armed)
    start = next(r["summary"] for r in logs if r["event"] == "started")
    assert (start["account_key"], start["journal_magic"], Path(start["audit_log"])) == (
        key,
        MAGIC,
        armed,
    )
    assert any(r["event"] == "broker_deal" and r["ticket"] == 701 for r in logs)
    assert any(
        r.get("entry_timing") == "live_exit_only" and r["entry_side"] == 0 for r in logs
    )
    receipts = {r["deal"]: r for r in logs if r["event"] == "demo_result"}
    assert len(receipts) == 4 and all(
        r["tf"] == tf and r["stop_tick_size"] == 0.025 and r["cross_atr"] > 0
        for r in receipts.values()
    )
    assert [r["outcome"] for r in logs if r["event"] == "label_decision"] == [
        "demo_batch_sent"
    ]
    for i, p in enumerate(mt.positions):
        # Independent fake broker records. No usable tag on the broker entry; only
        # the exact receipt deal ID can recover the lane. Manual magic-zero closing deal.
        broker.ds += [
            deal(
                p.ticket,
                p.identifier,
                T,
                p.type,
                0,
                p.volume,
                p.price_open,
                comment="broker rewrote comment",
                commission=-0.25,
                ms=p.time_msc % 1000,
            ),
            deal(
                p.ticket + 100,
                p.identifier,
                T + 10 + i,
                1 - p.type,
                1,
                p.volume,
                p.tp,
                profit=3,
                fee=-0.1,
                commission=-0.15,
                reason=5,
                magic=0,
            ),
        ]
    with closing(db_open(db)) as c:
        reporter = Reporter(c, folder, Desktop())
        broker.at = T + 2
        reporter.poll(broker, broker.at, repo=tmp_path)
        assert (
            exported() == historical and len(exported("setups")) == 2
        )  # no plan, projection, receipt or open leg is profit
        broker.at = T + 10
        reporter.poll(broker, broker.at, repo=tmp_path)
        assert len(exported()) == 3 and len(exported("setups")) == 2
        broker.at = T + 14
        status = reporter.poll(broker, broker.at, repo=tmp_path)
        assert status["bad_log_lines"] == 0 and status["has_log"]
        tickets = exported()
        setups = exported("setups")
        assert tickets[:2] == historical and len(tickets) == 6 and len(setups) == 3
        for row in tickets[2:]:
            receipt = receipts[int(row["entry_deals"])]
            assert (
                row["setup_type"] == "trend_hull_cross"
                and row["entry_timing"] == "confirmed_close"
            )
            assert row["timeframe"] == f"M{tf}" and row["signal_trigger_utc"] == iso(
                T * 1000
            )
            assert row["signal_bar_open_utc"] == iso((T - tf * 60) * 1000)
            assert row["trigger_bid"] == row["trigger_ask"] == "" and row[
                "send_quote_utc"
            ] == iso(receipt["send_tick_time_msc"])
            assert row["send_bid"] == plain(receipt["send_bid"]) and row[
                "send_ask"
            ] == plain(receipt["send_ask"])
            for k in (
                "configured_target_usd",
                "initial_goal_usd",
                "entry_spread_usd",
                "planned_tp_profit_usd",
            ):
                assert row[k] == plain(receipt[k])
            assert row['target_policy'] == receipt['target_policy']
            assert row["initial_sl"] == plain(receipt["request"]["sl"])
            assert row["frozen_cross_sl"] == plain(receipt["frozen_cross_sl"])
            assert (row["initial_sl"] != row["frozen_cross_sl"]) == (side == -1)
            assert (
                row["cross_candle_wick"] == plain(receipt["cross_candle_wick"])
                and row["net_profit"] == "2.50"
            )
            assert (
                row["mss_skip_exception"] == "failed_counter_mss_return"
                and row["initial_stop_policy"] == "confirmed_cross_candle_wick_v1"
            )
        assert (
            setups[-1]["ticket_count"] == "4"
            and setups[-1]["net_profit"] == "10.00"
            and setups[-1]["send_quote_utc"] == ""
            and setups[-1]["frozen_cross_sl"] == tickets[-1]["frozen_cross_sl"]
        )
        assert get(c, "log_cursor")["off"] == armed.stat().st_size
        saved = [(folder / f).read_bytes() for f in ("tickets.csv", "setups.csv")]
    with closing(db_open(db)) as c:
        Reporter(c, folder, Desktop()).poll(
            broker, broker.at + 1, repo=tmp_path, reconcile=True
        )
        assert saved == [
            (folder / f).read_bytes() for f in ("tickets.csv", "setups.csv")
        ]


def test_runtime_deal_log_includes_only_verified_manual_exit_and_deduplicates(
    hull_desk,
):
    mt, d, log = hull_desk("medium")
    assert d.batch(NAME, sig()) == "demo_batch_sent"
    p = mt.positions[0]
    rec = {
        "ticket": 51,
        "symbol": NAME,
        "magic": 0,
        "position_id": p.identifier,
        "order": 94,
        "entry": 1,
        "type": 1,
        "time": T + 1,
        "time_msc": (T + 1) * 1000,
        "reason": 0,
        "price": 2000.0,
        "profit": 2.0,
        "commission": -0.3,
        "swap": -0.1,
        "fee": -0.02,
        "volume": p.volume,
    }
    mt.deals = [
        NS(**rec),
        NS(**(rec | {"ticket": 52, "position_id": 999})),
        NS(**(rec | {"ticket": 53, "symbol": "OTHER"})),
        NS(**(rec | {"ticket": 54, "entry": 0})),
        NS(**(rec | {"ticket": 55, "time_msc": 100})),
        NS(**(rec | {"ticket": 56, "type": 0})),
    ]
    for _ in range(2):
        assert sync_deals(d, T + 2)
    assert [r["ticket"] for k, r in log if k == "broker_deal"] == [51]
    r = next(r for k, r in log if k == "broker_deal")
    assert (r["attribution"], r["tf"], r["position_id"], r["fee"], r["magic"]) == (
        "reconciled_position_exit",
        1,
        p.identifier,
        -0.02,
        0,
    )
