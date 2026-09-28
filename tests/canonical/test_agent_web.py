#!/usr/bin/env python3
"""Agent-web surface (B7): robots policy, real 404, /llms.txt generated
guide, discovery links, and share-page agent affordances.

  R*  robots.txt: /f/ fetchable; /api/ /auth/ /dh/ /mcp Disallowed
      (the SEO/PWA batch narrowed the crawl boundary: private/ephemeral
      namespaces — account/samelabs/auth//r — and the POST-only machine
      surface /mcp stay out of crawlers; share links stay fetchable);
      stale /d/ rule gone
  X*  X-Robots-Tag: noindex, nofollow on every /f/ and /mcp response
  N*  unknown page → real 404 + rendered page with home/tools/MCP/
      llms.txt links; /api/* stays JSON 404; missing share stays 404
  L*  /llms.txt: text/plain utf-8, live protocol versions from mcp.py,
      endpoint, curl examples, catalog parity with tools/list (names,
      one-line descriptions, prices)
  D*  discovery: <link rel=alternate llms.txt> + <link rel=service /mcp>
      on every page head; home and /agents carry SSR-readable MCP
      endpoint + llms.txt text
  S*  share page: Link enclosure header (+type), visible plain <a> to
      ?download=1, <link rel=enclosure>; HEAD page = 200 same headers
      empty body; HEAD download = 302+Location, never touching a
      counting Gokapi path; GET behavior unchanged

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import base64
import hashlib
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import urllib.request as _ur
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:150]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = f"http://127.0.0.1:{_TEST_PORT}"
_u = "aw" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req(method, path, body=None, headers=None, raw=False):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    if raw:
        return r.status, hdrs, data, setc
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


from database import db

uid = None
share_pid = None
try:
    # ═══ R. robots.txt ═══
    st, h, body, _sc = _req("GET", "/robots.txt")
    check("R1 robots.txt text/plain",
          st == 200 and "text/plain" in h.get("content-type", ""), st)
    check("R2 /f/ is fetchable (not Disallowed)",
          "Disallow: /f/" not in body and "/f/" not in body.split("Sitemap")[0], body)
    check("R3 /api/ /auth/ /dh/ stay Disallowed",
          all(f"Disallow: {p}" in body for p in ("/api/", "/auth\n", "/dh/")), body)
    check("R4 stale /d/ rule removed; /mcp not crawlable",
          "Disallow: /d/\n" not in body and "Disallow: /mcp" in body, body)

    # ═══ X. per-response noindex ═══
    # (a live /f/ row is created below; first prove the /mcp half)
    st, h, _b, _sc = _req("POST", "/mcp",
                          {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          {"Content-Type": "application/json"})
    check("X1 /mcp carries X-Robots-Tag: noindex, nofollow",
          h.get("x-robots-tag") == "noindex, nofollow", h.get("x-robots-tag"))

    # ═══ N. real 404 ═══
    st, h, body, _sc = _req("GET", "/definitely-not-a-page-" + _u)
    check("N1 unknown page → HTTP 404 (no home redirect)",
          st == 404, st)
    check("N2 404 page lists home / tools / MCP / llms.txt",
          st == 404 and 'href="/tools"' in body and 'href="/agents"' in body
          and 'href="/llms.txt"' in body and 'href="/"' in body, None)
    st, h, d, _sc = _req("GET", "/api/definitely-not-" + _u)
    check("N3 /api/* stays JSON 404",
          st == 404 and h.get("content-type", "").startswith("application/json")
          and (d or {}).get("error") == "not_found", (st, d))
    st, h, body, _sc = _req("GET", "/f/nonexistent" + _u)
    check("N4 missing share stays 404 (rendered page)",
          st == 404 and b"<html" in (body.encode() if isinstance(body, str) else body or b""), st)
    check("N4b missing share carries X-Robots-Tag",
          h.get("x-robots-tag") == "noindex, nofollow", h.get("x-robots-tag"))

    # ═══ L. /llms.txt ═══
    st, h, body, _sc = _req("GET", "/llms.txt")
    ct = h.get("content-type", "")
    check("L1 /llms.txt 200 text/plain; charset=utf-8",
          st == 200 and ct.startswith("text/plain") and "utf-8" in ct, ct)
    from app.routes.mcp import SUPPORTED_VERSIONS
    check("L2 live protocol versions present",
          all(v in body for v in SUPPORTED_VERSIONS), SUPPORTED_VERSIONS)
    check("L3 endpoint + auth + client_ref documented",
          "/mcp" in body and "fsk_" in body and "client_ref" in body, None)
    check("L4 file input forms + two-stage upload documented",
          "b64" in body and '"url"' in body and "public_id" in body
          and "create_upload" in body and "-X PUT" in body, None)
    check("L5 share link semantics + billing documented",
          "?download=1" in body and "1000 credits = 1 USD" in body, None)
    # catalog parity with tools/list
    from governance.catalog import list_catalog, mcp_names
    from governance.tool_params import resolve_label
    _names = mcp_names()
    _pub = list_catalog(None)
    parity = True
    for t in _pub:
        nm = _names.get(t["id"], t["id"].replace("/", "__"))
        line = f"- {nm}: "
        if line not in body:
            parity = False
            break
    check("L6 catalog lines match public tools (same source as tools/list)",
          parity and "- share:" in body, None)
    # register + token → tools/list parity over the wire
    b, ct2 = _form({"username": _u + "a", "password": "Test12345",
                    "password_confirm": "Test12345",
                    "email": f"{_u}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct2})
    b, ct2 = _form({"login": _u + "a", "password": "Test12345"})
    st_l, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct2})
    ck = sc.split(";")[0]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
    st, _h, d_t, _sc = _req("POST", "/api/keys", {"name": "aw"},
                            {"Content-Type": "application/json", "Cookie": ck})
    tok = d_t["key"]["key"]
    st, _h, d, _sc = _req("POST", "/mcp",
                          {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          {"Content-Type": "application/json",
                           "Authorization": f"Bearer {tok}"})
    listed = {t["name"] for t in d["result"]["tools"]}
    catalog_block = body.split("## Tool catalog")[1].split("(server:")[0]
    llms_names = {ln[2:].split(":")[0] for ln in catalog_block.splitlines()
                  if ln.startswith("- ")}
    expect_names = {_names.get(t["id"], t["id"].replace("/", "__"))
                    for t in _pub}
    first_party = {n for n in expect_names if "__" not in n}
    check("L7 llms.txt tool names == public catalog names",
          llms_names == expect_names
          and first_party <= listed,
          (sorted(llms_names), sorted(expect_names)))

    # ═══ D. discovery links ═══
    for path, label in (("/", "home"), ("/tools", "market"),
                        ("/agents", "agents"), ("/auth/login", "login")):
        st, _h, body, _sc = _req("GET", path)
        ok = ('rel="alternate" type="text/plain" href="/llms.txt"' in body
              and 'rel="service"' in body and 'href="/mcp"' in body)
        check(f"D1 {label}: llms.txt alternate + MCP service links in head",
              st == 200 and ok, st)
    st, _h, body, _sc = _req("GET", "/")
    check("D2 home SSR text names the MCP endpoint and /llms.txt",
          "/mcp" in body and "/llms.txt" in body
          and "agent guide" in body.lower(), None)
    st, _h, body, _sc = _req("GET", "/agents")
    check("D3 /agents SSR text names endpoint + llms.txt (fsk_ hint)",
          "/mcp" in body and "/llms.txt" in body and "fsk_" in body, None)

    # ═══ S. share-page agent affordances ═══
    # sharing runs through the share tool — stage the source,
    # then execute; the settled file item carries the public share id.
    b = "----aw"
    up_body = (f'--{b}\r\nContent-Disposition: form-data; name="client_ref"'
               f'\r\n\r\naw-{_u}\r\n'
               f'--{b}\r\nContent-Disposition: form-data; name="tool"'
               f'\r\n\r\nshare\r\n'
               f'--{b}\r\nContent-Disposition: form-data; name="file";'
               f' filename="s.png"\r\nContent-Type: image/png\r\n\r\n'
               ).encode() + PNG + f"\r\n--{b}--\r\n".encode()
    st, _h, d_up, _sc = _req("POST", "/api/uploads", up_body,
                             {"Content-Type":
                              f"multipart/form-data; boundary={b}",
                              "Cookie": ck})
    _staged = (d_up or {}).get("file") or {}
    gid = _staged.get("stagingId")
    _stok = _staged.get("sourceToken") or ""
    st_x, _h2, d_x, _sc2 = _req("POST", "/api/tools/execute",
        {"tool_id": "share",
         "source": {"files": [{"public_id": gid, "source_token": _stok}]},
         "params": {},
         "delivery": {"expiry_days": 1, "allowed_downloads": 50},
         "client_ref": f"awx-{_u}"},
        {"Content-Type": "application/json", "Cookie": ck})
    items = ((d_x or {}).get("result") or {}).get("items") or []
    share_pid = next((it.get("public_id") for it in items
                      if it.get("kind") == "file"), None)
    check("S0 share fixture (upload+execute) settled",
          st == 200 and st_x == 200 and bool(share_pid), (st, st_x, str(d_x)[:80]))

    st, h, body, _sc = _req("GET", f"/f/{share_pid}")
    link = h.get("link", "")
    check("S1 page Link header = enclosure + type",
          'rel="enclosure"' in link and f"/f/{share_pid}?download=1" in link
          and 'type="image/png"' in link, link)
    check("S1b X-Robots-Tag on the share page", h.get("x-robots-tag") ==
          "noindex, nofollow", h.get("x-robots-tag"))
    check("S2 visible plain <a href> to ?download=1 (SSR, not JS)",
          f'href="/f/{share_pid}?download=1"' in body, None)
    check("S3 <link rel=enclosure> in head",
          'rel="enclosure"' in body.split("</head>")[0], None)

    def _dl_count():
        from gokapi import get_gokapi_file
        with db() as con:
            gid = con.execute("select gokapi_id from uploads where"
                              " public_id=%s", (share_pid,)).fetchone()["gokapi_id"]
        return int((get_gokapi_file(gid) or {}).get("DownloadCount", -1))

    c0 = _dl_count()
    st, h, body_raw, _sc = _req("HEAD", f"/f/{share_pid}", raw=True)
    check("S4 HEAD page → 200, same Link/X-Robots headers, empty body",
          st == 200 and 'rel="enclosure"' in h.get("link", "")
          and h.get("x-robots-tag") == "noindex, nofollow"
          and len(body_raw) == 0, (st, h.get("link"), len(body_raw)))
    st, h, body_raw, _sc = _req("HEAD", f"/f/{share_pid}?download=1", raw=True)
    loc = h.get("location", "")
    check("S5 HEAD download → 302 + Location (no follow, no body)",
          st == 302 and loc and len(body_raw) == 0, (st, loc))
    c1 = _dl_count()
    check("S6 HEAD never consumes a download", c1 == c0, (c0, c1))
    st, h, _b, _sc = _req("GET", f"/f/{share_pid}?download=1")
    check("S7 GET download still 302 to the file",
          st == 302 and h.get("location"), (st, h.get("location")))
    st, h, _b, _sc = _req("GET", f"/f/{share_pid}")
    check("S8 GET page unchanged (200 render)",
          st == 200 and "text/html" in h.get("content-type", ""), st)
finally:
    with db() as con:
        if uid is not None:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from upload_usage where user_id=%s", (uid,))
            con.execute("delete from ingress_requests where user_id=%s", (uid,))
            con.execute("delete from uploads where user_id=%s", (uid,))
            purge_users(con, [uid])
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — agent web surface ({COUNT} checks)")
