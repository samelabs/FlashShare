"""视觉系统清理终检: 死类归零+重复归零+平衡+全站页面渲染样式链完整"""
import http.client, time, sys, os, re, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"vc{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'v','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

CSS = open("/opt/flashshare/static/style.css").read()

# 1. health
d = 0
for ch in CSS: d += (ch == '{') - (ch == '}')
c1 = d == 0
DEAD = ['acct-row-btns','cat-grid','icon-grid','earn-hist','samelabs-nav','userbox','cap-bar','auth-bonus','stats-grid','v-private']
c1b = not any(('.' + x) in CSS for x in DEAD)
print(("PASS" if c1 and c1b else "FAIL"), f"1. balance={d}, dead families gone")

# 2. duplicate selectors zero
from collections import Counter
sels = [re.sub(r'\s+', ' ', m.group(1).strip()) for m in re.finditer(r'(?m)^([.#][^{},\n]+(?:\s*,\s*[.#][^{},\n]+)*)\{', CSS)]
dupes = {k: v for k, v in Counter(sels).items() if v > 1}
c2 = not dupes
print(("PASS" if c2 else "FAIL"), f"2. exact duplicates: {dupes or 'none'}")

# 3. every page renders + links the stylesheet + key component rules live
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
pages = ["/", "/agents", "/account/tools", "/account/earnings", "/account/credits", "/account/tokens", "/account/profile", "/account/security"]
all_ok = True
for p in pages:
    conn.request("GET", p, headers={"Cookie": f"{COOKIE_NAME}={tok_s}"})
    r = conn.getresponse(); body = r.read().decode()
    ok = r.status == 200 and "style.css" in body
    if not ok: all_ok = False; print("   FAIL", p, r.status)
c3 = all_ok
print(("PASS" if c3 else "FAIL"), f"3. all {len(pages)} pages render + stylesheet linked")

# 4. core component rules alive (spot)
live = ['.acct-card {', '.acct-tab.active', '.earn-cell strong', '.wd-row', '.acct-pager', '.menu-sec + .menu-sec', '.token-reveal-row .btn-secondary', '.ledger-kind', '.tool-search', '.drop {']
missing = [k for k in live if k not in CSS]
c4 = not missing
print(("PASS" if c4 else "FAIL"), f"4. core rules alive (missing: {missing or 'none'})")

# 5. size reduction
c5 = len(CSS) < 75000
print(("PASS" if c5 else "FAIL"), f"5. size {len(CSS)} (was 83624)")

psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
ok = all([c1, c1b, c2, c3, c4, c5])
print("ALL PASS" if ok else "HAS FAILURES")
