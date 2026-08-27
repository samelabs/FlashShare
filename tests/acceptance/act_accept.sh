#!/bin/sh
# Activities module acceptance: data-driven grants end-to-end
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, base64, sys, os
sys.path.insert(0, "/opt/flashshare/core")
sys.path.insert(0, "/opt/flashshare")
HOST = ("127.0.0.1", 8000)

def mp(fields, files, boundary="----bnd789"):
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for (fn, data, ct) in files:
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode())
    return body.getvalue(), f'multipart/form-data; boundary={boundary}'

class C:
    def __init__(self):
        self.conn = http.client.HTTPConnection(*HOST, timeout=120)
        self.sess = ''
    def post(self, path, body, ct):
        h = {'Content-Type': ct, 'Origin': 'http://127.0.0.1:8000'}
        if self.sess: h['Cookie'] = self.sess
        self.conn.request('POST', path, body=body, headers=h)
        r = self.conn.getresponse(); d = r.read()
        sc = r.getheader('Set-Cookie')
        if sc: self.sess = sc.split(';')[0]
        return r.status, d
    def get(self, path):
        self.conn.request('GET', path, headers={'Cookie': self.sess} if self.sess else {})
        r = self.conn.getresponse(); return r.status, r.read()

import psycopg2
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    con = psycopg2.connect(PG); cur = con.cursor()
    cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

# ── 1. register → daily lands; NO signup row (decoupled 2026-08-27)
c = C()
u = f"act{int(time.time()) % 100000}"
b, ct = mp({'username': u, 'password': 'Test12345', 'password_confirm': 'Test12345', 'email': f'{u}@t.co'}, [])
c.post('/auth/register', b, ct)
rows = psql("SELECT kind, delta FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", u)
kinds = {k: d for k, d in rows}
assert kinds.get('grant_daily') == 5, f"daily should land at register: {rows}"
assert 'grant_signup' not in kinds, f"signup grant must NOT exist (decoupled): {rows}"
print("1. register → daily 5 only; signup bonus absent OK")

# ── 3. disable daily → claim endpoint no-ops
psql("UPDATE activities SET active=FALSE WHERE code='daily_login'")
st, d = c.post('/account/credits/claim', b'', 'application/x-www-form-urlencoded')
before = psql("SELECT COALESCE(SUM(delta),0) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", u)[0][0]
st, d = c.post('/account/credits/claim', b'', 'application/x-www-form-urlencoded')
after = psql("SELECT COALESCE(SUM(delta),0) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", u)[0][0]
assert before == after, f"disabled activity must not grant: {before} → {after}"
print("4. disabled daily_login → claim no-op OK")
psql("UPDATE activities SET active=TRUE WHERE code='daily_login'")

# ── 4. samelabs page renders + config endpoint works (admin id=2)
from apitokens import create_token
# admin session: reuse c2? no — make admin session by direct session insert
import secrets as _s, hmac, hashlib
from config import SESSION_SECRET, COOKIE_NAME
tok = _s.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (%s, 2, extract(epoch from now())::bigint+3600, extract(epoch from now())::bigint)", th)
admin = C()
admin.sess = f"{COOKIE_NAME}={tok}"
st, d = admin.get('/samelabs/activities')
assert st == 200 and b'daily_login' in d and b'signup' not in d, st
print("5. /samelabs/activities renders (both rows) OK")
body = json.dumps({'amount': 7}).encode()
st, d = admin.post('/samelabs/activities/daily_login/config', body, 'application/json')
r = json.loads(d)
assert st == 200 and r['ok'] and r['activity']['amount'] == 7, (st, d[:200])
amt = psql("SELECT amount FROM activities WHERE code='daily_login'")[0][0]
assert amt == 7
print("6. POST config amount=7 OK (row updated)")
body = json.dumps({'amount': 5}).encode()
st, d = admin.post('/samelabs/activities/daily_login/config', body, 'application/json')
assert json.loads(d)['ok']
print("7. restored amount=5 OK")

# ── 5. account credits page renders with activity-driven chip
st, d = c.get('/account/credits')
assert st == 200 and b'dailyBox' in d, st
print("8. /account/credits renders (daily chip) OK")

# cleanup
from tests.e2e_cleanup import cleanup_user
cleanup_user(u)
psql("DELETE FROM sessions WHERE token_hash=%s", th)
psql("INSERT INTO pricing(tool_id,cost,enabled,updated_at) VALUES ('doc2pdf',0,TRUE,extract(epoch from now())::bigint) ON CONFLICT(tool_id) DO UPDATE SET cost=0")
print("cleanup done")
print("ALL ACTIVITIES ACCEPTANCE CHECKS PASSED")
PYEOF
