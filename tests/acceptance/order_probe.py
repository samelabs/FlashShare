import http.client, time, sys, os
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"mo{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'m','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/account/credits", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"})
h = conn.getresponse().read().decode()
nav = h[h.find('class="acct-nav"'):h.find("</nav>")]
pos = [nav.find(x) for x in ["积分", "发布工具", "我的收益", "令牌", "资料", "安全"]]
c1 = all(p > 0 for p in pos) and all(pos[i] < pos[i+1] for i in range(len(pos)-1))
print(("PASS" if c1 else "FAIL"), f"order consume->supply->dev->account: {pos}")
seps = nav.count("acct-tab-sep")
c2 = seps == 3
print(("PASS" if c2 else "FAIL"), f"group separators = {seps}")
all_ok = True
for p in ["/account/tools", "/account/earnings", "/account/credits", "/account/tokens", "/account/profile", "/account/security"]:
    conn.request("GET", p, headers={"Cookie": f"{COOKIE_NAME}={tok_s}"})
    hh = conn.getresponse().read().decode()
    if "acct-tab active" not in hh or "acct-side" in hh: all_ok = False; print("FAIL", p)
print(("PASS" if all_ok else "FAIL"), "all 6 pages: active tab + no residue")
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if (c1 and c2 and all_ok) else "HAS FAILURES")
