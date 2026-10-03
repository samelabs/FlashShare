#!/usr/bin/env python3
"""Flow-clarity canonical — MCP discovery + the public developer guide.

  S*  Search single point: repo._market_base predicate for every surface
      (home selector API / /tools market / MCP catalog_search) — same
      ordering, multi-token AND, % and _ literal, empty query browses.
  M*  catalog_search returns the exact callable spec tools/list carries
      (shared constructor) + favorite flag; a hit is callable by name
      without listing the tool first.
  L*  initialize instructions and llms.txt carry the caller's / the free
      tier's real account limits from the entitlement projection.
  D*  /developers: anonymous 200, every number rendered from the code
      constants, the documented example request built by the real
      dispatch_payload and the example response validated by the real
      parse_endpoint_items; nav/footer/account wiring points at it.
"""
import base64
import http.client as _hc
import http.server
import json as _json
import os as _os
import re as _re
import sys as _sys
import threading
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
_u = "fc" + _uuid.uuid4().hex[:6]


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


def _mcp(body, token, mid=1):
    st, _h, d, _sc = _req("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token):
    return _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _sc_tools(payload):
    return (((payload or {}).get("result") or {})
            .get("structuredContent") or {}).get("tools") or []


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "fc"},
                              {"Content-Type": "application/json", "Cookie": ck})
    return ck, d_k["key"]["key"]


def _publish(cookie, tool_id, extra=None):
    body = {"tool_id": tool_id, "label": tool_id,
            "url": "https://93.184.216.34/x", "timeout_s": 30,
            "files_min": 0, "files_max": 1,
            "format_families": "image", "accept": [".png"],
            "outputs": ["text"], "cost": 0}
    body.update(extra or {})
    return _req("POST", "/api/usertools", body,
                {"Content-Type": "application/json", "Cookie": cookie})


def _go_public(key):
    from database import db
    with db() as con:
        con.execute("update platform_tools set visibility='public'"
                    " where tool_key=%s", (key,))


# ── in-suite local developer endpoint (fx1 pattern) ──────────────────
class _DevHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        items = [{"kind": "text", "text": "flow ok"}]
        out = _json.dumps({"items": items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DevHandler)
LOCAL_URL = f"http://127.0.0.1:{_srv.server_address[1]}/ok"
threading.Thread(target=_srv.serve_forever, daemon=True).start()

from database import db  # noqa: E402

# code authorities the docs must not drift from
from config import MAX_UPLOAD_BYTES as _MAX_UPLOAD_BYTES  # noqa: E402
from remote import (dispatch_payload, parse_endpoint_items,  # noqa: E402
                    MIN_TIMEOUT_S, MAX_TIMEOUT_S)
from governance.tool_supply import MAX_INPUT_FILES  # noqa: E402
from governance.tool_params import TEXT_PARAM_MAX_CHARS  # noqa: E402
from execution.results import (MAX_RESULT_ITEMS,  # noqa: E402
                               MAX_INLINE_RESULT_BYTES)
from governance.access_projection import project  # noqa: E402
from governance.entitlements import resolve_from_membership  # noqa: E402
from governance.catalog import mcp_names, tool_key_for_mcp_name  # noqa: E402

a_uid = r_uid = None
try:
    a_ck, A_TOK = _register(_u + "dev")
    r_ck, R_TOK = _register(_u + "usr")
    with db() as con:
        a_uid = int(con.execute("select id from users where username=%s",
                                (_u + "dev",)).fetchone()["id"])
        r_uid = int(con.execute("select id from users where username=%s",
                                (_u + "usr",)).fetchone()["id"])

    # ══════════ S: search single point ══════════
    key_multi = f"{_u}dev/multialpha"
    key_one = f"{_u}dev/onlyalpha"
    key_pct = f"{_u}dev/pcthalf"
    key_und = f"{_u}dev/underscore"
    st, _h, d, _sc = _publish(a_ck, "multialpha",
                              {"label": "Multi Alpha Beta"})
    st, _h, d, _sc = _publish(a_ck, "onlyalpha", {"label": "Alpha only"})
    st, _h, d, _sc = _publish(a_ck, "pcthalf",
                              {"label": "Pct 50% off"})
    st, _h, d, _sc = _publish(a_ck, "underscore",
                              {"label": "fl_ow special"})
    check("S0 setup: four unlisted tools created",
          all(_req("GET", f"/tool/{k}", None,
                   {"Cookie": a_ck, "Accept": "text/html"})[0] == 200
              for k in (key_multi, key_one, key_pct, key_und)))
    # unlisted tools must not appear in search (public predicate)
    st, _h, d, _sc = _req("GET", "/api/tools/search?q=alpha")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S1 unlisted tools never in search",
          key_multi not in ids and key_one not in ids, ids)
    for k in (key_multi, key_one, key_pct, key_und):
        _go_public(k)
    # multi-token AND
    st, _h, d, _sc = _req("GET", "/api/tools/search?q=alpha+beta")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S2 multi-word AND hits the tool carrying both words",
          key_multi in ids and key_one not in ids, ids)
    st, _h, d, _sc = _req("GET", "/api/tools/search?q=beta")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S3 single word still matches", key_multi in ids, ids)

    # % and _ literal
    st, _h, d, _sc = _req("GET", "/api/tools/search?q=50%25")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S4 % matches literally", key_pct in ids, ids)
    st, _h, d, _sc = _req("GET", "/api/tools/search?q=fl_w")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S5 _ is NOT a wildcard (fl_w does not match fl_ow)",
          key_und not in ids, ids)
    st, _h, d, _sc = _req("GET", "/api/tools/search?q=fl_ow")
    ids = [t["id"] for t in (d or {}).get("tools") or []]
    check("S6 _ matches literally", key_und in ids, ids)

    # three entrances, same query, same order
    q = "alpha"
    st, _h, d, _sc = _req("GET", f"/api/tools/search?q={q}")
    api_ids = [t["id"] for t in (d or {}).get("tools") or []]
    st, _h, page, _sc = _req("GET", f"/tools?q={q}")
    market_ids = _re.findall(r'href="/tool/([a-z0-9_/%-]+)"',
                             page or "", _re.I)
    seen = []
    for k in market_ids:
        if k not in seen:
            seen.append(k)
    st, d = _mcp({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": q}}}, R_TOK)
    mcp_keys = [tool_key_for_mcp_name(t.get("name"))
                for t in _sc_tools(d)]
    check("S7 api/tools/search == /tools market order (first page)",
          api_ids == seen[:len(api_ids)], f"{api_ids} vs {seen}")
    check("S8 MCP catalog_search == api/tools/search order",
          mcp_keys == api_ids, f"{mcp_keys} vs {api_ids}")

    # empty query = browse, top 20, same ordering for every surface
    st, _h, d, _sc = _req("GET", "/api/tools/search")
    browse = [t["id"] for t in (d or {}).get("tools") or []]
    check("S9 empty query browses (non-empty, capped at 20)",
          0 < len(browse) <= 20, len(browse))
    st, _h, page, _sc = _req("GET", "/tools")
    market_all = []
    for k in _re.findall(r'href="/tool/([a-z0-9_/%-]+)"', page or "", _re.I):
        if k not in market_all:
            market_all.append(k)
    check("S10 empty browse == market page-1 head order",
          browse == market_all[:len(browse)], f"{browse[:5]} vs {market_all[:5]}")
    st, d = _mcp({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": ""}}}, R_TOK)
    mcp_browse = [tool_key_for_mcp_name(t.get("name"))
                  for t in _sc_tools(d)]
    check("S11 MCP empty query browses the same top list",
          mcp_browse == browse, f"{mcp_browse[:5]} vs {browse[:5]}")

    # ══════════ M: catalog_search returns callable specs ══════════
    key_call = f"{_u}dev/callme"
    st, _h, d, _sc = _publish(a_ck, "callme", {
        "label": "Callable Now",
        "params": [{"key": "mode", "type": "enum",
                    "label": "Mode", "required": False,
                    "options": [{"value": "fast"}, {"value": "slow"}],
                    "default": "fast"}]})
    check("M0 setup: callable tool created", st == 200 and (d or {}).get("ok"),
          f"{st} {d}")
    # test plumbing: trusted row so the driver may reach 127.0.0.1
    with db() as con:
        con.execute("update platform_tools set trusted = TRUE"
                    " where tool_key=%s", (key_call,))
    st, _h, d, _sc = _req("PATCH", "/api/usertools/callme",
                          {"url": LOCAL_URL, "timeout_s": 30},
                          {"Content-Type": "application/json", "Cookie": a_ck})
    check("M0b endpoint pointed at the local listener",
          st == 200 and (d or {}).get("ok"), f"{st} {d}")
    _go_public(key_call)
    mname = key_call.replace("/", "__")

    st, d = _call("catalog_search", {"q": "Callable"}, R_TOK)
    hits = [t for t in _sc_tools(d) if t.get("name") == mname]
    check("M1 search returns the tool", len(hits) == 1, _sc_tools(d))
    hit = hits[0] if hits else {}
    check("M2 hit carries the full spec + favorite",
          {"name", "title", "description", "inputSchema", "outputSchema",
           "annotations", "favorite"} <= set(hit), sorted(hit))
    check("M3 unpinned → favorite False", hit.get("favorite") is False, hit.get("favorite"))
    # inputSchema before pinning must equal the tools/list spec after
    # pinning (same constructor, this bearer's entitlement enums)
    st, d = _call("pin_tool", {"name": mname}, R_TOK)
    check("M4 pin ok", st == 200 and not ((d or {}).get("result") or {}).get("isError"), d)
    st, d = _mcp({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, R_TOK)
    listed = [t for t in ((d or {}).get("result") or {}).get("tools") or []
              if t.get("name") == mname]
    check("M5 pinned tool in tools/list", len(listed) == 1, )
    listed_spec = listed[0] if listed else {}
    same = {k: hit.get(k) for k in
            ("name", "title", "description", "inputSchema",
             "outputSchema", "annotations")}
    check("M6 search spec == tools/list spec (shared constructor)",
          all(listed_spec.get(k) == v for k, v in same.items()),
          f"{ {k: listed_spec.get(k) == v for k, v in same.items()} }")
    st, d = _call("catalog_search", {"q": "Callable"}, R_TOK)
    hits = [t for t in _sc_tools(d) if t.get("name") == mname]
    check("M7 pinned → favorite True",
          hits and hits[0].get("favorite") is True, )
    # call it by name using only what the search result declared
    st, d = _call("unpin_tool", {"name": mname}, R_TOK)
    check("M8 unpin (call must work without listing)", st == 200, )
    st, d = _call(mname, {"params": {"mode": "slow"},
                          "client_ref": f"fc-{_u}-1"}, R_TOK)
    res = (d or {}).get("result") or {}
    check("M9 unpinned tool callable straight from the search spec",
          res.get("isError") is not True
          and "flow ok" in _json.dumps(res.get("structuredContent") or {}),
          str(d)[:200])

    # ══════════ L: limits in instructions + llms.txt ══════════
    ent = project(r_uid)["entitlements"]
    st, d = _mcp({"jsonrpc": "2.0", "id": 8, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26"}}, R_TOK)
    ins = ((d or {}).get("result") or {}).get("instructions") or ""
    from utils import format_bytes
    want = [
        f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB per input file",
        f"up to {ent['ingress_files_24h']} files",
        format_bytes(ent["ingress_bytes_24h"]),
        f"expiry_days {list(ent['expiry_days'])}",
        f"allowed_downloads {list(ent['download_limits'])}",
    ]
    for w in want:
        check(f"L1 instructions carry '{w[:44]}'", w in ins, ins[-300:])
    st, _h, llms, _sc = _req("GET", "/llms.txt")
    free = resolve_from_membership(None)
    lwant = [
        "## Limits",
        f"- Per file: {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        f"{free['ingress_files_24h']} files",
        format_bytes(free["ingress_bytes_24h"]),
        f"expiry_days {list(free['expiry_days'])}",
        "## Publish your own tool",
        "/developers",
    ]
    for w in lwant:
        check(f"L2 llms.txt carries '{w[:44]}'", w in (llms or ""),
              (llms or "")[:200])

    # ══════════ D: /developers ══════════
    st, _h, page, _sc = _req("GET", "/developers")
    check("D1 /developers anonymous 200", st == 200, st)
    page = page or ""
    check("D2 start-publishing button → /account/tools/publish",
          'href="/account/tools/publish"' in page)
    dwant = [
        str(_MAX_UPLOAD_BYTES // 1024 // 1024),
        str(MAX_INPUT_FILES),
        f"{MIN_TIMEOUT_S}–{MAX_TIMEOUT_S}",
        str(MAX_RESULT_ITEMS),
        str(MAX_INLINE_RESULT_BYTES),
        str(TEXT_PARAM_MAX_CHARS),
    ]
    for w in dwant:
        check(f"D3 page renders constant '{w}'", w in page, w)
    check("D4 nav + footer + mobile drawer link /developers",
          page.count('href="/developers"') >= 3,
          page.count('href="/developers"'))
    check("D5 zh locale renders the zh doc", "发布步骤" in _req(
        "GET", "/developers?lang=zh")[2], )
    # example request built by the REAL constructor
    m = _re.search(r'<pre class="doc-pre" id="ex-req">(.*?)</pre>',
                   page, _re.S)
    ex_req = _json.loads((m.group(1).strip().split("\n\n", 1)[1]
                          if m else "{}"))
    built = dispatch_payload(
        [{"filename": f["filename"], "content_type": f["content_type"],
          "size": f["size"], "url": f["url"]}
         for f in ex_req.get("files") or []],
        ex_req.get("params"))
    check("D6 example request == real dispatch_payload output",
          built == ex_req, f"{built} vs {ex_req}")
    # example response validated by the REAL validator
    m = _re.search(r'<pre class="doc-pre" id="ex-res">(.*?)</pre>',
                   page, _re.S)
    ex_res = _json.loads(m.group(1).strip() if m else "{}")

    def _no_fetch(u):
        raise AssertionError("example must not need a fetch: " + u)

    items = parse_endpoint_items(ex_res, _no_fetch)
    check("D7 example response passes real parse_endpoint_items",
          [it["kind"] for it in items] == ["text", "json", "file"],
          [it.get("kind") for it in items])
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
        "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
    check("D8 file item normalized with real size",
          items[-1]["size"] == len(png) and items[-1]["filename"] == "out.png",
          items[-1])
    # account tools page: one-line guide link, no collapsed contract
    st, _h, acct, _sc = _req("GET", "/account/tools", None,
                             {"Cookie": a_ck, "Accept": "text/html"})
    check("D9 account/tools links the guide, contract block gone",
          st == 200 and 'href="/developers"' in (acct or "")
          and "contract-doc" not in (acct or ""), st)

finally:
    try:
        _srv.shutdown()
    except Exception:
        pass
    uids = [u for u in (a_uid, r_uid) if u]
    with db() as con:
        purge_users(con, uids)
        con.execute("delete from pricing where tool_id LIKE %s",
                    (f"{_u}dev/%",))

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
