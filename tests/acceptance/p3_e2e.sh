#!/bin/sh
# Phase 3 E2E: user tool submission + visibility + settlement end-to-end
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, base64, sys, os, threading
import secrets as _sec
sys.path.insert(0, "/opt/flashshare/core")
sys.path.insert(0, "/opt/flashshare")
HOST = ("127.0.0.1", 8000)

# ── mock provider endpoint (provider = user B; https not possible on
#    loopback → we test the settlement/visibility via http by marking the
#    row trusted manually, which is exactly the operator-adoption path;
#    the SSRF pre-check is tested separately with real URLs) ──
from http.server import BaseHTTPRequestHandler, HTTPServer

class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        import urllib.request as _u
        src = payload["job_inputs"][0]
        with _u.urlopen(_u.Request(src["url"], headers={"User-Agent": "mock-provider/1.0"}), timeout=10) as r:
            data = r.read()
        png = base64.b64encode(data).decode()
        body = json.dumps({"filename": "prov_out.png", "content_type": "image/png",
                           "data_b64": png}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), Provider)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PROV_PORT = srv.server_address[1]

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
    def patch(self, path, body):
        return self.post(path, body, 'application/json') if False else self._req('PATCH', path, body)
    def _req(self, method, path, body=None):
        h = {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:8000'}
        if self.sess: h['Cookie'] = self.sess
        self.conn.request(method, path, body=body, headers=h)
        r = self.conn.getresponse(); return r.status, r.read()
    def get(self, path):
        return self._req('GET', path)
    def delete(self, path):
        return self._req('DELETE', path)

import psycopg2
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple):
        a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor()
    cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

# pre-clean residue from failed runs (idempotent)
psql("DELETE FROM platform_tools WHERE tool_key LIKE %s", "pv%/echo")
psql("DELETE FROM pricing WHERE tool_id LIKE %s", "pv%/echo")

# ── user B = provider; user A = caller ──
b = C(); ub = f"pv{int(time.time()) % 100000}"
rq, ct = mp({'username': ub, 'password': 'Test12345', 'password_confirm': 'Test12345', 'email': f'{ub}@t.co'}, [])
b.post('/auth/register', rq, ct)
a = C(); ua = f"cl{int(time.time()) % 100000}"
rq, ct = mp({'username': ua, 'password': 'Test12345', 'password_confirm': 'Test12345', 'email': f'{ua}@t.co'}, [])
a.post('/auth/register', rq, ct)

# 1. submission: https enforced → reject http url
body = json.dumps({"tool_id": "echo", "label": "Echo", "execution": "http",
                   "url": f"http://127.0.0.1:{PROV_PORT}/p", "output_filename": "out.png", "cost": 10, "category": "image"}).encode()
st, d = b.post('/api/usertools', body, 'application/json')
assert st == 422 and json.loads(d)["error"] == "https_required", (st, d[:120])
print("1. http url rejected (https_required) OK")

# 2. submission: private IP https → ssrf reject
body = json.dumps({"tool_id": "echo", "label": "Echo", "execution": "http",
                   "url": "https://192.168.1.1/x", "category": "image", "output_filename": "out.png", "cost": 10}).encode()
st, d = b.post('/api/usertools', body, 'application/json')
assert st == 422 and json.loads(d)["error"] == "https_required", (st, d[:120])
print("2. private-ip https rejected OK")

# 3. valid submission (public https host that resolves; driver won't be called
#    until we flip trusted for the local test endpoint)
body = json.dumps({"tool_id": "echo", "label": "Echo", "execution": "http",
                   "url": "https://example.com/x", "category": "image", "output_filename": "out.png", "cost": 10}).encode()
st, d = b.post('/api/usertools', body, 'application/json')
r = json.loads(d)
assert r["ok"] and r["tool_key"] == f"{ub}/echo", (st, d[:150])
row = psql("SELECT visibility, trusted FROM platform_tools WHERE tool_key=%s", r["tool_key"])[0]
assert row[0] == "private" and row[1] is False
print(f"3. submission OK → {r['tool_key']} (private, untrusted)")

# 4. duplicate rejected
st, d = b.post('/api/usertools', body, 'application/json')
assert st == 409 and json.loads(d)["error"] == "duplicate_tool"
print("4. duplicate rejected OK")

# 5. visibility: private→public without self-test → rejected
body = json.dumps({"visibility": "public"}).encode()
st, d = b._req('PATCH', f'/api/usertools/echo', body)
assert st == 422 and json.loads(d)["error"] == "self_test_required", (st, d[:120])
print("5. public without self-test rejected OK")

# 6. cross-user isolation: A cannot patch/delete B's tool
st, d = a._req('PATCH', '/api/usertools/echo', json.dumps({"visibility": "unlisted"}).encode())
assert st == 404, st
st, d = a.delete('/api/usertools/echo')
assert st == 404, st
print("6. cross-user isolation OK")

# 7. visibility ladder private→unlisted OK
st, d = b._req('PATCH', '/api/usertools/echo', json.dumps({"visibility": "unlisted"}).encode())
print('PATCH resp:', st, d[:200])
assert json.loads(d)["ok"], d[:200]
# caller A executes it (unlisted = callable): point to local mock via trusted flip
_ep = json.dumps({"url": f"http://127.0.0.1:{PROV_PORT}/p", "timeout_s": 30,
                  "output_filename": "out.png", "output_content_type": "image/png"})
_key = f"{ub}/echo"
print('UPDATE args:', repr(_ep)[:80], repr(_key))
try:
    psql("UPDATE platform_tools SET endpoint_json = %s, trusted = TRUE WHERE tool_key = %s", _ep, _key)
except Exception as ex:
    import traceback; traceback.print_exc()
png1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
rq, ct = mp({'expiryDays': '1', 'allowedDownloads': '10', 'tool': f'{ub}/echo'}, [('t.png', png1, 'image/png')])
st, d = a.post('/api/uploads', rq, ct)
f = json.loads(d)['file']
req = json.dumps({'tool_id': f'{ub}/echo', 'public_id': f['publicId'], 'source_token': f['sourceToken'],
                  'client_ref': f'p3-{ua}-1', 'params': {}}).encode()
st, d = a.post('/api/tools/execute', req, 'application/json')
res = json.loads(d)
assert res['ok'], (st, d[:500])
print(f"7. unlisted cross-user execute OK: {res['result']['publicId']}")

# 8. settlement: provider earned cost-commission in SAME tx
print('DEBUG res:', json.dumps(res)[:200])
comm = 30
earn = 10 - (10 * comm // 100)  # 7
rows = psql("SELECT kind, delta FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s AND kind='provider_earn'", ub)
assert rows and rows[0][1] == earn, rows
bal_b = psql("SELECT COALESCE(SUM(delta),0) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", ub)[0][0]
assert bal_b == 5 + earn, f"provider balance {bal_b} != {5+earn}"  # daily5 + 7
print(f"8. settlement OK: provider earned {earn} (cost 10, commission {comm}%), balance {bal_b}")

# 9. replay does NOT double-settle
req2 = json.dumps({'tool_id': f'{ub}/echo', 'public_id': 'x', 'params': {}}).encode()
# replay with same client_ref needs valid source — use recorded replay path
st, d = a.post('/api/tools/execute', json.dumps({'tool_id': f'{ub}/echo', 'public_id': f['publicId'], 'source_token': f['sourceToken'], 'client_ref': f'p3-{ua}-1', 'params': {}}).encode(), 'application/json')
res2 = json.loads(d)
assert res2.get('replay'), d[:150]
rows = psql("SELECT COUNT(*) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s AND kind='provider_earn'", ub)
assert rows[0][0] == 1, rows
print("9. replay → no double settlement OK")

# 10. failed call → refund, no payout
psql("UPDATE platform_tools SET endpoint_json=%s WHERE tool_key=%s",
     (json.dumps({"url": "https://example.com/nonexistent-path-xyz", "timeout_s": 10,
                  "category": "image", "output_filename": "out.png"}), f"{ub}/echo"))
rq, ct = mp({'expiryDays': '1', 'allowedDownloads': '10', 'tool': f'{ub}/echo'}, [('t.png', png1, 'image/png')])
st, d = a.post('/api/uploads', rq, ct)
f2 = json.loads(d)['file']
st, d = a.post('/api/tools/execute', json.dumps({'tool_id': f'{ub}/echo', 'public_id': f2['publicId'], 'source_token': f2['sourceToken'], 'client_ref': f'p3-{ua}-2', 'params': {}}).encode(), 'application/json')
res3 = json.loads(d)
assert not res3['ok'], d[:150]
bal_a = psql("SELECT COALESCE(SUM(delta),0) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s", ua)[0][0]
assert bal_a == 100 + 5 - 10, f"caller {bal_a} != 95 (failed refunded)"
rows = psql("SELECT COUNT(*) FROM ledger l JOIN users us ON l.user_id=us.id WHERE us.username=%s AND kind='provider_earn'", ub)
assert rows[0][0] == 1
print("10. failed call → refund, no payout OK")

# 11. MCP projection: flip to public (self-test satisfied by the ok call in step 7),
#     then namespaced tool appears in EVERY caller's tools/list as owner__tool
st, d = b._req('PATCH', '/api/usertools/echo', json.dumps({"visibility": "public"}).encode())
assert json.loads(d)["ok"], d[:150]
psql("UPDATE platform_tools SET endpoint_json=%s WHERE tool_key=%s",
     (json.dumps({"url": f"http://127.0.0.1:{PROV_PORT}/p", "timeout_s": 30,
                  "output_filename": "out.png", "output_content_type": "image/png"}), f"{ub}/echo"))
from apitokens import create_token
uid_a = psql("SELECT id FROM users WHERE username=%s", ua)[0][0]
tok = create_token(uid_a, "p3-e2e")
conn = http.client.HTTPConnection(*HOST, timeout=30)
rpc = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
conn.request("POST", "/mcp", body=rpc, headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok["token"]})
r = conn.getresponse(); out = json.loads(r.read())
names = [t["name"] for t in out["result"]["tools"]]
proj = f"{ub}__echo"
assert proj in names, names
print(f"11. MCP name projection {proj} in tools/list OK")

# 12. owner page renders + owner API lists the tool
st, d = b.get('/account/tools')
assert st == 200 and b'toolList' in d, st
st, d = b.get('/api/usertools')
lst = json.loads(d)
assert lst['ok'] and any(t['tool_key'] == ub + '/echo' for t in lst['tools']), lst
print("12. /account/tools renders OK")

# 13. delete
st, d = b.delete('/api/usertools/echo')
assert json.loads(d)["ok"]
assert not psql("SELECT 1 FROM platform_tools WHERE tool_key=%s", f"{ub}/echo")
print("13. delete OK")

# cleanup
from tests.e2e_cleanup import cleanup_user
cleanup_user(ub); cleanup_user(ua)
print("cleanup done")
print("ALL PHASE-3 C2C E2E PASSED")
PYEOF
