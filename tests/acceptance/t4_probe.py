import http.client, time, sys, os, json, hmac, hashlib, threading
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2, secrets as _sec
from config import SESSION_SECRET, COOKIE_NAME
from security import sign_source_token
from http.server import HTTPServer, BaseHTTPRequestHandler

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, *a):
    if len(a) == 1 and isinstance(a[0], tuple): a = a[0]
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6300010000050001')
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(ln)  # consume payload (source URLs etc.)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(PNG)
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(PNG)
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

ts = int(time.time())
uname = f"tl{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'t4','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO ledger (user_id, delta, balance_after, kind, ref_type, ref_id, idem_key, note, created_at) VALUES(%s,200,200,'manual_topup','probe','probe',%s,'',%s)", (uid, f"probe:{uid}", ts))
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=60)
ck = f"{COOKIE_NAME}={tok_s}; flashshare_lang=zh"

# 0. submission SSRF gate honest (loopback http rejected for user rows)
body = json.dumps({"tool_id": "edittest", "label": "Edit Test", "url": f"http://127.0.0.1:{PORT}/echo", "output_filename": "out.png", "cost": 3})
conn.request("POST", "/api/usertools", body=body, headers={"Cookie": ck, "Content-Type": "application/json"})
d = json.loads(conn.getresponse().read())
c0 = (not d["ok"]) and d["error"] == "https_required"
print(("PASS" if c0 else "FAIL"), "0. submission SSRF gate (loopback rejected)")

# 0b. seed trusted row (operator-adoption semantics)
tk = f"{uname}/edittest"
psql("INSERT INTO platform_tools (tool_key, owner_id, tool_id, execution, tool_type, label_json, visibility, endpoint_json, enabled, trusted, created_at, updated_at) VALUES(%s,%s,'edittest','http','produce',%s,'private',%s,TRUE,TRUE,%s,%s)",
     (tk, uid, json.dumps({"en": "Edit Test", "zh": "Edit Test"}), json.dumps({"url": f"http://127.0.0.1:{PORT}/echo", "timeout_s": 30, "output_filename": "out.png"}), ts, ts))
psql("INSERT INTO pricing (tool_id, cost, enabled, updated_by, updated_at) VALUES(%s,3,TRUE,%s,%s)", (tk, uid, ts))

# 1. EDIT: PATCH label+cost (trusted row: url re-check exempt)
body = json.dumps({"label": "Edit Test v2", "cost": 7, "url": f"http://127.0.0.1:{PORT}/echo"})
conn.request("PATCH", "/api/usertools/edittest", body=body, headers={"Cookie": ck, "Content-Type": "application/json"})
r = conn.getresponse(); d = json.loads(r.read())
row = psql("SELECT label_json FROM platform_tools WHERE tool_key=%s", (tk,))[0][0]
cost = int(psql("SELECT cost FROM pricing WHERE tool_id=%s", (tk,))[0][0])
c1 = r.status == 200 and d["ok"] and "Edit Test v2" in row and cost == 7
print(("PASS" if c1 else "FAIL"), "1. edit PATCH: label+cost persisted (trusted url exempt)")

# 2. SELF-TEST against trusted row: upload → real pipeline call
boundary = secrets_hex = __import__("uuid").uuid4().hex
payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"expiryDays\"\r\n\r\n1\r\n"
           f"--{boundary}\r\nContent-Disposition: form-data; name=\"allowedDownloads\"\r\n\r\n10\r\n"
           f"--{boundary}\r\nContent-Disposition: form-data; name=\"tool\"\r\n\r\n{tk}\r\n"
           f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.png\"\r\nContent-Type: image/png\r\n\r\n").encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
conn.request("POST", "/api/uploads", body=payload, headers={"Cookie": ck, "Content-Type": f"multipart/form-data; boundary={boundary}"})
up = json.loads(conn.getresponse().read())
finfo = (up.get("files") or [up.get("file")])[0]
pid, stok = finfo["publicId"], finfo.get("sourceToken", "")
if not stok:
    gid = psql("SELECT gokapi_id FROM uploads WHERE public_id=%s", (pid,))
    stok = sign_source_token(uid, gid[0][0] if gid else "")
conn.request("POST", "/api/usertools/edittest/selftest",
             body=json.dumps({"public_id": pid, "source_token": stok}),
             headers={"Cookie": ck, "Content-Type": "application/json"})
r = conn.getresponse(); d2 = json.loads(r.read())
c2 = d2.get("ok") and isinstance(d2.get("ms"), int)
print(("PASS" if c2 else "FAIL"), f"2. selftest real pipeline: ok={d2.get('ok')} ms={d2.get('ms')}")

# 3. self-test gate: after 1 ok self-call, public flip allowed
conn.request("PATCH", "/api/usertools/edittest", body=json.dumps({"visibility": "public"}),
             headers={"Cookie": ck, "Content-Type": "application/json"})
r = conn.getresponse(); d3 = json.loads(r.read())
c3 = d3.get("ok")
print(("PASS" if c3 else "FAIL"), "3. self-test gate satisfied by selftest button call")

# 4. DISABLE
conn.request("PATCH", "/api/usertools/edittest", body=json.dumps({"enabled": False}),
             headers={"Cookie": ck, "Content-Type": "application/json"})
r = conn.getresponse(); d4 = json.loads(r.read())
en = psql("SELECT enabled FROM platform_tools WHERE tool_key=%s", (tk,))[0][0]
conn.request("GET", f"/api/tools/search?q=edittest", headers={"Cookie": ck})
sr = json.loads(conn.getresponse().read())
gone = not any(t["id"] == tk for t in sr["tools"])
c4 = d4["ok"] and en is False and gone
print(("PASS" if c4 else "FAIL"), "4. disable: row off + out of search")

psql("DELETE FROM pricing WHERE tool_id=%s", (tk,))
psql("DELETE FROM tool_calls WHERE tool_id=%s", (tk,))
psql("DELETE FROM platform_tools WHERE tool_key=%s", (tk,))
psql("DELETE FROM ledger WHERE user_id=%s", (uid,))
psql("DELETE FROM uploads WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
srv.shutdown()
ok = all([c0, c1, c2, c3, c4])
print("ALL PASS" if ok else "HAS FAILURES")
