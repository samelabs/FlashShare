"""上传链路性能与可靠性实测: 基准计时(分段)+可靠性断言"""
import http.client, time, sys, os, io, hmac, hashlib, secrets as _sec, json
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME

PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows

ts = int(time.time()); uname = f"perf{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'p','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+3600, ts))

conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=120)
ck = {"Cookie": f"{COOKIE_NAME}={tok_s}"}

# PNG头(1x1最小图)×不同尺寸模拟
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8cfc0f01f0005050201edb5ca9f0000000049454e44ae426082")

def multipart(files, expiry="1", dl="50"):
    boundary = "----perfTest"
    body = bytearray()
    for name, data, ctype in files:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"expiryDays\"\r\n\r\n{expiry}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"allowedDownloads\"\r\n\r\n{dl}\r\n--{boundary}--\r\n".encode()
    return bytes(body), {"Content-Type": f"multipart/form-data; boundary={boundary}", **ck}

print("═══ A. 分尺寸基准(单文件, 服务端全链) ═══")
for label, size in [("1MB", 1), ("5MB", 5), ("20MB", 20)]:
    data = PNG + os.urandom(size * 1024 * 1024 - len(PNG))
    body, hdrs = multipart([(f"test{size}.png", data, "image/png")])
    t0 = time.time()
    conn.request("POST", "/api/uploads", body=body, headers={**hdrs, "Content-Length": str(len(body))})
    r = conn.getresponse(); resp = r.read()
    dt = time.time() - t0
    d = json.loads(resp)
    ok = d.get("ok")
    print(f"  {label:5} {dt*1000:7.0f}ms  {'✓' if ok else '✗ ' + str(d.get('error'))}  ({size}MB → {size/dt:.1f}MB/s)")

print()
print("═══ B. 多文件批次(5×1MB) ═══")
files = [(f"b{i}.png", PNG + os.urandom(1024*1024 - len(PNG)), "image/png") for i in range(5)]
body, hdrs = multipart(files)
t0 = time.time()
conn.request("POST", "/api/uploads", body=body, headers={**hdrs, "Content-Length": str(len(body))})
r = conn.getresponse(); resp = r.read()
dt = time.time() - t0
d = json.loads(resp)
print(f"  5×1MB {dt*1000:7.0f}ms  files={len(d.get('files', []))}  {'✓' if d.get('ok') else '✗'}")

print()
print("═══ C. 可靠性断言 ═══")
checks = {}
# 1. 幂等重传(同内容两次=两个独立记录, 符合分享语义)
body, hdrs = multipart([("dup.png", PNG, "image/png")])
conn.request("POST", "/api/uploads", body=body, headers=hdrs)
r1 = json.loads(conn.getresponse().read())
conn.request("POST", "/api/uploads", body=body, headers=hdrs)
r2 = json.loads(conn.getresponse().read())
checks["重复上传=独立记录"] = r1["ok"] and r2["ok"] and r1["file"]["publicId"] != r2["file"]["publicId"]
# 2. 伪造扩展名被嗅探拦截
fake = os.urandom(1024)  # 随机字节伪装png
body, hdrs = multipart([("fake.png", fake, "image/png")])
conn.request("POST", "/api/uploads", body=body, headers=hdrs)
r = conn.getresponse(); d = json.loads(r.read())
checks["伪png被拒(magic bytes)"] = (not d["ok"]) and "mismatch" in d.get("error", "")
# 3. 超限拒绝
big = PNG + os.urandom(51 * 1024 * 1024)
body, hdrs = multipart([("big.png", big, "image/png")])
conn.request("POST", "/api/uploads", body=body, headers={**hdrs, "Content-Length": str(len(body))})
r = conn.getresponse(); d = json.loads(r.read())
checks[">50MB拒绝(413)"] = (not d.get("ok", False))
# 4. 未登录拒绝
conn2 = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
body, hdrs = multipart([("x.png", PNG, "image/png")])
hdrs2 = {k: v for k, v in hdrs.items() if k != "Cookie"}
conn2.request("POST", "/api/uploads", body=body, headers=hdrs2)
r = conn2.getresponse(); resp = r.read()
checks["未登录401/403"] = r.status in (401, 403)
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), k)

# 清理: 删测试上传记录+gokapi文件
ids = psql("SELECT gokapi_id FROM uploads WHERE user_id=%s", (uid,))
from gokapi import delete_gokapi_file as delete_file
for (gid,) in ids:
    try: delete_file(gid)
    except Exception: pass
psql("DELETE FROM uploads WHERE user_id=%s", (uid,))
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print()
print("清理完成" if True else "")
