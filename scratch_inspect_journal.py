import sqlite3

con = sqlite3.connect('HullJournal/data/journal.sqlite3')
cur = con.cursor()
tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
print('Tables:', tables)

for (t,) in tables:
    cnt = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f'Table {t}: {cnt} rows')
    cur.execute(f"SELECT * FROM {t} LIMIT 1")
    if cur.description:
        cols = [d[0] for d in cur.description]
        print('  Columns:', cols)

print('\nCheck tickets dates:')
if ('tickets',) in tables:
    rows = cur.execute("SELECT close_time, symbol, side, lots, profit FROM tickets ORDER BY close_time ASC").fetchall()
    print(f'First 5 tickets: {rows[:5]}')
    print(f'Last 5 tickets: {rows[-5:]}')

con.close()
