#!/bin/sh
# Phase 1 acceptance: web execute chain (upload→execute→link) via session
set -e
set -a; . /etc/flashshare.env; set +a
/usr/bin/python3 - <<'PYEOF'
import http.client, io, json, time, zipfile, sys, os, base64

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

c = C()
u = f"p1{int(time.time()) % 100000}"
b, ct = mp({'username': u, 'password': 'Test12345', 'password_confirm': 'Test12345', 'email': f'{u}@t.co'}, [])
c.post('/auth/register', b, ct)
b, ct = mp({'login': u, 'password': 'Test12345'}, [])
c.post('/auth/login', b, ct)

# tiny PNG
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

# 1. upload with tool=compress (tool flow: gokapi intermediate + sourceToken)
b, ct = mp({'expiryDays': '1', 'allowedDownloads': '50', 'tool': 'compress'}, [('t.png', png, 'image/png')])
st, d = c.post('/api/uploads', b, ct)
up = json.loads(d)
assert st == 200 and up['ok'], (st, d[:200])
f = up['file']
print("1. upload OK:", f['publicId'], f['contentType'], f['size'])

# 2. execute
body = json.dumps({"tool_id": "compress", "public_id": f['publicId'], "source_token": f['sourceToken'], "params": {"preset": "2mb"}}).encode()
st, d = c.post('/api/tools/execute', body, 'application/json')
out = json.loads(d)
assert st == 200 and out['ok'], (st, d[:300])
print("2. execute OK:", out['result']['publicId'], out['result']['contentType'], out['result']['size'])

# 3. multi-file tool (zip_pack) — regression on multi path
b, ct = mp({'expiryDays': '1', 'allowedDownloads': '50', 'tool': 'zip_pack'}, [('a.txt', b'hello', 'text/plain'), ('b.txt', b'world', 'text/plain')])
st, d = c.post('/api/uploads', b, ct)
up2 = json.loads(d)
assert st == 200 and up2['ok'], (st, d[:200])
files = up2['files']
body = json.dumps({"tool_id": "zip_pack", "public_ids": [x['publicId'] for x in files], "source_tokens": [x['sourceToken'] for x in files], "params": {}}).encode()
st, d = c.post('/api/tools/execute', body, 'application/json')
out2 = json.loads(d)
assert st == 200 and out2['ok'], (st, d[:300])
print("3. multi zip_pack OK:", out2['result']['publicId'], out2['result']['contentType'], out2['result']['size'])

# 4. share flow (no tool) — DB record path
b, ct = mp({'expiryDays': '1', 'allowedDownloads': '50'}, [('s.txt', b'shareme', 'text/plain')])
st, d = c.post('/api/uploads', b, ct)
up3 = json.loads(d)
assert st == 200 and up3['ok'], (st, d[:200])
print("4. share upload OK:", up3['file']['publicId'])

# 5. unknown tool → 404
body = json.dumps({"tool_id": "no_such", "public_id": f['publicId'], "params": {}}).encode()
st, d = c.post('/api/tools/execute', body, 'application/json')
assert st == 404, (st, d[:100])
print("5. unknown tool 404 OK")

# 6. detail page renders
st, d = c.get(out["result"]["downloadUrl"].replace('https://518.plus', ''))
assert st == 200 and b'download' in d.lower(), st
print("6. detail page OK")

# cleanup test user (uploads + user cascade)
sys.path.insert(0, '/opt/flashshare/core'); sys.path.insert(0, '/opt/flashshare')
from tests.e2e_cleanup import cleanup_user
import subprocess
cleanup_user(u)
print("cleanup done")
print("ALL PHASE-1 ACCEPTANCE CHECKS PASSED")
PYEOF
