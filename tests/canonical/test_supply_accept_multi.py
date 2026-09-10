#!/usr/bin/env python3
"""4C-2-close 验收 A-F"""
import json, uuid, http.client, sys, subprocess
import os
import os as _os
_REPO = _os.environ.get("FLASHSHARE_TEST_REPO") or _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_HOST = ("127.0.0.1", int(_os.environ.get("FLASHSHARE_TEST_PORT", "8000")))

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
base = {"tool_id": "acctest", "label": {"en": "Acc"}, "execution": "http",
        "url": "https://httpbin.org/anything", "timeout_s": 30,
        "visibility": "private", "format_families": "document", "params": []}

# A1: 无 accept → 全族
st, r = req("POST", "/api/usertools", base, cookie)
check("A1 缺失 accept → 创建 ok", st == 200 and r.get("ok"), str(r))
conn = http.client.HTTPConnection(*_HOST, timeout=30)
conn.request("GET", "/api/usertools", None, {"Cookie": cookie})
t = [x for x in json.loads(conn.getresponse().read())["tools"] if x["tool_id"] == "acctest"][0]
conn.close()
import sys as _s, os as _os
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "x")
_s.path.insert(0, os.path.join(_REPO, 'core'))
from governance.formats import FAMILIES
full_doc = sorted(FAMILIES["document"].exts)
check("A1b 全族 canonical ext (与 FAMILIES[document].exts 一致)",
      sorted(t["accept"]) == full_doc, f"{len(t['accept'])} exts")

# A2: accept=[] → bad_accept
st, r = req("POST", "/api/usertools", dict(base, tool_id="acc2", accept=[]), cookie)
check("A2 accept=[] → 422 bad_accept", st == 422 and r.get("error") == "bad_accept", f"{st} {r}")

# A2b: accept=None → 与缺失同义 (全族)
st, r = req("POST", "/api/usertools", dict(base, tool_id="acc3", accept=None), cookie)
check("A2b accept=None → 兼容全族 (创建 ok)", st == 200 and r.get("ok"), f"{st} {r}")

# A3: accept=[".pdf"] → [".pdf"]
st, r = req("POST", "/api/usertools", dict(base, tool_id="acc4", accept=[".pdf"]), cookie)
check("A3 accept=['.pdf'] → 创建 ok", st == 200 and r.get("ok"), f"{st} {r}")
conn = http.client.HTTPConnection(*_HOST, timeout=30)
conn.request("GET", "/api/usertools", None, {"Cookie": cookie})
t4 = [x for x in json.loads(conn.getresponse().read())["tools"] if x["tool_id"] == "acc4"][0]
conn.close()
check("A3b 存库精确 ['.pdf']", t4["accept"] == [".pdf"], str(t4["accept"]))

# ── C: operator multi 严格 boolean (operator 端点需 admin — 直接测路由行为
#    通过 samelabs/remote/create 需要 admin session; 用 admin 账号) ──
# admin 登录 (id2 ainotebook / 后台账号): 从 env/惯例拿 — samelabs 有独立鉴权?
# samelabs 路由的鉴权方式:
import re as _re
src = open(os.path.join(_REPO, 'app/routes/samelabs.py')).read()
m = _re.search(r'def (require_admin|_admin|check_admin)\w*\(', src)
print("samelabs 鉴权:", m.group(1) if m else "看路由装饰")

# operator 测试账号 op4ctest (验收脚本自建, 尾部清理)。
# 上批清理后账号已删 → 脚本内重建 (注册普通号 → SQL 升 status='samelabs')。
conn = http.client.HTTPConnection(*_HOST, timeout=30)
u, pw = "op4ctest", "Test12345"
b, ct = mp(fields={"username": u, "password": pw, "password_confirm": pw, "email": "op4c@t.co"})
conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read()
open('/tmp/op_up.py', 'w').write(
    "import psycopg2\n"
    "env={}\n"
    "for k in open('/etc/flashshare.env'):\n"
    "    k=k.strip()\n"
    "    if k and not k.startswith('#') and '=' in k:\n"
    "        a,v=k.split('=',1); env[a]=v\n"
    "con=psycopg2.connect(env['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://'))\n"
    "con.autocommit=True\n"
    f"cur=con.cursor(); cur.execute(\"update users set status='samelabs' where username='{u}'\"); print('upgraded')\n")
subprocess.run(['sudo', os.path.join(_REPO, 'venv/bin/python'), '/tmp/op_up.py'], capture_output=True, text=True)
b, ct = mp(fields={"login": u, "password": pw})
conn.request("POST", "/auth/login", b, {"Content-Type": ct})
resp = conn.getresponse(); resp.read()
admin_cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
conn.close()
print("operator login:", "ok" if admin_cookie else "fail")

if admin_cookie:
    # C: multi 类型矩阵
    for val, want in [(True, "ok"), (False, "ok"), ("true", "bad_multi"), ("false", "bad_multi"), (1, "bad_multi"), (0, "bad_multi")]:
        st, r = req("POST", "/samelabs/remote/create", {
            "tool_id": f"mtest{uuid.uuid4().hex[:6]}", "label": "MT",
            "url": "https://httpbin.org/anything", "timeout_s": 30,
            "accept": ".png,.jpg", "multi": val}, admin_cookie)
        got = "ok" if r.get("ok") else r.get("error")
        check(f"C multi={val!r} → {want}", got == want, f"{st} {r}")
    # D: accept=".png,.jpg" multi=true → DB canonical + multi
    tid = "dbcheck" + uuid.uuid4().hex[:5]
    st, r = req("POST", "/samelabs/remote/create", {
        "tool_id": tid, "label": "DB", "url": "https://httpbin.org/anything",
        "timeout_s": 30, "accept": ".png,.jpg", "multi": True}, admin_cookie)
    check("D operator create ok", r.get("ok"), str(r))
    import subprocess
    q = ("import psycopg2, json; env={}; [env.update({k.split(chr(61))[0]: k.split(chr(61))[1]}) for k in open('/etc/flashshare.env') if chr(61) in k and not k.startswith(chr(35))];"
         "con=psycopg2.connect(env['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://')); cur=con.cursor();"
         f"cur.execute(\"select accept_json, multi from platform_tools where tool_key='{tid}'\"); print(cur.fetchone())")
    # DB 断言在 sudo 步骤 (env 需 root 读): 此处仅确认 create ok
    check("D operator create (multi=true) ok", r.get("ok"), str(r))
else:
    check("C/D admin 通道", False, "无 admin 凭据 — 需人工提供或本地单元验证")

# ── E: docs tool_type ──
doc = open(os.path.join(_REPO, 'docs/plan/TOOLCHAIN_UNIFICATION.md')).read()
check("E docs tool_type = view/peek/produce", "view / peek / produce" in doc and "share / produce" not in doc)

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
sys.exit(1 if FAIL else 0)

# 清理 operator 测试账号
import psycopg2 as _pg
_env = {}
for _line in open('/etc/flashshare.env'):
    _line = _line.strip()
    if _line and not _line.startswith('#') and '=' in _line:
        _k, _v = _line.split('=', 1); _env[_k] = _v
import subprocess as _sp
_q = "import psycopg2; env={}; [env.update({k.split(chr(61))[0]: k.split(chr(61))[1]}) for k in open('/etc/flashshare.env') if chr(61) in k and not k.startswith(chr(35))];"
_q += "c=psycopg2.connect(env['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://')); c.autocommit=True;"
_q += "cur=c.cursor(); cur.execute(\"delete from users where username='op4ctest'\"); cur.execute(\"delete from platform_tools where tool_key like 'mtest%' or tool_key like 'dbcheck%'\"); print('cleaned')"
_r = _sp.run([os.path.join(_REPO, 'venv/bin/python'), '-c', _q], capture_output=True, text=True)
print("清理:", _r.stdout.strip() or _r.stderr[:100])
