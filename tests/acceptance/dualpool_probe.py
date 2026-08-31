import os, sys, time, json, http.client, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
from economy.billing import spendable_balance, consume, InsufficientCredits
from database import db

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"dp{ts%100000}"
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'d','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]

# seed: +50 gift (spendable), +200 earn (withdraw-only)
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,50,50,'grant_signup','activity','signup',%s,'',%s)", (uid, f"sg:{uid}", ts))
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,200,250,'provider_earn','call','c1',%s,'x/probe',%s)", (uid, f"pe:{uid}", ts))

# 1. spendable = 50 (earn excluded)
c1 = spendable_balance(uid) == 50
print(("PASS" if c1 else "FAIL"), f"1. spendable=50 excludes earn (got {spendable_balance(uid)})")

# 2. consume 60 (> spendable 50) → InsufficientCredits even though total=250
try:
    consume(uid, 60, "consume", idem_key=f"c1:{uid}")
    c2 = False
except InsufficientCredits:
    c2 = True
print(("PASS" if c2 else "FAIL"), "2. consume 60 rejected (total 250 but earn-pool untouchable)")

# 3. consume 40 OK → spendable 10, earn intact 200
r = consume(uid, 40, "consume", idem_key=f"c2:{uid}", note="tool:x")
c3 = spendable_balance(uid) == 10
rows = psql("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE user_id=%s AND kind='provider_earn'", (uid,))
c3b = rows[0][0] == 200
print(("PASS" if c3 and c3b else "FAIL"), f"3. consume 40 OK → spendable=10, earn=200 intact")

# 4. earnings summary unaffected (200 available to withdraw)
from economy.earnings import earnings_summary
s = earnings_summary(uid)
c4 = s["earned"] == 200 and s["available"] == 200
print(("PASS" if c4 else "FAIL"), f"4. withdrawable=200 independent ({s})")

psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c3, c3b, c4])
print("ALL PASS" if ok else "HAS FAILURES")
