"""Build the public companions ZIP from an explicit allowlist; never archive private runtime folders."""

from hashlib import sha256
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
    "qa/check_chart_rules.py",
    "qa/trend_cases.hpp",
    "qa/v4-cases.csv",
    "qa/v4-verification.txt",
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
JOURNAL_INTRO = """HULL LEDGER + BOS HALF-RANGE v4 CHART INDICATORS

Journal: extract the entire kit, read HullJournal/START-HERE.md, and use Start-Journal.cmd on the Windows MT5 computer. Keep your existing private data directory. The journal examples are synthetic; Symbol performance also contains separately labelled historical Exness price research. See research/session_volatility/ for the quote evidence.

"""
CHART_INTRO = """MT5: copy MT5/FC-Hull-Either-16-Structure.mq5 to MQL5/Indicators, open THAT file in MetaEditor and compile with F7. Refresh Navigator and attach FC Hull BOS Half-Range v4. Do not paste Pine in MetaEditor.

TradingView: paste indicators/FC-Hull-Either-16-Structure.pine into Pine Editor. Save and add FC Range 4. Select Valid suggestions only. Delete v3 limit-ready alerts and recreate desired v4 alerts.

Rules: docs/HULL_STRUCTURE.md. CHoCH is context only. No reversal watch, no staged limit, no entry on a retest. A new completed trend cross must qualify in a confirmed BOS half-range. The Python scanner uses the same entry policy, bos_half_range_close_v4 and confirmed_close_only, when separately installed. MT5/qa/v4-cases.csv and MT5/qa/v4-verification.txt contain worked test traces and the offline results. A chart installation cannot start or update the bot. Do not replace a running armed scanner or delete its state.
"""


def pack(target, names, intro):
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as tmp:
        temp = Path(tmp.name)
    try:
        with ZipFile(temp, "w") as z:
            items = {
                "READ-ME-FIRST.txt": intro.encode(),
                **{n: (ROOT / n).read_bytes() for n in names},
            }
            items["SHA256SUMS.txt"] = "".join(
                f"{sha256(v).hexdigest()}  {n}\n" for n, v in sorted(items.items())
            ).encode()
            for name, payload in items.items():
                info = ZipInfo(name, (2000, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                z.writestr(info, payload, compress_type=ZIP_DEFLATED, compresslevel=9)
        with ZipFile(temp) as z:
            if (
                z.testzip()
                or len(z.infolist()) != len(items)
                or any(z.read(k) != v for k, v in items.items())
            ):
                raise ValueError(f"Verification failed for {target.name}")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    print(f"{target.name}: {len(items)} allowlisted files")


def main():
    if (ROOT / PINE).read_bytes() != (ROOT / "MT5/qa/reference.pine").read_bytes():
        raise ValueError("Published Pine differs from parity reference")
    charts = [*("MT5/" + f for f in MT5), PINE, "docs/HULL_STRUCTURE.md"]
    pack(
        ROOT / "downloads/Hull-Chart-Indicators-v4.zip",
        charts,
        "FC HULL BOS HALF-RANGE V4 | CHARTS ONLY\n\n" + CHART_INTRO,
    )
    pack(
        ROOT / "downloads/Hull-Ledger-Analytics-and-MT5.zip",
        [
            *("HullJournal/" + f for f in JOURNAL),
            *("research/session_volatility/" + f for f in SESSION_EVIDENCE),
            *charts,
        ],
        JOURNAL_INTRO + CHART_INTRO,
    )


if __name__ == "__main__":
    main()
