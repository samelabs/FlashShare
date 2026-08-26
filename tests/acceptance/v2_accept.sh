#!/bin/sh
# v2 acceptance: modal markup, icon grid, earnings in API, nav wrap CSS
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, base64, sys, os, re, threading
import secrets as _sec
sys.path.insert(0, "/opt/flashshare/core"); sys.path.insert(0, "/opt/flashshare")
import psycopg2

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

from config import SESSION_SECRET, COOKIE_NAME
u = f"v2u{int(time.time())%100000}"
tok_s = _sec.token_urlsafe(32)
import hmac, hashlib
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,%s,'user',%s,%s)",
     (u, "v2", int(time.time()), int(time.time())))
uid = psql("SELECT id FROM users WHERE username=%s", u)[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)",
     (th, uid, int(time.time())+600, int(time.time())))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
def get(path):
    conn.request("GET", path, headers={"Cookie": f"{COOKIE_NAME}={tok_s}"})
    r = conn.getresponse(); return r.status, r.read()

# 1. page renders: modal + category picker (icon derived) + NO raw icon input
st, d = get("/account/tools")
html = d.decode()
assert st == 200, st
assert 'id="toolModal"' in html and 'class="modal hidden"' in html, "modal missing"
assert 'id="catGrid"' in html and 'role="radiogroup"' in html, "category picker missing"
assert 'id="fIcon"' not in html and 'name="icon"' not in html, "raw icon input still present!"
assert '我的发布' in html or 'acct_tools' in html, "title missing"
assert 'showToolForm' not in html, "old inline-form toggle still present"
print("1. modal + category picker, no icon input OK")

# 2. picker names derive from window.__ICONS__ dict injected by base.html
#    (page route no longer overrides icons_json — that broke lookups site-wide)
m2 = re.search(r'const cats = (\[.*?\]);', html, re.S)
assert m2, "categories data missing"
cats = json.loads(m2.group(1))
assert len(cats) == 12, len(cats)
from icons import _ICONS
assert all(c["icon"] in _ICONS and _ICONS[c["icon"]].strip() for c in cats)
m3 = re.search(r'window\.__ICONS__ = (\{.*?\});', html, re.S)
assert m3 and isinstance(json.loads(m3.group(1)), dict)
print(f"2. category picker: {len(cats)} cats, icons derived + valid OK")

# 3. API returns earned field
st, d = get("/api/usertools")
res = json.loads(d)
assert res["ok"] and all("earned" in t["calls"] for t in res["tools"]), res
print("3. /api/usertools includes calls.earned OK")

# 4. earnings correctness against a synthetic payout
ts = int(time.time())
key = u + "/probe"
psql("""INSERT INTO platform_tools(tool_key, owner_id, tool_id, execution, tool_type,
       label_json, icon, visibility, enabled, created_at, updated_at)
       VALUES(%s,%s,'probe','http','produce','{"en":"P"}','wand','private',TRUE,%s,%s)""",
     (key, uid, ts, ts))
# caller uid2 makes an ok call of cost 10 → payout 7
u2 = u + "b"
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,%s,'user',%s,%s)",
     (u2, "v2b", ts, ts))
uid2 = psql("SELECT id FROM users WHERE username=%s", u2)[0][0]
cid = psql("""INSERT INTO tool_calls(client_ref, user_id, tool_id, status, cost_snapshot,
            started_at, finished_at) VALUES('v2probe', %s, %s, 'ok', 10, %s, %s) RETURNING id""",
           (uid2, key, ts, ts))[0][0]
psql("""INSERT INTO ledger(user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at)
       VALUES(%s, 7, 7, 'provider_earn', 'tool_call', %s, %s, 'probe', %s)""",
     (uid, str(cid), f"payout:{cid}", ts))
st, d = get("/api/usertools")
res = json.loads(d)
probe = [t for t in res["tools"] if t["tool_key"] == key][0]
assert probe["calls"]["earned"] == 7, probe["calls"]
print("4. earned = 7 (payout join) OK")

# 5. nav CSS: wrap rule present; 640px media query
css = open("/opt/flashshare/static/style.css").read()
assert "@media (max-width: 640px)" in css and "flex-wrap: wrap" in css[css.rfind("@media (max-width: 640px)"):][:300], "nav wrap css missing"
assert "overflow-x: visible" in css, "nav overflow fix missing"
print("5. acct-nav mobile wrap CSS OK")

# 6. i18n rename live (zh)
conn.request("GET", "/account/tools", headers={"Cookie": f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"})
r = conn.getresponse(); dzh = r.read()
assert '发布工具' in dzh.decode(), dzh[:200]
print("6. title unified 发布工具 OK")

# cleanup
psql("DELETE FROM sessions WHERE user_id IN (%s, %s)", (uid, uid2))
psql("DELETE FROM platform_tools WHERE owner_id IN (%s, %s)", (uid, uid2))
psql("DELETE FROM tool_calls WHERE user_id IN (%s, %s)", (uid, uid2))
psql("DELETE FROM ledger WHERE user_id IN (%s, %s)", (uid, uid2))
psql("DELETE FROM users WHERE id IN (%s, %s)", (uid, uid2))
print("ALL V2 ACCEPTANCE CHECKS PASSED")
PYEOF
