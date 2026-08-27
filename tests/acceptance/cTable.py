import http.client, time, sys, os, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"ct{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'c','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
# seed rows with LONG notes + long timestamps to stress alignment
for k, note in [("grant_signup", ""), ("consume", "a"*200), ("refund", "x"*150)]:
    psql("INSERT INTO ledger (user_id, delta, balance_after, kind, note, created_at) VALUES(%s,5,5,%s,%s,%s)", (uid, k, note, ts))
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/account/credits", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"})
h = conn.getresponse().read().decode()
checks = {
  "table renders with rows": h.count("<tr>") >= 4,
  "5 columns per row (header+3 rows)": h.count("<td") >= 15,
  "note ellipsis cell class": 'credit-note-cell' in h,
}
css = open('/opt/flashshare/static/style.css').read()
checks["fixed layout"] = 'table-layout: fixed' in css
checks["column contract (5 widths + nowrap)"] = css.count('.acct-table td:nth-child') == 6
ok = True
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), k); ok = ok and v
psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if ok else "HAS FAILURES")
