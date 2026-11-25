import sqlite3, json

con = sqlite3.connect('HullJournal/data/journal.sqlite3')
cur = con.cursor()

meta = cur.execute("SELECT k, v FROM meta").fetchall()
print('Meta:')
for k, v in meta:
    print(f'  {k}: {v}')

print('\nSample deal body:')
deal = cur.execute("SELECT id, pid, body FROM deals ORDER BY id DESC LIMIT 5").fetchall()
for d in deal:
    print(d[0], d[1], d[2][:150])

con.close()
