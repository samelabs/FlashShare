#!/usr/bin/env python3
"""Publish gate canonical — a tool goes public only after its author
has successfully run it once.

  G1* create defaults to unlisted: not on /tools, not in a non-owner's
      MCP tools/list; the author's own tool page renders (with the
      gate hint sentence).
  G2* explicit visibility=public at creation → 422 needs_successful_run.
  G3* PATCH public without any successful call → 422.
  G4* another user's successful call does not unlock (owner-only).
  G5* the author's FAILED call does not unlock.
  G6* the author's successful call unlocks: PATCH public → 200, the
      tool appears on /tools and in a non-owner's tools/list.
  G7* un-publishing (public → unlisted) stays unrestricted.

The developer endpoint is an in-suite local http listener (the fx1
pattern). The publish POST uses a literal public IP https URL (the
existing c2a fixture) — the SSRF guard resolves, nothing is fetched.
To point the tool at the local listener the row is flagged trusted
(the same exemption operator-created tools get; here it is only test
plumbing so the runtime driver may call 127.0.0.1).
"""
import base64
import http.client as _hc
import http.server
import json as _json
import os as _os
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
_u = "b9" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


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


def _tools_list(token):
    st, d = _mcp({"jsonrpc": "2.0", "id": 99, "method": "tools/list"}, token)
    return [t.get("name") for t in
            (((d or {}).get("result") or {}).get("tools") or [])]


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "b9"},
                              {"Content-Type": "application/json", "Cookie": ck})
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


# ── in-suite local developer endpoint (fx1 pattern) ──────────────────
class _DevHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/fail":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        items = [{"kind": "text", "text": "gate ok"}]
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
LOCAL_FAIL_URL = f"http://127.0.0.1:{_srv.server_address[1]}/fail"
threading.Thread(target=_srv.serve_forever, daemon=True).start()

from database import db  # noqa: E402

author_uid = None
reader_uid = None
try:
    author_uid, a_ck, A_TOK = _register(_u + "dev")
    reader_uid, _rck, R_TOK = _register(_u + "reader")
    tk = f"{_u}dev/gate1"

    def _publish(cookie, tool_id, extra=None):
        body = {"tool_id": tool_id, "label": "Gate Tool",
                "url": "https://93.184.216.34/x", "timeout_s": 30,
                "files_min": 0, "files_max": 3,
                "format_families": "image", "accept": [".png"],
                "outputs": ["text"], "cost": 0}
        body.update(extra or {})
        return _req("POST", "/api/usertools", body,
                    {"Content-Type": "application/json", "Cookie": cookie})

    # ═══ G1: default unlisted, invisible to others, author page works ═══
    st, _h, d, _sc = _publish(a_ck, "gate1")
    check("G1a create accepted (no visibility field)",
          st == 200 and (d or {}).get("ok"), f"{st} {d}")
    with db() as con:
        vis = con.execute("select visibility from platform_tools"
                          " where tool_key=%s", (tk,)).fetchone()
    check("G1b lands unlisted", vis and vis["visibility"] == "unlisted",
          str(vis and dict(vis)))
    st, _h, market, _sc = _req("GET", "/tools")
    check("G1c not on /tools", tk not in (market or ""))
    check("G1d not in a non-owner's MCP tools/list",
          tk.replace("/", "__") not in _tools_list(R_TOK))
    st, _h, page, _sc = _req("GET", f"/tool/{tk}", None,
                             {"Cookie": a_ck, "Accept": "text/html"})
    check("G1e author's own tool page renders with the gate hint",
          st == 200 and "tool_gate_hint" in (page or "")
          and "data-i18n=\"tool_gate_hint\"" in (page or ""), st)
    check("G1f tool page shows no run-count tag",
          "tool_runs" not in (page or ""), st)

    # ═══ G2: explicit public at creation refused ═════════════════════
    st, _h, d, _sc = _publish(a_ck, "gate2", {"visibility": "public"})
    check("G2 explicit public at creation → 422 needs_successful_run",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G3: PATCH public without any successful call ════════════════
    def _patch_vis(cookie, tool_id, vis):
        return _req("PATCH", f"/api/usertools/{tool_id}",
                    {"visibility": vis},
                    {"Content-Type": "application/json", "Cookie": cookie})

    st, _h, d, _sc = _patch_vis(a_ck, "gate1", "public")
    check("G3 PATCH public with zero calls → 422",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # point the tool at the local listener (trusted = the operator-tool
    # exemption, test plumbing so the runtime driver may reach 127.0.0.1)
    with db() as con:
        con.execute("update platform_tools set trusted = TRUE"
                    " where tool_key=%s", (tk,))

    def _set_url(url):
        return _req("PATCH", "/api/usertools/gate1", {"url": url, "timeout_s": 30},
                    {"Content-Type": "application/json", "Cookie": a_ck})

    # ═══ G4: another user's successful call does not unlock ══════════
    st, _h, _d, _sc = _set_url(LOCAL_URL)
    st, d = _call(f"{_u}dev__gate1", {"client_ref": f"b9r-{_u}"}, R_TOK)
    with db() as con:
        row = con.execute(
            "select status from tool_calls where tool_id=%s"
            " order by id desc limit 1", (tk,)).fetchone()
    check("G4a reader's call succeeded", (d.get("result") or {})
          .get("isError") is False and row["status"] == "ok",
          f"{str(d)[:80]} {row and dict(row)}")
    st, _h, d, _sc = _patch_vis(a_ck, "gate1", "public")
    check("G4b someone else's ok does not unlock → 422",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G5: the author's failed call does not unlock ════════════════
    _set_url(LOCAL_FAIL_URL)
    st, d = _call(f"{_u}dev__gate1", {"client_ref": f"b9f-{_u}"}, A_TOK)
    with db() as con:
        row = con.execute(
            "select status from tool_calls where tool_id=%s"
            " order by id desc limit 1", (tk,)).fetchone()
    check("G5a author's call failed", (d.get("result") or {})
          .get("isError") is True and row["status"] == "failed",
          f"{str(d)[:80]} {row and dict(row)}")
    st, _h, d, _sc = _patch_vis(a_ck, "gate1", "public")
    check("G5b a failed author run does not unlock → 422",
          st == 422 and (d or {}).get("error") == "needs_successful_run",
          f"{st} {d}")

    # ═══ G6: the author's successful call unlocks ════════════════════
    _set_url(LOCAL_URL)
    st, d = _call(f"{_u}dev__gate1", {"client_ref": f"b9o-{_u}"}, A_TOK)
    check("G6a author's call succeeded", (d.get("result") or {})
          .get("isError") is False, str(d)[:100])
    st, _h, d, _sc = _patch_vis(a_ck, "gate1", "public")
    check("G6b PATCH public after the owner's ok run → 200",
          st == 200 and (d or {}).get("ok"), f"{st} {d}")
    st, _h, market, _sc = _req("GET", "/tools?q=" + _u)
    check("G6c searchable on /tools", tk in (market or ""))
    st, d = _call("pin_tool", {"name": tk.replace("/", "__")}, R_TOK)
    check("G6d a non-owner can pin it into their MCP tools/list",
          ((d.get("result") or {}).get("structuredContent") or {})
          .get("favorite") is True
          and tk.replace("/", "__") in _tools_list(R_TOK), str(d)[:200])

    # ═══ G7: un-publishing stays free ════════════════════════════════
    st, _h, d, _sc = _patch_vis(a_ck, "gate1", "unlisted")
    check("G7 public → unlisted unrestricted", st == 200 and (d or {}).get("ok"),
          f"{st} {d}")

finally:
    try:
        _srv.shutdown()
    except Exception:
        pass
    uids = [u for u in (author_uid, reader_uid) if u]
    with db() as con:
        purge_users(con, uids)
        con.execute("delete from pricing where tool_id LIKE %s",
                    (f"{_u}dev/%",))

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
