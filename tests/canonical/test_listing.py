#!/usr/bin/env python3
"""Listing canonical — browse vs search, list paging bounds, text-only tools.

  L*  Web browse lists (home grid, /tools, /api/tools/search) carry only
      featured tools; search finds every public tool; featuring a tool
      puts it on the browse lists. First-party seed rows start featured.
      MCP catalog_search with an EMPTY query enumerates the FULL public
      catalog (featured or not) — the agent-side browse; /llms.txt keeps
      listing recommended tools only.
  P*  Paging inputs never reach SQL out of range: negative / huge
      offsets and pages answer 200 (web) or -32602 (MCP explicit
      illegal values); catalog_search pages with page/total_pages.
  T*  Text-only tools (files 0..0): created without format family /
      accept, accept with files_max 0 is refused, the MCP spec carries
      no files argument, switching back to files requires accept.
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
_u = "ls" + _uuid.uuid4().hex[:6]
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
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "ls"},
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
    _st, _h, d, _sc = _req("GET", "/api/tools/search?q=")
    return [t["id"] for t in (d or {}).get("tools", [])]


def _usertool(ck, body):
    return _req("POST", "/api/usertools", body,
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
    _st, _h, d, _sc = _req("GET", "/api/tools/search?q=" + _u)
    check("L3 search finds the unfeatured tool",
          key in [t["id"] for t in (d or {}).get("tools", [])], d)
    _st, _h, page, _sc = _req("GET", "/tools", None, {"Accept": "text/html"})
    check("L4 /tools browse omits it", f'data-tool-id="{key}"' not in (page or ""))
    _st, _h, page, _sc = _req("GET", "/tools?q=" + _u, None, {"Accept": "text/html"})
    check("L5 /tools?q= finds it", f'data-tool-id="{key}"' in (page or ""))
    _st, _h, page, _sc = _req("GET", "/", None, {"Accept": "text/html"})
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
    _st, _h, txt, _sc = _req("GET", "/llms.txt")
    check("L9 llms.txt lists recommended tools only",
          mname not in (txt or "") and "Recommended tools" in (txt or ""))
    with db() as con:
        con.execute("update platform_tools set featured = true"
                    " where tool_key=%s", (key,))
    check("L10 featuring puts it on the browse list", key in _api_browse())
    _st, _h, page, _sc = _req("GET", "/tools", None, {"Accept": "text/html"})
    check("L11 featured tool on the /tools browse list",
          f'data-tool-id="{key}"' in (page or ""))

    # ═══ P: paging bounds ═══
    for path in ("/api/uploads?offset=-5",
                 "/api/uploads?offset=99999999999999999999999",
                 "/account/credits?page=99999999999999999999",
                 "/api/earnings?rev_page=99999999999999999999",
                 "/api/earnings?wd_page=-1"):
        st, _h, _d, _sc = _req("GET", path, None,
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
        "timeout_s": 30, "files_min": 0, "files_max": 0, "outputs": ["text"],
        "params": [{"key": "q", "type": "text", "label": "Q"}], "cost": 0})
    _txt_key = (d or {}).get("tool_key")
    check("T1 text-only create without format family/accept", st == 200 and _txt_key,
          f"{st} {d}")
    st, _h, d, _sc = _usertool(a_ck, {
        "label": "Txt", "url": "https://93.184.216.34/u",
        "timeout_s": 30, "files_min": 0, "files_max": 0,
        "format_families": "image", "accept": [".png"], "outputs": ["text"],
        "cost": 0})
    check("T2 accept with files_max 0 → bad_accept",
          st == 422 and (d or {}).get("error") == "bad_accept", f"{st} {d}")
    spec = _names(A_TOK).get(_txt_key) or {}
    props = (spec.get("inputSchema") or {}).get("properties") or {}
    check("T3 MCP spec has no files argument",
          spec and "files" not in props and "params" in props, spec)
    st, _h, d, _sc = _req("PATCH", f"/api/usertools/{_txt_key}",
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
    st, _h, d, _sc = _req("POST", "/api/usertools", {
        "label": "With ID", "url": "https://93.184.216.34/x",
        "timeout_s": 30, "files_min": 1, "files_max": 1,
        "format_families": "image", "accept": [".png"], "outputs": ["file"],
        "cost": 0, "tool_id": "rejected"}, {"Content-Type": "application/json", "Cookie": a_ck})
    check("S3 request carrying tool_id → 422 invalid_request",
          st == 422 and (d or {}).get("error") == "invalid_request", f"{st} {d}")
    st, _h, d, _sc = _req("PATCH", f"/api/usertools/{key}",
                          {"label": "Not Mine"}, {"Content-Type": "application/json",
                                                  "Cookie": _other_ck})
    check("S4 PATCH by another user's key → 404", st == 404, st)
    st, _h, d, _sc = _req("DELETE", f"/api/usertools/{key}", None,
                          {"Cookie": _other_ck})
    check("S5 DELETE by another user's key → 404", st == 404, st)
finally:
    if uids:
        with db() as con:
            purge_users(con, uids)

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
