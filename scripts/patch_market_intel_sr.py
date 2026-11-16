"""Add sr_zone dict to market_intel.write in bot/engine.py"""
from pathlib import Path

FILE = Path("bot/engine.py")
content = FILE.read_text(encoding="utf-8")

OLD = """                            "htf": htf,
                            "pending_setups": len(getattr(self.strategy, "pending_setups", [])),"""

NEW = """                            "htf": htf,
                            "sr_zone": getattr(self.strategy, "_last_sr", {}),
                            "pending_setups": len(getattr(self.strategy, "pending_setups", [])),"""

if OLD not in content:
    print("ERROR: OLD not found in content")
    exit(1)

content = content.replace(OLD, NEW, 1)
FILE.write_text(content, encoding="utf-8")
print("Added sr_zone to market_intel logging!")
