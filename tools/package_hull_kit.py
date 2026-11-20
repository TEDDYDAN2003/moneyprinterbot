"""Build the public companions ZIP from an explicit allowlist; never archive private runtime folders."""

from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = [
    "START-HERE.md",
    "Start-Journal.cmd",
    "Hull-Journal.html",
    "journal.py",
    "ledger.py",
    "hull_analytics.py",
    "requirements-windows.txt",
    "examples/generate.py",
    "examples/tickets.csv",
    "examples/setups.csv",
    "tests/test_journal.py",
    "tests/test_analytics.py",
    "tests/fixtures.py",
    "ui/build_dashboard.py",
    "ui/page.tpl",
    "ui/style.css",
    "ui/app.js",
    "ui/analytics.js",
    "ui/market_windows.js",
    "ui/market_windows.tpl",
    "ui/market_windows.json",
]
MT5 = [
    "FC-Hull-Either-16-Structure.mq5",
    "INSTALL-MT5.md",
    "optional/Check-FC-Hull-Structure.mq5",
    "qa/README.md",
    "qa/package.json",
    "qa/package-lock.json",
    "qa/pine_oracle.cjs",
    "qa/check_port.py",
    "qa/check_bot.py",
    "qa/candles.json",
    "qa/protected_cases.json",
    "qa/reference.pine",
]
SESSION_EVIDENCE = [
    "README.md",
    "archives.json",
    "windows.csv",
    "windows_by_utc_date.csv",
]
PINE = "indicators/FC-Hull-Either-16-Structure.pine"
INTRO = """HULL LEDGER + PROTECTED SWING v2 CHART STUDIES

Journal: extract the entire kit, read HullJournal/START-HERE.md, and use Start-Journal.cmd on the Windows MT5 computer. Keep your existing private data directory. The journal examples are synthetic; Symbol performance also contains separately labelled historical Exness price research. See research/session_volatility/ for the quote evidence.

MT5: copy MT5/FC-Hull-Either-16-Structure.mq5 to MQL5/Indicators, open THAT file in MetaEditor and compile with F7. Refresh Navigator and attach the Protected v2 indicator. Do not paste Pine in MetaEditor.

TradingView: paste indicators/FC-Hull-Either-16-Structure.pine into Pine Editor. Save and add the updated study. Select Valid suggestions only and recreate any alerts tied to an old script snapshot.

Rules: see docs/HULL_STRUCTURE.md. These chart studies do not send orders or update a running scanner. To update the Python bot, install the matching repository revision and verify entry_policy: protected_swing_v2 and entry_timing: live_continuation_v1 in the observer. Do not replace a running armed scanner or delete its state.
"""


def main():
    if (ROOT / PINE).read_bytes() != (ROOT / "MT5/qa/reference.pine").read_bytes():
        raise ValueError("Published Pine differs from parity reference")
    names = [
        *("HullJournal/" + f for f in JOURNAL),
        *("MT5/" + f for f in MT5),
        *("research/session_volatility/" + f for f in SESSION_EVIDENCE),
        PINE,
        "docs/HULL_STRUCTURE.md",
    ]
    target = ROOT / "downloads/Hull-Ledger-Analytics-and-MT5.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as tmp:
        temp = Path(tmp.name)
    try:
        with ZipFile(temp, "w") as z:
            for name in ["READ-ME-FIRST.txt", *names]:
                info = ZipInfo(name, (2000, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                payload = (
                    INTRO.encode()
                    if name == "READ-ME-FIRST.txt"
                    else (ROOT / name).read_bytes()
                )
                z.writestr(info, payload, compress_type=ZIP_DEFLATED, compresslevel=9)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    print(f"{target.name}: {len(names) + 1} allowlisted files")


if __name__ == "__main__":
    main()
