"""Independent UTC oracles for the shipped weekday clocks. No MT5 or price data."""

import json
import math
from datetime import datetime

import pytest
import yaml

from bot.config import entry_sessions as es

TOKYO, FRANK, LONDON, FULL, KZ, CASH = (
    "tokyo_first_hour",
    "frankfurt_first_hour",
    "london_before_new_york",
    "london_ny_overlap",
    "overlap_killzone",
    "ny_cash_first_hour",
)
RULES = {
    "JP225": (TOKYO,),
    "USDJPY": (TOKYO, FULL),
    "DE40": (FRANK,),
    "GBPJPY": (LONDON, FULL),
    **{s: (FULL,) for s in ("XAUUSD", "EURUSD", "AUDUSD", "GBPUSD")},
    **{s: (KZ, CASH) for s in ("NAS100USD", "US30", "US500")},
}


@pytest.fixture(scope="module")
def policy():
    return es.load_entry_sessions()


def midnight(date):
    return datetime.fromisoformat(date + "T00:00:00+00:00").timestamp()


@pytest.mark.parametrize(
    "day,berlin_open,london_open,ny_open,london_end,kz_start,ny_cash",
    [
        # Minute of UTC day, specified independently of the policy/zoneinfo implementation.
        ("2026-01-13", 480, 480, 780, 1020, 780, 870),  # both standard
        ("2026-07-14", 420, 420, 720, 960, 720, 810),  # both summer
        ("2026-03-17", 480, 480, 720, 1020, 780, 810),  # US switched, Europe not yet
        ("2026-10-27", 480, 480, 720, 1020, 780, 810),  # Europe back, US still on EDT
    ],
)
def test_utc_day_all_markets_and_both_sides_of_every_edge(
    policy, day, berlin_open, london_open, ny_open, london_end, kz_start, ny_cash
):
    windows = {
        TOKYO: (0, 60),
        FRANK: (berlin_open, berlin_open + 60),
        LONDON: (london_open, ny_open),
        FULL: (ny_open, london_end),
        KZ: (kz_start, kz_start + 180),
        CASH: (ny_cash, ny_cash + 60),
    }
    base = midnight(day)
    assert set(policy.markets) == RULES.keys()
    for sym, allowed in RULES.items():
        gate = policy.bind(sym)
        assert tuple(w.name for w in gate.windows) == allowed
        for minute in range(1440):
            expect = any(windows[w][0] <= minute < windows[w][1] for w in allowed)
            assert (gate.veto(base + minute * 60) == "") == expect, (sym, day, minute)
        for w in allowed:
            for edge in windows[w]:
                # Immediately before and exactly at each edge; the other windows
                # may still admit this market (e.g. GBPJPY handover or US cash).
                for stamp in (
                    base + 60 * edge,
                    math.nextafter(base + 60 * edge, -math.inf),
                ):
                    expected = [
                        k
                        for k in allowed
                        if base + windows[k][0] * 60
                        <= stamp
                        < base + windows[k][1] * 60
                    ]
                    assert gate.report(stamp)["active"] == expected


@pytest.mark.parametrize(
    "day", ["2026-07-11", "2026-07-12", "2026-10-25", "2026-11-01"]
)
def test_weekend_all_windows_closed_even_on_transition_sunday(policy, day):
    for minute in (0, 30, 420, 480, 600, 720, 780, 840, 900, 990):
        assert all(
            policy.bind(s).veto(midnight(day) + 60 * minute) == es.OUTSIDE
            for s in policy.markets
        )
    if (
        day == "2026-07-12"
    ):  # Tokyo's Monday morning already open on New York Sunday evening.
        assert not policy.bind("JP225m").veto(midnight(day) + 86400)


def test_exact_mapping_and_invalid_clocks(policy):
    for name in (
        "FAKEXAUUSDm",
        "AUUSD",
        "EURUSD.xx",
        "V75",
        "us30m",
        " US30m",
        "*",
        "",
    ):
        assert policy.bind(name).issue == es.UNMAPPED
    assert policy.bind("USDJPYm", "GBPJPY").issue == es.MISMATCH
    assert policy.bind("DE30m", "DE30") is policy.bind("DE40")
    at = midnight("2026-01-13") + 15 * 3600
    assert policy.bind("USTEC_x100m").report(at)["active"] == [KZ, CASH]
    for bad in (None, "15:00", True, math.nan, math.inf, -math.inf, 1e100):
        assert policy.bind("XAUUSD").veto(bad) == "invalid_entry_session_clock"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "empty",
        "version",
        "extra",
        "duplicate_yaml_key",
        "wrong_zone",
        "bad_time",
        "overnight",
        "empty_clock_set",
        "unspecified_clock",
        "duplicate_clock",
        "unspecified_window",
        "empty_window_set",
        "ambiguous_alias",
        "untrimmed",
        "wildcard",
    ],
)
def test_bad_policy_blocks_without_fallback(monkeypatch, tmp_path, mutation):
    f = tmp_path / "policy.yaml"
    data = yaml.safe_load(es.POLICY_FILE.read_text())
    clock = data["clocks"]["tokyo_open"]
    window = data["windows"][TOKYO]
    m = data["markets"]["JP225"]
    if mutation == "version":
        data["version"] = "unknown"
    if mutation == "extra":
        data["allow_outside"] = True
    if mutation == "wrong_zone":
        clock["tz"] = "Atlantis/Unknown"
    if mutation == "bad_time":
        clock["end"] = "24:90"
    if mutation == "overnight":
        clock["start"] = "23:00"
    if mutation == "empty_clock_set":
        window["all_of"] = []
    if mutation == "unspecified_clock":
        window["all_of"] = ["tokyo"]
    if mutation == "duplicate_clock":
        window["all_of"] *= 2
    if mutation == "unspecified_window":
        m["windows"] = ["unknown_window"]
    if mutation == "empty_window_set":
        m["windows"] = []
    if mutation == "ambiguous_alias":
        m["aliases"] += ["usdjpym"]
    if mutation == "untrimmed":
        m["aliases"] += [" JP225"]
    if mutation == "wildcard":
        m["aliases"] += ["JP225*"]
    if mutation != "missing":
        raw = yaml.safe_dump(data)
        if mutation == "empty":
            raw = ""
        if mutation == "duplicate_yaml_key":
            raw += "\nversion: symbol-entry-sessions/v1\n"
        f.write_text(raw)
    monkeypatch.setattr(es, "POLICY_FILE", f)
    with pytest.raises(es.SessionConfigError, match="Invalid mandatory entry sessions"):
        es.load_entry_sessions()
    locked = es.sessions_for_runner()
    assert locked.error and locked.sha256 is None
    with pytest.raises(es.EntrySessionVeto, match=es.UNAVAILABLE):
        locked.bind("XAUUSDm").require(
            "first_quote", decision=midnight("2026-01-13") + 15 * 3600
        )


def test_offline_lookup_ignores_decoy_cwd_and_equivalent_offset_inputs(
    monkeypatch, tmp_path, capsys, policy
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "entry_sessions.yaml").write_text("allow_all: true")
    assert es.load_entry_sessions().sha256 == policy.sha256
    outputs = []
    for when in ["2026-10-27T12:00:00Z", "2026-10-27T15:00:00+03:00"]:
        assert es.main(["--at", when]) == 0
        outputs.append(json.loads(capsys.readouterr().out))
    assert outputs[0] == outputs[1]
    assert outputs[0]["symbols"]["AUDUSD"]["active"] == [FULL]
    assert outputs[0]["symbols"]["US30"]["status"] == es.OUTSIDE
    assert es.main(["--symbol", "1HZ100V"]) == 2
    assert (
        json.loads(capsys.readouterr().out)["symbols"]["1HZ100V"]["status"]
        == es.UNMAPPED
    )
    with pytest.raises(SystemExit) as err:
        es.main(["--at", "2026-10-27T12:00:00"])
    assert err.value.code == 2
