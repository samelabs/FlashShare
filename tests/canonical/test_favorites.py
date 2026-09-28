#!/usr/bin/env python3
"""Favorites canonical — 我的收藏 on the web and in MCP.

  W*  Web API: pin/unpin a visible tool; GET lists it; an unknown tool
      and another user's non-public tool are refused (404); the account
      page renders the pinned tool; the tool page star reflects state.
  M*  MCP working set: tools/list = meta (incl. pin_tool/unpin_tool) +
      first-party + own tools + favorites. A community tool is absent
      until pinned, present after pin_tool, absent after unpin_tool, and
      catalog_search finds it either way.
"""
import base64
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

_REPO = _os.path.abspath(_os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "fv" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n%%EOF\n"


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
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


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp(body, token):
    st, _h, d, _sc = _req("POST", "/mcp", body, {
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
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "fv"},
                             {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


def _names(token):
    _st, d = _mcp({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, token)
    return [t["name"] for t in ((d or {}).get("result") or {}).get("tools", [])]


def _fav(cookie, key, on):
    return _req("POST", "/api/favorites", {"tool_key": key, "on": on},
                {"Content-Type": "application/json", "Cookie": cookie})


from database import db  # noqa: E402

uids = []
try:
    a_uid, a_ck, A_TOK = _register(_u + "dev")
    r_uid, r_ck, R_TOK = _register(_u + "usr")
    uids = [a_uid, r_uid]
    tid = "favtool"
    key = f"{_u}dev/{tid}"
    st, _h, d, _sc = _req("POST", "/api/usertools", {
        "tool_id": tid, "label": "Fav Tool", "url": "https://93.184.216.34/x",
        "timeout_s": 30, "files_min": 0, "files_max": 1,
        "format_families": "image", "accept": [".png"], "outputs": ["text"],
        "cost": 0}, {"Content-Type": "application/json", "Cookie": a_ck})
    with db() as con:
        con.execute("update platform_tools set visibility='public'"
                    " where tool_key=%s", (key,))
    mname = key.replace("/", "__")
    check("M0 setup: public community tool exists", st == 200, f"{st} {d}")

    # ═══ M: MCP working set ═══
    names = _names(R_TOK)
    check("M1 meta tools incl. pin_tool/unpin_tool",
          {"create_upload", "catalog_search", "pin_tool", "unpin_tool"} <= set(names),
          names)
    check("M2 first-party tools listed", "share" in names and "compress" in names, names)
    check("M3 community tool NOT listed before pinning", mname not in names, names)
    check("M4 author sees own tool in tools/list", mname in _names(A_TOK))
    st, d = _call("catalog_search", {"q": "Fav Tool"}, R_TOK)
    found = [t["name"] for t in (((d or {}).get("result") or {})
                                 .get("structuredContent") or {}).get("tools", [])]
    check("M5 catalog_search finds the unpinned tool", mname in found, found)
    st, d = _call("pin_tool", {"name": mname}, R_TOK)
    sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    check("M6 pin_tool ok", sc.get("favorite") is True and sc.get("name") == mname, d)
    check("M7 pinned tool now in tools/list", mname in _names(R_TOK))
    st, d = _call("pin_tool", {"name": "no_such_tool_xyz"}, R_TOK)
    _t = _tool_text(d)
    check("M8 pin unknown tool → isError tool_not_found",
          ((d or {}).get("result") or {}).get("isError") is True
          and _t.get("error") == "tool_not_found", d)
    st, d = _call("unpin_tool", {"name": mname}, R_TOK)
    check("M9 unpin_tool ok, gone from tools/list",
          (((d or {}).get("result") or {}).get("structuredContent") or {}).get("favorite") is False
          and mname not in _names(R_TOK))

    # ═══ W: web ═══
    st, _h, d, _sc = _fav(r_ck, key, True)
    check("W1 web pin ok", st == 200 and (d or {}).get("ok"), f"{st} {d}")
    st, _h, d, _sc = _req("GET", "/api/favorites", None, {"Cookie": r_ck})
    check("W2 GET lists it", (d or {}).get("tools") == [key], d)
    check("W3 web pin shows up in MCP tools/list", mname in _names(R_TOK))
    st, _h, page, _sc = _req("GET", "/account/favorites", None,
                             {"Cookie": r_ck, "Accept": "text/html"})
    check("W4 account page renders the pinned tool",
          st == 200 and f"/tool/{key}" in (page or ""), st)
    st, _h, page, _sc = _req("GET", f"/tool/{key}", None,
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
    st, _h, d, _sc = _req("GET", "/api/favorites", None, {"Cookie": r_ck})
    check("W8 no-longer-visible favorite drops out of the list",
          (d or {}).get("tools") == [], d)
    st, _h, d, _sc = _fav(r_ck, key, False)
    check("W9 unpin always allowed", st == 200, f"{st} {d}")
    st, _h, d, _sc = _req("POST", "/api/favorites", {"tool_key": key, "on": True},
                          {"Content-Type": "application/json"})
    check("W10 anonymous → 401", st == 401, st)
finally:
    if uids:
        with db() as con:
            purge_users(con, uids)

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
