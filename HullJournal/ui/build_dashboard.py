"""Build the self-contained offline/live dashboard from sources + strictly synthetic examples."""

import csv
import json
import sys
from pathlib import Path

assets = Path(__file__).resolve().parent
root = assets.parent
sys.path.insert(0, str(root))
from hull_analytics import REASON_LABELS, REASON_NOTES, SESSIONS
from ledger import FIELDS

pack = {"fields": FIELDS}
policy = {"sessions": SESSIONS, "labels": REASON_LABELS, "notes": REASON_NOTES}
for k in ("tickets", "setups"):
    with (root / "examples" / (k + ".csv")).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
        assert rows and all(r["account_key"] == "SAMPLE" for r in rows)
        pack[k] = [[r[x] for x in FIELDS] for r in rows]


def js(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


s = (
    (assets / "page.tpl")
    .read_text(encoding="utf8")
    .replace(
        "__MARKET_SECTION__", (assets / "market_windows.tpl").read_text(encoding="utf8")
    )
    .replace(
        "__MARKET_DATA__",
        js(json.loads((assets / "market_windows.json").read_text(encoding="utf8"))),
    )
    .replace("__STYLE__", (assets / "style.css").read_text(encoding="utf8"))
    .replace(
        "__SCRIPT__",
        (assets / "app.js")
        .read_text(encoding="utf8")
        .replace(
            "/* ANALYTICS_MODULE */",
            (assets / "analytics.js").read_text(encoding="utf8")
            + "\n"
            + (assets / "market_windows.js").read_text(encoding="utf8"),
        ),
    )
    .replace("__SAMPLE__", js(pack))
    .replace("__POLICY__", js(policy))
)
(root / "Hull-Journal.html").write_text(s, encoding="utf8")
