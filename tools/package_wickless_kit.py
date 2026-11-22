"""Build a standalone Windows-ready kit + incremental Git bundle from the committed tree."""

import argparse
import hashlib
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "6707c17"


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def pack(out, base):
    head = git("rev-parse", "HEAD").decode().strip()
    base = git("rev-parse", base).decode().strip()
    git("merge-base", "--is-ancestor", base, head)
    if head == base:
        raise ValueError("No Wickless change above base")
    tracked = git("ls-tree", "-r", "--name-only", "HEAD").decode().splitlines()
    singles = {
        "pyproject.toml",
        ".gitignore",
        "wickless.example.json",
        "docs/WICKLESS.md",
        "docs/ENTRY_SESSIONS.md",
        "src/bot/config/entry_sessions.yaml",
        "indicators/Wickless-M15-Rejection.pine",
        "WicklessDesk/index.html",
        "tests/test_entry_sessions.py",
        "config.yaml",  # shared test fixtures read the paper template
        "tests/hull_mt.py",
        "MT5/qa/package.json",
        "MT5/qa/package-lock.json",
    }
    files = {
        p: git("show", f"HEAD:{p}")
        for p in tracked
        if p in singles
        or p.startswith(
            ("research/wickless_m15/", "tests/wickless_qa/", "tests/test_wickless_")
        )
        or (p.startswith("src/bot/") and p.endswith(".py"))
    }
    if not singles <= files.keys():
        raise ValueError("Missing required files in committed tree")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "Wickless-M15.bundle"
        git("bundle", "create", str(bundle), "HEAD", f"^{base}")
        files[bundle.name] = bundle.read_bytes()
    files["START-HERE.md"] = f"""# Wickless Desk · M15 rejection bot

**Start here:** open `WicklessDesk/index.html` to select the broker and pairs. For TradingView, paste `indicators/Wickless-M15-Rejection.pine` into the Pine Editor on a standard 15-minute chart. The page and chart cannot send broker orders. The test in `research/wickless_m15` did not demonstrate a profitable edge.

**Use the portable kit:** extract the entire ZIP into a **new** folder, not over a running checkout. In that folder follow `docs/WICKLESS.md` for Windows Python setup, config validation, read-only preflight and observation. All required Python source is included. `wickless.example.json` is a paper example. Export your own `wickless.local.json`. No credentials, terminal connection or running scanner are included.

**Already using the Git repo?** The tested commit is `{head}`, based on `{base}`. To install into an existing **clean** local `main` that can fast-forward, while no affected scanner is armed or managing exposure, open PowerShell in that repo. Replace the bundle path with your actual extracted path:

```powershell
git status --short --branch
# Stop if dirty, not on main, or a running scanner/exposure could be affected.
git fetch 'C:\\path\\to\\Wickless-M15.bundle' HEAD
if ($LASTEXITCODE -ne 0) {{ throw 'Missing base or invalid bundle' }}
git merge --ff-only FETCH_HEAD
if ($LASTEXITCODE -ne 0) {{ throw 'Diverged checkout. Do not reset or force; use the portable kit.' }}
git rev-parse HEAD
```

Verify the full hash above; the bundle does not push or update a running process. Do not discard private configs, order state or other branches. The separate Hull study and the existing Hull routes are not required to run Wickless.

`SHA256SUMS.txt` covers every payload except itself. The local-only website can also be served with `python -m http.server 8035 --bind 127.0.0.1 --directory WicklessDesk` on your own machine. No trading process starts with the site.
""".encode()
    files["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(files[k]).hexdigest()}  {k}\n" for k in sorted(files)
    ).encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in sorted(files.items()):
            i = zipfile.ZipInfo(k, (2026, 1, 1, 0, 0, 0))
            i.compress_type = zipfile.ZIP_DEFLATED
            i.external_attr = 0o100644 << 16
            z.writestr(i, v)
    print(
        f"{out}: {len(files)} files · commit {head} · SHA256 {hashlib.sha256(out.read_bytes()).hexdigest()}"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--base", default=BASE)
    a = p.parse_args()
    pack(a.out, a.base)
