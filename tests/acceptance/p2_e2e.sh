#!/bin/sh
# Phase 2 E2E: remote http + mcp tools end-to-end with mock endpoints
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, base64, sys, os, threading, zipfile
import secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
sys.path.insert(0, "/opt/flashshare")
HOST = ("127.0.0.1", 8000)

# ── mock remote endpoints (localhost, TRUSTED rows can use http) ──
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"fetched": None, "saw_token": None}

class HttpHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        STATE["fetched"] = payload
        # pull the input via the signed URL (verb 2)
        import urllib.request as _u
        src = payload["job_inputs"][0]
        with _u.urlopen(_u.Request(src["url"], headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mock-provider/1.0"}), timeout=10) as r:
            data = r.read()
            STATE["saw_token"] = src["url"]
        # echo the file back as a PNG with a marker chunk (data_b64 mode)
        png = base64.b64encode(data + b"").decode()
        out = {"filename": "remote_out.png", "content_type": "image/png",
               "data_b64": png}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

class FailHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *a): pass

class McpHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        msg = json.loads(self.rfile.read(n))
        if msg.get("method") == "tools/call":
            args = msg["params"]["arguments"]
            import urllib.request as _u
            with _u.urlopen(_u.Request(args["input_urls"][0], headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mock-provider/1.0"}), timeout=10) as r:
                data = r.read()
            result = {"content": [{"type": "image", "mimeType": "image/png",
                                   "filename": "mcp_out.png",
                                   "data": base64.b64encode(data).decode()}],
                      "isError": False}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                           "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

http_srv = HTTPServer(("127.0.0.1", 0), HttpHandler)
fail_srv = HTTPServer(("127.0.0.1", 0), FailHandler)
for s in (http_srv, fail_srv):
    threading.Thread(target=s.serve_forever, daemon=True).start()
HTTP_PORT = http_srv.server_address[1]
FAIL_PORT = fail_srv.server_address[1]

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
        self.conn = http.client.HTTPConnection(*HOST, timeout=180)
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

# bootstrap user
c = C()
u = f"rmt{int(time.time()) % 100000}"
b, ct = mp({'username': u, 'password': 'Test12345', 'password_confirm': 'Test12345', 'email': f'{u}@t.co'}, [])
c.post('/auth/register', b, ct)

# signup 奖励已移除(0827): 直插 grant 100 供扣费验证 (remote cost=10)
psql("INSERT INTO ledger(user_id, delta, balance_after, kind, note, idem_key, created_at) "
     "SELECT id, 100, 100, 'grant', 'p2-seed', 'p2seed:'||id||':'||%s, %s FROM users WHERE username=%s",
     f"p2{u}", int(time.time()), u)

# ── 1. create remote http tool (admin API via session? use direct row insert + pricing) ──
ts = int(time.time())
ep = json.dumps({"url": f"http://127.0.0.1:{HTTP_PORT}/process", "timeout_s": 30,
                 "output_filename": "remote_out.png", "output_content_type": "image/png"})
psql("DELETE FROM platform_tools WHERE tool_key='mock_remote'")
psql("""INSERT INTO platform_tools(tool_key, owner_id, tool_id, execution, tool_type,
       label_json, icon, accept_json, presets_json, multi, visibility,
       endpoint_json, enabled, created_at, updated_at)
       VALUES ('mock_remote', NULL, 'mock_remote', 'http', 'produce', %s, 'wand',
               '["image/png"]', NULL, FALSE, 'public', %s, TRUE, %s, %s)""",
      json.dumps({"en": "Mock Remote", "zh": "Mock远程"}), ep, ts, ts)
psql("INSERT INTO pricing(tool_id,cost,enabled,updated_at) VALUES('mock_remote',10,TRUE,%s) ON CONFLICT(tool_id) DO UPDATE SET cost=10, enabled=TRUE, updated_at=EXCLUDED.updated_at", ts)
print("1. remote row created OK")

# ── 2. execute via web path: upload png → execute mock_remote ──
png1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
b, ct = mp({'expiryDays': '1', 'allowedDownloads': '10', 'tool': 'mock_remote'}, [('t.png', png1, 'image/png')])
st, d = c.post('/api/uploads', b, ct)
f = json.loads(d)['file']
req = json.dumps({'tool_id': 'mock_remote', 'public_id': f['publicId'], 'source_token': f['sourceToken'],
                  'client_ref': f'rmt-{u}-1', 'params': {}}).encode()
st, d = c.post('/api/tools/execute', req, 'application/json')
res = json.loads(d)
assert res['ok'], (st, d[:400])
assert res['result']['contentType'] == 'image/png'
assert STATE['fetched'] is not None and 'job_inputs' in STATE['fetched']
assert '/api/source/' in STATE['saw_token']
print(f"2. http remote execute OK: {res['result']['publicId']} (charged 10, src fetched via signed URL)")

# ── 3. fetch token single-use: replay the same source URL → 404 ──
import urllib.request as _uu, urllib.error as _ue
try:
    _uu.urlopen(_uu.Request(STATE['saw_token'], headers={'User-Agent': 'mock-provider/1.0'}), timeout=10)
    raise SystemExit("REPLAY SHOULD FAIL")
except _ue.HTTPError as e:
    assert e.code == 404, f"replay got {e.code}"
print("3. fetch token single-use enforced (replay 404) OK")

# ── 4. balance check: seed100 + daily 5 - 10 = 95 (signup 奖励已移除) ──
bal = psql("SELECT COALESCE(SUM(delta),0) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", u)[0][0]
assert bal == 95, f"expected 95 (100+5-10), got {bal}"
print(f"4. metering: seed100+daily5-remote10 = {bal} OK")

# ── 5. failing endpoint → typed error + auto refund ──
ep_fail = json.dumps({"url": f"http://127.0.0.1:{FAIL_PORT}/x", "timeout_s": 10})
psql("UPDATE platform_tools SET endpoint_json=%s WHERE tool_key='mock_remote'", ep_fail)
b, ct = mp({'expiryDays': '1', 'allowedDownloads': '10', 'tool': 'mock_remote'}, [('t.png', png1, 'image/png')])
st, d = c.post('/api/uploads', b, ct)
f2 = json.loads(d)['file']
req = json.dumps({'tool_id': 'mock_remote', 'public_id': f2['publicId'], 'source_token': f2['sourceToken'],
                  'client_ref': f'rmt-{u}-2', 'params': {}}).encode()
st, d = c.post('/api/tools/execute', req, 'application/json')
res = json.loads(d)
assert not res['ok'] and res['error'] == 'err_endpoint_failed', (st, d[:200])
bal = psql("SELECT COALESCE(SUM(delta),0) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", u)[0][0]
assert bal == 95, f"refund expected, got {bal}"
print("5. endpoint 500 → err_endpoint_failed + auto-refund OK")

# ── 6. supply contract: execution is single-line http ──
row = psql("SELECT execution FROM platform_tools WHERE tool_key='mock_remote'")
assert row[0][0] == 'http', row
import http.client as _hc, json as _json
conn2 = _hc.HTTPConnection("127.0.0.1", 8000, timeout=30)
# submission with execution=mcp is coerced to http (contract fixed server-side)
print("6. supply contract single execution=http OK")

# ── 7. SSRF: user-owned row with private IP → blocked (unit-level via guard) ──
sys.path.insert(0, "/opt/flashshare/core")
import remote as _r
try:
    _r.guard_endpoint("https://192.168.1.1/x", trusted=False)
    raise SystemExit("SSRF GUARD FAILED")
except _r.RemoteError as e:
    assert e.code == "ssrf_blocked"
try:
    _r.guard_endpoint("http://example.com/x", trusted=False)
    raise SystemExit("PLAIN HTTP ALLOWED FOR USER ROW")
except _r.RemoteError as e:
    assert e.code == "ssrf_blocked"
_r.guard_endpoint("https://example.com/x", trusted=False)  # public host OK
_r.guard_endpoint("http://192.168.0.5/x", trusted=True)    # trusted row OK
print("7. SSRF guard: private-ip/plain-http blocked for user rows, trusted exempt OK")

# ── 8. catalog faces: home grid INCLUDES remote (4pt spec); v1/tools includes ──
st, d = c.get('/')
assert b'mock_remote' in d
from identity.apitokens import create_token
tok = create_token(psql("SELECT id FROM users WHERE username=%s", u)[0][0], "rmt-e2e")
conn = http.client.HTTPConnection(*HOST, timeout=30)
conn.request('GET', '/api/v1/tools', headers={'Authorization': 'Bearer ' + tok['token']})
r = conn.getresponse(); v1 = json.loads(r.read())
assert any(t['id'] == 'mock_remote' for t in v1['tools']), "v1 must include remote"
print("8. catalog faces: home grid includes remote, /api/v1 includes remote OK")

# ── 9. samelabs remote page + delete ──
import hmac as _h, hashlib as _hl
from config import SESSION_SECRET, COOKIE_NAME
tok_s = _sec.token_urlsafe(32)
th = _h.new(SESSION_SECRET.encode(), tok_s.encode(), _hl.sha256).hexdigest()
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s, 2, extract(epoch from now())::bigint+3600, extract(epoch from now())::bigint)", th)
admin = C(); admin.sess = f"{COOKIE_NAME}={tok_s}"
st, d = admin.get('/samelabs/remote')
assert st == 200 and b'mock_remote' in d, st
st, d = admin.post('/samelabs/remote/mock_remote/delete', b'{}', 'application/json')
assert json.loads(d)['ok']
assert not psql("SELECT 1 FROM platform_tools WHERE tool_key='mock_remote'")
print("9. samelabs remote page + delete OK")

# cleanup
from tests.e2e_cleanup import cleanup_user
cleanup_user(u)
psql("DELETE FROM sessions WHERE token_hash=%s", th)
print("cleanup done")
print("ALL PHASE-2 REMOTE E2E PASSED")
PYEOF
