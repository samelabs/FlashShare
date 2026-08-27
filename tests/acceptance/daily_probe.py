import http.client, time, sys, os, hmac, hashlib, secrets as _sec, re
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"dc{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'d','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"

# 1. available state (not yet claimed today — no grant rows)
st, h = 0, ""
conn.request("GET", "/account/credits", headers={"Cookie": ck})
h = conn.getresponse().read().decode()
c1 = ("每日登录赠送" in h and "+5" in h and "领取" in h and 'id="dailyBox"' in h
      and 'class="acct-daily"' in h and "acct-daily done" not in h)
print(("PASS" if c1 else "FAIL"), "1. available state: label/+5/claim btn, not-done styling")

# 2. claim → grant lands, state flips to done (idempotent double-claim)
conn.request("POST", "/account/credits/claim", headers={"Cookie": ck})
r = conn.getresponse(); r.read()
rows = psql("SELECT kind, delta FROM ledger WHERE user_id=%s AND kind='grant_daily'", (uid,))
c2 = len(rows) == 1 and rows[0][1] == 5
conn.request("POST", "/account/credits/claim", headers={"Cookie": ck})
r = conn.getresponse(); r.read()
rows2 = psql("SELECT COUNT(*) FROM ledger WHERE user_id=%s AND kind='grant_daily'", (uid,))[0][0]
c2b = rows2 == 1
print(("PASS" if c2 and c2b else "FAIL"), f"2. claim grants 5 once (double-claim idempotent: {rows2} row)")

conn.request("GET", "/account/credits", headers={"Cookie": ck})
h = conn.getresponse().read().decode()
db = h[h.find("dailyBox"):h.find("dailyBox")+600]
c3 = "今日已领取" in db and "明天再来" in db and "acct-daily done" in h and "daily_claim" not in db and "<form" not in db
print(("PASS" if c3 else "FAIL"), "3. done state: green check + 今日已领取 + 明天再来, no button")

# 4. flow row translated
c4 = ">grant_daily<" not in h and "每日签到" in h
print(("PASS" if c4 else "FAIL"), "4. ledger flow shows 每日签到 (translated kind)")

# 5. en locale sanity
conn.request("GET", "/account/credits", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=en"})
he = conn.getresponse().read().decode()
c5 = "Claimed today" in he
print(("PASS" if c5 else "FAIL"), "5. en locale done-state renders")

psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c2, c2b, c3, c4, c5])
print("ALL PASS" if ok else "HAS FAILURES")
