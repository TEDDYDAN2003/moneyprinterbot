import sqlite3, json, sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path('HullJournal').resolve()))
import ledger

cutoff_s = int(datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc).timestamp())
cutoff_ms = cutoff_s * 1000

con = sqlite3.connect('HullJournal/data/journal.sqlite3')
cur = con.cursor()

# 1. Delete deals before cutoff
cur.execute("DELETE FROM deals WHERE CAST(json_extract(body, '$.ms') AS INTEGER) < ?", (cutoff_ms,))

# 2. Get remaining pids
remaining_pids = set(r[0] for r in cur.execute("SELECT DISTINCT pid FROM deals"))
print('Remaining pids in deals:', len(remaining_pids))

# 3. Clean tracked table
cur.execute(f"DELETE FROM tracked WHERE pid NOT IN ({','.join('?' for _ in remaining_pids)})", list(remaining_pids))

# 4. Clean receipts, cross_receipts
deal_ids = set(str(r[0]) for r in cur.execute("SELECT id FROM deals"))
pid_ids = set(str(r[0]) for r in cur.execute("SELECT pid FROM deals"))
valid_ids = deal_ids | pid_ids

cur.execute(f"DELETE FROM receipts WHERE id NOT IN ({','.join('?' for _ in valid_ids)})", list(valid_ids))
cur.execute(f"DELETE FROM cross_receipts WHERE id NOT IN ({','.join('?' for _ in deal_ids)})", list(deal_ids))

# 5. Clean labels, exit_notes, finished
cur.execute("DELETE FROM labels WHERE signal < ?", (cutoff_s,))
cur.execute("DELETE FROM exit_notes WHERE at_ms < ?", (cutoff_ms,))
cur.execute("DELETE FROM finished WHERE signal < ?", (cutoff_s,))

# 6. Update meta floor
ledger.put(con, "floor", cutoff_s)

con.commit()

# Re-run make_views
views, counts, due = ledger.make_views(con)
t_rows = views["tickets"]
s_rows = views["setups"]
print(f'New tickets view count: {len(t_rows)}, setups view count: {len(s_rows)}')
print(f'Counts: {counts}')

# Write CSVs
t_csv = ledger.csv_bytes(t_rows)
s_csv = ledger.csv_bytes(s_rows)
ledger.atomic(Path('HullJournal/data/tickets.csv'), t_csv)
ledger.atomic(Path('HullJournal/data/setups.csv'), s_csv)
print('Successfully re-wrote tickets.csv and setups.csv!')

con.close()
