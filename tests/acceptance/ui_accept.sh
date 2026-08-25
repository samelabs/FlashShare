#!/bin/sh
# UI acceptance: topbar MCP entry + icon fix + full form + earnings panel markup
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

ts = int(time.time())
uname = f"ui{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'u','user',%s,%s)", uname, ts, ts)
uid = psql("SELECT id FROM users WHERE username=%s", uname)[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", th, uid, ts+600, ts)

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"

# 1. topbar MCP entry on home (both anon + authed)
conn.request("GET", "/", headers={"Cookie": "flashshare_lang=zh"})
html = conn.getresponse().read().decode()
assert 'class="topbar-agents"' in html and "MCP 接入" in html, "topbar entry missing"
assert html.find("topbar-agents") < html.find("site-footer"), "must be in header"
print("1. topbar MCP entry present (before footer) OK")

# 2. /agents title + MCP-first + no stale 'API与集成'
conn.request("GET", "/agents", headers={"Cookie": ck})
html = conn.getresponse().read().decode()
assert "API与集成" not in html and "MCP" in html
print("2. agents page: no stale naming OK")

# 3. icon dict intact on /account/tools (the regression)
conn.request("GET", "/account/tools", headers={"Cookie": ck})
html = conn.getresponse().read().decode()
m = re.search(r'window\.__ICONS__ = (\{.*?\});', html, re.S)
assert m, "__ICONS__ missing"
icons = json.loads(m.group(1))
assert isinstance(icons, dict) and "wand" in icons and "search" in icons and icons["wand"].startswith("<"), type(icons)
assert "icons_json" not in html.replace("window.__ICONS__", "").replace(" = ", ""), "double inject"
print(f"3. window.__ICONS__ is a dict ({len(icons)} icons), wand path present OK")

# 4. form fields complete
for fid in ["fTimeout", "fAccept", "fOutCt", "fAuthHeader", "fAuthToken", "fMcpRow"]:
    assert f'id="{fid}"' in html, fid
assert 'id="fMcpRow" class="hidden"' in html  # hidden unless mcp
print("4. submit form: timeout/accept/outct/auth x2/mcp-conditional rows OK")

# 5. earnings panel markup + withdraw modal
for eid in ["earnCard", "eTotal", "eAvail", "ePending", "eWithdrawn", "earnHist", "withdrawModal", "submitWithdraw"]:
    assert f'id="{eid}"' in html, eid
assert "发布工具" in html  # unified naming
assert "我的发布" not in html and "我的工具" not in html
print("5. earnings panel + withdraw modal + unified naming OK")

# 6. earnings endpoint renders data for this user (zero state)
conn.request("GET", "/api/usertools/earnings", headers={"Cookie": ck})
d = json.loads(conn.getresponse().read())
assert d["ok"] and d["summary"]["available"] == 0
print("6. earnings endpoint zero-state OK")

psql("DELETE FROM sessions WHERE user_id=%s", uid)
psql("DELETE FROM users WHERE id=%s", uid)
print("ALL UI ACCEPTANCE PASSED")
PYEOF
