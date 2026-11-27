"""Acceptance contract for the shipped Hull desk, using in-process MT5 fixtures only."""

import json

import pytest
from hull_mt import ROOT, T
from test_hull_intrabar_market import NAME, OFF, ON, Tape

from bot.broker import DemoSafetyError
from bot.cli import main
from bot.config.loader import load_yaml
from bot.hull.config import HullConfig

# Independent operator contract: don't derive the expectations from production constants.
WANTED = {5: 4, 10: 8, 15: 12}


def test_shipped_and_omitted_map_agree_without_a_private_config():
    cfg = load_yaml(ROOT / "config.yaml")
    assert cfg["run"]["mode"] == "paper"
    raw = cfg["hull"].copy()
    assert raw.pop("pairs", None) == WANTED
    for h in [HullConfig.model_validate(raw), HullConfig.model_validate(cfg["hull"])]:
        assert h.pairs == WANTED and h.lanes() == (5, 10, 15)
        assert (
            h.effective_timing == "intrabar_cross"
            and h.entry_route == "market_next_quote"
        )


def test_real_cli_preflight_all_three_from_stock_config(tmp_path, monkeypatch, capsys):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF)], parents=tuple(WANTED), child=None)
    assert (
        main(
            [
                "hull-preflight",
                "--config",
                str(ROOT / "config.yaml"),
                "--paired-confirm",
            ]
        )
        == 0
    )
    s = json.loads(capsys.readouterr().out)
    assert (
        s["mode"] == "READ ONLY"
        and s["entry_timing"] == "intrabar_cross"
        and s["pair_history_ready"]
    )
    assert s["parent_to_trigger"] == {"M5": "M4", "M10": "M8", "M15": "M12"}
    checks = s["history_check"][NAME]
    assert set(checks) == {"M4", "M5", "M8", "M10", "M12", "M15"}
    for parent, child in WANTED.items():
        assert (
            checks[f"M{parent}"]["parent"] and not checks[f"M{parent}"]["trigger_for"]
        )
        assert checks[f"M{child}"]["trigger_for"] == [f"M{parent}"]
    assert checks["M8"]["bar_source"] == "2 x M4, broker D1 anchor"
    assert not tape.mt.requests and not (tmp_path / "reports").exists()


@pytest.mark.parametrize("parent,child", WANTED.items())
@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("last,holds", [(ON, True), (OFF, False)])
def test_fresh_forming_cross_and_deferred_stop_on_exact_origin(
    tmp_path, monkeypatch, parent, child, side, last, holds
):
    end = child * 60000
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (200, ON), (end + 100, ON), (end + 200, ON)],
        parents=(parent,),
        side=side,
        child=None,
    )
    tape.hidden = [(end - 1, last)]
    tape.run(count=2)
    rows = tape.events("demo_result")
    assert len(rows) == 4 and len(tape.mt.positions) == 4
    starts = {p.ticket: (p.sl, p.tp) for p in tape.mt.positions}
    for r in rows:
        assert (r["parent_tf"], r["trigger_tf"], r["entry_side"]) == (
            parent,
            child,
            side,
        )
        assert (
            r["trigger_msc"]
            == T * 1000 + 200
            < r["origin_bar_end_s"] * 1000
            == T * 1000 + end
        )
        assert r["request"]["comment"].startswith(f"q{parent}x{child}_")
        assert (
            r["cross_region_action"] == "awaiting_origin_close"
            and min(r["request"]["sl"], r["request"]["tp"]) > 0
        )
    tape.run(start=2)
    assert len(tape.events("demo_result")) == 4
    (close,) = tape.events("origin_cross_check")
    assert close["phase"] == "final_close" and close["observed_msc"] == T * 1000 + end
    if holds:
        assert len(tape.mt.positions) == 4
        for p in tape.mt.positions:
            stop, tp = starts[p.ticket]
            assert side * (p.sl - stop) > 0 and p.tp == tp
    else:
        assert not tape.mt.positions and len(tape.events("exit_request")) == 4


@pytest.mark.parametrize("missing", ["D1", "M4"])
def test_m8_cannot_fall_back_on_another_timeframe(tmp_path, monkeypatch, missing):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON)], parents=(10,), child=None
    )
    if missing == "D1":
        tape.mt.copy_rates_range = lambda *_: []
    else:
        tape.bad = lambda tf, pos: tf == 4
    with pytest.raises(DemoSafetyError, match="history unavailable or stale"):
        tape.run(armed=False, preflight=True)
    tape.run(armed=False)
    assert not tape.mt.requests and not tape.events("intrabar_pair_cross", shadow=True)


@pytest.mark.parametrize(
    "timing,wrong_map,permitted",
    [
        ("intrabar_cross", False, True),
        ("by_route", False, True),
        ("confirmed_cross_next_bar", False, False),
        ("intrabar_cross", True, False),
    ],
)
def test_release_preflight_refuses_wrong_clock_or_pair_and_never_sends(
    tmp_path, monkeypatch, capsys, timing, wrong_map, permitted
):
    import yaml

    from bot.hull.terminal import source_build

    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    import preflight_release as release

    tape = Tape(tmp_path, monkeypatch, [(100, OFF)], parents=tuple(WANTED), child=None)
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"]["entry_timing"] = timing
    if wrong_map:
        cfg["hull"]["pairs"][10] = 5
    path = tmp_path / "config.demo.local.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        release, "check_git", lambda *a: None
    )  # Git cleanliness tested independently below
    calls = []
    tape.mt.initialize = lambda: not calls.append(1)
    if permitted:
        release.run_preflight(source_build()["checkout_head_at_start"], path)
        text = capsys.readouterr().out
        assert (
            "Read-only preflight completed" in text
            and '"M10": "M8"' in text
            and '"mode": "READ ONLY"' in text
        )
    else:
        with pytest.raises(release.ReleaseCheckError, match="Config does not request"):
            release.run_preflight(source_build()["checkout_head_at_start"], path)
    assert calls == ([1] if permitted else []) and not tape.mt.requests


def test_release_git_check_is_read_only_and_refuses_ambiguous_checkout(
    tmp_path, monkeypatch
):
    import subprocess

    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    from preflight_release import ReleaseCheckError, check_git

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], text=True
        ).strip()

    git("init", "-q")
    src = tmp_path / "demo.py"
    src.write_text("original\n")
    git("add", "demo.py")
    git(
        "-c",
        "user.name=Testing",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    sha = git("rev-parse", "HEAD")
    check_git(tmp_path, sha)
    for bad in [sha[:7], "0" * 40]:
        with pytest.raises(ReleaseCheckError):
            check_git(tmp_path, bad)
    src.write_text("uncommitted\n")
    with pytest.raises(ReleaseCheckError, match="not the clean release"):
        check_git(tmp_path, sha)
    assert src.read_text() == "uncommitted\n"


@pytest.mark.parametrize("parent,child", WANTED.items())
@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("clock", ["by_route", "confirmed_cross_next_bar", "retest"])
def test_independently_closed_cross_prices_only_successor_or_exact_retest(
    tmp_path, monkeypatch, parent, child, side, clock
):
    from test_hull_direct_retest import PendingMT

    end = child * 60000
    pending = clock == "retest"
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(end - 350, OFF), (end - 150, OFF), (end + 100, ON), (end + 200, ON)],
        parents=(parent,),
        side=side,
        child=None,
        broker=PendingMT() if pending else None,
    )
    tape.hidden = [
        (end - 1, ON)
    ]  # independently closed; no intrabar admission was seen
    if pending:
        tape.cfg["hull"]["entry_route"] = "direct_retest_limit"
        base_tick = tape.tick

        def q(name):
            tape.mt.now = tape.clock()
            return base_tick(name)

        tape.mt.symbol_info_tick = q
    else:
        tape.cfg["hull"]["entry_timing"] = clock
    tape.run()
    (r,) = tape.events("closed_label")
    assert (r["parent_tf"], r["trigger_tf"], r["entry_side"], r["close_s"]) == (
        parent,
        child,
        side,
        T + child * 60,
    )
    assert r["hull_cross_price"] is not None
    opens = [
        q
        for q in tape.mt.requests
        if q["action"] == (5 if pending else 1) and "position" not in q
    ]
    assert len(opens) == 4
    for q in opens:
        assert (
            q["comment"].startswith(
                f"{'r' if pending else 'p'}{parent}x{child}_{T + child * 60}_"
            )
            and min(q["sl"], q["tp"]) > 0
        )
        if pending:
            assert q["expiration"] == T + 2 * child * 60


@pytest.mark.parametrize("parent,child", WANTED.items())
def test_journal_uses_actual_new_pair_and_fill_target_not_older_mapping(
    tmp_path, monkeypatch, parent, child
):
    from contextlib import closing

    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from ledger import db_open, make_views, sync

    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (200, ON), (300, OFF)],
        parents=(parent,),
        child=None,
    )
    tape.cfg["hull"]["symbols"]["XAUUSD"]["target_usd"] = 17.25
    tape.run()
    receipts = tape.events("demo_result")
    assert len(receipts) == 4 and not tape.mt.positions
    broker = FakeMT([], T + child * 60)
    broker.login, broker.host = 1, "t"
    for r in receipts:
        did = r["deal"]
        di = deal(did, did, T, 0, 0, 0.1, r["price"], ms=205, comment="rewritten")
        di.order = r["opening_order"]
        broker.ds.extend(
            [
                di,
                deal(did + 100, did, T, 1, 1, 0.1, r["price"] - 0.3, ms=310, profit=-3),
            ]
        )
    with closing(db_open(":memory:")) as db:
        sync(db, broker, broker.at, repo=tmp_path)
        data = make_views(db)[0]
        assert len(data["tickets"]) == 4 and len(data["setups"]) == 1
        for row in data["tickets"] + data["setups"]:
            assert (
                row["timeframe"],
                row["trigger_timeframe"],
                row["entry_timing"],
            ) == (f"M{parent}", f"M{child}", "intrabar_pair_cross")
            assert float(row["configured_target_usd"]) == 17.25
            assert float(row["initial_goal_usd"]) == pytest.approx(
                17.25 + float(row["entry_spread_usd"])
            )


def test_closed_desk_rejects_historical_trigger_under_new_map_before_consuming(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from test_hull_direct_retest import make

    mt, desk, r, _, _ = make(tmp_path, monkeypatch)
    old = replace(
        r,
        trigger_tf=3,
        anchor_s=r.close_s - 180,
        cross_bar=replace(r.cross_bar, t=r.close_s - 180),
    )
    assert old.paired and desk.batch(NAME, old) == "pair_mapping_mismatch"
    assert not mt.requests and not desk.state.get("pair_consumed")
    assert desk.batch(NAME, r) == "retest_batch_placed" and len(mt.orders) == 10


def test_m8_never_projects_a_stale_or_incomplete_second_half(tmp_path, monkeypatch):
    from bot.hull.curves import LiveTick
    from bot.hull.feed import Stream

    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (240100, OFF), (240200, ON)],
        parents=(10,),
        child=None,
    )
    h = HullConfig.model_validate(tape.cfg["hull"])
    lane = Stream(8, h.indicator, 0.01, tick_size=0.025)
    lane.refresh(tape.mt, NAME, tape.clock())
    tape.i = 2
    q = LiveTick.read(tape.tick(NAME))
    current = lane.current_bar(tape.mt, NAME, q)
    assert current and current.t == T and current.c == q.bid
    rates = tape.mt.copy_rates_from_pos
    for omit in (0, 1):

        def incomplete(name, tf, pos, n, *, omit=omit):
            rows = rates(name, tf, pos, n)
            return (
                [row for i, row in enumerate(rows) if i != omit]
                if tf == 4 and pos == 0
                else rows
            )

        tape.mt.copy_rates_from_pos = incomplete
        assert lane.current_bar(tape.mt, NAME, q) is None


@pytest.mark.parametrize(
    "clock,route",
    [
        ("intrabar_cross", "market_next_quote"),
        ("confirmed_cross_next_bar", "market_next_quote"),
        ("by_route", "direct_retest_limit"),
    ],
)
@pytest.mark.parametrize("mapping", [{5: 4, 10: 12, 15: 12}, {5: 4, 15: 12}])
def test_bad_pair_cannot_bypass_validation_on_old_or_new_clock(clock, route, mapping):
    h = load_yaml(ROOT / "config.yaml")["hull"]
    with pytest.raises(ValueError, match="parent/trigger mapping"):
        HullConfig.model_validate(
            h | {"entry_timing": clock, "entry_route": route, "pairs": mapping}
        )
