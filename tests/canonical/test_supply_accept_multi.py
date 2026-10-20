#!/usr/bin/env python3
"""4C-2-close 验收 A-F"""
import json, uuid, http.client, sys
import os
import os as _os, sys as _sys
_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
from tests.support.cleanup import purge_users  # noqa: E402

# ── FAIL-CLOSED GATE ─────────────────────────────────────────────────────
# Canonical integration tests run ONLY against an explicitly provided
# isolated service port + isolated test database. No defaults, no
# production fallbacks. Refuse to run before any HTTP request or DB write.
_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required (isolated service port, never the production port).\n")
    _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer port number.\n")
    _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port; canonical tests must use an isolated port.\n")
    _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required (isolated test database DSN).\n")
    _sys.exit(2)
_HOST = ("127.0.0.1", _TEST_PORT_INT)

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

def mp(files=None, fields=None):
    out = b""; fields = fields or {}
    for k, v in fields.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, ct, data in (files or []):
        out += f'--B\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + data + b"\r\n"
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"

def new_user():
    conn = http.client.HTTPConnection(*_HOST, timeout=30)
    u = "cc" + uuid.uuid4().hex[:6]
    b, ct = mp(fields={"username": u, "password": "Test12345", "password_confirm": "Test12345", "email": f"{u}@t.co"})
    conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read()
    b, ct = mp(fields={"login": u, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    resp = conn.getresponse(); resp.read()
    conn.close()
    return (resp.getheader("Set-Cookie") or "").split(";")[0]

def req(method, path, body, cookie=None):
    conn = http.client.HTTPConnection(*_HOST, timeout=30)
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    conn.request(method, path, json.dumps(body), h)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data

# ── A: user accept 语义 ──
cookie = new_user()
base = {"label": {"en": "Acc"}, "execution": "http",
        "url": "https://93.184.216.34/anything", "timeout_s": 30,
        "visibility": "private", "format_families": "document", "params": []}

# A1: 无 accept → bad_accept (accept 是显式必填 contract, 不猜全族)
st, r = req("POST", "/api/usertools", base, cookie)
check("A1 缺失 accept → 422 bad_accept",
      st == 422 and r.get("error") == "bad_accept", f"{st} {r}")

# A2: accept=[] → bad_accept
st, r = req("POST", "/api/usertools", dict(base, accept=[]), cookie)
check("A2 accept=[] → 422 bad_accept", st == 422 and r.get("error") == "bad_accept", f"{st} {r}")

# A2b: accept=None → bad_accept (同缺失, 不兼容)
st, r = req("POST", "/api/usertools", dict(base, accept=None), cookie)
check("A2b accept=None → 422 bad_accept",
      st == 422 and r.get("error") == "bad_accept", f"{st} {r}")

# A3: accept=[".pdf"] → [".pdf"]
st, r = req("POST", "/api/usertools", dict(base, accept=[".pdf"]), cookie)
check("A3 accept=['.pdf'] → 创建 ok", st == 200 and r.get("ok"), f"{st} {r}")
conn = http.client.HTTPConnection(*_HOST, timeout=30)
conn.request("GET", "/api/usertools", None, {"Cookie": cookie})
t4 = [x for x in json.loads(conn.getresponse().read())["tools"] if x["tool_id"] == r.get("tool_key")][0]
conn.close()
check("A3b 存库精确 ['.pdf']", t4["accept"] == [".pdf"], str(t4["accept"]))

# ── C: operator multi 严格 boolean (operator 端点需 admin — 直接测路由行为
#    通过 samelabs/remote/create 需要 admin session; 用 admin 账号) ──
# admin 登录 (id2 ainotebook / 后台账号): 从 env/惯例拿 — samelabs 有独立鉴权?
# samelabs 鉴权: session gate (users.status='samelabs') — S4 起路由在
# app/routes/samelabs/ 包内, 无需源内省

# operator 测试账号 op4ctest (验收脚本自建, 尾部清理)。
# 上批清理后账号已删 → 脚本内重建 (注册普通号 → SQL 升 status='samelabs')。
conn = http.client.HTTPConnection(*_HOST, timeout=30)
u, pw = "op4ctest", "Test12345"
b, ct = mp(fields={"username": u, "password": pw, "password_confirm": pw, "email": "op4c@t.co"})
conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read()
import psycopg2 as _pg0
_c0 = _pg0.connect(TEST_DB); _c0.autocommit = True
_c0.cursor().execute("update users set status='samelabs' where username=%s", (u,))
_c0.close(); print("operator upgraded (direct DB)")
b, ct = mp(fields={"login": u, "password": pw})
conn.request("POST", "/auth/login", b, {"Content-Type": ct})
resp = conn.getresponse(); resp.read()
admin_cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
conn.close()
print("operator login:", "ok" if admin_cookie else "fail")

if admin_cookie:
    # C: files_max 类型矩阵 (IO-1)
    for val, want in [(5, "ok"), (20, "ok"), ("true", "bad_files_max"),
                      (True, "bad_files_max"), (21, "bad_files_max"),
                      (0, "bad_files_range"), (-1, "bad_files_max")]:
        st, r = req("POST", "/samelabs/tools/create", {
            "tool_id": f"mt{uuid.uuid4().hex[:6]}", "label": "MT",
            "url": "https://93.184.216.34/anything", "timeout_s": 30,
            "format_families": "image", "accept": ".png,.jpg", "files_max": val}, admin_cookie)
        got = "ok" if r.get("ok") else r.get("error")
        check(f"C files_max={val!r} → {want}", got == want, f"{st} {r}")
    # C2: text-only window (files 0..0) is legal only without accept
    for acc, want in [("", "ok"), (".png", "bad_accept")]:
        st, r = req("POST", "/samelabs/tools/create", {
            "tool_id": f"mt{uuid.uuid4().hex[:6]}", "label": "MT",
            "url": "https://93.184.216.34/anything", "timeout_s": 30,
            "accept": acc, "files_min": 0, "files_max": 0}, admin_cookie)
        got = "ok" if r.get("ok") else r.get("error")
        check(f"C2 text-only accept={acc!r} → {want}", got == want,
              f"{st} {r}")
    # D: accept=".png,.jpg" files_max=20 → DB canonical + bounds
    tid = "dbcheck" + uuid.uuid4().hex[:5]
    st, r = req("POST", "/samelabs/tools/create", {
        "tool_id": tid, "label": "DB", "url": "https://93.184.216.34/anything",
        "timeout_s": 30, "format_families": "image", "accept": ".png,.jpg", "files_max": 20}, admin_cookie)
    check("D operator create ok", r.get("ok"), str(r))
    import psycopg2 as _pg2, json as _js
    _c2 = _pg2.connect(TEST_DB); _c2.autocommit = True
    _cur2 = _c2.cursor(); _cur2.execute(f"select accept_json, files_max from platform_tools where tool_key='{tid}'")
    row = _cur2.fetchone(); _c2.close()
    check("D DB canonical ['.jpg','.png'] + files_max=20",
          _js.loads(row[0]) == [".jpg", ".png"] and row[1] == 20, str(row))
else:
    check("C/D admin 通道", False, "无 admin 凭据 — 需人工提供或本地单元验证")

# 清理 operator 测试账号 (可达 — 在退出前执行; 测试库终将 DROP, 这里只做轻量收尾)。
# 顺序: operator_audit 行先删 — actor FK (users.id) 不带 CASCADE, 测试清理
# 必须先解除 audit 引用才能删 actor user; 这是 test-only cleanup, 不是生产
# audit 删除机制 (生产 writer 保持 append-only)。
import psycopg2 as _pg3
_c3 = _pg3.connect(TEST_DB); _c3.autocommit = True
_cur3 = _c3.cursor()
_cur3.execute("select id from users where username='op4ctest'")
_op_row = _cur3.fetchone()
if _op_row:
    _cur3.execute("delete from operator_audit where actor_user_id = %s",
                  (_op_row[0],))
purge_users(_cur3, [_op_row[0]] if _op_row else [])
_cur3.execute("delete from pricing where tool_id like 'mtest%' or tool_id like 'dbcheck%'")
_cur3.execute("delete from platform_tools where tool_key like 'mtest%' or tool_key like 'dbcheck%'")
_c3.close(); print("清理: cleaned")

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
sys.exit(1 if FAIL else 0)
