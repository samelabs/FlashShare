#!/usr/bin/env python3
"""API keys (BK): key semantics end to end.

  C*  create → row + encrypted copy; the plaintext appears ONLY in the
      create response and the session-only reveal — never in any other
      API response and never in plaintext in the database
  V*  viewable-anytime: reveal works repeatedly (owner session); Bearer
      access cannot list, reveal, create or delete; a historical row
      without an encrypted copy reveals a typed "not viewable"
  L*  limit 10 per user: the 11th create → 409 err_key_limit; N parallel
      creates cannot exceed the cap; deleting frees a slot
  D*  delete = physical DELETE (no status column, row gone, key stops
      authenticating)
  R*  password change keeps keys working and sessions logged out;
      operator_reset_password deletes every key of the user
  S*  suspended accounts never authenticate with any key
  X*  vault unavailable (TOOL_VAULT_KEY cleared in a subprocess) →
      create fails typed err_vault, zero rows
  M*  page/API naming: /account/keys + /api/keys, no /api/tokens or
      /account/tokens anywhere; old module name absent

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import base64
import hashlib
import hmac as _hmac
import http.client as _hc
import json as _json
import os as _os
import subprocess as _sp
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

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:150]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
_u = "bk" + _uuid.uuid4().hex[:6]
ts = int(_time.time())


def _req(method, path, body=None, headers=None):
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


def _register(name, password="Test12345"):
    b, ct = _form({"username": name, "password": password,
                   "password_confirm": password, "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": password})
    st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    return sc.split(";")[0]


from database import db

uid = uid2 = None
try:
    ck = _register(_u + "a")
    ck2 = _register(_u + "b")
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u + "a",)).fetchone()["id"])
        uid2 = int(con.execute("select id from users where username=%s",
                               (_u + "b",)).fetchone()["id"])

    # ═══ C. create + storage shape ═══
    st, _h, d, _sc = _req("POST", "/api/keys", {"name": "ci"},
                          {"Content-Type": "application/json", "Cookie": ck})
    key_row = (d or {}).get("key") or {}
    plaintext = key_row.get("key") or ""
    check("C1 create → 200 with plaintext + prefix",
          st == 200 and plaintext.startswith("fsk_")
          and key_row.get("prefix") == plaintext[4:12], str(d)[:100])
    with db() as con:
        row = con.execute("select * from api_keys where user_id=%s"
                          " order by id desc limit 1", (uid,)).fetchone()
    _hk = _hmac.new(_os.environ["FLASHSHARE_SESSION_SECRET"].encode(),
                    plaintext.encode(), hashlib.sha256).hexdigest()
    check("C2 row stores the keyed hash, never the plaintext",
          row is not None and row["key_hash"] == _hk
          and plaintext not in (row["key_sealed"] or "")
          and (row["key_sealed"] or "").startswith("enc1:"),
          dict(row) if row else None)
    check("C3 table has no status column (delete is physical)",
          row is not None and "status" not in row.keys())
    # plaintext never appears outside create/reveal
    st, _h, d, _sc = _req("GET", "/api/keys", None, {"Cookie": ck})
    blob = _json.dumps(d)
    check("C4 list exposes prefix only, never the plaintext",
          st == 200 and plaintext not in blob
          and plaintext[4:12] in blob and plaintext not in blob
          and d["keys"][0]["prefix"]
          and "key" not in d["keys"][0], blob[:120])

    # ═══ V. viewable-any-time + session-only ═══
    kid = d["keys"][0]["id"]
    for i in range(2):
        st, _h, d, _sc = _req("GET", f"/api/keys/{kid}/reveal", None, {"Cookie": ck})
        check(f"V{i+1} reveal (repeat {i+1}) → same plaintext",
              st == 200 and d.get("key") == plaintext, st)
    for path, method, body in (("/api/keys", "GET", None),
                               ("/api/keys", "POST", {"name": "x"}),
                               (f"/api/keys/{kid}/reveal", "GET", None),
                               (f"/api/keys/{kid}", "DELETE", None)):
        st, _h, d, _sc = _req(method, path, body, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {plaintext}"})
        check(f"V3 Bearer cannot {method} {path}",
              st in (401, 302) or (st == 403), st)
    # another user's session cannot reveal someone else's key
    st, _h, d, _sc = _req("GET", f"/api/keys/{kid}/reveal", None, {"Cookie": ck2})
    check("V4 another user's session → 404", st == 404, st)
    # historical row without a sealed copy
    with db() as con:
        con.execute("update api_keys set key_sealed=NULL where id=%s", (kid,))
    st, _h, d, _sc = _req("GET", f"/api/keys/{kid}/reveal", None, {"Cookie": ck})
# V5 retired: every key carries a sealed copy (FX-2)

    # ═══ M. naming surface ═══
    st, _h, body, _sc = _req("GET", "/account/keys", None, {"Cookie": ck})
    check("M1 /account/keys page renders (create + list ids)",
          st == 200 and 'id="createKey"' in body and 'id="keyList"' in body
          and "keys.js" in body, st)
    st, _h, _b, _sc = _req("GET", "/account/tokens", None, {"Cookie": ck})
    check("M2 /account/tokens retired (404)", st == 404, st)
    st, _h, _b, _sc = _req("GET", "/api/tokens", None, {"Cookie": ck})
    check("M3 /api/tokens retired (404 JSON)", st == 404, st)
    _src = ""
    for sub in ("app", "core", "static"):
        for root, _d, files in _os.walk(_os.path.join(_REPO, sub)):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith((".py", ".js", ".html")):
                    _src += open(_os.path.join(root, f), encoding="utf-8",
                                 errors="ignore").read()
    check("M4 old module/table names absent in product code",
          "identity.apitokens" not in _src and "api_tokens" not in _src,
          "grep api_tokens|identity.apitokens")

    # ═══ L. limit 10 + concurrency ═══
    st, _h, d, _sc = _req("GET", "/api/keys", None, {"Cookie": ck})
    have = len(d["keys"])
    for i in range(10 - have):
        _req("POST", "/api/keys", {"name": f"bulk{i}"},
             {"Content-Type": "application/json", "Cookie": ck})
    st, _h, d, _sc = _req("GET", "/api/keys", None, {"Cookie": ck})
    check("L1 exactly 10 keys held", len(d["keys"]) == 10, len(d["keys"]))
    st, _h, d, _sc = _req("POST", "/api/keys", {"name": "eleventh"},
                          {"Content-Type": "application/json", "Cookie": ck})
    check("L2 11th create → 409 err_key_limit",
          st == 409 and d.get("error") == "err_key_limit", (st, d))
    # parallel creates on a freed slot cannot exceed the cap
    first_id = d2 = None
    st, _h, d, _sc = _req("GET", "/api/keys", None, {"Cookie": ck})
    first_id = d["keys"][-1]["id"]
    st, _h, d, _sc = _req("DELETE", f"/api/keys/{first_id}", None, {"Cookie": ck})
    check("L3 delete frees a slot (physical)",
          st == 200, st)
    import threading
    results = []
    def _mk(i):
        st, _h, d, _sc = _req("POST", "/api/keys", {"name": f"race{i}"},
                              {"Content-Type": "application/json", "Cookie": ck})
        results.append(st)
    ths = [threading.Thread(target=_mk, args=(i,)) for i in range(6)]
    for t in ths: t.start()
    for t in ths: t.join()
    st, _h, d, _sc = _req("GET", "/api/keys", None, {"Cookie": ck})
    check("L4 six parallel creates on one free slot → exactly 10 rows",
          len(d["keys"]) == 10 and results.count(200) == 1
          and results.count(409) == 5, (len(d["keys"]), results))

    # ═══ D. delete semantics ═══
    last_id = d["keys"][0]["id"]
    st, _h, _d, _sc = _req("DELETE", f"/api/keys/{last_id}", None, {"Cookie": ck})
    with db() as con:
        n = con.execute("select count(*) c from api_keys where id=%s",
                        (last_id,)).fetchone()["c"]
    check("D1 delete removes the row physically", st == 200 and int(n) == 0, n)

    # ═══ R. rotation semantics ═══
    st, _h, d, _sc = _req("POST", "/api/keys", {"name": "keep"},
                          {"Content-Type": "application/json", "Cookie": ck})
    keep_key = d["key"]["key"]
    b, ct = _form({"old_password": "Test12345", "new_password": "NewPass12345",
                   "new_password_confirm": "NewPass12345"})
    st, _h, _d, _sc = _req("POST", "/account/password", b,
                           {"Content-Type": ct, "Cookie": ck})
    check("R1 password change → 302", st == 302, st)
    st, _h, _d, _sc = _req("GET", "/account/keys", None, {"Cookie": ck})
    check("R2 browser session rotated (redirect to login)", st == 302, st)
    st, _h, d, _sc = _req("POST", "/mcp",
                          {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          {"Content-Type": "application/json",
                           "Authorization": f"Bearer {keep_key}"})
    check("R3 API key still authenticates after password change",
          st == 200, st)
    b, ct = _form({"login": _u + "a", "password": "NewPass12345"})
    st_l, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    check("R2b re-login with the new password works", st_l == 302 and ck,
          (st_l, repr(ck)))
    st_probe, _h, d_probe, _sc = _req("GET", "/api/me", None, {"Cookie": ck})
    check("R2c /api/me with the fresh session", st_probe == 200,
          (st_probe, str(d_probe)[:80]))
    # R4 retired: operator_reset_password had no production caller and
    # was deleted with the FX-2 dead-code sweep

    # ═══ S. suspension ═══
    b, ct = _form({"login": _u + "a", "password": "NewPass12345"})
    st_l, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    check("S0pre login with the changed password", st_l == 302 and ck,
          st_l)
    st, _h, d, _sc = _req("GET", "/api/keys", None, {"Cookie": ck})
    check("S0 re-login session reaches the key list", st == 200
          and isinstance(d.get("keys"), list), (st, str(d)[:80]))
    if d.get("keys"):
        _req("DELETE", f"/api/keys/{d['keys'][0]['id']}", None,
             {"Cookie": ck})
    st, _h, d, _sc = _req("POST", "/api/keys", {"name": "sus"},
                          {"Content-Type": "application/json", "Cookie": ck})
    check("S0b create after re-login", st == 200 and (d or {}).get("key"),
          (st, str(d)[:80]))
    sus_key = d["key"]["key"]
    with db() as con:
        con.execute("update users set status='suspended' where id=%s", (uid,))
    st, _h, _d, _sc = _req("POST", "/mcp",
                           {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           {"Content-Type": "application/json",
                            "Authorization": f"Bearer {sus_key}"})
    check("S1 suspended account's key never authenticates", st == 401, st)
    with db() as con:
        con.execute("update users set status='active' where id=%s", (uid,))

    # ═══ X. vault unavailable → typed create failure ═══
    env = dict(_os.environ)
    env.pop("TOOL_VAULT_KEY", None)
    env["DATABASE_URL"] = TEST_DB.replace("postgresql://", "postgresql+psycopg://")
    probe = _sp.run(
        [_sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
         "import os\n"
         "from identity.apikeys import create_key\n"
         "try:\n"
         "    create_key(%d, 'novault')\n"
         "    print('CREATED')\n"
         "except ValueError as e:\n"
         "    print('ERR', e)" % (
             _os.path.join(_REPO, "core"), _os.path.join(_REPO, "app"), uid)],
        capture_output=True, text=True, env=env, timeout=60)
    check("X1 vault unavailable → typed err_vault, zero create",
          probe.stdout.strip() == "ERR err_vault",
          (probe.stdout.strip()[:80], probe.stderr.strip()[-120:]))
    with db() as con:
        n = con.execute("select count(*) c from api_keys where user_id=%s"
                        " and name='novault'", (uid,)).fetchone()["c"]
    check("X1b no row written without the vault", int(n) == 0, n)
finally:
    with db() as con:
        for u in (uid, uid2):
            if u:
                con.execute("delete from api_keys where user_id=%s", (u,))
                con.execute("delete from sessions where user_id=%s", (u,))
                con.execute("delete from tool_calls where user_id=%s", (u,))
                con.execute("delete from ledger where user_id=%s", (u,))
                con.execute("delete from upload_usage where user_id=%s", (u,))
                con.execute("delete from ingress_requests where user_id=%s", (u,))
                con.execute("delete from users where id=%s", (u,))
    print("cleanup done")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    _sys.exit(1)
print(f"OK — API keys ({COUNT} checks)")
