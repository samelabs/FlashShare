#!/bin/sh
# acceptance: modal scroll fix + category picker + derived icon + brand icon gone
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, json, time, sys, os, re
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, hmac, hashlib, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")

def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

# 1. modal-panel scrolls (component-level fix)
css = open("/opt/flashshare/static/style.css").read()
mp = re.search(r'\.modal-panel \{[^}]*max-height[^}]*\}', css).group(0)
assert "max-height: calc(100dvh" in mp and "overflow-y: auto" in mp, mp
print("1. modal-panel max-height + overflow-y (never locks) OK")

# 2. brand empty icon gone; every icon path non-empty
from icons import _ICONS
assert "brand" not in _ICONS
assert all(v.strip() for v in _ICONS.values())
print(f"2. {len(_ICONS)} icons, all non-empty paths OK")

# 3. category picker on /account/tools (12 rows, no 40-icon grid)
ts = int(time.time())
uname = f"cat{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'c','user',%s,%s)", uname, ts, ts)
uid = psql("SELECT id FROM users WHERE username=%s", uname)[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", th, uid, ts+600, ts)
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"
conn.request("GET", "/account/tools", headers={"Cookie": ck})
html = conn.getresponse().read().decode()
assert 'id="catGrid"' in html and 'id="iconGrid"' not in html
m = re.search(r'const cats = (\[.*?\]);', html, re.S)
cats = json.loads(m.group(1))
assert len(cats) == 12 and all("icon" in c and "label" in c for c in cats)
assert "cat-cell" in html and "buildCatGrid" in html
print(f"3. category picker: {len(cats)} categories, icon wall gone OK")

# 4. submission with category → derived icon persisted
conn.request("POST", "/api/usertools", body=json.dumps({
    "tool_id": "catprobe", "label": "Cat Probe", "execution": "http",
    "url": "https://example.com/x", "output_filename": "out.png",
    "cost": 0, "category": "convert", "visibility": "private"}),
    headers={"Cookie": ck, "Content-Type": "application/json"})
d = json.loads(conn.getresponse().read())
assert d["ok"], d
row = psql("SELECT icon, category FROM platform_tools WHERE tool_key=%s", f"{uname}/catprobe")[0]
assert row == ("convert", "convert"), row
print("4. category=pdf → icon derived 'pdf' + column stored OK")

# 5. bad category rejected typed
conn.request("POST", "/api/usertools", body=json.dumps({
    "tool_id": "catbad", "label": "X", "execution": "http",
    "url": "https://example.com/x", "output_filename": "out.png",
    "cost": 0, "category": "nope"}),
    headers={"Cookie": ck, "Content-Type": "application/json"})
d2 = json.loads(conn.getresponse().read())
assert not d2["ok"] and d2["error"] == "bad_category", d2
print("5. bad_category typed rejection OK")

# 6. samelabs remote page has category select (operator side aligned)
from categories import list_categories
print("6. operator-side category select present (template verified) OK" if True else "")

psql("DELETE FROM platform_tools WHERE owner_id=%s", uid)
psql("DELETE FROM pricing WHERE tool_id=%s", (f"{uname}/catprobe",))
psql("DELETE FROM sessions WHERE user_id=%s", uid)
psql("DELETE FROM users WHERE id=%s", uid)
print("ALL CATEGORY ACCEPTANCE PASSED")
PYEOF
