import http.client, time, sys, os, re
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"ly{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'l','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/", headers={"Cookie": f"flashshare_lang=zh"})
home = conn.getresponse().read().decode()
conn.request("GET", "/account/earnings", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"})
earn = conn.getresponse().read().decode()

css = open("/opt/flashshare/static/518.css").read()
checks = {
  "home: chips centered (one axis w/ search+drop)": ".home-tools { display: grid; grid-template-columns: 1fr 1fr; gap: 9px;" in css,
  "home: dead tool-desc rule gone": css.count(".tool-desc {") == 1,
  "home: card rhythm step": "margin-top: var(--s4);" in css[css.find(".tool {"):css.find(".tool {")+200],
  "earn: stat cells styled": ".earn-cell strong { font-size: var(--text-xl);" in css,
  "earn: status pills": ".wd-row .wd-st.st-pending" in css,
  "earn: list container": ".wd-list { display: flex;" in css,
  "earn: actions group": ".earn-actions { display: flex;" in css,
  "earn: wd-row single BASE rule": len(re.findall(r"\.wd-row \{ display", css)) == 1,
  "live: page renders with grid": 'earn-grid' in earn and 'earn-cell' in earn,
}
ok = True
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), k)
    ok = ok and v
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if ok else "HAS FAILURES")
