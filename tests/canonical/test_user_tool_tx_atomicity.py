#!/usr/bin/env python3
"""User tool PATCH/DELETE declaration+pricing single-transaction canonical.

一个 user tool 的商业状态 mutation (platform_tools declaration + pricing)
属于同一次用户操作时必须同一 DB transaction 提交或回滚。

Fault injection: the app under test runs IN-PROCESS (real uvicorn socket,
same DATABASE_URL), so monkeypatching repo.catalog.upsert_pricing reaches
the server's pricing writer. Happy paths + fault paths both go over real
HTTP; rollback is proven by direct DB row reads.
"""
import json as _json, uuid, http.client, sys, threading, time, socket
import os as _os, sys as _sys
_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n")
    _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n")
    _sys.exit(2)
_PORT = int(_TEST_PORT, 10)
if _PORT == 8000:
    _sys.stderr.write("ERROR: never the production port.\n")
    _sys.exit(2)
_HOST = ("127.0.0.1", _PORT)

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok: FAIL.append(name)

def mp(fields):
    out = b""
    for k, v in fields.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"

def req(method, path, body, cookie=None, host=_HOST):
    conn = http.client.HTTPConnection(*host, timeout=30)
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    conn.request(method, path, _json.dumps(body) if body is not None else None, h)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = _json.loads(raw)
    except Exception:
        data = {"raw": raw[:120].decode("utf-8", "replace")}
    conn.close()
    return resp.status, data

import psycopg2 as _pg
_db = _pg.connect(TEST_DB); _db.autocommit = True
def db_row(sql, args=()):
    cur = _db.cursor(); cur.execute(sql, args); r = cur.fetchone(); cur.close()
    return r

def new_user(host=_HOST):
    conn = http.client.HTTPConnection(*host, timeout=30)
    u = "tx" + uuid.uuid4().hex[:6]
    b, ct = mp({"username": u, "password": "Test12345",
                "password_confirm": "Test12345", "email": f"{u}@t.co"})
    conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read()
    b, ct = mp({"login": u, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    resp = conn.getresponse(); resp.read()
    conn.close()
    return u, (resp.getheader("Set-Cookie") or "").split(";")[0]

# ── in-process app with the pricing fault hook ──
sys.path.insert(0, _REPO)
sys.path.insert(0, _os.path.join(_REPO, "core"))
sys.path.insert(0, _os.path.join(_REPO, "app"))
import repo.catalog as _catalog_mod
_fault = {"boom": False}
_orig_upsert = _catalog_mod.upsert_pricing
def _hooked_upsert(*a, **k):
    if _fault["boom"]:
        raise RuntimeError("pricing_write_fail")
    return _orig_upsert(*a, **k)
_catalog_mod.upsert_pricing = _hooked_upsert

import uvicorn
from app.main import app as _fl_app
_PORT2 = _PORT + 1
with socket.socket() as _s:
    _s.bind(("127.0.0.1", _PORT2))  # refuse if taken
_HOST2 = ("127.0.0.1", _PORT2)
_srv = uvicorn.Server(uvicorn.Config(_fl_app, host="127.0.0.1",
                                     port=_PORT2, log_level="warning"))
threading.Thread(target=_srv.run, daemon=True).start()
for _ in range(100):
    try:
        with socket.create_connection(_HOST2, timeout=1):
            break
    except OSError:
        time.sleep(0.2)

# ── setup: one user + one tool on the in-process app ──
_uname, cookie = new_user(_HOST2)
_tool = "txtx" + uuid.uuid4().hex[:4]
_tool_key = f"{_uname}/{_tool}"
st, r = req("POST", "/api/usertools", {
    "tool_id": _tool, "label": {"en": "Tx Tool"}, "execution": "http",
    "url": "https://httpbin.org/anything", "timeout_s": 30,
    "visibility": "private", "format_families": "document",
    "accept": [".pdf"], "cost": 5}, cookie=cookie, host=_HOST2)
check("T0 create ok (cost=5)", st == 200 and r.get("ok"), f"{st} {r}")

# ── 1. PATCH declaration + cost normal → both land together ──
st, r = req("PATCH", f"/api/usertools/{_tool}",
            {"label": {"en": "Tx v2"}, "cost": 9}, cookie=cookie, host=_HOST2)
row_pt = db_row("select label_json from platform_tools where tool_key=%s", (_tool_key,))
row_pr = db_row("select cost, enabled from pricing where tool_id=%s", (_tool_key,))
check("T1 PATCH decl+cost → both committed",
      st == 200 and r.get("ok")
      and _json.loads(row_pt[0])["en"] == "Tx v2"
      and row_pr[0] == 9 and row_pr[1] is True,
      f"{st} {r} {row_pt} {row_pr}")

# ── 2. PATCH pricing write fail → declaration rollback ──
_fault["boom"] = True
try:
    st, r = req("PATCH", f"/api/usertools/{_tool}",
                {"label": {"en": "Should Not Land"}, "cost": 77},
                cookie=cookie, host=_HOST2)
except Exception:
    st = 500
_fault["boom"] = False
row_pt = db_row("select label_json from platform_tools where tool_key=%s", (_tool_key,))
row_pr = db_row("select cost, enabled from pricing where tool_id=%s", (_tool_key,))
check("T2 pricing fail → declaration rolled back (label/cost unchanged)",
      _json.loads(row_pt[0])["en"] == "Tx v2" and row_pr[0] == 9,
      f"label={row_pt} pricing={row_pr}")

# ── 3. DELETE normal → row gone + pricing disabled ──
st, r = req("DELETE", f"/api/usertools/{_tool}", None, cookie=cookie, host=_HOST2)
row_pt = db_row("select count(*) from platform_tools where tool_key=%s", (_tool_key,))
row_pr = db_row("select cost, enabled from pricing where tool_id=%s", (_tool_key,))
check("T3 DELETE → tool row deleted + pricing disabled",
      st == 200 and r.get("ok") and row_pt[0] == 0
      and row_pr[0] == 0 and row_pr[1] is False,
      f"{st} {r} pt={row_pt} pr={row_pr}")

# ── 4. DELETE pricing write fail → tool row survives, pricing untouched ──
_tool2 = "txdl" + uuid.uuid4().hex[:4]
_tool2_key = f"{_uname}/{_tool2}"
st, r = req("POST", "/api/usertools", {
    "tool_id": _tool2, "label": {"en": "Del Fail"}, "execution": "http",
    "url": "https://httpbin.org/anything", "timeout_s": 30,
    "visibility": "private", "format_families": "document",
    "accept": [".pdf"], "cost": 3}, cookie=cookie, host=_HOST2)
check("T4a second tool created (cost=3)", st == 200 and r.get("ok"), f"{st} {r}")
_fault["boom"] = True
try:
    st, r = req("DELETE", f"/api/usertools/{_tool2}", None, cookie=cookie, host=_HOST2)
except Exception:
    st = 500
_fault["boom"] = False
row_pt = db_row("select count(*) from platform_tools where tool_key=%s", (_tool2_key,))
row_pr = db_row("select cost, enabled from pricing where tool_id=%s", (_tool2_key,))
check("T4 pricing fail → tool row still exists + pricing original state",
      row_pt[0] == 1 and row_pr[0] == 3 and row_pr[1] is True,
      f"pt={row_pt} pr={row_pr}")

_db.close()
print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
sys.exit(1 if FAIL else 0)
