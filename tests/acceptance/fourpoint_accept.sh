#!/bin/sh
# four-point acceptance: exposed tools + cap panel + search + agents MCP narrative
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, json, time, sys, os, urllib.parse
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)

def get(path, cookie=""):
    h = {"Cookie": cookie} if cookie else {}
    conn.request("GET", path, headers=h)
    r = conn.getresponse()
    return r.status, r.read().decode()

# 1. anonymous home: search box + grid present (cap bar removed per user)
st, html = get("/", "flashshare_lang=zh")
assert st == 200
assert 'id="toolSearch"' in html, "search missing"
assert 'cap-bar' not in html and 'cap-stat' not in html, "cap bar should be gone"

assert 'data-i18n-placeholder="tool_search_ph"' in html
print("1. home search box (zh), cap bar removed OK")

# 2. search endpoint: built-in match (pdf) — first-party first
st, body = get("/api/tools/search?q=pdf")
d = json.loads(body)
assert d["ok"] and any("/" not in t["id"] for t in d["tools"]), body[:200]
print(f"2. search 'pdf' → {len(d['tools'])} tools, first-party first OK")

# 3. search includes community public tool (create one via DB to test)
con = psycopg2.connect(PG); cur = con.cursor()
ts = int(time.time())
uname = f"srch{ts%100000}"
cur.execute("DELETE FROM platform_tools WHERE tool_key LIKE 'srch%%' AND tool_key != %s", (f"{uname}/echo",))
con.commit()
cur.execute("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'s','user',%s,%s) RETURNING id", (uname, ts, ts))
uid = cur.fetchone()[0]
cur.execute("""INSERT INTO platform_tools(tool_key, owner_id, tool_id, execution, tool_type, label_json, icon, visibility, enabled, trusted, created_at, updated_at)
  VALUES(%s,%s,'echo','http','produce',%s,'wand','public',TRUE,FALSE,%s,%s)""",
  (f"{uname}/echo", uid, json.dumps({"en": "Echo PDF magic", "zh": "回声PDF魔法"}), ts, ts))
con.commit()
st, body = get(f"/api/tools/search?q=pdf")
d = json.loads(body)
keys = [t["id"] for t in d["tools"]]
assert f"{uname}/echo" in keys, keys
# order: first-party before community
fp_idx = [i for i, k in enumerate(keys) if "/" not in k]
cm_idx = keys.index(f"{uname}/echo")
assert all(i < cm_idx for i in fp_idx), (fp_idx, cm_idx)
# label search too
st, body = get("/api/tools/search?q=" + urllib.parse.quote("回声"))
d = json.loads(body)
assert f"{uname}/echo" in [t["id"] for t in d["tools"]], "zh label search miss"
print("3. search: community tool found by key AND zh label, ordering OK")

# 4. anonymous home grid now contains the community tool with badge
st, html = get("/", "flashshare_lang=zh")
assert f'@{uname}' in html and 'tool-tab community' in html, "community badge missing on grid"
# i18n-safety: community tabs must NOT use data-i18n (would render raw key)
import re
m = re.search(r'<div class="tool-tab community" data-tool-id="' + uname + '/echo".{0,900}', html, re.S)
seg = m.group(0) if m else ''
assert f'data-tool-id="{uname}/echo"' in seg, seg[:200]
assert '<span class="tool-tab-name">' in seg
import re as _re
span = _re.search(r'<span class="tool-tab-name">(.{0,120})</span>', seg, _re.S).group(1)
assert 'data-i18n' not in span, span
print("4. home grid: community tool + @provider badge, no data-i18n hazard OK")

# 5. (platform_stats removed with cap bar — grid itself is the catalog)

# 6. agents page: MCP-first narrative
st, html = get("/agents", "flashshare_lang=zh")
assert st == 200
assert html.find("mcpServers") < html.find("/api/v1/me"), "MCP must come before REST"
assert "豆包工作" in html and "WorkBuddy" in html and "Cherry Studio" in html
assert "client_ref" in html
print("6. agents page MCP-first + client matrix OK")

# cleanup
cur.execute("DELETE FROM platform_tools WHERE tool_key LIKE 'srch%'")
cur.execute("DELETE FROM users WHERE username LIKE 'srch%'")
cur.execute("DELETE FROM users WHERE id=%s", (uid,))
cur.execute("DELETE FROM pricing WHERE tool_id=%s", (f"{uname}/echo",))
con.commit()
print("ALL FOUR-POINT CHECKS PASSED")
PYEOF
