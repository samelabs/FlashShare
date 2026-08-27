import http.client, time, sys, os, json, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"gz{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'g','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

# seed 120 revenue lines (3 pages worth)
for i in range(120):
    psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, note, created_at) VALUES(%s,7,7,'provider_earn','call',%s,%s,%s)",
         (uid, f"call{i}", f"payout {uname}/tool{i%5}", ts - i))
# seed 25 withdrawals
for i in range(25):
    psql("INSERT INTO withdrawals (user_id, amount, status, note, created_at) VALUES(%s,50,%s,'probe',%s)",
         (uid, ["approved", "pending", "rejected"][i % 3], ts - i))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}"

# API: page 1
conn.request("GET", "/api/earnings", headers={"Cookie": ck})
d = json.loads(conn.getresponse().read())
c1 = d["ok"] and len(d["revenue_lines"]) == 50 and d["rev_total"] == 120 and d["rev_page"] == 1
print(("PASS" if c1 else "FAIL"), f"1. rev page1: 50/120 rows, total+page in payload")

# API: page 3 via query param
conn.request("GET", "/api/earnings?rev_page=3", headers={"Cookie": ck})
d3 = json.loads(conn.getresponse().read())
c2 = len(d3["revenue_lines"]) == 20 and d3["rev_page"] == 3
print(("PASS" if c2 else "FAIL"), f"2. rev page3: 20 rows (tail page)")

# withdrawals paginated
conn.request("GET", "/api/earnings", headers={"Cookie": ck})
d = json.loads(conn.getresponse().read())
c3 = len(d["withdrawals"]) == 20 and d["wd_total"] == 25
print(("PASS" if c3 else "FAIL"), f"3. wd page1: 20/25")
conn.request("GET", "/api/earnings?wd_page=2", headers={"Cookie": ck})
d = json.loads(conn.getresponse().read())
c4 = len(d["withdrawals"]) == 5
print(("PASS" if c4 else "FAIL"), "4. wd page2: 5 rows")

# per-tool cap: 120 lines across 5 tools → breakdown = 5 rows (bounded)
c5 = len(d["tools"]) == 5
print(("PASS" if c5 else "FAIL"), f"5. per-tool breakdown bounded: {len(d['tools'])}")

# page renders pagers
conn.request("GET", "/account/earnings?rev_page=2", headers={"Cookie": ck + "; flashshare_lang=zh"})
h = conn.getresponse().read().decode()
c6 = 'id="revPager"' in h and 'id="wdPager"' in h
print(("PASS" if c6 else "FAIL"), "6. earnings page carries both pager containers")

# cleanup
psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM withdrawals WHERE user_id=%s AND note='probe'", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c3, c4, c5, c6])
print("ALL PASS" if ok else "HAS FAILURES")
