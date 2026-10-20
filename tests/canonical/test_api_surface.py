#!/usr/bin/env python3
"""API surface canonical (/v1) — the fsk_-only public tool API (P5).

Real HTTP against the real app + a local trusted-endpoint listener
(trusted rows may be plain http). Fault paths that need process-level
injection run in-process against the route function (the URL-source leg
with the pinned transport replaced — the same pattern the MCP suite
uses).
"""
import os as _os
import sys as _sys

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT or _TEST_PORT == "8000":
    _sys.stderr.write("ERROR: isolated FLASHSHARE_TEST_PORT required.\n"); _sys.exit(2)
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:120]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import base64
import http.client as _hc
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ts = int(time.time())
_u = "v1s" + _os.urandom(3).hex()
_HOST = ("127.0.0.1", int(_TEST_PORT))

from database import db  # noqa: E402

# ── local trusted endpoint (trusted rows may be plain http) ──
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
HITS = {"boom": 0, "slow": 0, "rawfile": 0}

class _Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        body = self._read()
        path = self.path.split("?")[0]
        if path == "/echo-text":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._reply(200, b"ECHO " + body + b" Q=" + qs.encode(),
                        "text/plain; charset=utf-8")
        elif path == "/echo-json":
            self._reply(200, body, "application/json")
        elif path == "/boom":
            HITS["boom"] += 1
            self._reply(500, b"E" * 20480, "text/plain")
        elif path == "/slowtext":
            HITS["slow"] += 1
            time.sleep(4)
            self._reply(200, b"slow done", "text/plain; charset=utf-8")
        elif path == "/rawfile":
            HITS["rawfile"] += 1
            self._reply(200, PNG, "image/png",
                        {"Content-Disposition": 'attachment; filename="r.png"'})
        elif path == "/items":
            try:
                env = json.loads(body or b"{}")
            except Exception:
                env = {}
            self._reply(200, json.dumps({"items": [
                {"kind": "text",
                 "text": "file-tool ok " + json.dumps(
                     env.get("params") or {}, sort_keys=True)}]}).encode(),
                "application/vnd.518.items+json")
        else:
            self._reply(404, b"no", "text/plain")

_ep_srv = ThreadingHTTPServer(("127.0.0.1", 0), _Endpoint)
EP_PORT = _ep_srv.server_address[1]
threading.Thread(target=_ep_srv.serve_forever, daemon=True).start()
EP = f"http://127.0.0.1:{EP_PORT}"


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    raw = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        parsed = json.loads(raw) if raw else None
    except Exception:
        parsed = None
    return r.status, hdrs, parsed, raw


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345",
                   "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = con.execute("select id from users where username=%s",
                          (name,)).fetchone()["id"]
    return int(uid), ck


def _uid_of(name):
    with db() as con:
        return int(con.execute("select id from users where username=%s",
                               (name,)).fetchone()["id"])


from identity.apikeys import create_key
from economy.billing import grant as _grant, spendable_balance as _sb

uidA, ckA = _register(_u + "a")
uidB, ckB = _register(_u + "b")
uidT, ckT = _register(_u + "t")
uidF, ckF = _register(_u + "f")
uidI, ckI = _register(_u + "i")
uidG, ckG = _register(_u + "g")
uidC, ckC = _register(_u + "c")
for _uid in (uidA, uidB, uidT, uidF, uidI, uidG, uidC):
    _grant(_uid, 100000, "manual_topup", idem_key=f"v1s:{_u}:{_uid}", note="v1s")
KEY_A = create_key(uidA, "v1sa")["key"]
KEY_B = create_key(uidB, "v1sb")["key"]
KEY_T = create_key(uidT, "v1st")["key"]
KEY_F = create_key(uidF, "v1sf")["key"]
KEY_I = create_key(uidI, "v1si")["key"]
KEY_G = create_key(uidG, "v1sg")["key"]
KEY_C = create_key(uidC, "v1sc")["key"]
KEY_LIMITED = create_key(uidA, "v1sl", scopes="files:read")["key"]
AH = {"Authorization": f"Bearer {KEY_A}"}

# ── trusted tool rows (owner NULL → trusted transport, plain http ok) ──
_TOOLS = []
def _tool(key, **cols):
    _TOOLS.append(key)
    with db() as con:
        sets = {
            "tool_key": key,
            "owner_id": None, "tool_id": key, "execution": "http",
            "label_json": json.dumps({"en": key}),
            "visibility": "public", "enabled": "TRUE",
            "accept_json": cols.get("accept"),
            "format_family": cols.get("tag"),
            "body_format": cols.get("body_format"),
            "files_min": cols.get("files_min", 0),
            "files_max": cols.get("files_max", 0),
            "outputs_json": json.dumps(cols.get("outputs", ["text"])),
            "endpoint_json": json.dumps({"url": cols["url"], "timeout_s": 30}),
            "created_at": ts, "updated_at": ts,
        }
        keys = ", ".join(sets)
        vals = ", ".join(
            "NULL" if sets[k] is None else "%s"
            for k in sets)
        con.execute(
            f"insert into platform_tools ({keys}) values ({vals})",
            tuple(sets[k] for k in sets if sets[k] is not None))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (key, cols.get("cost", 0), ts))

def _drop_tools():
    with db() as con:
        for key in _TOOLS:
            con.execute("delete from pricing where tool_id=%s", (key,))
            con.execute("delete from platform_tools where tool_key=%s", (key,))

_u2 = _os.urandom(3).hex()
TOOL_TEXT = f"v1bt{_u2}"
TOOL_JSON = f"v1bj{_u2}"
TOOL_FILE = f"v1ft{_u2}"
TOOL_RAWF = f"v1rf{_u2}"
TOOL_BAD = f"v1bad{_u2}"
TOOL_SLOW = f"v1sl{_u2}"
_tool(TOOL_TEXT, body_format="text", url=f"{EP}/echo-text", outputs=["text"], cost=3)
_tool(TOOL_JSON, body_format="json", url=f"{EP}/echo-json", outputs=["json"], cost=0)
_tool(TOOL_FILE, accept='[".png"]', tag="image", files_min=1, files_max=2,
      url=f"{EP}/items", outputs=["text"], cost=0)
_tool(TOOL_RAWF, body_format="text", url=f"{EP}/rawfile", outputs=["file"], cost=0)
_tool(TOOL_BAD, body_format="text", url=f"{EP}/boom", outputs=["text"], cost=0)
_tool(TOOL_SLOW, body_format="text", url=f"{EP}/slowtext", outputs=["text"], cost=0)


def _cors_free(hdrs):
    return not any(k.startswith("access-control-") for k in hdrs)


# ═══ A. authentication model ═══
st, h, d, raw = _req("GET", "/v1/balance")
check("A1 no credentials → 401 JSON, no CORS",
      st == 401 and d == {"ok": False, "error": "unauthorized"}
      and _cors_free(h), f"{st} {d} {h}")
st, h, d, raw = _req("GET", "/v1/balance", headers={
    "Authorization": "Bearer fsot_whatever", "Cookie": ckA})
check("A2 fsot_ + valid session cookie → 401 (cookie is not /v1 identity)",
      st == 401 and d.get("error") == "unauthorized", f"{st} {d}")
st, h, d, raw = _req("GET", "/v1/balance", headers={"Cookie": ckA})
check("A3 session cookie alone → 401", st == 401, f"{st} {d}")
st, h, d, raw = _req("GET", "/v1/balance", headers={
    "Cookie": ckB, "Authorization": f"Bearer {KEY_A}"})
check("A4 cookie(B) + fsk_(A) → fsk_ wins, identified as A",
      st == 200 and d.get("ok") and d.get("balance") == _sb(uidA),
      f"{st} {d}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}", "x",
                     {"Authorization": f"Bearer {KEY_LIMITED}",
                      "Content-Type": "text/plain"})
check("A5 key without tools:call → 403 err_insufficient_scope",
      st == 403 and d.get("error") == "err_insufficient_scope"
      and d.get("required_scope") == "tools:call", f"{st} {d}")
st, h, d, raw = _req("GET", "/v1/tools", headers={
    "Authorization": f"Bearer {KEY_LIMITED}"})
check("A5b read endpoints stay open to any valid key",
      st == 200 and d.get("ok"), f"{st} {str(d)[:80]}")
st, h, d, raw = _req("GET", "/v1/balance", headers={
    "Authorization": "Bearer fsk_definitely-not-a-key"})
check("A6 unknown key → 401", st == 401, f"{st} {d}")

# ═══ B. catalog surfaces ═══
st, h, d, raw = _req("GET", "/v1/tools", headers=AH)
_bt = next((t for t in (d.get("tools") or []) if t.get("id") == TOOL_TEXT), {})
check("B1 GET /v1/tools lists the body tool (projection carries body_format)",
      st == 200 and _bt.get("body_format") == "text"
      and _cors_free(h), f"{st} {str(_bt)[:90]}")
st, h, d, raw = _req("GET", f"/v1/tools/{TOOL_TEXT}", headers=AH)
check("B2 GET /v1/tools/{key} detail (cost from pricing)",
      st == 200 and d["tool"]["body_format"] == "text"
      and d["tool"]["cost"] == 3 and d["tool"]["files_max"] == 0,
      f"{st} {str(d)[:100]}")
st, h, d, raw = _req("GET", f"/v1/tools/no_such_{_u}", headers=AH)
check("B3 unknown key → 404 JSON tool_not_found, no CORS",
      st == 404 and d == {"ok": False, "error": "tool_not_found"}
      and _cors_free(h), f"{st} {d}")
st, h, d, raw = _req("DELETE", "/v1/tools", headers=AH)
check("B4 wrong method → 405 JSON method_not_allowed",
      st == 405 and d.get("error") == "method_not_allowed", f"{st} {d}")

# ═══ C. body tools ═══
TH = {"Authorization": f"Bearer {KEY_T}", "Content-Type": "text/plain"}
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}?x=5&flag=true",
                     "hello world", TH)
check("C1 text body call → echoed text body + call-id/cost headers",
      st == 200 and raw == b"ECHO hello world Q=x=5&flag=true"
      and h.get("x-518-call-id", "").isdigit()
      and h.get("x-518-cost") == "3"
      and h.get("content-type", "").startswith("text/plain"),
      f"{st} {raw[:60]} {h.get('x-518-cost')}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}", "hello world",
                     {**TH, "Idempotency-Key": f"c1-{_u}"})
_call_idem = h.get("x-518-call-id")
st2, h2, d2, raw2 = _req("POST", f"/v1/tools/{TOOL_TEXT}", "hello world",
                         {**TH, "Idempotency-Key": f"c1-{_u}"})
check("C2 same Idempotency-Key + same body → replay (same call id, cost kept)",
      st == 200 and st2 == 200 and h2.get("x-518-call-id") == _call_idem
      and h2.get("x-518-cost") == "3"
      and (d2 is None or d2.get("replay") is None),  # raw text body either way
      f"{_call_idem} vs {h2.get('x-518-call-id')}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}", "DIFFERENT",
                     {**TH, "Idempotency-Key": f"c1-{_u}"})
check("C3 same key + different body → 409 idempotency_conflict",
      st == 409 and d.get("error") == "idempotency_conflict", f"{st} {d}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}", "x",
                     {**TH, "Content-Type": "application/json"})
check("C4 wrong content-type → 415 err_content_type",
      st == 415 and d.get("error") == "err_content_type", f"{st} {d}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}?x=1&x=2", "x", TH)
check("C5 duplicate query key → 400 duplicate_param",
      st == 400 and d.get("error") == "duplicate_param", f"{st} {d}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_TEXT}", "x" * 600000, TH)
check("C6 body over the 512KB read cap → 413",
      st == 413 and d.get("error") == "err_request_too_large", f"{st} {d}")

JH = {"Authorization": f"Bearer {KEY_T}", "Content-Type": "application/json"}
_root_cases = (
    ("object", b'{"a": [1, 2], "b": "x"}', b'{"a":[1,2],"b":"x"}'),
    ("array", b'[1, 2, 3]', b'[1,2,3]'),
    ("null", b'null', b'null'),
    ("number", b'42.5', b'42.5'),
    ("string", b'"txt"', b'"txt"'),
)
for label, payload, want in _root_cases:
    st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_JSON}", payload, JH)
    check(f"C7 json body root {label} → echoed as application/json",
          st == 200 and raw == want
          and h.get("content-type", "").startswith("application/json"),
          f"{st} {raw[:50]}")
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_JSON}", b"{not json", JH)
check("C8 invalid JSON → 400 err_body_invalid",
      st == 400 and d.get("error") == "err_body_invalid", f"{st} {d}")

# ═══ D. file tools — three sources ═══
FH = {"Authorization": f"Bearer {KEY_F}"}

def _multipart_file(fields, files):
    b = "----v1s"
    parts = []
    for k, v in (fields or {}).items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for fn, content, ctype in files:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fn}"\r\n'
                     f'Content-Type: {ctype}\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"

_mp, _mpct = _multipart_file({"tool": TOOL_FILE, "client_ref": f"v1fix-{_u}"},
                             [("a.png", PNG, "image/png")])
st, h, d, raw = _req("POST", "/api/uploads", _mp,
                     {"Content-Type": _mpct, "Cookie": ckF})
_src = (d or {}).get("file") or {}
check("D0 web fixture staged (stagingId + sourceToken)",
      st == 200 and _src.get("stagingId") and _src.get("sourceToken"),
      f"{st} {str(d)[:80]}")

_mp2, _mpct2 = _multipart_file(None, [("a.png", PNG, "image/png")])
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_FILE}?preset=fast", _mp2,
                     {**FH, "Content-Type": _mpct2,
                      "Idempotency-Key": f"d1-{_u}"})
_d1_items = (d or {}).get("items") or []
check("D1 multipart source → JSON {ok, call_id, cost, items}",
      st == 200 and d.get("ok") and isinstance(d.get("call_id"), int)
      and d.get("cost") == 0 and len(_d1_items) == 1
      and _d1_items[0]["kind"] == "text"
      and '"preset": "fast"' in _d1_items[0]["text"],
      f"{st} {str(d)[:110]}")

st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_FILE}",
                     {"files": [{"public_id": _src.get("stagingId"),
                                 "source_token": _src.get("sourceToken")}],
                      "params": {}},
                     {**FH, "Content-Type": "application/json",
                      "Idempotency-Key": f"d2-{_u}"})
_d2_items = (d or {}).get("items") or []
check("D2 reference source {public_id, source_token} → ok",
      st == 200 and d.get("ok") and len(_d2_items) == 1, f"{st} {str(d)[:100]}")

# URL source: in-process route invocation with the pinned transport
# replaced (https-on-localhost is not servable; same pattern as the MCP
# suite). Reference + URL mixed in one request → order preserved.
from app.routes import v1 as _v1mod
from starlette.requests import Request as _SReq
import remote as _remote_mod

def _sreq(path, headers, body_bytes, method="POST"):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "method": method, "path": path,
             "headers": raw_headers, "query_string": b"",
             "client": ("127.0.0.1", 4321)}
    async def receive():
        return {"type": "http.request", "body": body_bytes,
                "more_body": False}
    return _SReq(scope, receive)

_orig_pinned_v1 = _remote_mod.pinned_request
_remote_mod.pinned_request = lambda url, **k: (200, PNG)
try:
    import asyncio as _aio
    _url_body = json.dumps({
        "files": [{"public_id": _src.get("stagingId"),
                   "source_token": _src.get("sourceToken")},
                  {"url": f"https://files.example/{_u}/a.png"}],
        "params": {}}).encode()
    _r = _aio.run(_v1mod.call_tool(
        TOOL_FILE,
        _sreq(f"/v1/tools/{TOOL_FILE}",
              {"Authorization": f"Bearer {KEY_F}",
               "Content-Type": "application/json",
               "Idempotency-Key": f"d3-{_u}"},
              _url_body)))
    _d3 = json.loads(_r.body)
    check("D3 url source + mixed [ref, url] → ok (in-process, pinned transport replaced)",
          _r.status_code == 200 and _d3.get("ok")
          and len(_d3.get("items") or []) == 1, str(_d3)[:110])
finally:
    _remote_mod.pinned_request = _orig_pinned_v1

st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_FILE}", "plain text",
                     {**FH, "Content-Type": "text/plain"})
check("D4 file tool with a non-multipart/json body → 415",
      st == 415 and d.get("error") == "err_content_type", f"{st} {d}")

# ═══ E. deep idempotency ═══
IH = {"Authorization": f"Bearer {KEY_I}", "Content-Type": "text/plain"}
_result_box = {}
def _slow_call():
    st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_SLOW}", "go",
                         {**IH, "Idempotency-Key": f"e1-{_u}"})
    _result_box["first"] = (st, h.get("x-518-call-id"))
_t = threading.Thread(target=_slow_call)
_t.start()
time.sleep(1.2)
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_SLOW}", "go",
                     {**IH, "Idempotency-Key": f"e1-{_u}"})
check("E1 replay while running → 409 err_call_in_progress (no re-execution)",
      st == 409 and d.get("error") == "err_call_in_progress"
      and HITS["slow"] == 1, f"{st} {d} hits={HITS['slow']}")
_t.join()
check("E1b the running call completes",
      _result_box.get("first", (0, None))[0] == 200, str(_result_box))
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_SLOW}", "go",
                     {**IH, "Idempotency-Key": f"e1-{_u}"})
check("E1c replay after completion → same call id, endpoint not re-hit",
      st == 200 and h.get("x-518-call-id") == _result_box["first"][1]
      and HITS["slow"] == 1,
      f"{st} {h.get('x-518-call-id')} vs {_result_box.get('first')} hits={HITS['slow']}")

# file-item response + expiry: success returns bytes through the
# internal non-counting channel; killing the mapping → replay 410.
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_RAWF}", "make file",
                     {**IH, "Content-Type": "text/plain",
                      "Idempotency-Key": f"e2-{_u}"})
_e2_call = h.get("x-518-call-id")
check("E2 file item as response body (bytes + disposition + headers)",
      st == 200 and raw == PNG
      and h.get("content-type") == "image/png"
      and "r.png" in h.get("content-disposition", "")
      and (_e2_call or "").isdigit(),
      f"{st} {len(raw)}B {h.get('content-disposition')}")
with db() as con:
    _row = con.execute(
        "select result_json from tool_calls where id=%s",
        (int(_e2_call),)).fetchone()
_e2_items = json.loads(_row["result_json"]).get("items") or []
_e2_pid = _e2_items[0].get("public_id") if _e2_items else ""
with db() as con:
    con.execute("delete from uploads where public_id=%s", (_e2_pid,))
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_RAWF}", "make file",
                     {**IH, "Content-Type": "text/plain",
                      "Idempotency-Key": f"e2-{_u}"})
check("E3 replay after the file mapping died → 410 result_expired (no re-run)",
      st == 410 and d.get("error") == "result_expired"
      and h.get("x-518-call-id") == _e2_call and HITS["rawfile"] == 1,
      f"{st} {d} hits={HITS['rawfile']}")

# ═══ F. provider failure → 502 with detail ═══
GH = {"Authorization": f"Bearer {KEY_G}", "Content-Type": "text/plain"}
_bal_g0 = _sb(uidG)
st, h, d, raw = _req("POST", f"/v1/tools/{TOOL_BAD}", "x", GH)
_det = (d or {}).get("detail") or ""
check("F1 provider 500 → platform 502 err_endpoint_failed + truncated detail",
      st == 502 and d.get("error") == "err_endpoint_failed"
      and _det.startswith("500 ") and len(_det) == 4096
      and set(_det[4:]) == {"E"}, f"{st} {str(d)[:80]} len={len(_det)}")
check("F2 failure refunded (balance unchanged)",
      _sb(uidG) == _bal_g0, f"{_bal_g0} -> {_sb(uidG)}")

# ═══ G. rate limit shared with MCP ═══
CH = {"Authorization": f"Bearer {KEY_C}"}
_limited = False
for _i in range(61):
    st, h, d, raw = _req("GET", "/v1/tools", headers=CH)
    if st == 429:
        _limited = (d or {}).get("error") == "err_rate_limited"
        break
check("G1 /v1 entry throttles at the shared window",
      _limited, f"last={st} {d}")
if _limited:
    st, h, d, raw = _req("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "catalog_search", "arguments": {"q": ""}}},
        {**CH, "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"})
    _g2 = ((d or {}).get("result") or {}).get("isError")
    check("G2 the SAME counter throttles MCP for the same user",
          _g2 is True, str(d)[:110])

# ═══ H. key survives a password change ═══
_b, _ct = _form({"old_password": "Test12345", "new_password": "NewPass99x",
                 "new_password_confirm": "NewPass99x"})
st, h, d, raw = _req("POST", "/account/password", _b,
                     {"Content-Type": _ct, "Cookie": ckA})
st, h, d, raw = _req("GET", "/v1/balance", headers=AH)
check("H1 key still authenticates after the password change",
      st == 200 and d.get("ok"), f"{st} {d}")

# ═══ L. ledger → call link (P6 pages, funded user) ═══
# T 的付费成功调用 (C2) 产生 consume 行; /account/calls 行锚 + 净支出 +
# /account/credits 的流水→调用链接 (浏览器用户在 CI 无积分, 此断言在
# 服务端做)。
st, h, _p, raw_html = _req("GET", "/account/calls?page=1",
                            headers={"Cookie": ckT})
_html = raw_html.decode(errors="replace") if isinstance(raw_html, bytes) else ""
_check_rows = __import__("re").findall(r'<tr id="call-(\d+)">', _html)
st_c, h_c, _p_c, raw_c = _req("GET", "/account/credits",
                               headers={"Cookie": ckT})
_credits = raw_c.decode(errors="replace") if isinstance(raw_c, bytes) else ""
_m = __import__("re").search(r'href="(/account/calls#call-(\d+))"', _credits)
check("L1 credits rows link to the anchored call row",
      st == 200 and st_c == 200 and _m is not None
      and _m.group(2) in _check_rows,
      f"calls={st} credits={st_c} link={_m and _m.group(1)}"
      f" rows={_check_rows[:4]}")
if _m:
    st_l, _h_l, _p_l, raw_l = _req("GET", _m.group(1),
                                    headers={"Cookie": ckT})
    _landed = raw_l.decode(errors="replace") if isinstance(raw_l, bytes) else ""
    check("L2 the link target renders the anchored call row",
          st_l == 200 and f'id="call-{_m.group(2)}"' in _landed,
          f"{st_l}")

# ═══ cleanup ═══
_drop_tools()
_ep_srv.shutdown()
with db() as con:
    _uids = [r["id"] for r in con.execute(
        "select id from users where username like %s", (f"{_u}%",)).fetchall()]
    for uid in _uids:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid,))
    for uid in _uids:
        con.execute("delete from tool_calls where user_id=%s", (uid,))
        con.execute("delete from uploads where user_id=%s", (uid,))
        con.execute("delete from ledger where user_id=%s", (uid,))
        con.execute("delete from api_keys where user_id=%s", (uid,))
        con.execute("delete from sessions where user_id=%s", (uid,))
    for uid in _uids:
        purge_users(con, [uid])
print("cleanup: api-surface fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
