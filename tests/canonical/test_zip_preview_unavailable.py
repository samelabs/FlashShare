#!/usr/bin/env python3
"""Route-level regression (INL-7, restored from hotfix
web-flow-result-production): when the internal authenticated Gokapi
channel fails (401/404) the ZIP preview endpoint /f/{id}/zip-list must
answer a typed 503 preview_unavailable — never a 422 ZIP-complexity
classification, never a fallback to the counted public download path.

Fail-closed: FLASHSHARE_TEST_PORT (isolated, != 8000) + TEST_DB_URL +
a running app and fake Gokapi are required.
"""
import io
import json
import os
import sys
import urllib.request
import uuid
import zipfile

_TEST_PORT = os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        sys.stderr.write("ERROR: production port not allowed.\n"); sys.exit(2)
except ValueError:
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); sys.exit(2)
TEST_DB = os.environ.get("TEST_DB_URL")
if not TEST_DB:
    sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); sys.exit(2)
GOKAPI = os.environ.get("FLASHSHARE_GOKAPI_URL")
GOKAPI_KEY = os.environ.get("FLASHSHARE_GOKAPI_KEY")
if not GOKAPI or not GOKAPI_KEY:
    sys.stderr.write("ERROR: FLASHSHARE_GOKAPI_URL/KEY are required.\n"); sys.exit(2)

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "core"))
os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:120]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


import http.client as _hc

_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "zp" + uuid.uuid4().hex[:6]


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=30)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    raw = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (json.loads(raw) if raw else None), setc
    except Exception:
        return r.status, hdrs, raw.decode(errors="replace"), setc


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _gk_fail(mode):
    req = urllib.request.Request(
        GOKAPI.rstrip("/") + "/ci/fail-internal",
        data=json.dumps({"mode": mode or None}).encode(),
        headers={"apikey": GOKAPI_KEY, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


from database import db

uid = None
try:
    # fixture: user + a real zip upload through the web surface
    b, ct = _form({"username": _u, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{_u}@t.co"})
    st, _h, _b, _sc = _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": _u, "password": "Test12345"})
    st_l, _h, _b, ck = _req("POST", "/auth/login", b, {"Content-Type": ct})
    check("F1 fixtures: register + login", st in (200, 302) and st_l in (200, 302),
          (st, st_l))
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u,)).fetchone()["id"])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    # IO-3a: sharing runs through the share tool — stage the source,
    # then execute; the settled file item carries the public share id.
    body = (f'--B\r\nContent-Disposition: form-data; name="client_ref"\r\n\r\ninl7-{_u}\r\n'
            f'--B\r\nContent-Disposition: form-data; name="tool"\r\n\r\nshare\r\n'
            f'--B\r\nContent-Disposition: form-data; name="file"; filename="p.zip"\r\n'
            f'Content-Type: application/zip\r\n\r\n').encode() + buf.getvalue() + b"\r\n--B--\r\n"
    st, _h, d, _sc = _req("POST", "/api/uploads", body,
                          {"Content-Type": "multipart/form-data; boundary=B",
                           "Cookie": ck})
    staged = (d or {}).get("file") or {}
    st_x, _h2, dx, _sc2 = _req("POST", "/api/tools/execute",
        {"tool_id": "share",
         "source": {"files": [{"public_id": staged.get("stagingId"),
                               "source_token": staged.get("sourceToken") or ""}]},
         "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
         "client_ref": f"inl7x-{_u}"},
        {"Content-Type": "application/json", "Cookie": ck})
    items = ((dx or {}).get("result") or {}).get("items") or []
    pid = next((it.get("public_id") for it in items if it.get("kind") == "file"), None)
    check("F2 zip upload via web (share tool)", st == 200 and st_x == 200 and bool(pid),
          (st, st_x, str(dx)[:90]))

    if pid:
        st, _h, d, _sc = _req("GET", f"/f/{pid}/zip-list", {"Cookie": ck})
        check("INL-5 baseline zip-list ok", st == 200 and (d or {}).get("ok"), st)

        for mode in ("401", "404"):
            _gk_fail(mode)
            try:
                st_f, _hf, df, _scf = _req("GET", f"/f/{pid}/zip-list",
                                           {"Cookie": ck})
                check(f"INL-7 internal {mode} → 503 preview_unavailable",
                      st_f == 503 and (df or {}).get("error") == "preview_unavailable",
                      (st_f, str(df)[:80]))
                check(f"INL-7 internal {mode} never 422", st_f != 422, st_f)
            finally:
                _gk_fail(None)

        st, _h, d, _sc = _req("GET", f"/f/{pid}/zip-list", {"Cookie": ck})
        check("INL-7b channel restored → 200 again", st == 200, st)
finally:
    _gk_fail(None)
    if uid is not None:
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from users where id=%s", (uid,))
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    sys.exit(1)
print(f"OK — zip preview unavailability regression ({COUNT} checks)")
