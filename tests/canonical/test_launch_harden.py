#!/usr/bin/env python3
"""Launch hardening canonical — the seven launch-stability guards.

  H1*  Remote results carry a CUMULATIVE file-bytes ceiling
       (config MAX_BATCH_BYTES): exceeding it raises RemoteError
       'too_large' and no further item is fetched or decoded (both the
       url and data_b64 paths count).
  H2*  One local handler run is bounded by LOCAL_EXEC_TIMEOUT_S (120):
       a wedged worker times out into pool reset + old-worker
       terminate and the EXISTING err_endpoint_timeout refund path;
       the in-flight slot returns to zero and the next call executes.
  H4*  The tiny-form surfaces (/api/client-error, /auth/login,
       /auth/register) reject bodies over 64KB with 413
       err_payload_too_large before the concurrency semaphore.
  H5*  Receipts stuck running are settled by the hourly housekeeping
       sweep (older_than_s=1800): stale rows settle failed with their
       exact usage released; fresh receipts stay running.
  H6*  NaN/Inf number params reject (param_invalid); notifications are
       never answered (including ping) and the tools/list cursor maps
       Infinity to -32602; the password cap is bcrypt's own 72 UTF-8
       bytes on the single validate_password point (register/change/
       set all share it).
  H7*  QR minting endpoints: payloads beyond the fixed encoder capacity
       (78 bytes) answer typed 400/404 — never a 500; the site QR mints
       same-site paths only; QR responses are cacheable.

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import asyncio
import base64
import concurrent.futures as _cf
import hashlib
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _ts
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:170]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "lh" + _uuid.uuid4().hex[:6]
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


def _mcp_raw(raw_body: bytes, token: str):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
    conn.request("POST", "/mcp", raw_body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, (_json.loads(data) if data else None)
    except Exception:
        return r.status, None


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "lh"},
                             {"Content-Type": "application/json", "Cookie": ck})
    from database import db
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


uid = None
try:
    uid, ck, TOK = _register(_u + "a")
    check("F0 fixture registered", uid and TOK, uid)

    # ═══ H1. cumulative remote file-bytes ceiling ═══
    import remote as _remote
    _CAP = 1000
    _orig_cap = _remote.MAX_BATCH_BYTES
    _remote.MAX_BATCH_BYTES = _CAP
    _fetches = []

    def _spy_fetch(url):
        _fetches.append(url)
        return PNG + b"\x00" * (100 - len(PNG))

    try:
        _out = {"items": [
            {"kind": "file", "filename": f"x{i}.png",
             "content_type": "image/png",
             "url": f"https://ep.example/{i}.png"}
            for i in range(1, 21)]}
        try:
            _remote.parse_endpoint_items(_out, _spy_fetch)
            _code = "no-error"
        except _remote.RemoteError as exc:
            _code = exc.code
        check("H1 cumulative overage raises too_large", _code == "too_large", _code)
        check("H1 items after the breach are never fetched",
              len(_fetches) == 11, len(_fetches))
        # under the ceiling everything still lands
        _fetches.clear()
        _small = {"items": [
            {"kind": "file", "filename": f"s{i}.png",
             "content_type": "image/png",
             "url": f"https://ep.example/s{i}.png"}
            for i in range(3)]}
        _res = _remote.parse_endpoint_items(_small, _spy_fetch)
        check("H1 under the ceiling all items land",
              len(_fetches) == 3 and [r["size"] for r in _res] == [100] * 3,
              (len(_fetches), len(_res)))
    finally:
        _remote.MAX_BATCH_BYTES = _orig_cap

    # ═══ H2. local execution timeout → reset/terminate/refund path ═══
    import execution.orchestrator as _orch
    from execution.gate import run as gate_run
    from execution.invocation import ToolInvocation
    from execution.results import StagedItems
    from execution import ExecutionError
    from execution.pool import inflight_count
    from governance.catalog import resolve as _resolve

    check("H2a LOCAL_EXEC_TIMEOUT_S contract is 120",
          _orch.LOCAL_EXEC_TIMEOUT_S == 120, _orch.LOCAL_EXEC_TIMEOUT_S)

    class _StuckExecutor:
        def submit(self, fn, *a, **k):
            return _cf.Future()  # never resolves

    async def _stuck():
        return await _orch._run_handler(_StuckExecutor(), lambda *a: None,
                                        "share", [], {})

    async def _ok():
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    def _inv(ref):
        return ToolInvocation("share", [], {}, {"expiry_days": 1,
                                                "allowed_downloads": 50}, ref)

    tool, terr = _resolve("share", uid)
    tool["id"] = tool["tool_key"]
    check("H2b fixture: share is a local tool", terr is None
          and tool.get("execution") == "local", (terr, tool and tool.get("execution")))

    _orig_to = _orch.LOCAL_EXEC_TIMEOUT_S
    _orch.LOCAL_EXEC_TIMEOUT_S = 0.5
    try:
        _p, err, status = asyncio.run(
            gate_run({"id": uid}, tool, _inv(f"lh-h2-{_u}"), _stuck))
    finally:
        _orch.LOCAL_EXEC_TIMEOUT_S = _orig_to
    check("H2c wedged handler → err_endpoint_timeout (existing refund path)",
          status == 400 and err.get("error") == "err_endpoint_timeout"
          and err.get("ok") is False, (status, err))
    check("H2d in-flight slot released after the timeout",
          inflight_count() == 0, inflight_count())
    from database import db
    with db() as con:
        _row = con.execute(
            "select status from tool_calls where user_id=%s and client_ref=%s",
            (uid, f"lh-h2-{_u}")).fetchone()
    check("H2e the attempt is finalized failed", _row and _row["status"] == "failed",
          _row and dict(_row))
    _p, err, _s = asyncio.run(
        gate_run({"id": uid}, tool, _inv(f"lh-h2b-{_u}"), _ok))
    check("H2f the next call executes normally",
          err is None and _p.get("ok") is True and inflight_count() == 0,
          (err, inflight_count()))

    # ═══ H4. tiny-form bodies capped at 64KB → 413 ═══
    _big = b"x" * 70000
    st, _h, d, _sc = _req("POST", "/api/client-error", _big,
                          {"Content-Type": "application/json"})
    check("H4a oversized /api/client-error → 413 err_payload_too_large",
          st == 413 and d.get("ok") is False
          and d.get("error") == "err_payload_too_large", (st, d))
    st, _h, d, _sc = _req("POST", "/auth/login", _big,
                          {"Content-Type": "application/json"})
    check("H4b oversized /auth/login → 413 err_payload_too_large",
          st == 413 and (d or {}).get("error") == "err_payload_too_large", (st, d))
    st, _h, d, _sc = _req("POST", "/auth/register", _big,
                          {"Content-Type": "application/json"})
    check("H4c oversized /auth/register → 413 err_payload_too_large",
          st == 413 and (d or {}).get("error") == "err_payload_too_large", (st, d))
    st, _h, d, _sc = _req("POST", "/api/client-error",
                          b'{"lang":"en","url":"/","msg":"probe"}',
                          {"Content-Type": "application/json"})
    check("H4d normal-size client-error still accepted",
          st == 200 and (d or {}).get("ok") is True, (st, d))

    # ═══ H5. hourly sweep settles stale running receipts ═══
    from sharing.ingress_receipt import claim_ingress_quota
    from sharing.housekeeping import housekeeping_once
    from repo import ingress_requests as _ir

    def _claim(ref, size=100):
        h = hashlib.sha256(ref.encode()).hexdigest()
        receipt, usage_ids, replay = claim_ingress_quota(
            uid, "127.0.0.1", ref, h, [size], scope="lh")
        return int(receipt["id"]), list(usage_ids)

    old_id, old_usage = _claim(f"lh-old-{_u}")
    new_id, new_usage = _claim(f"lh-new-{_u}")
    with db() as con:
        con.execute("update ingress_requests set started_at=%s where id=%s",
                    (int(_ts.time()) - 3600, old_id))
    sweep = housekeeping_once()
    with db() as con:
        _st_old = dict(con.execute(
            "select status from ingress_requests where id=%s",
            (old_id,)).fetchone())
        _st_new = dict(con.execute(
            "select status from ingress_requests where id=%s",
            (new_id,)).fetchone())
        _usage_left = [int(r["id"]) for r in con.execute(
            "select id from upload_usage where user_id=%s", (uid,)).fetchall()]
    check("H5a stale running receipt settled by the hourly sweep",
          sweep.get("receipts_recovered", 0) >= 1 and _st_old["status"] == "failed",
          (sweep, _st_old))
    check("H5b stale receipt's exact usage released",
          not (set(old_usage) & set(_usage_left)), (old_usage, _usage_left))
    check("H5c fresh receipt stays running with its usage intact",
          _st_new["status"] == "running"
          and set(new_usage) <= set(_usage_left), (_st_new, _usage_left))

    # ═══ H6a. NaN/Inf number params reject ═══
    from governance.tool_params import enforce_params_decl, ParamsValueError
    _decl = [{"key": "n", "type": "number", "label": "N", "min": 0, "max": 10}]
    for _v in (float("nan"), float("inf"), float("-inf")):
        try:
            enforce_params_decl(_decl, {"n": _v})
            _res6 = "accepted"
        except ParamsValueError as exc:
            _res6 = str(exc)
        check(f"H6a non-finite {_v} → param_invalid",
              _res6 == "param_invalid", _res6)
    check("H6a finite number passes the same declaration",
          enforce_params_decl(_decl, {"n": 1.5}) == {"n": 1.5},
          enforce_params_decl(_decl, {"n": 1.5}))
    # through the real MCP surface: a user tool with a number param,
    # called with a raw NaN literal (python json accepts it) — the
    # stored-contract enforcement must reject it as param_invalid
    _st, _h, _d, _sc = _req("POST", "/api/usertools", {
        "label": f"Num {_u}",
        "url": "https://93.184.216.34/x", "timeout_s": 30,
        "format_families": "image", "accept": [".png"],
        "files_min": 1, "files_max": 1, "outputs": ["text"],
        "params": [{"key": "n", "type": "number", "label": "N",
                    "min": 0, "max": 10}], "cost": 0},
        {"Content-Type": "application/json", "Cookie": ck})
    _numkey = (_d or {}).get("tool_key")
    check("H6a fixture: number-param tool created", _st == 200 and _numkey, (_st, _d))
    _nan_body = (f'{{"jsonrpc":"2.0","id":30,"method":"tools/call","params":{{'
                 f'"name":"{_numkey}","arguments":{{"params":{{"n":NaN}},'
                 f'"client_ref":"lh6a-{_u}"}}}}}}').encode()
    _st30, _d30 = _mcp_raw(_nan_body, TOK)
    _t30 = (((_d30 or {}).get("result") or {}).get("structuredContent")
            or {})
    check("H6a MCP NaN param → isError param_invalid naming the field",
          (_d30 or {}).get("result", {}).get("isError") is True
          and _t30.get("error") == "param_invalid"
          and _t30.get("field") == "n", str(_d30)[:160])

    # ═══ H6b. notifications never answered; cursor OverflowError ═══
    st_np, d_np = _mcp_raw(b'{"jsonrpc":"2.0","method":"ping"}', TOK)
    check("H6b ping notification → 202, no response body",
          st_np == 202 and d_np is None, (st_np, d_np))
    st_p, d_p = _mcp_raw(b'{"jsonrpc":"2.0","id":31,"method":"ping"}', TOK)
    check("H6b ping request still answered",
          st_p == 200 and ((d_p or {}).get("result") or {}) == {}, (st_p, d_p))
    st_c, d_c = _mcp_raw(b'{"jsonrpc":"2.0","id":32,"method":"tools/list",'
                         b'"params":{"cursor":1e999}}', TOK)
    check("H6b Infinity cursor → -32602 invalid cursor",
          ((d_c or {}).get("error") or {}).get("code") == -32602, str(d_c)[:120])
    st_c2, d_c2 = _mcp_raw(b'{"jsonrpc":"2.0","id":33,"method":"tools/list",'
                           b'"params":{"cursor":0}}', TOK)
    check("H6b cursor 0 still pages fine",
          st_c2 == 200 and ((d_c2 or {}).get("result") or {}).get("tools") is not None,
          st_c2)

    # ═══ H6c. password cap = bcrypt's 72 UTF-8 bytes (single point) ═══
    from identity.auth import validate_password
    check("H6c 72 bytes pass, 73 bytes reject",
          validate_password("a" * 72) is None
          and validate_password("a" * 73) == "err_password_long",
          (validate_password("a" * 72), validate_password("a" * 73)))
    check("H6c multibyte counted in bytes (24 CJK ok, 25 CJK reject)",
          validate_password("漢" * 24) is None
          and validate_password("漢" * 25) == "err_password_long",
          (validate_password("漢" * 24), validate_password("漢" * 25)))
    b, ct = _form({"username": _u + "pw", "password": "漢" * 25,
                   "password_confirm": "漢" * 25,
                   "email": f"{_u}pw@t.co"})
    st_pw, _h, d_pw, _sc = _req("POST", "/auth/register", b, {
        "Content-Type": ct, "Accept": "application/json"})
    check("H6c register with a 75-byte password → typed err_password_long",
          (d_pw or {}).get("error") == "err_password_long", (st_pw, d_pw))

    # ═══ H7. QR surfaces: capacity guard + same-site-only + cacheable ═══
    # The fixed V4-L encoder holds 78 payload bytes and RAISES beyond
    # that; every public minting endpoint must reject out-of-capacity
    # input as a typed 4xx, never surface a 500. The site QR mints
    # same-site paths only (no scheme/protocol-relative/backslash/colon)
    # and all QR responses are cacheable.
    long_p = "/" + "a" * 100
    st, _h, _d, _sc = _req("GET", "/qr.svg?p=" + long_p)
    check("H7a site QR over capacity → 400 (no 500 from the encoder)",
          st == 400, st)
    for bad, why in (("%2F%2Fevil.com", "protocol-relative"),
                     ("https%3A%2F%2Fevil.com", "scheme"),
                     ("%5Cevil", "backslash"),
                     ("javascript%3Aalert(1)", "colon")):
        st, _h, _d, _sc = _req("GET", "/qr.svg?p=" + bad)
        check(f"H7a site QR rejects {why} → 400", st == 400, (bad, st))
    st, h, _d, _sc = _req("GET", "/qr.svg?p=%2F")
    check("H7a site QR home path → 200 svg + Cache-Control",
          st == 200 and "svg" in (h.get("content-type") or "")
          and "max-age" in (h.get("cache-control") or ""), (st, h.get("cache-control")))
    st, _h, _d, _sc = _req("GET", "/tool/compress/qr.svg")
    check("H7b bare-id tool QR → 200", st == 200, st)
    st, _h, _d, _sc = _req("GET", "/tool/no_such_tool_xyz/qr.svg")
    check("H7b unknown tool QR → 404 (page's own surface)",
          st == 404, st)
    st, _h, _d, _sc = _req("GET", "/r/no_such_result/qr.svg")
    check("H7b unknown result QR → 404 (no existence oracle)",
          st == 404, st)

    # ═══ H7c. QR encoder invariants: data-region size + standard samples ═══
    # The V4 (33×33) data region is exactly 807 modules (100 codewords × 8
    # + 7 remainder bits); the historical reservation bug marked two data
    # cells as format area and shrank it to 805, desynchronizing every
    # data bit after them. The three fixed samples (short URL, typical
    # /r/{id} address, 78-byte capacity cap) are generated by the Nayuki
    # QR-Code-generator reference (see the fixture header) and must match
    # the produced matrices cell by cell.
    from utils import _qr_function_modules, qr_svg  # noqa: E402
    _mods, _res = _qr_function_modules(33)
    _free = sum(1 for _row in _res for _c in _row if not _c)
    check("H7c V4 non-reserved modules == 807 (100 codewords × 8 + 7 remainder bits)",
          _free == 807, _free)
    import re as _re
    with open(_os.path.join(_REPO, "tests", "support", "qr_v4_samples.json")) as _f:
        _samples = _json.load(_f)
    for _case in _samples["cases"]:
        _grid = [[0] * 33 for _ in range(33)]
        for _m in _re.finditer(r'<rect x="(\d+)" y="(\d+)"', qr_svg(_case["payload"])):
            _x, _y = int(_m.group(1)) // 8 - 4, int(_m.group(2)) // 8 - 4
            if 0 <= _x < 33 and 0 <= _y < 33:
                _grid[_y][_x] = 1
        _diff = sum(1 for _yy in range(33) for _xx in range(33)
                    if _grid[_yy][_xx] != int(_case["matrix"][_yy][_xx]))
        check(f'H7c qr_svg({_case["bytes"]}-byte payload) == reference sample cell by cell',
              _diff == 0, _diff)

    # ═══ H8. Gokapi listing stays off the event loop ═══
    # The uploads listing performs a slow HTTP call to Gokapi; run in the
    # event loop it stalls every concurrent request (healthz included).
    # The fake Gokapi accepts a list-latency injection (/ci/delay): with a
    # 1s listing in flight, a concurrent /healthz must answer < 200ms.
    _gk_base = _os.environ.get("FLASHSHARE_GOKAPI_URL",
                               "http://127.0.0.1:53898").rstrip("/")
    def _goki_delay(ms: int) -> int:
        _c = _hc.HTTPConnection(_gk_base.replace("http://", ""), timeout=5)
        _c.request("POST", "/ci/delay",
                   body=_json.dumps({"ms": ms}).encode(),
                   headers={"Content-Type": "application/json",
                            "apikey": _os.environ.get("FLASHSHARE_GOKAPI_KEY", "")})
        _r = _c.getresponse()
        _r.read()
        _c.close()
        return _r.status
    _uid8, _ck8, _key8 = _register(f"{_u}h8")
    check("H8 fake list delay injected (1s)", _goki_delay(1000) == 200)
    def _pull_uploads():
        st_u, _hu, _du, _scu = _req("GET", "/api/uploads?offset=0",
                                    headers={"Cookie": _ck8})
        return st_u
    _pull_fut = _cf.ThreadPoolExecutor(1).submit(_pull_uploads)
    _t0 = _ts.time()
    st_hz, _hh, _dh, _sch = _req("GET", "/healthz")
    _hz_ms = int((_ts.time() - _t0) * 1000)
    _st_up = _pull_fut.result()
    check("H8 /healthz answers <200ms while a Gokapi listing is in flight",
          st_hz == 200 and _st_up == 200 and _hz_ms < 200,
          f"healthz={st_hz} uploads={_st_up} { _hz_ms }ms")
    check("H8 delay reset to zero", _goki_delay(0) == 200)
finally:
    if uid:
        from database import db
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from upload_usage where user_id=%s", (uid,))
            con.execute("delete from ingress_requests where user_id=%s", (uid,))
            purge_users(con, [uid])

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
_sys.exit(1 if FAIL else 0)
