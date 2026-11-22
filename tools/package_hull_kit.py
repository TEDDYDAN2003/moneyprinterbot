"""Build the current offline desk from reviewed Git index blobs, never local runtime data."""

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SINGLES = {
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "config.yaml",
    "wickless.example.json",
    ".gitignore",
    ".gitattributes",
}
# Limit every directory by file type. Journal CSVs may ONLY be the two synthetic samples.
TREES = {
    "src/bot/": {".py"},
    "tests/": {".py", ".cjs", ".json"},
    "docs/": {".md", ".html"},
    "MT5/": {".mq5", ".md", ".py", ".hpp", ".json"},
    "indicators/": {".pine", ".js", ".json"},
    "HullJournal/": {".py", ".js", ".tpl", ".css", ".cmd", ".md", ".html", ".txt"},
    "WicklessDesk/": {".html"},
    "research/session_volatility/": {".md", ".py", ".json", ".csv"},
    "research/wickless_m15/": {".md", ".py", ".json", ".csv"},
    "tools/": {".py"},
}
SINGLES |= {
    "src/bot/config/entry_sessions.yaml",
    "HullJournal/examples/tickets.csv",
    "HullJournal/examples/setups.csv",
    "HullJournal/ui/market_windows.json",
    "HullJournal/.gitignore",
}
REQUIRED = SINGLES | {
    "src/bot/cli.py",
    "src/bot/hull/terminal.py",
    "docs/HULL_DEMO.md",
    "MT5/FC-Dynamic-Hull-Lanes.mq5",
    "HullJournal/ledger.py",
    "HullJournal/journal.py",
    "HullJournal/Hull-Journal.html",
    "tests/test_hull_journal_bridge.py",
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def allowed(name):
    return name in SINGLES or (
        not name.startswith("HullJournal/data/")
        and any(
            name.startswith(d) and Path(name).suffix in ext for d, ext in TREES.items()
        )
    )


def build(out):
    files = {
        n: git("show", ":" + n)
        for n in git("ls-files", "-z").decode().split("\0")
        if n and allowed(n)
    }
    if not REQUIRED <= files.keys():
        raise ValueError("Missing expected desk files: " + str(REQUIRED - files.keys()))
    # The portable copy already *is* the download; don't put a dead self-link in it.
    files["README.md"] = files["README.md"].replace(
        b"[Download the current desk](downloads/Dynamic-Hull-Desk.zip)",
        b"**Portable desk**",
    )
    # Git normalises tracked text; Windows launchers and CSVs ship with CRLF.
    for n in (
        "HullJournal/Start-Journal.cmd",
        "HullJournal/examples/tickets.csv",
        "HullJournal/examples/setups.csv",
    ):
        files[n] = files[n].replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    files["START-HERE.md"] = (
        b"""# Dynamic Hull desktop kit\n\nNew install: extract into a NEW folder and open README.md. The checked-in template is paper-only; nothing starts automatically. Use the included HullJournal for actual closed demo fills, the MQ5 indicator for chart context, and the profile picker for observation. The charts are not an order manager.\n\nExisting install: preserve config.demo.local.yaml, reports/hull_runtime, home-directory account state, magic, and the journal output folder containing journal.sqlite3. Do not overlay this kit on your running checkout. Use a verified Git fast-forward while flat, or migrate offline in a copy with backups and the same working directory before restarting. For a separate reporter, --repo points to where the scanner writes its reports and --out to the ORIGINAL journal folder. No private records or credentials are included.\n\nSHA256SUMS.txt covers every other file in this kit. The ZIP contains sources, not Git history; the Git checkout has the downloads link and the packaging tools.\n"""
    )
    files["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(files.items())
    ).encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        f = Path(tmp) / "desk.zip"
        with ZipFile(f, "w") as z:
            for n, data in sorted(files.items()):
                i = ZipInfo(n, (2000, 1, 1, 0, 0, 0))
                i.create_system = 3
                i.external_attr = 0o100644 << 16
                z.writestr(i, data, compress_type=ZIP_DEFLATED, compresslevel=9)
        with ZipFile(f) as z:
            if (
                z.testzip()
                or set(z.namelist()) != set(files)
                or any(z.read(n) != d for n, d in files.items())
            ):
                raise ValueError("Archive verification failed")
        f.replace(out)
    print(
        f"{out}: {len(files)} files; SHA256 {hashlib.sha256(out.read_bytes()).hexdigest()}"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=ROOT / "downloads/Dynamic-Hull-Desk.zip")
    build(p.parse_args().out)
