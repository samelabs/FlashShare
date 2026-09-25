#!/usr/bin/env python3
"""MCP two-stage upload (B4b): create_upload → one-time PUT → tools/call
reference. File bytes never appear in any JSON-RPC body.

  P*  positive: create → PUT → tools/call(share ref) → result; 5MB bytes
      identical end-to-end; every JSON-RPC body in the flow is small
      (bytes-only legs are the binary PUT)
  E*  create_upload contract: appears in tools/list (official, free);
      validation (filename/size/content_type/client_ref); quota precheck
      runs BEFORE a ticket exists
  T*  ticket lifecycle: expiry → 410; replay → 409; forged → 404;
      another user's Bearer on someone else's ticket → 403
  U*  PUT guards: over-declared-size → 413 with zero side effects;
      unsupported format → 415; staging failure → 500 with quota and
      storage reclaimed
  I*  idempotency: same client_ref + same params → same usable ticket;
      same client_ref + different params → -32009
  Q*  quota: exhausted 24h budget → typed -32602 at create (no ticket)

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

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:150]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


import io

_HOST = ("127.0.0.1", int(_TEST_PORT))
_BASE = f"http://127.0.0.1:{_TEST_PORT}"
_u = "mu" + _uuid.uuid4().hex[:6]
ts = int(_time.time())
BIG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==") + b"\x89PNG\r\n\x1a\n" + _os.urandom(5 * 1024 * 1024 - 84)
BIG_MD5 = hashlib.md5(BIG).hexdigest()
PNG_SMALL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")

_rpc_bytes_sent = 0  # total JSON-RPC request bytes in the positive flow


def _req(method, path, body=None, headers=None, raw=False):
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


def _mcp(body, token, mid=None):
    global _rpc_bytes_sent
    payload = _json.dumps(body).encode()
    _rpc_bytes_sent += len(payload)
    st, _h, d, _sc = _req("POST", "/mcp", payload, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return st, d


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _create_upload(args, token, mid=1):
    st, d = _call("create_upload", args, token, mid)
    txt = None
    try:
        txt = _json.loads((((d or {}).get("result") or {})
                           .get("content") or [{}])[0].get("text") or "null")
    except Exception:
        pass
    return st, d, txt


from database import db


def _gk(method, path, body=None):
    base = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
    req = _ur.Request(base + path, method=method,
                      headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = _json.dumps(body).encode()
    with _ur.urlopen(req, data, timeout=5) as r:
        raw = r.read()
    return _json.loads(raw) if raw else None


def _gk_n():
    return len(_gk("GET", "/api/files/list"))


def _gk_fail(mode):
    if mode is None:
        _gk("POST", "/ci/fail-internal", {"mode": None})
    else:
        _gk("POST", "/ci/fail-internal", {"mode": mode})


uid = uid2 = None
ck = ck2 = tok = tok2 = None
try:
    # fixtures: two users (owner + stranger) with tokens + credits
    for name_i, (uname, cred) in enumerate(((_u + "a", "uid"), (_u + "b", "uid2"))):
        b, ct = _form({"username": uname, "password": "Test12345",
                       "password_confirm": "Test12345",
                       "email": f"{uname}@t.co"})
        _req("POST", "/auth/register", b, {"Content-Type": ct})
        b, ct = _form({"login": uname, "password": "Test12345"})
        st_l, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
        cookie = sc.split(";")[0]
        st_t, _h, d_t, _sc = _req("POST", "/api/keys",
                                  {"name": "mu"},
                                  {"Content-Type": "application/json",
                                   "Cookie": cookie})
        if name_i == 0:
            ck, uid, tok = cookie, None, d_t["key"]["key"]
        else:
            ck2, tok2 = cookie, d_t["key"]["key"]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        uid2 = int(con.execute("select id from users where username=%s",
                               (_u + "b",)).fetchone()["id"])
        from repo.ledger import insert_row
        insert_row(con, uid, 100000, 100000, "manual_topup", None, None,
                   f"mu:{_u}", None, ts)
        insert_row(con, uid2, 100000, 100000, "manual_topup", None, None,
                   f"mu:{_u}:2", None, ts)
    check("F1 two users + tokens + credits", uid and uid2 and tok and tok2,
          (uid, uid2))

    # ═══ E. create_upload contract ═══
    st, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, tok)
    tools = ((d.get("result") or {}).get("tools") or [])
    spec = next((t for t in tools if t["name"] == "create_upload"), None)
    check("E1 create_upload in tools/list with required schema",
          spec is not None
          and spec["inputSchema"]["required"] == ["filename", "size",
                                                  "client_ref"], None)
    from config import MAX_UPLOAD_BYTES
    st, d, t = _create_upload({"filename": "a.png", "size": MAX_UPLOAD_BYTES + 1,
                               "client_ref": "e2"}, tok)
    check("E2 size over the platform cap → -32602, no ticket",
          (d.get("error") or {}).get("code") == -32602 and t is None, str(d)[:90])
    with db() as con:
        n = con.execute("select count(*) c from mcp_upload_tickets"
                        " where user_id=%s", (uid,)).fetchone()["c"]
    check("E2b rejected create leaves zero ticket rows", int(n) == 0, n)
    for bad, why in (({"size": 0}, "size<=0"), ({"size": "5"}, "size str"),
                     ({"filename": " "}, "empty filename"),
                     ({"client_ref": ""}, "empty ref")):
        args = {"filename": "a.png", "size": 100, "client_ref": f"e-{why}"}
        args.update(bad)
        st, d, t = _create_upload(args, tok)
        check(f"E3 invalid {why} → -32602",
              (d.get("error") or {}).get("code") == -32602, str(d)[:80])

    # ═══ I. idempotency ═══
    a1 = {"filename": "idem.png", "size": 1234, "client_ref": f"idem-{_u}"}
    st1, d1, t1 = _create_upload(a1, tok, 10)
    st2, d2, t2 = _create_upload(a1, tok, 11)
    check("I1 same client_ref + same params → same usable ticket",
          t1 and t2 and t1["upload_url"] == t2["upload_url"]
          and t2["expires_at"] == t1["expires_at"], (t1, t2))
    a2 = dict(a1, size=4321)
    st3, d3, t3 = _create_upload(a2, tok, 12)
    check("I2 same client_ref + different params → -32009",
          (d3.get("error") or {}).get("code") == -32009
          and (d3["error"] or {}).get("message") == "idempotency_conflict",
          str(d3)[:90])

    # ═══ P. positive: 5MB two-stage flow ═══
    st, d, t = _create_upload({"filename": "big.png", "size": len(BIG),
                               "content_type": "image/png",
                               "client_ref": f"p1-{_u}"}, tok, 20)
    check("P1 create_upload issues a ticket",
          st == 200 and t and t["upload_url"].startswith(_BASE + "/mcp/upload/")
          and t["max_bytes"] == MAX_UPLOAD_BYTES
          and t["expires_at"] > ts, t)
    ticket_path = t["upload_url"][len(_BASE):]
    st_p, _h, body_p, _sc = _req("PUT", ticket_path, BIG,
                                 {"Content-Type": "application/octet-stream"})
    check("P2 PUT 5MB raw bytes → 200 with {public_id, source_token}",
          st_p == 200 and body_p.get("public_id") and body_p.get("source_token"),
          (st_p, str(body_p)[:90]))
    ref = body_p
    st_c, d_c = _call("share", {"files": [{"public_id": ref["public_id"],
                                           "source_token": ref["source_token"]}],
                                "client_ref": f"share-{_u}"}, tok, 21)
    rc = ((d_c.get("result") or {}).get("content") or [{}])[0].get("text")
    out = (_json.loads(rc).get("result") or {}) if rc else {}
    share_pid = out.get("publicId") or out.get("public_id")
    check("P3 tools/call(share) with the reference → executed result",
          (d_c.get("result") or {}).get("isError") is False and share_pid,
          str(d_c)[:120])
    # anonymous raw download: resolve the redirect, fetch the gokapi URL
    st_d, h_d, _b, _sc = _req("GET", f"/f/{share_pid}?download=1")
    loc = h_d.get("location", "")
    dl_url = loc if loc.startswith("http") else (
        _os.environ.get("FLASHSHARE_GOKAPI_URL", "").rstrip("/")
        + loc if loc else "")
    if not dl_url:
        # no-hotlink fallback streams through Python — fetch inline
        st_i, _h, blob, _sc = _req("GET", f"/f/{share_pid}?download=1", raw=True)
        got = blob
    else:
        with _ur.urlopen(dl_url, timeout=30) as r:
            got = r.read()
    check("P4 anonymous download bytes identical (5MB md5)",
          hashlib.md5(got).hexdigest() == BIG_MD5,
          f"{len(got)} vs {len(BIG)}")
    check("P5 file bytes never inside any JSON-RPC body "
          "(total RPC request bytes ≪ file size)",
          _rpc_bytes_sent < 100 * 1024, _rpc_bytes_sent)
    with db() as con:
        calls = con.execute("select count(*) c from tool_calls where"
                            " user_id=%s and tool_id='create_upload'",
                            (uid,)).fetchone()["c"]
    check("P6 create_upload is a meta tool (zero tool_call rows)",
          int(calls) == 0, calls)

    # ═══ T. ticket lifecycle ═══
    st, d, t = _create_upload({"filename": "t.png", "size": 100,
                               "client_ref": f"t1-{_u}"}, tok, 30)
    tp = t["upload_url"][len(_BASE):]
    with db() as con:
        con.execute("update mcp_upload_tickets set expires_at=%s"
                    " where ticket=%s", (ts - 1, t["upload_url"].rsplit("/", 1)[1]))
    st_t, _h, d_t, _sc = _req("PUT", tp, PNG_SMALL)
    check("T1 expired ticket → 410 ticket_expired",
          st_t == 410 and d_t.get("error") == "ticket_expired", (st_t, d_t))
    st, d, t = _create_upload({"filename": "t2.png", "size": 100,
                               "client_ref": f"t2-{_u}"}, tok, 31)
    tp = t["upload_url"][len(_BASE):]
    st_t, _h, d_t, _sc = _req("PUT", tp, PNG_SMALL)
    st_r, _h, d_r, _sc = _req("PUT", tp, PNG_SMALL)
    check("T2 first PUT ok, replay → 409 ticket_used",
          st_t == 200 and st_r == 409 and d_r.get("error") == "ticket_used",
          (st_t, st_r, d_r))
    st_f, _h, d_f, _sc = _req("PUT", "/mcp/upload/forged-" + _u, PNG_SMALL)
    check("T3 forged ticket → 404 ticket_not_found",
          st_f == 404 and d_f.get("error") == "ticket_not_found", (st_f, d_f))
    st, d, t = _create_upload({"filename": "t3.png", "size": 100,
                               "client_ref": f"t3-{_u}"}, tok, 32)
    st_o, _h, d_o, _sc = _req("PUT", t["upload_url"][len(_BASE):], PNG_SMALL,
                              {"Authorization": f"Bearer {tok2}"})
    check("T4 another user's Bearer on someone else's ticket → 403",
          st_o == 403 and d_o.get("error") == "ticket_owner_mismatch",
          (st_o, d_o))
    st_o2, _h, d_o2, _sc = _req("PUT", t["upload_url"][len(_BASE):], PNG_SMALL)
    check("T4b the owner (no Bearer) still succeeds after the mismatch",
          st_o2 == 200 and d_o2.get("public_id"), (st_o2, str(d_o2)[:80]))

    # ═══ U. PUT guards ═══
    st, d, t = _create_upload({"filename": "u1.png", "size": 50,
                               "client_ref": f"u1-{_u}"}, tok, 40)
    _gk0 = _gk_n()
    st_u, _h, d_u, _sc = _req("PUT", t["upload_url"][len(_BASE):], PNG_SMALL)
    check("U1 body over declared size → 413, zero storage",
          st_u == 413 and d_u.get("error") == "err_file_too_large"
          and _gk_n() == _gk0, (st_u, d_u))
    st, d, t = _create_upload({"filename": "u2.exe", "size": 100,
                               "client_ref": f"u2-{_u}"}, tok, 41)
    st_u, _h, d_u, _sc = _req("PUT", t["upload_url"][len(_BASE):], b"MZ\x90\x00" * 25)
    check("U2 unsupported format → 415 typed sniff error",
          st_u == 415 and d_u.get("error", "").startswith("err_"),
          (st_u, d_u))
    # staging failure → quota and storage reclaimed (gokapi on-demand fail)
    _gk("POST", "/ci/fail-upload", {"mode": "500"})
    try:
        st, d, t = _create_upload({"filename": "u3.png", "size": len(PNG_SMALL),
                                   "client_ref": f"u3-{_u}"}, tok, 42)
        st_u, _h, d_u, _sc = _req("PUT", t["upload_url"][len(_BASE):], PNG_SMALL)
    finally:
        _gk("POST", "/ci/fail-upload", {"mode": None})
    check("U3 staging failure → 500 err_upload_failed",
          st_u == 500 and d_u.get("error") == "err_upload_failed", (st_u, d_u))
    with db() as con:
        rec = con.execute(
            """select status from ingress_requests ir
               join mcp_upload_tickets tk on tk.client_ref = ir.request_ref
               where tk.user_id=%s and tk.client_ref=%s""",
            (uid, f"u3-{_u}")).fetchone()
    _gk1 = _gk_n()
    check("U3b failed staging leaves no orphan gokapi object", _gk1 == _gk0,
          (_gk0, _gk1))
    check("U3c receipt settled failed (quota released)",
          rec is not None and rec["status"] == "failed", rec and dict(rec))

    # ═══ W. URL-source early abort on Content-Length (B4 item 5) ═══
    # A local HTTPS fixture with a self-signed CA: the server sends a
    # Content-Length far over the cap and then NEVER sends the body —
    # an early-abort transport returns promptly with RemoteError
    # 'too_large'; a download-first transport would hang to the timeout.
    import ssl as _ssl
    import subprocess as _sp
    import tempfile as _tf
    import threading as _th
    import http.server as _hs
    _td = _tf.mkdtemp(prefix="b4b-tls-")
    ca_key = _os.path.join(_td, "ca.key"); ca_pem = _os.path.join(_td, "ca.pem")
    sv_key = _os.path.join(_td, "sv.key"); sv_pem = _os.path.join(_td, "sv.pem")
    try:
        for cmd in (
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", ca_key, "-out", ca_pem, "-subj", "/CN=b4b-ca",
             "-days", "1"],
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", sv_key, "-out", _os.path.join(_td, "sv.csr"),
             "-subj", "/CN=127.0.0.1"],
            ["openssl", "x509", "-req", "-in", _os.path.join(_td, "sv.csr"),
             "-CA", ca_pem, "-CAkey", ca_key, "-out", sv_pem,
             "-days", "1", "-extfile", "/dev/stdin"],
        ):
            stdin = b"subjectAltName=IP:127.0.0.1\n" if cmd[1] == "x509" else None
            r = _sp.run(cmd, capture_output=True, input=stdin)
            assert r.returncode == 0, r.stderr[:200]

        class _H(_hs.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(100 * 1024 * 1024))
                self.end_headers()
                _time.sleep(30)  # body NEVER arrives
            def log_message(self, *a):
                pass

        _ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        _ctx.load_cert_chain(sv_pem, sv_key)
        _srv2 = _hs.HTTPServer(("127.0.0.1", 0), _H)
        _port_w = _srv2.server_address[1]
        _srv2.socket = _ctx.wrap_socket(_srv2.socket, server_side=True)
        _th.Thread(target=_srv2.serve_forever, daemon=True).start()

        _os.environ["SSL_CERT_FILE"] = ca_pem
        import socket as _sock
        import remote as _remote_mod
        _orig_va = _remote_mod._validated_addresses
        # resolver seam ONLY (the SSRF address validation is not under
        # test here): TLS, headers, the Content-Length gate and the read
        # cap all run the REAL transport code against the fixture.
        _remote_mod._validated_addresses = (
            lambda url: ("127.0.0.1", _port_w, f"127.0.0.1:{_port_w}",
                         [(_sock.AF_INET, _sock.SOCK_STREAM,
                           _sock.IPPROTO_TCP, ("127.0.0.1", _port_w))]))
        try:
            _t0 = _time.time()
            try:
                _remote_mod.pinned_request(
                    f"https://fixture.local/big.png", method="GET",
                    timeout_s=8, max_bytes=1024)
                early = "no-error"
            except _remote_mod.RemoteError as exc:
                early = exc.code
            took = _time.time() - _t0
            check("W1 Content-Length over cap aborts BEFORE download",
                  early == "too_large" and took < 6, (early, round(took, 1)))
        finally:
            _remote_mod._validated_addresses = _orig_va
            _os.environ.pop("SSL_CERT_FILE", None)
    finally:
        import shutil as _sh
        _sh.rmtree(_td, ignore_errors=True)

    # ═══ Q. quota precheck at issue time ═══
    # one oversized historical usage row exhausts the 24h byte budget
    with db() as con:
        con.execute("""insert into upload_usage (user_id, ip, size_bytes, created_at)
                       values (%s, '127.0.0.1', 999999999999, %s)""",
                    (uid, ts))
    st, d, t = _create_upload({"filename": "q.png", "size": 1000,
                               "client_ref": f"q1-{_u}"}, tok, 50)
    err = d.get("error") or {}
    check("Q1 exhausted 24h quota → -32602 with typed denial",
          err.get("code") == -32602
          and (err.get("data") or {}).get("error") in (
              "err_ingress_files_24h", "err_ingress_bytes_24h"), str(d)[:120])
    with db() as con:
        n = con.execute("select count(*) c from mcp_upload_tickets"
                        " where user_id=%s and client_ref=%s",
                        (uid, f"q1-{_u}")).fetchone()["c"]
    check("Q1b no ticket issued on quota denial", int(n) == 0, n)
finally:
    _gk_fail(None)
    with db() as con:
        for u in (uid, uid2):
            if u:
                con.execute("delete from tool_calls where user_id=%s", (u,))
                con.execute("delete from ledger where user_id=%s", (u,))
                con.execute("delete from upload_usage where user_id=%s", (u,))
                con.execute("delete from ingress_requests where user_id=%s", (u,))
                con.execute("delete from mcp_upload_tickets where user_id=%s", (u,))
                con.execute("delete from users where id=%s", (u,))
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — MCP two-stage upload ticket ({COUNT} checks)")
