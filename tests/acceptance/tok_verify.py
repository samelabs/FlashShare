import http.client, time, sys, os, re, hmac, hashlib, secrets as _sec, json
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"tk{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'t','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+900, ts))
ck = {"Cookie": f"{COOKIE_NAME}={tok_s}", "Content-Type": "application/json"}
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
ok = True

# 造2活1吊销令牌
conn.request("POST", "/api/tokens", json.dumps({"name": "alive-one"}), ck); r = conn.getresponse(); r.read()
conn.request("POST", "/api/tokens", json.dumps({"name": "alive-two"}), ck); r = conn.getresponse(); r.read()
conn.request("POST", "/api/tokens", json.dumps({"name": "dead-one"}), ck); r = conn.getresponse(); d1 = json.loads(r.read())
tid = d1["token"]["id"]
conn.request("POST", f"/api/tokens/{tid}/revoke", "", ck); r = conn.getresponse(); r.read()

# API仍返回3条(数据完整), 前端过滤——验证API与模板
conn.request("GET", "/api/tokens", headers=ck)
d = json.loads(conn.getresponse().read())
n_active = len([t for t in d["tokens"] if t["status"] == "active"])
n_revoked = len([t for t in d["tokens"] if t["status"] != "active"])
checks = {
    "造数: 2活+1吊销": n_active == 2 and n_revoked == 1,
}
# JS过滤逻辑在位
js = open('/opt/flashshare/static/pages/tokens.js').read()
checks["前端只渲染active"] = "filter(x => x.status === 'active')" in js
# 模板MCP卡删除
conn.request("GET", "/account/tokens", headers=ck)
h = conn.getresponse().read().decode()
checks["MCP接入卡已删"] = "acct-agents-link" not in h and "agents_tokens_hint" not in h
# emoji+横线
css = open('/opt/flashshare/static/style.css').read()
checks["书签emoji删除"] = "🏷" not in css
checks["无名'—'回退删除"] = "name || '—'" not in js
checks["空名字行隐藏"] = ".token-new-name:empty { display: none; }" in css
# 结构完整
checks["页面div平衡"] = h.count("<div") == h.count("</div>")
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), k)
    ok = ok and v
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print("ALL PASS" if ok else "HAS FAILURES")
