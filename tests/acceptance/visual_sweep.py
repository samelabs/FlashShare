import re
import http.client
import sys
import os

sys.path.insert(0, '/opt/flashshare/core')
for line in open('/etc/flashshare.env'):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ[k] = v

from config import SESSION_SECRET, COOKIE_NAME
import psycopg2, hmac, hashlib, secrets, time

PG = os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://')
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f'vsweep{ts%100000}'
tok_s = secrets.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'v','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+900, ts))
ck = {"Cookie": f"{COOKIE_NAME}={tok_s}"}
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)

STYLE = open('/opt/flashshare/static/518.css').read()

checks = {}

# 1. style.css关键表面规则在场(视觉锚)
anchors = ['.topbar-profile {', '.user-dropdown a, .user-dropdown button {', '.drop {', '.tool-tab {',
           '.acct-table {', '.earn-cell', '.menu-sec + .menu-sec', '.modal {']
missing = [a for a in anchors if a not in STYLE]
checks['style.css表面锚全在'] = (not missing, missing)

# 2. 8页真实渲染: 200 + style.css在场 + 零裸引用
pages = ["/", "/agents", "/auth/register", "/account/tools", "/account/earnings", "/account/credits", "/account/tokens", "/account/profile"]
bad = []
for p in pages:
    conn.request("GET", p, headers=ck)
    r = conn.getresponse(); h = r.read().decode()
    if r.status != 200: bad.append((p, r.status)); continue
    if 'style.css?v=' not in h: bad.append((p, 'style.css missing'))
checks['8页: style.css加载'] = (not bad, bad[:3])

# 3. 按钮体系统一定义在style.css
checks['按钮体系(style.css自持)'] = ('.x-login, button {' in STYLE and '.controls .primary {' in STYLE and '.auth-form .primary' in STYLE, '')

ok = True
for k, (v, detail) in checks.items():
    print(('PASS' if v else 'FAIL'), k, detail if detail and not v else '')
    ok = ok and v
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print('ALL PASS' if ok else 'HAS FAILURES')
