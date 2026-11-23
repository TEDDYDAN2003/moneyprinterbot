import json
import shutil
import hashlib
from pathlib import Path

# 1. Fix state.json
state_path = Path(r"C:\Users\hp\.moneyprinterbot\hull\9662f0e276119a33\state.json")
if state_path.exists():
    backup_path = state_path.with_suffix(".json.bak")
    shutil.copy2(state_path, backup_path)
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if "manual_hold" in state:
        popped = state.pop("manual_hold")
        print(f"Removed manual_hold: {popped}")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        print("Updated state.json saved.")

# 2. Update config.demo.local.yaml
cfg_path = Path("config.demo.local.yaml")
cfg_text = cfg_path.read_text(encoding="utf-8")
old_line = "    XAUUSD:   {aliases: [XAUUSDm, XAUUSD, GOLD], lots: 0.10, target_usd: 10, rate: slow, pip_size: 0.01, max_spread: 0.300}"
new_line = "    XAUUSD:   {broker_symbol: XAUUSD247m, aliases: [XAUUSD247m, XAUUSDm, XAUUSD, GOLD], lots: 0.10, target_usd: 10, rate: slow, pip_size: 0.01, max_spread: 0.300}"
if old_line in cfg_text:
    cfg_text = cfg_text.replace(old_line, new_line)
    cfg_path.write_text(cfg_text, encoding="utf-8")
    print("config.demo.local.yaml updated with XAUUSD247m.")

# 3. Update entry_sessions.yaml and sha256
es_path = Path("src/bot/config/entry_sessions.yaml")
es_text = es_path.read_text(encoding="utf-8")
old_es = "  XAUUSD:   {windows: [london_before_new_york, london_ny_overlap, new_york_day_session], aliases: [XAUUSDm, GOLD]}"
new_es = "  XAUUSD:   {windows: [london_before_new_york, london_ny_overlap, new_york_day_session], aliases: [XAUUSD247m, XAUUSDm, GOLD]}"
if old_es in es_text:
    es_text = es_text.replace(old_es, new_es)

# Recompute sha256 ignoring the sha256 header line
lines = es_text.splitlines()
body_lines = [l for l in lines if not l.startswith("sha256:")]
body_content = "\n".join(body_lines).strip() + "\n"

# Check how entry_sessions calculates its sha256
from bot.hull.entry_clock import _sha256
new_sha = hashlib.sha256(body_content.encode("utf-8")).hexdigest()
print("New SHA256 of entry_sessions body:", new_sha)

# Update sha256 in file
for i, l in enumerate(lines):
    if l.startswith("sha256:"):
        lines[i] = f"sha256: {new_sha}"
        break
es_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("entry_sessions.yaml updated and saved.")
