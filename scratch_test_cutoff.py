import sqlite3
from datetime import datetime, timezone

db_path = 'HullJournal/data/journal.sqlite3'
con = sqlite3.connect(db_path)
cur = con.cursor()

# 2026-11-23 00:00:00 UTC
cutoff_s = int(datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc).timestamp())
cutoff_ms = cutoff_s * 1000

print(f'Cutoff timestamp: {cutoff_s} ({cutoff_ms} ms)')

# Check deals
deals_before = cur.execute("SELECT COUNT(*) FROM deals WHERE json_extract(body, '$.ms') < ?", (cutoff_ms,)).fetchone()[0]
deals_after = cur.execute("SELECT COUNT(*) FROM deals WHERE json_extract(body, '$.ms') >= ?", (cutoff_ms,)).fetchone()[0]
print(f'Deals before cutoff: {deals_before}, after cutoff: {deals_after}')

con.close()
