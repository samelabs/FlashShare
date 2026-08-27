# 阶段0验收: pico引入后 8页渲染正常, 关键业务样式不被破坏(抽查类规则仍在style.css)
import http.client
import time, sys, os, hmac, hashlib, secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"pc{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'p','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))
ck = {"Cookie": f"{COOKIE_NAME}={tok_s}"}
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
pages = ["/", "/agents", "/auth/login", "/auth/register", "/account/tools", "/account/earnings", "/account/credits", "/account/tokens"]
ok = True
for p in pages:
    conn.request("GET", p, headers=ck)
    r = conn.getresponse(); h = r.read().decode()
    has_pico = 'vendor/pico.classless.min.css' in h
    has_style = 'style.css?v=' in h
    order_ok = h.find('pico.classless') < h.find('style.css?v=')
    good = r.status == 200 and has_pico and has_style and order_ok
    if not good: ok = False; print("FAIL", p, r.status, has_pico, has_style, order_ok)
print(("PASS" if ok else "FAIL"), "8页: pico引入+顺序正确(pico→style)")
# pico文件本身可达
conn.request("GET", "/static/vendor/pico.classless.min.css")
r = conn.getresponse(); r.read()
print(("PASS" if r.status == 200 else "FAIL"), "pico.min.css served:", r.status)
css = open('/opt/flashshare/static/style.css').read()
print(("PASS" if '.acct-card {' in css and '.topbar' in css and '.drop {' in css else "FAIL"), "业务样式层完整(未被pico碰)")
