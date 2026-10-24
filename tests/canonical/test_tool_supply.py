#!/usr/bin/env python3
"""Tool chain — Declaration → Supply → Catalog: declaration validation
(accept / params / locale), browse vs search listing, publish gate and
governance switches, owner badge projection, favorites, and the
declaration+pricing single transaction.
"""
from _harness import PORT, REPO, TEST_DB, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# Supply accept / multi-file declaration
#
# 4C-2-close 验收 A-F
# ════════════════════════════════════════════════════════════════════════
section('Supply accept / multi-file declaration')

import json, uuid, http.client, sys
import os
import os as _os, sys as _sys

_HOST = ("127.0.0.1", PORT)


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
    # C2: body-tool window (files 0..0) is legal only with body_format,
    # without accept
    for acc, want in [("", "ok"), (".png", "bad_accept")]:
        st, r = req("POST", "/samelabs/tools/create", {
            "tool_id": f"mt{uuid.uuid4().hex[:6]}", "label": "MT",
            "url": "https://93.184.216.34/anything", "timeout_s": 30,
            "accept": acc, "files_min": 0, "files_max": 0,
            "body_format": "text"}, admin_cookie)
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


# ════════════════════════════════════════════════════════════════════════
# Number parameter declaration bounds
#
# Number parameter declarations must stay finite and self-consistent.
#
# float() accepts inf/nan, and the declaration validator historically
# didn't reject them — a public tool with a non-finite bound poisoned
# JSON serialization and took the anonymous MCP tools/list down with a
# 500. The validator now rejects non-finite min/max/default and defaults
# outside their own bounds. Stored junk declarations are excluded by a
# deploy-time read-only precheck, not by a runtime projection channel.
# ════════════════════════════════════════════════════════════════════════
section('Number parameter declaration bounds')

import os as _os
import sys as _sys


_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")


import http.client as _hc
import json
import time
ts = int(time.time())
_u = "pb" + _os.urandom(3).hex()
_HOST_pdb = ("127.0.0.1", int(TEST_PORT))

from database import db


def _req(method, path, body=None, headers=None, cookies=None):
    conn = _hc.HTTPConnection(*_HOST_pdb, timeout=60)
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = cookies
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw.decode(errors="replace")


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register_login(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    conn = _hc.HTTPConnection(*_HOST_pdb, timeout=30)
    conn.request("POST", "/auth/register", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    conn.close()
    b, ct = _form({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    return ck


def _mktool(ck, name_suffix, params):
    return _req("POST", "/api/usertools", {
        "label": f"PB {name_suffix}", "url": "https://93.184.216.34/pb",
        "timeout_s": 30, "files_min": 0, "files_max": 1,
        "format_families": "image",
        "accept": [".png"], "cost": 0,
        "params": params}, {"Content-Type": "application/json", "Cookie": ck})


ck = _register_login(_u)
with db() as con:
    uid = con.execute("select id from users where username=%s", (_u,)).fetchone()["id"]

# ── 1. declaration gate: non-finite bounds are rejected ──
for label, params in [
    ("inf min", [{"key": "a", "type": "number", "min": "inf", "max": 10}]),
    ("nan default", [{"key": "a", "type": "number", "min": 0, "max": 10,
                      "default": "nan"}]),
    ("inf max", [{"key": "a", "type": "number", "max": float("inf")}]),
]:
    st, r = _mktool(ck, label.replace(" ", ""), params)
    check(f"1 '{label}' declaration → 422 bad_param_default",
          st == 422 and r.get("error") == "bad_param_default", f"{st} {str(r)[:70]}")

# ── 2. default must satisfy its own bounds ──
st, r = _mktool(ck, "range", [{"key": "a", "type": "number", "min": 1,
                               "max": 10, "default": 0}])
check("2 out-of-range default → 422 bad_param_default",
      st == 422 and r.get("error") == "bad_param_default", f"{st} {str(r)[:70]}")

# ── 3. control: a legal bounded default still passes ──
st, r = _mktool(ck, "ok", [{"key": "a", "type": "number", "min": 1,
                            "max": 10, "default": 5}])
check("3 legal bounded declaration accepted", st == 200 and r.get("ok"),
      f"{st} {str(r)[:70]}")

# No legacy-row projection defense exists by design: non-finite values in
# stored declarations are a deploy-time read-only precheck concern
# (params_json ~* '(infinity|nan)' must be empty), not a runtime channel.

with db() as con:
    con.execute("delete from platform_tools where owner_id=%s", (uid,))
    purge_users(con, [uid])


# ════════════════════════════════════════════════════════════════════════
# Locale = language tag of the text
#
# Locale mechanism canonical — locale = the language tag of the text
# itself. The publish form submits {主语言: 文本} with the main-language
# key first; extra language rows patch other locales.
#
#   L1  a tool stored with a str label/desc, edited via the Web patch
#       header to ADD a locale, keeps the original text under the
#       edit_locale key (merge base {edit_locale: old str}, not {}).
#   L2  creating with a dict label stores it verbatim — the main
#       language key stays FIRST (dict first key = original language).
#   L3  params text leaves (label / placeholder / options[].label)
#       submitted as {主语言: 文本} patch the old declaration —
#       unmentioned locales survive the edit.
#   L4  an empty submitted locale value deletes that locale key
#       (tombstone) while the others stay untouched.
# ════════════════════════════════════════════════════════════════════════
section('Locale = language tag of the text')

import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


from database import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app as _app  # noqa: E402
from app.web_auth import create_browser_session, COOKIE_NAME  # noqa: E402

client = TestClient(_app)
ts_loc = int(_time.time())
_u = "bl" + _uuid.uuid4().hex[:6]


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_loc, ts_loc)).fetchone()
    return int(row["id"])


def _row(tool_key):
    with db() as con:
        return dict(con.execute(
            "select label_json, desc_json, params_json from platform_tools"
            " where tool_key = %s", (tool_key,)).fetchone())


_uid = _mkuser(_u)
_sess = create_browser_session(_uid)

def _create(label, desc=None, params=None):
    body = {
        "label": label, "url": "https://93.184.216.34/x", "timeout_s": 30,
        "files_min": 0, "files_max": 1, "format_families": "image",
        "accept": [".png"], "outputs": ["text"], "cost": 0,
    }
    if desc is not None:
        body["desc"] = desc
    if params is not None:
        body["params"] = params
    r = client.post("/api/usertools", json=body, cookies={COOKIE_NAME: _sess})
    assert r.status_code == 200, r.text
    return r.json()["tool_key"]


# ══════════ L1. str original survives a Web locale patch that adds ja ══════════
_k1 = _create("Locale Original", desc="Original description",
              params=[{"key": "style", "type": "enum", "required": False,
                       "label": "Style",
                       "options": [{"value": "a", "label": "Alpha"}]}])
# rewrite label/desc into the legacy str storage shape (rows created
# before dict normalization / operator-sealed str declarations) — the
# exact rows the {edit_locale: old str} merge base exists for
with db() as con:
    con.execute(
        "update platform_tools set label_json = %s, desc_json = %s"
        " where tool_key = %s",
        (_json.dumps("Locale Original"), _json.dumps("Original description"), _k1))
# the exact Web-form shape: main locale first with its unchanged text,
# the added locale as an extra row
r = client.patch(f"/api/usertools/{_k1}", json={
    "label": {"zh": "Locale Original", "ja": "ロケール原名"},
    "desc": {"zh": "Original description", "ja": "元の説明"},
    "params": [{"key": "style", "type": "enum", "required": False,
                "label": {"zh": "Style"},
                "options": [{"value": "a", "label": {"zh": "Alpha"}}]}],
}, headers={"X-518-Edit-Locale": "zh"}, cookies={COOKIE_NAME: _sess})
check("L1a Web patch (add ja) returns ok", r.status_code == 200 and r.json().get("ok"),
      f"{r.status_code} {r.text[:120]}")
_row1 = _row(_k1)
_lab1 = _json.loads(_row1["label_json"])
_desc1 = _json.loads(_row1["desc_json"])
check("L1b str label original preserved under edit_locale",
      _lab1 == {"zh": "Locale Original", "ja": "ロケール原名"}, _lab1)
check("L1c str desc original preserved under edit_locale",
      _desc1 == {"zh": "Original description", "ja": "元の説明"}, _desc1)
_par1 = _json.loads(_row1["params_json"])[0]
check("L1d str param leaf original preserved as {edit_locale: old str}",
      _par1["label"] == {"zh": "Style"},
      _par1["label"])
check("L1e enum option leaf locale-patched",
      _par1["options"][0]["label"] == {"zh": "Alpha"},
      _par1["options"][0]["label"])

# ══════════ L2. dict creation stores the main language key FIRST ══════════
_k2 = _create({"ja": "ツール名", "en": "Tool Name"}, desc={"ja": "説明", "en": "Desc"})
_lab2 = _json.loads(_row(_k2)["label_json"])
_desc2 = _json.loads(_row(_k2)["desc_json"])
check("L2a created label is the dict, main language key first",
      list(_lab2.keys()) == ["ja", "en"], _lab2)
check("L2b created desc is the dict, main language key first",
      list(_desc2.keys()) == ["ja", "en"], _desc2)

# ══════════ L3. params dict leaves patch, unmentioned locales survive ══════════
_k3 = _create("Param I18n", params=[{
    "key": "style", "type": "enum", "required": False,
    "label": {"en": "Style", "ja": "スタイル"},
    "options": [{"value": "a", "label": {"en": "Alpha", "ja": "アルファ"}}]},
])
r = client.patch(f"/api/usertools/{_k3}", json={
    "label": {"zh": "参数多语"},
    "params": [{"key": "style", "type": "enum", "required": False,
                "label": {"zh": "风格"},
                "options": [{"value": "a", "label": {"zh": "甲"}}]}],
}, headers={"X-518-Edit-Locale": "zh"}, cookies={COOKIE_NAME: _sess})
check("L3a params locale patch returns ok", r.status_code == 200 and r.json().get("ok"),
      f"{r.status_code} {r.text[:120]}")
_par3 = _json.loads(_row(_k3)["params_json"])[0]
check("L3b param label: zh patched, en+ja survive",
      _par3["label"] == {"en": "Style", "ja": "スタイル", "zh": "风格"},
      _par3["label"])
check("L3c option label: zh patched, en+ja survive",
      _par3["options"][0]["label"] == {"en": "Alpha", "ja": "アルファ", "zh": "甲"},
      _par3["options"][0]["label"])
_lab3 = _json.loads(_row(_k3)["label_json"])
check("L3d tool label dict base preserved, zh patched in",
      _lab3 == {"en": "Param I18n", "zh": "参数多语"}, _lab3)

# ══════════ L4. empty submitted locale = tombstone delete ══════════
r = client.patch(f"/api/usertools/{_k2}", json={
    "label": {"ja": "ツール名", "en": ""},
    "desc": {"ja": "説明", "en": ""},
}, headers={"X-518-Edit-Locale": "ja"}, cookies={COOKIE_NAME: _sess})
check("L4a tombstone patch returns ok", r.status_code == 200 and r.json().get("ok"),
      f"{r.status_code} {r.text[:120]}")
_row4 = _row(_k2)
check("L4b deleted locale gone, original (first key) preserved",
      _json.loads(_row4["label_json"]) == {"ja": "ツール名"},
      _row4["label_json"])
check("L4c desc tombstone deletes only that locale",
      _json.loads(_row4["desc_json"]) == {"ja": "説明"},
      _row4["desc_json"])

# ── cleanup ──
with db() as con:
    con.execute("delete from platform_tools where tool_key in %s",
                ((_k1, _k2, _k3),))
    con.execute("delete from pricing where tool_id in %s",
                ((_k1, _k2, _k3),))
    purge_users(con, [_uid])


# ════════════════════════════════════════════════════════════════════════
# Listing: browse vs search, paging, text-only tools
#
# Listing canonical — browse vs search, list paging bounds, text-only tools.
#
# L*  Web browse lists (home grid, /tools, /api/tools/search) carry only
#     featured tools; search finds every public tool; featuring a tool
#     puts it on the browse lists. First-party seed rows start featured.
#     MCP catalog_search with an EMPTY query enumerates the FULL public
#     catalog (featured or not) — the agent-side browse; /llms.txt keeps
#     listing recommended tools only.
# P*  Paging inputs never reach SQL out of range: negative / huge
#     offsets and pages answer 200 (web) or -32602 (MCP explicit
#     illegal values); catalog_search pages with page/total_pages.
# T*  Text-only tools (files 0..0): created without format family /
#     accept, accept with files_max 0 is refused, the MCP spec carries
#     no files argument, switching back to files requires accept.
# ════════════════════════════════════════════════════════════════════════
section('Listing: browse vs search, paging, text-only tools')

import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


_HOST_lst = ("127.0.0.1", int(TEST_PORT))
_u = "ls" + _uuid.uuid4().hex[:6]
ts_lst = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


def _req_lst(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_lst, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form_lst(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req_lst("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _b64(b):
    return base64.b64encode(b).decode()


def _tool_text(d):
    """content[0].text parsed as JSON (the unified isError shape)."""
    try:
        return _json.loads(
            (((d or {}).get("result") or {}).get("content")
             or [{}])[0].get("text") or "{}")
    except Exception:
        return {}


def _register(name):
    b, ct = _form_lst({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req_lst("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_lst({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req_lst("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req_lst("POST", "/api/keys", {"name": "ls"},
                             {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


from database import db  # noqa: E402


def _names(token):
    _st, d = _mcp({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, token)
    return {t["name"]: t for t in ((d or {}).get("result") or {}).get("tools", [])}


def _search(q, token, page=None):
    args = {"q": q}
    if page is not None:
        args["page"] = page
    st, d = _call("catalog_search", args, token)
    return d, (((d or {}).get("result") or {}).get("structuredContent") or {})


def _api_browse():
    _st, _h, d, _sc = _req_lst("GET", "/api/tools/search?q=")
    return [t["id"] for t in (d or {}).get("tools", [])]


def _usertool(ck, body):
    return _req_lst("POST", "/api/usertools", body,
                {"Content-Type": "application/json", "Cookie": ck})


uids = []
try:
    a_uid, a_ck, A_TOK = _register(_u + "dev")
    _b_uid, _other_ck, _ = _register(_u + "oth")
    uids = [a_uid]
    label = f"Listing {_u}"
    st, _h, d, _sc = _usertool(a_ck, {
        "label": label, "url": "https://93.184.216.34/x",
        "timeout_s": 30, "files_min": 1, "files_max": 1,
        "format_families": "image", "accept": [".png"], "outputs": ["file"],
        "cost": 0})
    key = (d or {}).get("tool_key")
    with db() as con:
        con.execute("update platform_tools set visibility='public'"
                    " where tool_key=%s", (key,))
        seeded = con.execute(
            "select bool_and(featured) as f from platform_tools"
            " where owner_id is null and execution = 'local'").fetchone()["f"]
    check("L0 setup: public community tool exists", st == 200, f"{st} {d}")
    check("L1 first-party seed rows start featured", seeded is True, seeded)

    # ═══ L: browse = featured, search = all public ═══
    browse = _api_browse()
    check("L2 /api/tools/search empty query omits the unfeatured tool",
          key not in browse and "share" in browse, browse)
    _st, _h, d, _sc = _req_lst("GET", "/api/tools/search?q=" + _u)
    check("L3 search finds the unfeatured tool",
          key in [t["id"] for t in (d or {}).get("tools", [])], d)
    _st, _h, page, _sc = _req_lst("GET", "/tools", None, {"Accept": "text/html"})
    check("L4 /tools browse omits it", f'data-tool-id="{key}"' not in (page or ""))
    _st, _h, page, _sc = _req_lst("GET", "/tools?q=" + _u, None, {"Accept": "text/html"})
    check("L5 /tools?q= finds it", f'data-tool-id="{key}"' in (page or ""))
    _st, _h, page, _sc = _req_lst("GET", "/", None, {"Accept": "text/html"})
    check("L6 home grid omits it", f'data-tool-id="{key}"' not in (page or ""))
    mname = key.replace("/", "__")
    _d, sc = _search("", A_TOK)
    _page_names = [t["name"] for t in sc.get("tools", [])]
    check("L7 catalog_search empty query enumerates the full public catalog",
          mname in _page_names and "share" in _page_names
          and sc.get("total", 0) >= len(_page_names), sc)
    _d, sc = _search(_u, A_TOK)
    check("L8 catalog_search finds it",
          mname in [t["name"] for t in sc.get("tools", [])], sc)
    _st, _h, txt, _sc = _req_lst("GET", "/llms.txt")
    check("L9 llms.txt lists recommended tools only",
          mname not in (txt or "") and "Recommended tools" in (txt or ""))
    with db() as con:
        con.execute("update platform_tools set featured = true"
                    " where tool_key=%s", (key,))
    check("L10 featuring puts it on the browse list", key in _api_browse())
    _st, _h, page, _sc = _req_lst("GET", "/tools", None, {"Accept": "text/html"})
    check("L11 featured tool on the /tools browse list",
          f'data-tool-id="{key}"' in (page or ""))

    # ═══ P: paging bounds ═══
    for path in ("/api/uploads?offset=-5",
                 "/api/uploads?offset=99999999999999999999999",
                 "/account/credits?page=99999999999999999999",
                 "/api/earnings?rev_page=99999999999999999999",
                 "/api/earnings?wd_page=-1"):
        st, _h, _d, _sc = _req_lst("GET", path, None,
                               {"Cookie": a_ck, "Accept": "text/html"})
        check(f"P1 {path} → 200", st == 200, st)
    st, d = _call("list_uploads", {"offset": -1}, A_TOK)
    check("P2 MCP list_uploads offset -1 → -32602",
          ((d or {}).get("error") or {}).get("code") == -32602, d)
    d, _sc = _search("", A_TOK, page=0)
    check("P3 catalog_search page 0 → -32602",
          ((d or {}).get("error") or {}).get("code") == -32602, d)
    _d, sc = _search("", A_TOK, page=10 ** 12)
    check("P4 catalog_search page past the end clamps to the last page",
          sc.get("page") == sc.get("total_pages") and sc.get("total", 0) >= 1,
          {k: sc.get(k) for k in ("page", "total_pages", "total")})

    # ═══ T: text-only tools ═══
    st, _h, d, _sc = _usertool(a_ck, {
        "label": "Txt", "url": "https://93.184.216.34/t",
        "timeout_s": 30, "files_min": 0, "files_max": 0,
        "body_format": "text", "outputs": ["text"],
        "params": [{"key": "q", "type": "text", "label": "Q"}], "cost": 0})
    _txt_key = (d or {}).get("tool_key")
    check("T1 text-only create without format family/accept", st == 200 and _txt_key,
          f"{st} {d}")
    st, _h, d, _sc = _usertool(a_ck, {
        "label": "Txt", "url": "https://93.184.216.34/u",
        "timeout_s": 30, "files_min": 0, "files_max": 0,
        "body_format": "text",
        "format_families": "image", "accept": [".png"], "outputs": ["text"],
        "cost": 0})
    check("T2 accept with files_max 0 → bad_accept",
          st == 422 and (d or {}).get("error") == "bad_accept", f"{st} {d}")
    spec = _names(A_TOK).get(_txt_key) or {}
    props = (spec.get("inputSchema") or {}).get("properties") or {}
    check("T3 MCP spec has no files argument",
          spec and "files" not in props and "params" in props, spec)
    st, _h, d, _sc = _req_lst("PATCH", f"/api/usertools/{_txt_key}",
                          {"files_min": 1, "files_max": 1},
                          {"Content-Type": "application/json", "Cookie": a_ck})
    check("T4 switching to files requires a format family",
          st == 422 and (d or {}).get("error") == "bad_format_family", f"{st} {d}")

    # ═══ S: system slug = key (D6 行为测试) ═══
    import re as _re6
    check("S1 create returns a system slug matching ^[a-z]{5}$",
          bool(_re6.match(r"^[a-z]{5}$", key)), key)
    with db() as con:
        n = con.execute("select count(*) c from platform_tools"
                        " where tool_key = %s", (key,)).fetchone()["c"]
    check("S2 slug is unique (exactly one row)", n == 1, n)
    st, _h, d, _sc = _req_lst("POST", "/api/usertools", {
        "label": "With ID", "url": "https://93.184.216.34/x",
        "timeout_s": 30, "files_min": 1, "files_max": 1,
        "format_families": "image", "accept": [".png"], "outputs": ["file"],
        "cost": 0, "tool_id": "rejected"}, {"Content-Type": "application/json", "Cookie": a_ck})
    check("S3 request carrying tool_id → 422 invalid_request",
          st == 422 and (d or {}).get("error") == "invalid_request", f"{st} {d}")
    st, _h, d, _sc = _req_lst("PATCH", f"/api/usertools/{key}",
                          {"label": "Not Mine"}, {"Content-Type": "application/json",
                                                  "Cookie": _other_ck})
    check("S4 PATCH by another user's key → 404", st == 404, st)
    st, _h, d, _sc = _req_lst("DELETE", f"/api/usertools/{key}", None,
                          {"Cookie": _other_ck})
    check("S5 DELETE by another user's key → 404", st == 404, st)
finally:
    if uids:
        with db() as con:
            purge_users(con, uids)


# ════════════════════════════════════════════════════════════════════════
# Publish gate
#
# Publish gate canonical — a tool goes public only after its author
# has successfully run it once.
#
#   G1* create defaults to unlisted: not on /tools, not in a non-owner's
#       MCP tools/list; the author's own tool page renders (with the
#       gate hint sentence).
#   G2* explicit visibility=public at creation → 422 needs_successful_run.
#   G3* PATCH public without any successful call → 422.
#   G4* another user's successful call does not unlock (owner-only).
#   G5* the author's FAILED call does not unlock.
#   G6* the author's successful call unlocks: PATCH public → 200, the
#       tool appears on /tools and in a non-owner's tools/list.
#   G7* un-publishing (public → unlisted) stays unrestricted.
#
# The publish POST uses a literal public IP https URL (the existing c2a
# fixture) — the SSRF guard resolves, nothing is fetched. Community
# rows are never SSRF-exempt, so the gate's call FACTS (ok/failed rows
# by specific users) are seeded directly: the gate reads exactly those
# rows; the real execution envelopes are covered by the operator-tool
# e2e elsewhere (platform H3, remote_security MIME-CT).
# ════════════════════════════════════════════════════════════════════════
section('Publish gate')

import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import threading
import time as _time
import uuid as _uuid


_HOST_pgt = ("127.0.0.1", int(TEST_PORT))
_u = "b9" + _uuid.uuid4().hex[:6]
ts_pgt = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req_pgt(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_pgt, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form_pgt(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_pgt(body, token):
    st, _h, d, _sc = _req_pgt("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call_pgt(name, arguments, token, mid=1):
    return _mcp_pgt({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _tools_list(token):
    st, d = _mcp_pgt({"jsonrpc": "2.0", "id": 99, "method": "tools/list"}, token)
    return [t.get("name") for t in
            (((d or {}).get("result") or {}).get("tools") or [])]


def _register(name):
    b, ct = _form_pgt({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req_pgt("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_pgt({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req_pgt("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req_pgt("POST", "/api/keys", {"name": "b9"},
                              {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


from database import db  # noqa: E402

author_uid = None
reader_uid = None
try:
    author_uid, a_ck_pgt, A_TOK = _register(_u + "dev")
    reader_uid, _rck, R_TOK = _register(_u + "reader")

    def _publish(cookie, tool_id, extra=None):
        body = {"label": "Gate Tool",
                "url": "https://93.184.216.34/x", "timeout_s": 30,
                "files_min": 0, "files_max": 3,
                "format_families": "image", "accept": [".png"],
                "outputs": ["text"], "cost": 0}
        body.update(extra or {})
        return _req_pgt("POST", "/api/usertools", body,
                    {"Content-Type": "application/json", "Cookie": cookie})

    # ═══ G1: default unlisted, invisible to others, author page works ═══
    st, _h, d, _sc = _publish(a_ck_pgt, "gate1")
    tk = (d or {}).get("tool_key")
    check("G1a create accepted (no visibility field)",
          st == 200 and (d or {}).get("ok"), f"{st} {d}")
    with db() as con:
        vis = con.execute("select visibility from platform_tools"
                          " where tool_key=%s", (tk,)).fetchone()
    check("G1b lands unlisted", vis and vis["visibility"] == "unlisted",
          str(vis and dict(vis)))
    st, _h, market, _sc = _req_pgt("GET", "/tools")
    check("G1c not on /tools", tk not in (market or ""))
    check("G1d not in a non-owner's MCP tools/list",
          tk not in _tools_list(R_TOK))
    st, _h, page, _sc = _req_pgt("GET", f"/tool/{tk}", None,
                             {"Cookie": a_ck_pgt, "Accept": "text/html"})
    check("G1e author's own tool page renders with the gate hint",
          st == 200 and "tool_gate_hint" in (page or "")
          and "data-i18n=\"tool_gate_hint\"" in (page or ""), st)
    check("G1f tool page shows no run-count tag",
          "tool_runs" not in (page or ""), st)

    # ═══ G2: explicit public at creation refused ═════════════════════
    st, _h, d, _sc = _publish(a_ck_pgt, "gate2", {"visibility": "public"})
    check("G2 explicit public at creation → 422 needs_successful_run",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G3: PATCH public without any successful call ════════════════
    def _patch_vis(cookie, vis):
        return _req_pgt("PATCH", f"/api/usertools/{tk}",
                    {"visibility": vis},
                    {"Content-Type": "application/json", "Cookie": cookie})

    st, _h, d, _sc = _patch_vis(a_ck_pgt, "public")
    check("G3 PATCH public with zero calls → 422",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G4: another user's successful call does not unlock ══════════
    def _seed_call_fact(uid, status, ref):
        with db() as con:
            con.execute(
                "insert into tool_calls (client_ref, user_id, tool_id,"
                " status, cost_snapshot, started_at, finished_at,"
                " invocation_hash) values (%s, %s, %s, %s, 0, %s, %s, %s)",
                (f"{ref}-{_u}", uid, tk, status, ts_pgt, ts_pgt,
                 _uuid.uuid4().hex * 2))

    _seed_call_fact(reader_uid, "ok", "b9r")
    with db() as con:
        row = con.execute(
            "select status from tool_calls where tool_id=%s"
            " order by id desc limit 1", (tk,)).fetchone()
    check("G4a reader's ok call fact seeded", row["status"] == "ok",
          row and dict(row))
    st, _h, d, _sc = _patch_vis(a_ck_pgt, "public")
    check("G4b someone else's ok does not unlock → 422",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G5: the author's failed call does not unlock ════════════════
    _seed_call_fact(author_uid, "failed", "b9f")
    with db() as con:
        row = con.execute(
            "select status from tool_calls where tool_id=%s"
            " order by id desc limit 1", (tk,)).fetchone()
    check("G5a author's failed call fact seeded", row["status"] == "failed",
          row and dict(row))
    st, _h, d, _sc = _patch_vis(a_ck_pgt, "public")
    check("G5b a failed author run does not unlock → 422",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G6: the author's successful call unlocks ════════════════════
    _seed_call_fact(author_uid, "ok", "b9o")
    check("G6a author's ok call fact seeded", True)
    st, _h, d, _sc = _patch_vis(a_ck_pgt, "public")
    check("G6b PATCH public after the owner's ok run → 200",
          st == 200 and (d or {}).get("ok"), f"{st} {d}")
    st, _h, market, _sc = _req_pgt("GET", "/tools?q=Gate+Tool")
    check("G6c searchable on /tools", tk in (market or ""))
    st, d = _call_pgt("pin_tool", {"name": tk}, R_TOK)
    check("G6d a non-owner can pin it into their MCP tools/list",
          ((d.get("result") or {}).get("structuredContent") or {})
          .get("favorite") is True
          and tk in _tools_list(R_TOK), str(d)[:200])

    # ═══ G7: un-publishing stays free ════════════════════════════════
    st, _h, d, _sc = _patch_vis(a_ck_pgt, "unlisted")
    check("G7 public → unlisted unrestricted", st == 200 and (d or {}).get("ok"),
          f"{st} {d}")

finally:
    uids = [u for u in (author_uid, reader_uid) if u]
    with db() as con:
        purge_users(con, uids)


# ════════════════════════════════════════════════════════════════════════
# Governance & discovery: delist vs enable, search ordering
#
# Governance & discovery closure canonical.
#
# A*  publish gate: a new community tool lands visibility=unlisted
#     (never listed before the owner has run it successfully once);
#     PATCH public before that → 422 needs_successful_run, after the
#     owner's ok call → public + visible on home/market.
# B*  operator delist vs developer enabled are independent switches:
#     delist hides the tool from home/market/search/author page/MCP
#     list+search and makes resolve() refuse; the developer's enable
#     PATCH is refused while delisted (typed) and the owner view shows
#     the takedown; developer enable/disable writes an audit row.
# C*  suspended provider leaves the market page too (the one surface
#     the earlier suspended-owner sweep missed).
# D*  search = one ordering with the lists (featured → sort_order → id)
#     and matches tool_key + label + desc (all languages).
# E*  author visibility: community cards carry a /u/{handle} link on
#     home and market; MCP summaries and the llms.txt catalog note
#     "by @handle"; official rows keep the official badge and no
#     fabricated author.
# ════════════════════════════════════════════════════════════════════════
section('Governance & discovery: delist vs enable, search ordering')

import os as _os
import sys as _sys


import json
import http.client as _hc
import time
ts_gov = int(time.time())
_u = "c2" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(TEST_PORT))


def _req_gov(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body).encode()
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, json.loads(data) if data else None
    except Exception:
        return r.status, hdrs, data.decode(errors="replace")


def _form_gov(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form_gov({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req_gov("POST", "/auth/register", b, {"Content-Type": ct})
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form_gov({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


def _publish(ck, tool_id, label, extra=None):
    body = {"label": label,
            "url": "https://93.184.216.34/x", "timeout_s": 30,
            "files_min": 1, "files_max": 1,
            "format_families": "image", "accept": [".png"],
            "outputs": ["file"], "cost": 0}
    body.update(extra or {})
    return _req_gov("POST", "/api/usertools", body,
                {"Content-Type": "application/json", "Cookie": ck})


dev_ck_user = _u + "dev"
uid, CK = _register(dev_ck_user)

# ═══ A. publish gate: unlisted until the owner runs it once ═══
st, _h, d = _publish(CK, "aaa", "Alpha Tool")
tk_gov = (d or {}).get("tool_key")
check("A1 create accepted", st == 200 and d.get("ok") and tk_gov, f"{st} {d}")
with db() as con:
    row = con.execute("select visibility, enabled, delisted from"
                      " platform_tools where tool_key=%s", (tk_gov,)).fetchone()
check("A2 lands unlisted + enabled + not delisted",
      dict(row) == {"visibility": "unlisted", "enabled": True,
                    "delisted": False}, str(row and dict(row)))
st, _h, home = _req_gov("GET", "/")
st, _h, market = _req_gov("GET", "/tools")
check("A3 not on home/market before the owner's successful run",
      tk_gov not in (home or "") and tk_gov not in (market or ""))
# publish gate: unlisted→public requires ONE successful owner call
st, _h, d = _req_gov("PATCH", f"/api/usertools/{tk_gov}", {"visibility": "public"},
                 {"Content-Type": "application/json", "Cookie": CK})
check("A4 public without a successful run → 422 needs_successful_run",
      st == 422 and d.get("error") == "needs_successful_run",
      f"{st} {d}")
with db() as con:
    con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, started_at, finished_at, commission_rate_snapshot,"
        " invocation_hash) values (%s, %s, %s, 'ok', 0, %s, %s, 0, %s)",
        (f"c2a-run-{_u}", uid, tk_gov, int(time.time()), int(time.time()),
         "a" * 64))
st, _h, d = _req_gov("PATCH", f"/api/usertools/{tk_gov}", {"visibility": "public"},
                 {"Content-Type": "application/json", "Cookie": CK})
check("A4b public after the owner's successful run", st == 200 and d.get("ok"),
      f"{st} {d}")
# public = searchable (browse lists carry featured tools only)
st, _h, market = _req_gov("GET", "/tools?q=Alpha")
st, _h, d = _req_gov("GET", "/api/tools/search?q=alpha")
check("A3b searchable on market + selector once public",
      tk_gov in (market or "")
      and any(t["id"] == tk_gov for t in (d.get("tools") or [])))
# explicit private still honored
st, _h, d = _publish(CK, "bbb", "Beta", {"visibility": "private"})
_bbb_key = (d or {}).get("tool_key")
with db() as con:
    row = con.execute("select visibility from platform_tools where"
                      " tool_key=%s", (_bbb_key,)).fetchone()
check("A5 explicit private honored", row["visibility"] == "private",
      str(row and dict(row)))

# ═══ B. delist vs enabled ═══
op, op_ck = _register(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
from backoffice.tools import set_tool_delisted
set_tool_delisted(op, tk_gov, True)
st, _h, home = _req_gov("GET", "/")
st, _h, market = _req_gov("GET", "/tools?q=Alpha")
st, _h, author = _req_gov("GET", f"/u/{dev_ck_user}")
# author page asserts the tool's visible artifacts (card link + label),
# never a bare substring — cache-bust stamps (?v=…) can contain any hex
# run and once false-positived on "aaa".
card_href = f'href="/tool/{tk_gov}"'
check("B1 delisted: gone from home + market (author page row gone)",
      tk_gov not in (home or "") and tk_gov not in (market or "")
      and card_href not in (author or "")
      and "Alpha Tool" not in (author or ""),
      f"home={tk_gov in (home or '')} market={tk_gov in (market or '')}"
      f" authorCard={card_href in (author or '')}")
check("B1b author page shows no call-count stat",
      "owner_calls_n" not in (author or ""), "")
st, _h, d = _req_gov("GET", f"/api/tools/search?q=alpha")
check("B2 delisted: search misses", st == 200 and
      not any(t["id"] == tk_gov for t in (d.get("tools") or [])), str(d)[:100])
from governance.catalog import resolve
t, err = resolve(tk_gov, None)
t2, err2 = resolve(tk_gov, uid)
check("B3 resolve: public refuses, owner still resolves",
      t is None and err == "not_found" and t2 is not None,
      f"{err} {err2}")
st, _h, d = _req_gov("PATCH", f"/api/usertools/{tk_gov}", {"enabled": False},
                 {"Content-Type": "application/json", "Cookie": CK})
check("B4 dev enable PATCH refused while delisted (typed)",
      st == 403 and d.get("error") == "err_tool_delisted", f"{st} {d}")
with db() as con:
    au = con.execute(
        "select action, target_id from operator_audit where"
        " action='tool.delisted.update' and target_id=%s"
        " order by id desc limit 1", (tk_gov,)).fetchone()
check("B5 delist audited", au is not None, str(au))
set_tool_delisted(op, tk_gov, False)
st, _h, market = _req_gov("GET", "/tools?q=Alpha")
check("B6 restore revives listing", tk_gov in (market or ""))
# dev enable writes audit
st, _h, d = _req_gov("PATCH", f"/api/usertools/{tk_gov}", {"enabled": False},
                 {"Content-Type": "application/json", "Cookie": CK})
with db() as con:
    au = con.execute(
        "select after_json from operator_audit where"
        " action='tool.enabled.update' and target_id=%s"
        " order by id desc limit 1", (tk_gov,)).fetchone()
check("B7 developer enable/disable audited (actor=owner)",
      st == 200 and au is not None
      and au["after_json"]["enabled"] is False,
      f"{st} {au}")

# ═══ C. suspended provider leaves the market ═══
_r3 = _publish(CK, "ccc", "Gamma ZZZ")
tk3 = (_r3[2] or {}).get("tool_key")
# publish gate: C/D need this tool PUBLIC — unlock with one successful
# owner call (the A-section pattern), same as any real author would
with db() as con:
    con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, started_at, finished_at, commission_rate_snapshot,"
        " invocation_hash) values (%s, %s, %s, 'ok', 0, %s, %s, 0, %s)",
        (f"c2a-run3-{_u}", uid, tk3, int(time.time()), int(time.time()),
         "b" * 64))
_req_gov("PATCH", f"/api/usertools/{tk3}", {"visibility": "public"},
     {"Content-Type": "application/json", "Cookie": CK})
with db() as con:
    con.execute("update users set status='suspended' where id=%s", (uid,))
st, _h, home = _req_gov("GET", "/")
st, _h, market = _req_gov("GET", "/tools")
check("C1 suspended provider: home AND market both hide",
      tk3 not in (home or "") and tk3 not in (market or ""),
      f"home={tk3 in (home or '')} market={tk3 in (market or '')}")
with db() as con:
    con.execute("update users set status='active' where id=%s", (uid,))

# ═══ D. search: one ordering + desc match ═══
# make the tool featured and give it a desc, then search by desc word
with db() as con:
    con.execute("update platform_tools set featured=true, sort_order=1,"
                " desc_json='{\"en\":\"unusualxy multiplier\"}'"
                " where tool_key=%s", (tk3,))
st, _h, d = _req_gov("GET", "/api/tools/search?q=unusualxy")
hits = [t["id"] for t in (d.get("tools") or [])]
check("D1 search matches desc (all languages)", tk3 in hits, str(hits)[:100])
st, _h, d2 = _req_gov("GET", "/api/tools/search?q=gamma")
hits2 = [t["id"] for t in (d2.get("tools") or [])]
check("D2 label still matches", tk3 in hits2, str(hits2)[:100])
# ordering parity: two label matches, the featured one ranks first
st, _h, d = _req_gov("PATCH", f"/api/usertools/{tk_gov}", {"enabled": True},
                 {"Content-Type": "application/json", "Cookie": CK})
_r4 = _publish(CK, "ddd", "Delta Tool")
_ddd = (_r4[2] or {}).get("tool_key")
with db() as con:
    con.execute(
        "insert into tool_calls (client_ref, user_id, tool_id, status,"
        " cost_snapshot, started_at, finished_at, commission_rate_snapshot,"
        " invocation_hash) values (%s, %s, %s, 'ok', 0, %s, %s, 0, %s)",
        (f"c2a-run4-{_u}", uid, _ddd,
         int(time.time()), int(time.time()), "c" * 64))
_req_gov("PATCH", f"/api/usertools/{_ddd}", {"visibility": "public"},
     {"Content-Type": "application/json", "Cookie": CK})
with db() as con:
    con.execute("update platform_tools set featured=true, sort_order=99"
                " where tool_key=%s", (tk_gov,))
st, _h, d3 = _req_gov("GET", "/api/tools/search?q=Tool")
ids3 = [t["id"] for t in (d3.get("tools") or [])]
check("D3 featured tool ranks first (list ordering parity)",
      ids3 and ids3[0] == tk_gov and _ddd in ids3,
      str(ids3[:5]))
with db() as con:
    con.execute("update platform_tools set featured=false, desc_json=null"
                " where tool_key=%s", (tk3,))

# ═══ E. author visibility ═══
# home-polish: the card is a single navigation surface (→ tool page);
# the author renders as text on the card, the author page is reached
# from the tool page.
st, _h, found = _req_gov("GET", "/tools?q=Alpha")
check("E1 community card shows the author", f'@{dev_ck_user}' in (found or ""))
st, _h, market = _req_gov("GET", "/tools")
check("E2 market card shows the author", f'@{dev_ck_user}' in (market or ""))
# MCP summaries
from identity.apikeys import create_key
from economy.billing import grant as _grant
_grant(uid, 1000, "manual_topup", idem_key=f"c2a:{_u}", note="c2a")
TOK = create_key(uid, "c2a")["key"]
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream",
     "Authorization": f"Bearer {TOK}"}
st, _h, d = _req_gov("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
                                   "method": "tools/list"}, H)
summaries = (d.get("result") or {}).get("tools") or []
mine = [t for t in summaries if t["name"] in (tk_gov, tk3)]
check("E3 MCP summary notes the author",
      mine and all(f"by @{dev_ck_user}" in t["description"] for t in mine),
      str([t["description"][:60] for t in mine])[:120])
st, _h, d = _req_gov("POST", "/mcp",
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": "gamma"}}}, H)
tools = ((d.get("result") or {}).get("structuredContent") or {}).get("tools") or []
check("E4 catalog_search summary notes the author",
      tools and all(f"by @{dev_ck_user}" in t["description"] for t in tools),
      str([t["description"][:60] for t in tools])[:120])
st, _h, llms = _req_gov("GET", "/llms.txt")
check("E5 llms.txt catalog notes the author",
      f"by @{dev_ck_user}" in (llms or ""))
# official tools: no fabricated author
st, _h, d = _req_gov("POST", "/mcp",
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": "compress"}}}, H)
tools = ((d.get("result") or {}).get("structuredContent") or {}).get("tools") or []
check("E6 official tools carry no author tag",
      tools and all("by @" not in t["description"] for t in tools),
      str([t["description"][:60] for t in tools])[:120])

# ═══ F. i18n + t fallback (source-level facts) ═══
_i18n_src = open(_os.path.join(REPO, "core", "i18n.py"),
                 encoding="utf-8").read()
check("F1 err_insufficient_credits present in all locales",
      _i18n_src.count('"err_insufficient_credits":') >= 10,
      str(_i18n_src.count('"err_insufficient_credits":')))
_engine = open(_os.path.join(REPO, "static", "pages", "base-engine.js"),
               encoding="utf-8").read()
check("F2 window.t falls back to en before the key name",
      "E[k]||k" in _engine.replace(" ", "") or "E[k] || k" in _engine,
      "")
_cfg = open(_os.path.join(REPO, "core", "config.py"),
            encoding="utf-8").read()
check("F3 no wrong-port defaults; required envs fail loud",
      "53843" not in _cfg and "53842" not in _cfg
      and "_required_env" in _cfg)

with db() as con:
    purge_users(con, [uid, op])


# ════════════════════════════════════════════════════════════════════════
# Owner verified badge projection
#
# Owner verified badge canonical — batch membership predicate + projection.
#
# A*  effective_member_ids() is the batch read side of plan_of().member:
#     identical verdicts for active / expired / no-membership users, one
#     round trip, empty input → empty set (fail-closed toward "no badge").
# B*  Catalog projection: third-party tools carry owner_verified driven
#     by that same predicate; first-party rows (owner NULL) always
#     project False. Official tools never badge.
# C*  Badge disappearance: the moment the membership is no longer
#     effective (renews_at in the past), the projection flips to False —
#     same predicate, no separate membership logic anywhere.
# ════════════════════════════════════════════════════════════════════════
section('Owner verified badge projection')

import os as _os
import sys as _sys
import time


ts_own = int(time.time())
_sfx = _os.urandom(3).hex()
from database import db
from economy.subscription import plan_of, effective_member_ids
from tools import load_all as _load_all
_load_all()
from governance.catalog import list_catalog, search_catalog, market_page


def _new_user(prefix):
    with db() as con:
        con.execute(
            "insert into users (username, display_name, status, created_at,"
            " last_login_at, updated_at)"
            " values (%s, %s, 'active', %s, %s, %s) returning id",
            (prefix + _sfx, prefix + _sfx, ts_own, ts_own, ts_own))
        return con.execute("select id from users where username=%s",
                           (prefix + _sfx,)).fetchone()["id"]


def _seed_plan(user_id, status, renews_at):
    with db() as con:
        row = con.execute("select id from plans where code=%s", ("plus",)).fetchone()
        if not row:
            con.execute(
                "insert into plans (code, name, monthly_credits, price_cents, created_at, updated_at)"
                " values ('plus', 'PLUS', 0, 0, %s, %s) returning id", (ts_own, ts_own))
            plan_id = con.execute("select id from plans where code='plus'").fetchone()["id"]
        else:
            plan_id = row["id"]
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id, status,"
            " started_at, renews_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict do nothing",
            (user_id, plan_id, f"sub:ov:{user_id}", status, ts_own, renews_at, ts_own))


# ═══ A. batch predicate ≡ plan_of().member (access projection verdict) ═══
u_active = _new_user("ovact")
u_expired = _new_user("ovexp")
u_free = _new_user("ovfree")
_seed_plan(u_active, "active", ts_own + 30 * 86400)
_seed_plan(u_expired, "active", ts_own - 100)

verdicts = {u: bool((plan_of(u) or {}).get("member"))
            for u in (u_active, u_expired, u_free)}
batch = effective_member_ids([u_active, u_expired, u_free])
check("OV-A1 active member in batch set", batch == {u_active},
      f"batch={batch} plan_of verdicts={verdicts}")
check("OV-A2 batch verdicts identical to plan_of().member",
      all(((u in batch) == verdicts[u]) for u in verdicts))
check("OV-A3 expired/absent memberships never badge",
      u_expired not in batch and u_free not in batch)
check("OV-A4 empty input → empty set (fail-closed)", effective_member_ids([]) == set())
check("OV-A5 duplicates collapse", effective_member_ids([u_active, u_active]) == {u_active})

# ═══ B/C. catalog projection owner_verified ═══
official = [t for t in list_catalog() if t["origin"] == "official"]
check("OV-B1 seeded catalog has first-party tools", len(official) >= 1, len(official))
check("OV-B2 official tool projects owner_verified=False",
      all(t["owner_verified"] is False for t in official))

KEY = official[0]["id"]
with db() as con:
    con.execute("update platform_tools set owner_id=%s where tool_key=%s",
                (u_active, KEY))
try:
    items = [t for t in search_catalog("") if t["id"] == KEY]
    check("OV-B3 verified owner surfaces on the market projection",
          len(items) == 1 and items[0]["owner_verified"] is True, items and items[0].get("owner_verified"))
    page = [t for t in market_page(q=KEY)["items"] if t["id"] == KEY]
    check("OV-B4 market_page projection carries the same flag",
          len(page) == 1 and page[0]["owner_verified"] is True)
    listed = [t for t in list_catalog() if t["id"] == KEY]
    check("OV-B5 list_catalog projection carries the same flag",
          len(listed) == 1 and listed[0]["owner_verified"] is True)
    from governance.catalog import owner_public_tools
    own = [t for t in owner_public_tools(u_active) if t["id"] == KEY]
    check("OV-B6 author-page projection carries owner_handle + verified",
          len(own) == 1 and own[0]["owner_handle"]
          and own[0]["owner_verified"] is True,
          own and (own[0].get("owner_handle"), own[0].get("owner_verified")))

    # ═══ C. expiry flips the flag — no second membership judgment ═══
    with db() as con:
        con.execute("update memberships set renews_at=%s where user_id=%s",
                    (ts_own - 100, u_active))
    after = [t for t in search_catalog("") if t["id"] == KEY]
    check("OV-C1 expired membership → projection False (badge disappears)",
          len(after) == 1 and after[0]["owner_verified"] is False)
    after_own = [t for t in owner_public_tools(u_active) if t["id"] == KEY]
    check("OV-C2 author-page projection flips too",
          len(after_own) == 1 and after_own[0]["owner_verified"] is False)
    check("OV-C3 plan_of agrees with the projection",
          (plan_of(u_active) or {}).get("member") is False)
finally:
    with db() as con:
        con.execute("update platform_tools set owner_id=NULL where tool_key=%s", (KEY,))

with db() as con:
    purge_users(con, [u_active, u_expired, u_free])


# ════════════════════════════════════════════════════════════════════════
# Favorites (web + MCP)
#
# Favorites canonical — 我的收藏 on the web and in MCP.
#
# W*  Web API: pin/unpin a visible tool; GET lists it; an unknown tool
#     and another user's non-public tool are refused (404); the account
#     page renders the pinned tool; the tool page star reflects state.
# M*  MCP master list + independent favorites: tools/list = meta (incl.
#     pin_tool/unpin_tool/list_favorites) + every public tool + own
#     tools, each with a favorite flag. A community tool is present
#     with favorite:false before pinning, flag true after pin_tool,
#     false again after unpin_tool (still listed); list_favorites
#     carries exactly the pins; catalog_search finds it either way.
# ════════════════════════════════════════════════════════════════════════
section('Favorites (web + MCP)')

import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid


_HOST_fav = ("127.0.0.1", int(TEST_PORT))
_u = "fv" + _uuid.uuid4().hex[:6]
ts_fav = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


def _req_fav(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST_fav, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form_fav(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_fav(body, token):
    st, _h, d, _sc = _req_fav("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call_fav(name, arguments, token, mid=1):
    return _mcp_fav({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _b64(b):
    return base64.b64encode(b).decode()


def _tool_text(d):
    """structuredContent — the machine channel (content text is the
    readable summary)."""
    return (((d or {}).get("result") or {}).get("structuredContent")
            or {})


def _register(name):
    b, ct = _form_fav({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req_fav("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form_fav({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req_fav("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req_fav("POST", "/api/keys", {"name": "fv"},
                             {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


def _tools(token):
    _st, d = _mcp_fav({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, token)
    return [t for t in ((d or {}).get("result") or {}).get("tools", [])]


def _names(token):
    return [t["name"] for t in _tools(token)]


def _flag(token, mcp_name):
    return next((t.get("favorite") for t in _tools(token)
                 if t.get("name") == mcp_name), None)


def _fav_names(token):
    _st, d = _mcp_fav({"jsonrpc": "2.0", "id": 19, "method": "tools/call",
                   "params": {"name": "list_favorites",
                              "arguments": {}}}, token)
    return [t["name"] for t in
            (((d or {}).get("result") or {}).get("structuredContent")
             or {}).get("tools", [])]


def _fav(cookie, key, on):
    return _req_fav("POST", "/api/favorites", {"tool_key": key, "on": on},
                {"Content-Type": "application/json", "Cookie": cookie})


from database import db  # noqa: E402

uids = []
try:
    a_uid, a_ck_fav, A_TOK = _register(_u + "dev")
    r_uid, r_ck, R_TOK = _register(_u + "usr")
    uids = [a_uid, r_uid]
    tid = "favtool"
    st, _h, d, _sc = _req_fav("POST", "/api/usertools", {
        "label": "Fav Tool", "url": "https://93.184.216.34/x",
        "timeout_s": 30, "files_min": 0, "files_max": 1,
        "format_families": "image", "accept": [".png"], "outputs": ["text"],
        "cost": 0}, {"Content-Type": "application/json", "Cookie": a_ck_fav})
    key = (d or {}).get("tool_key")
    with db() as con:
        con.execute("update platform_tools set visibility='public'"
                    " where tool_key=%s", (key,))
    mname = key
    check("M0 setup: public community tool exists", st == 200, f"{st} {d}")

    # ═══ M: MCP master list + independent favorites ═══
    names = _names(R_TOK)
    check("M1 meta tools incl. pin_tool/unpin_tool/list_favorites",
          {"create_upload", "catalog_search", "pin_tool", "unpin_tool",
           "list_favorites"} <= set(names),
          names)
    check("M2 first-party tools listed", "share" in names and "compress" in names, names)
    check("M3 master list: community tool present, not yet favorite",
          mname in names and _flag(R_TOK, mname) is False, names)
    check("M4 author sees own tool in tools/list", mname in _names(A_TOK))
    st, d = _call_fav("catalog_search", {"q": "Fav Tool"}, R_TOK)
    found = [t["name"] for t in (((d or {}).get("result") or {})
                                 .get("structuredContent") or {}).get("tools", [])]
    check("M5 catalog_search finds the unpinned tool", mname in found, found)
    st, d = _call_fav("pin_tool", {"name": mname}, R_TOK)
    sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    check("M6 pin_tool ok", sc.get("favorite") is True and sc.get("name") == mname, d)
    check("M7 pinned: favorite flag true + list_favorites carries it",
          _flag(R_TOK, mname) is True and mname in _fav_names(R_TOK))
    st, d = _call_fav("pin_tool", {"name": "no_such_tool_xyz"}, R_TOK)
    _t = _tool_text(d)
    check("M8 pin unknown tool → isError tool_not_found",
          ((d or {}).get("result") or {}).get("isError") is True
          and _t.get("error") == "tool_not_found", d)
    st, d = _call_fav("unpin_tool", {"name": mname}, R_TOK)
    check("M9 unpin_tool ok — flag false, gone from list_favorites, still in master list",
          (((d or {}).get("result") or {}).get("structuredContent") or {}).get("favorite") is False
          and mname not in _fav_names(R_TOK)
          and _flag(R_TOK, mname) is False
          and mname in _names(R_TOK))

    # ═══ W: web ═══
    st, _h, d, _sc = _fav(r_ck, key, True)
    check("W1 web pin ok", st == 200 and (d or {}).get("ok"), f"{st} {d}")
    st, _h, d, _sc = _req_fav("GET", "/api/favorites", None, {"Cookie": r_ck})
    check("W2 GET lists it", (d or {}).get("tools") == [key], d)
    check("W3 web pin shows in MCP: flag true + in list_favorites",
          _flag(R_TOK, mname) is True and mname in _fav_names(R_TOK))
    st, _h, page, _sc = _req_fav("GET", "/account/favorites", None,
                             {"Cookie": r_ck, "Accept": "text/html"})
    check("W4 account page renders the pinned tool",
          st == 200 and f"/tool/{key}" in (page or ""), st)
    st, _h, page, _sc = _req_fav("GET", f"/tool/{key}", None,
                             {"Cookie": r_ck, "Accept": "text/html"})
    check("W5 tool page star pressed", st == 200
          and 'id="favBtn"' in (page or "") and 'aria-pressed="true"' in (page or ""), st)
    st, _h, d, _sc = _fav(r_ck, "nobody/nothing", True)
    check("W6 unknown tool → 404", st == 404 and (d or {}).get("error") == "tool_not_found", f"{st} {d}")
    with db() as con:
        con.execute("update platform_tools set visibility='private'"
                    " where tool_key=%s", (key,))
    st, _h, d, _sc = _fav(r_ck, key, True)
    check("W7 another user's private tool → 404", st == 404, f"{st} {d}")
    st, _h, d, _sc = _req_fav("GET", "/api/favorites", None, {"Cookie": r_ck})
    check("W8 no-longer-visible favorite drops out of the list",
          (d or {}).get("tools") == [], d)
    st, _h, d, _sc = _fav(r_ck, key, False)
    check("W9 unpin always allowed", st == 200, f"{st} {d}")
    st, _h, d, _sc = _req_fav("POST", "/api/favorites", {"tool_key": key, "on": True},
                          {"Content-Type": "application/json"})
    check("W10 anonymous → 401", st == 401, st)
finally:
    if uids:
        with db() as con:
            purge_users(con, uids)


# ════════════════════════════════════════════════════════════════════════
# User tool declaration+pricing transaction
#
# User tool PATCH/DELETE declaration+pricing single-transaction canonical.
#
# 一个 user tool 的商业状态 mutation (platform_tools declaration + pricing)
# 属于同一次用户操作时必须同一 DB transaction 提交或回滚。
#
# Fault injection: the app under test runs IN-PROCESS (real uvicorn socket,
# same DATABASE_URL), so monkeypatching repo.catalog.upsert_pricing reaches
# the server's pricing writer. Happy paths + fault paths both go over real
# HTTP; rollback is proven by direct DB row reads.
# ════════════════════════════════════════════════════════════════════════
section('User tool declaration+pricing transaction')

import json as _json, uuid, http.client, sys, threading, time, socket
import os as _os, sys as _sys

_PORT = int(TEST_PORT, 10)
if _PORT == 8000:
    _sys.stderr.write("ERROR: never the production port.\n")
    _sys.exit(2)
_HOST_utx = ("127.0.0.1", _PORT)


def mp_utx(fields):
    out = b""
    for k, v in fields.items():
        out += f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"

def req(method, path, body, cookie=None, host=_HOST_utx):
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

def new_user(host=_HOST_utx):
    conn = http.client.HTTPConnection(*host, timeout=30)
    u = "tx" + uuid.uuid4().hex[:6]
    b, ct = mp_utx({"username": u, "password": "Test12345",
                "password_confirm": "Test12345", "email": f"{u}@t.co"})
    conn.request("POST", "/auth/register", b, {"Content-Type": ct}); conn.getresponse().read()
    b, ct = mp_utx({"login": u, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    resp = conn.getresponse(); resp.read()
    conn.close()
    return u, (resp.getheader("Set-Cookie") or "").split(";")[0]

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
_tool_key = None  # 从 POST 响应取 (系统 slug)
st, r = req("POST", "/api/usertools", {
    "label": {"en": "Tx Tool"}, "execution": "http",
    "url": "https://93.184.216.34/anything", "timeout_s": 30,
    "visibility": "private", "format_families": "document",
    "accept": [".pdf"], "cost": 5}, cookie=cookie, host=_HOST2)
_tool_key = (r or {}).get("tool_key")
check("T0 create ok (cost=5)", st == 200 and r.get("ok") and _tool_key,
      f"{st} {r}")

# ── 1. PATCH declaration + cost normal → both land together ──
st, r = req("PATCH", f"/api/usertools/{_tool_key}",
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
    st, r = req("PATCH", f"/api/usertools/{_tool_key}",
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
st, r = req("DELETE", f"/api/usertools/{_tool_key}", None, cookie=cookie, host=_HOST2)
row_pt = db_row("select count(*) from platform_tools where tool_key=%s", (_tool_key,))
row_pr = db_row("select cost, enabled from pricing where tool_id=%s", (_tool_key,))
check("T3 DELETE → tool row deleted + pricing disabled",
      st == 200 and r.get("ok") and row_pt[0] == 0
      and row_pr[0] == 0 and row_pr[1] is False,
      f"{st} {r} pt={row_pt} pr={row_pr}")

# ── 4. DELETE pricing write fail → tool row survives, pricing untouched ──
_tool2 = "txdl" + uuid.uuid4().hex[:4]
_tool2_key = None  # 从 POST 响应取
st, r = req("POST", "/api/usertools", {
    "label": {"en": "Del Fail"}, "execution": "http",
    "url": "https://93.184.216.34/anything", "timeout_s": 30,
    "visibility": "private", "format_families": "document",
    "accept": [".pdf"], "cost": 3}, cookie=cookie, host=_HOST2)
_tool2_key = (r or {}).get("tool_key")
check("T4a second tool created (cost=3)", st == 200 and r.get("ok") and _tool2_key,
      f"{st} {r}")
_fault["boom"] = True
try:
    st, r = req("DELETE", f"/api/usertools/{_tool2_key}", None, cookie=cookie, host=_HOST2)
except Exception:
    st = 500
_fault["boom"] = False
row_pt = db_row("select count(*) from platform_tools where tool_key=%s", (_tool2_key,))
row_pr = db_row("select cost, enabled from pricing where tool_id=%s", (_tool2_key,))
check("T4 pricing fail → tool row still exists + pricing original state",
      row_pt[0] == 1 and row_pr[0] == 3 and row_pr[1] is True,
      f"pt={row_pt} pr={row_pr}")

# ── 5. enabled PATCH 拒绝 (bad_enabled) → 零审计、工具不变 ──
_a0 = db_row("select count(*) from operator_audit where"
             " action='tool.enabled.update' and target_id=%s", (_tool2_key,))
st, r = req("PATCH", f"/api/usertools/{_tool2_key}",
            {"enabled": "false"}, cookie=cookie, host=_HOST2)
_a1 = db_row("select count(*) from operator_audit where"
             " action='tool.enabled.update' and target_id=%s", (_tool2_key,))
row_en = db_row("select enabled from platform_tools where tool_key=%s",
                (_tool2_key,))
check("T5 enabled='false' (non-bool) → 422 bad_enabled, zero audit, unchanged",
      st == 422 and r.get("error") == "bad_enabled"
      and _a1[0] == _a0[0] and row_en[0] is True,
      f"{st} {r} audit {_a0[0]}->{_a1[0]} en={row_en}")

# ── 6. 合法 enabled 修改 → 恰一条准确审计 ──
st, r = req("PATCH", f"/api/usertools/{_tool2_key}",
            {"enabled": False}, cookie=cookie, host=_HOST2)
_rows = db_row(
    "select count(*), count(*) filter"
    " (where after_json->>'enabled' = 'false')"
    " from operator_audit where action='tool.enabled.update'"
    " and target_id=%s", (_tool2_key,))
row_en = db_row("select enabled from platform_tools where tool_key=%s",
                (_tool2_key,))
check("T6 legal enable→disable commits with exactly one accurate audit",
      st == 200 and r.get("ok") and row_en[0] is False
      and _rows[0] == _a0[0] + 1 and int(_rows[1]) == 1,
      f"{st} {r} audit rows={_rows} en={row_en}")

# ── 7. 正文工具 PATCH 撞名 (换 URL 带撞名查询键) → 供给单点 422 零写入 ──
st, r = req("POST", "/api/usertools", {
    "label": {"en": "Tx Body"}, "execution": "http",
    "url": "https://93.184.216.34/bb", "timeout_s": 30,
    "visibility": "private", "files_min": 0, "files_max": 0,
    "body_format": "text", "params": [
        {"key": "q", "type": "text", "label": {"en": "Q"}}]},
    cookie=cookie, host=_HOST2)
_bkey = (r or {}).get("tool_key")
check("T7a body tool created", st == 200 and r.get("ok") and _bkey,
      f"{st} {str(r)[:90]}")
st, r = req("PATCH", f"/api/usertools/{_bkey}",
            {"url": "https://93.184.216.34/bb?q=1"}, cookie=cookie,
            host=_HOST2)
_row_b = db_row("select endpoint_json from platform_tools"
                " where tool_key=%s", (_bkey,))
check("T7 PATCH url with colliding query key → 422 bad_binding, zero write",
      st == 422 and r.get("error") == "bad_binding"
      and _row_b is not None and "q=1" not in (_row_b[0] or ""),
      f"{st} {r} ep={_row_b}")

_db.close()


finish()
