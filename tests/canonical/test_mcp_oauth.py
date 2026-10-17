#!/usr/bin/env python3
"""OAuth 2.1 canonical for the main /mcp surface (CIMD clients).

  W*  Well-known metadata: RFC 9728 protected-resource (the resource IS
      BASE_URL/mcp; scopes = the capability triple) and RFC 8414
      authorization-server (PKCE S256, public clients, NO registration
      endpoint — CIMD replaced DCR).
  A*  CIMD validation: the client_id URL fetch goes through the pinned
      SSRF-guarded transport; the document must self-reference, carry
      valid redirect_uris and a public-client auth method.
  F*  Full flow (identity core in-process with the fetch patched; the
      resource server side over real HTTP): authorize validation →
      consent code → PKCE exchange → access token calls /mcp → refresh
      rotation → resource binding → PKCE negatives.
  S*  Capability enforcement at the one chokepoint: a token granted
      only tools:call executes tools but is refused files:read /
      files:delete with err_insufficient_scope; fsk_ keys keep the full
      set; OAuth plugin attribution lands on tool_calls.oauth_client_id.
  C*  Challenge: anonymous tools/call → 401 + WWW-Authenticate pointing
      at the protected-resource metadata; `register` stays anonymously
      callable (the one-call onboarding path).
  R*  Revocation: grants are viewable and revocable from the
      session-authenticated account surface — a revoked token is 401 on
      /mcp and its refresh is invalid_grant; another user's grant is
      untouchable (404); a password change revokes the user's OAuth
      tokens while the fsk_ key survives.

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import asyncio
import base64
import hashlib
import http.client as _hc
import json as _json
import os as _os
import secrets
import sys as _sys
import time as _ts
import urllib.parse as _up
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
_os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53901")
_os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "test-gk-key")

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:150]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
BASE = _os.environ.get("FLASHSHARE_BASE_URL", f"http://127.0.0.1:{_TEST_PORT}").rstrip("/")
RESOURCE = f"{BASE}/mcp"
ts = int(_ts.time())
_u = "oa" + _uuid.uuid4().hex[:6]


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        parsed = _json.loads(data) if data else None
    except Exception:
        parsed = data.decode("utf-8", "replace")
    return r.status, hdrs, parsed


def _mcp(body, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return _req("POST", "/mcp", body, h)


# ── fixtures: a real web user (owns the OAuth grant) ──
from database import db  # noqa: E402
from identity.apikeys import create_key  # noqa: E402


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])


UID = _mkuser(_u)
UID2 = None  # second user, created by the R* revocation section
FSK = create_key(UID, "oa-suite")["key"]

PNG_HEX = ("89504e470d0a1a0a0000000d4948445200000001000000010806"
           "0000001f15c4890000000d49444154789c626001000000ffff03"
           "000006000557bfabd40000000049454e44ae426082")
PNG_B64 = base64.b64encode(bytes.fromhex(PNG_HEX)).decode()

# ═══ W. well-known metadata ═══
st, _h, d = _req("GET", "/.well-known/oauth-protected-resource")
check("W1 protected-resource metadata: resource IS /mcp, AS = issuer",
      st == 200 and d.get("resource") == RESOURCE
      and d.get("authorization_servers") == [BASE], d)
check("W2 scopes_supported = capability triple",
      d.get("scopes_supported") == ["tools:call", "files:read", "files:delete"], d)

st, _h, d = _req("GET", "/.well-known/oauth-authorization-server")
check("W3 AS metadata: PKCE S256, public clients, NO registration endpoint (CIMD)",
      st == 200 and d.get("code_challenge_methods_supported") == ["S256"]
      and d.get("token_endpoint_auth_methods_supported") == ["none"]
      and "registration_endpoint" not in d
      and d.get("scopes_supported") == ["tools:call", "files:read", "files:delete"], d)

# ═══ C. challenge contract ═══
st, h, d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "share",
                            "arguments": {"client_ref": f"c-{_u}"}}})
_wa = (h or {}).get("www-authenticate") or ""
check("C1 anonymous tools/call → 401 + WWW-Authenticate resource_metadata",
      st == 401 and "resource_metadata" in _wa
      and "oauth-protected-resource" in _wa, (st, _wa))

st, h, d = _mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "register", "arguments": {}}})
check("C2 register stays anonymously callable (200, mints fsk_ key)",
      st == 200 and "fsk_" in _json.dumps(d), st)

st, h, d = _mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "catalog_search", "arguments": {"q": "x"}}},
                token="fsk_definitely_not_real")
check("C3 presented-but-invalid bearer → 401 (never silent anonymous)",
      st == 401, st)

# invalid-token challenge carries the RFC 6750 error code
st, h, d = _mcp({"jsonrpc": "2.0", "id": 4, "method": "ping"},
                token="fsot_not_a_real_token")
_wa = (h or {}).get("www-authenticate") or ""
check("C4 invalid bearer → 401 + error=\"invalid_token\" + resource_metadata",
      st == 401 and 'error="invalid_token"' in _wa
      and "resource_metadata" in _wa, (st, _wa))

# authorize per-IP throttle (CIMD outbound + codes-write amplifier gate)
import app.routes.oauth_provider as _rp  # noqa: E402
from starlette.requests import Request as _SReq  # noqa: E402
_rp._authz_hits.clear()
def _fake_req(ip="9.9.9.9"):
    scope = {"type": "http", "method": "GET", "path": "/oauth/authorize",
             "query_string": b"", "headers": [],
             "client": (ip, 1234), "server": ("test", 80)}
    return _SReq(scope)
_throttled = [_rp._authorize_throttled(_fake_req())
              for _ in range(_rp._AUTHZ_LIMIT + 1)]
check("C5 authorize per-IP throttle engages at the limit",
      all(_throttled[:_rp._AUTHZ_LIMIT]) is False
      and _throttled[-1] is True, (sum(_throttled), _rp._AUTHZ_LIMIT))
_rp._authz_hits.clear()

# /oauth/token per-IP throttle: separate store, its own limit
_rp._token_hits.clear()
_throttled_t = [_rp._token_throttled(_fake_req())
                for _ in range(_rp._TOKEN_LIMIT + 1)]
check("C6 token per-IP throttle engages at the limit",
      all(_throttled_t[:_rp._TOKEN_LIMIT]) is False
      and _throttled_t[-1] is True, (sum(_throttled_t), _rp._TOKEN_LIMIT))
_rp._token_hits.clear()

# full consent flow over the in-process ASGI app (TestClient): the
# patched fetch IS the server's fetch — login, consent render, code
# redirect with state echo, deny path, CSRF nonce enforcement
from fastapi.testclient import TestClient as _TC  # noqa: E402
from app.main import app as _app  # noqa: E402

# ═══ A. CIMD validation (fetch patched to a served document) ═══
import remote as _remote  # noqa: E402
import identity.oauth_provider as _oa  # noqa: E402

CIMD_URL = f"https://plugin-{_u}.example/oauth.json"
REDIRECT = f"https://plugin-{_u}.example/callback"
CIMD_DOC = {
    "client_id": CIMD_URL,
    "client_name": "Suite Plugin",
    "redirect_uris": [REDIRECT, "http://localhost:8943/callback"],
    "token_endpoint_auth_method": "none",
    "logo_uri": "https://plugin.example/logo.png",
}


def _serve_cimd(doc):
    return (200, _json.dumps(doc).encode())


_orig_pinned = _remote.pinned_request

_st, _h, _d = None, None, None
try:
    _remote.pinned_request = lambda url, **k: _serve_cimd(CIMD_DOC)
    _oa._cimd_cache.clear()

    doc = _oa.cimd_fetch(CIMD_URL)
    check("A1 CIMD fetch returns the verified document",
          bool(doc) and doc["client_name"] == "Suite Plugin"
          and set(doc["redirect_uris"]) == set(CIMD_DOC["redirect_uris"]), doc)

    bad = dict(CIMD_DOC, client_id="https://other.example/oauth.json")
    _remote.pinned_request = lambda url, **k: _serve_cimd(bad)
    _oa._cimd_cache.clear()
    check("A2 non-self-referential document rejected (anti-impersonation)",
          _oa.cimd_fetch(CIMD_URL) is None)

    _remote.pinned_request = lambda url, **k: _serve_cimd(
        dict(CIMD_DOC, redirect_uris=["ftp://x/y"]))
    _oa._cimd_cache.clear()
    check("A3 non-https/loopback redirect entries rejected",
          _oa.cimd_fetch(CIMD_URL) is None)

    _remote.pinned_request = lambda url, **k: _serve_cimd(
        dict(CIMD_DOC, token_endpoint_auth_method="client_secret_basic"))
    _oa._cimd_cache.clear()
    check("A4 confidential-client auth method rejected (public-only MVP)",
          _oa.cimd_fetch(CIMD_URL) is None)

    check("A5 http:// client_id rejected before any fetch",
          _oa.cimd_fetch("http://plain.example/oauth.json") is None)

    # ═══ F. full flow (identity core; resource-server side over real HTTP) ═══
    _remote.pinned_request = lambda url, **k: _serve_cimd(CIMD_DOC)
    _oa._cimd_cache.clear()

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    vdoc, scope = _oa.validate_authorization(
        CIMD_URL, REDIRECT, "tools:call", RESOURCE,
        challenge, "S256")
    check("F1 authorize validation passes (CIMD client, PKCE S256, resource=/mcp)",
          vdoc["client_name"] == "Suite Plugin" and scope == "tools:call", scope)

    try:
        _oa.validate_authorization(CIMD_URL, REDIRECT, "tools:call",
                                   f"{BASE}/other", challenge, "S256")
        ok = False
    except ValueError as e:
        ok = str(e) == "invalid_target"
    check("F2 resource binding: audience ≠ /mcp rejected", ok)

    code = _oa.create_authorization_code(
        UID, CIMD_URL, REDIRECT, challenge, scope, RESOURCE)
    check("F3 consent code minted (fsoa_ prefix)", code.startswith("fsoa_"), code[:12])

    toks = _oa.exchange_authorization_code(
        code, CIMD_URL, REDIRECT, verifier, RESOURCE)
    ACCESS = toks["access_token"]
    REFRESH = toks["refresh_token"]
    check("F4 PKCE exchange mints fsot_ access + fsor_ refresh, scope echoed",
          ACCESS.startswith("fsot_") and REFRESH.startswith("fsor_")
          and toks["scope"] == "tools:call", list(toks))

    ident = _oa.authenticate_access_token(ACCESS, RESOURCE)
    check("F5 access token authenticates as the granting user with client attribution",
          ident and ident["user"]["id"] == UID
          and ident["scopes"] == {"tools:call"}
          and ident["client_id"] == CIMD_URL,
          ident and {k: ident[k] for k in ("scopes", "client_id")})

    try:
        _oa.exchange_authorization_code(
            code, CIMD_URL, REDIRECT, verifier, RESOURCE)
        ok = False
    except ValueError as e:
        ok = str(e) == "invalid_grant"
    check("F6 authorization code is single-use", ok)

    # ── the resource-server side over real HTTP ──
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "a.png", "b64": PNG_B64}],
                                     "client_ref": f"oa-{_u}"}}}, token=ACCESS)
    _r = (d or {}).get("result") or {}
    check("S1 OAuth token executes a tool over HTTP (readable result)",
          st == 200 and _r.get("isError") is False
          and "Result page:" in ((_r.get("content") or [{}])[0].get("text", "")),
          str(d)[:120])

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                      "params": {"name": "list_uploads",
                                 "arguments": {}}}, token=ACCESS)
    _r = (d or {}).get("result") or {}
    _sc = _r.get("structuredContent") or {}
    check("S2 tools:call-only token refused files:read (err_insufficient_scope)",
          _r.get("isError") is True
          and _sc.get("error") == "err_insufficient_scope"
          and _sc.get("required_scope") == "files:read", _sc)

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                      "params": {"name": "delete_upload",
                                 "arguments": {"public_id": "x"}}}, token=ACCESS)
    _sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    check("S3 tools:call-only token refused files:delete",
          _sc.get("error") == "err_insufficient_scope"
          and _sc.get("required_scope") == "files:delete", _sc)

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "b.png", "b64": PNG_B64}],
                                     "client_ref": f"fsk-{_u}"}}}, token=FSK)
    check("S4 fsk_ key keeps the full capability set (no regression)",
          st == 200 and not ((d or {}).get("result") or {}).get("isError"), st)

    # S6: the platform-tool path enforces tools:call too — mint a
    # files:read-only token and try to execute (paid tools spend credits)
    code_rd = _oa.create_authorization_code(
        UID, CIMD_URL, REDIRECT, challenge, "files:read", RESOURCE)
    toks_rd = _oa.exchange_authorization_code(
        code_rd, CIMD_URL, REDIRECT, verifier, RESOURCE)
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                      "params": {"name": "share",
                                 "arguments": {
                                     "files": [{"filename": "c.png", "b64": PNG_B64}],
                                     "client_ref": f"s6-{_u}"}}},
                     token=toks_rd["access_token"])
    _sc = ((d or {}).get("result") or {}).get("structuredContent") or {}
    check("S6 files:read-only token refused a PLATFORM tool (tools:call)",
          ((d or {}).get("result") or {}).get("isError") is True
          and _sc.get("error") == "err_insufficient_scope"
          and _sc.get("required_scope") == "tools:call", _sc)
    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                      "params": {"name": "list_uploads",
                                 "arguments": {}}},
                     token=toks_rd["access_token"])
    check("S6b the same files:read token DOES list uploads",
          st == 200 and not ((d or {}).get("result") or {}).get("isError"),
          st)

    with db() as con:
        row = con.execute(
            "select oauth_client_id from tool_calls where user_id=%s"
            " and client_ref=%s", (UID, f"oa-{_u}")).fetchone()
    check("S5 OAuth plugin attribution stamped on tool_calls",
          row and row["oauth_client_id"] == CIMD_URL, row)

    # refresh rotation
    toks2 = _oa.refresh_access_token(REFRESH, CIMD_URL, RESOURCE)
    check("F7 refresh rotation mints a fresh pair",
          toks2["access_token"].startswith("fsot_")
          and toks2["access_token"] != ACCESS, list(toks2)[:1])
    try:
        _oa.refresh_access_token(REFRESH, CIMD_URL, RESOURCE)
        ok = False
    except ValueError:
        ok = True
    check("F8 used refresh token is dead (rotation is one-time)", ok)

    st, _h, d = _mcp({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                      "params": {"name": "catalog_search",
                                 "arguments": {"q": ""}}}, token=toks2["access_token"])
    check("F9 rotated token works over HTTP (discovery = tools:call)",
          st == 200 and not ((d or {}).get("result") or {}).get("isError"), st)

    # PKCE negative: fresh code, wrong verifier
    code2 = _oa.create_authorization_code(
        UID, CIMD_URL, REDIRECT, challenge, "tools:call", RESOURCE)
    try:
        _oa.exchange_authorization_code(
            code2, CIMD_URL, REDIRECT, "x" * 50, RESOURCE)
        ok = False
    except ValueError as e:
        ok = str(e) == "invalid_grant"
    check("F10 wrong PKCE verifier rejected", ok)

finally:
    _remote.pinned_request = _orig_pinned
    _oa._cimd_cache.clear()

# ═══ consent flow (TestClient, in-process server + patched CIMD) ═══
try:
    _remote.pinned_request = lambda url, **k: _serve_cimd(CIMD_DOC)
    _oa._cimd_cache.clear()
    with db() as con:
        _set_pw = con.execute(
            "update users set password_hash=%s where id=%s",
            (__import__("bcrypt").hashpw(b"Test12345", __import__("bcrypt").gensalt()).decode(), UID)).rowcount
    with _TC(_app, base_url="https://testserver") as _tc:
        _lr = _tc.post("/auth/login",
                       data={"login": _u, "password": "Test12345"},
                       follow_redirects=False)
        check("P1 web login for the consent flow", _lr.status_code in (302, 303),
              _lr.status_code)
        authz_url = ("/oauth/authorize?" + _up.urlencode({
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call files:read",
            "state": f"st-{_u}", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE}))
        page = _tc.get(authz_url)
        _html = page.text
        check("P2 consent page renders the CIMD-verified name + both scopes",
              page.status_code == 200 and "Suite Plugin" in _html
              and "oauth_scope_tools_call" in _html
              and "oauth_scope_files_read" in _html
              and "oauth_scope_files_delete" not in _html,
              page.status_code)
        # anti-impersonation lines: where the client lives + where the
        # browser returns (loopback redirect distinguishes the two hosts)
        _page_lb = _tc.get("/oauth/authorize?" + _up.urlencode({
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": "http://localhost:8943/callback",
            "scope": "tools:call", "state": f"st-{_u}",
            "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE}))
        _html_lb = _page_lb.text
        check("P2b consent page shows client host + redirect host lines",
              _page_lb.status_code == 200
              and 'data-i18n="oauth_client_from"' in _html_lb
              and 'data-i18n="oauth_redirect_to"' in _html_lb
              and f"plugin-{_u}.example" in _html_lb
              and "localhost" in _html_lb,
              _page_lb.status_code)
        # CSRF: posting without the nonce cookie fails
        bad = _tc.post("/oauth/authorize", data={
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call",
            "state": "x", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE,
            "consent_nonce": "forged", "action": "allow"})
        check("P3 consent submit without the nonce cookie rejected",
              bad.status_code == 400, bad.status_code)
        # deny → error redirect with state echo
        deny = _tc.post("/oauth/authorize", data={
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call",
            "state": f"st-{_u}", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE,
            "consent_nonce": _tc.cookies.get("fs_oauth_consent"),
            "action": "deny"}, follow_redirects=False)
        _loc = deny.headers.get("location") or ""
        check("P4 deny → redirect error=access_denied + state echoed",
              deny.status_code == 302 and "error=access_denied" in _loc
              and f"state=st-{_u}" in _loc, _loc[:100])
        # allow → code redirect with state echo + iss (a fresh render
        # first — the deny above consumed the one-shot nonce cookie)
        _tc.get(authz_url)
        allow = _tc.post("/oauth/authorize", data={
            "response_type": "code", "client_id": CIMD_URL,
            "redirect_uri": REDIRECT, "scope": "tools:call",
            "state": f"st-{_u}", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": RESOURCE,
            "consent_nonce": _tc.cookies.get("fs_oauth_consent"),
            "action": "allow"}, follow_redirects=False)
        _loc = allow.headers.get("location") or ""
        _q = dict(_up.parse_qsl(_up.urlsplit(_loc).query))
        check("P5 allow → 302 with code + state + iss",
              allow.status_code == 302 and _q.get("code", "").startswith("fsoa_")
              and _q.get("state") == f"st-{_u}" and _q.get("iss") == BASE,
              _loc[:120])
        # the consent-minted code exchanges at the real token endpoint
        _tokr = _tc.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": CIMD_URL,
            "code": _q.get("code"), "redirect_uri": REDIRECT,
            "code_verifier": verifier, "resource": RESOURCE})
        _td = _tokr.json() if _tokr.status_code == 200 else {}
        check("P6 consent code exchanges at /oauth/token over HTTP",
              _tokr.status_code == 200 and _td.get("access_token", "").startswith("fsot_")
              and _td.get("scope") == "tools:call", _tokr.status_code)
        if _td.get("access_token"):
            _st2, _h2, d2 = _mcp({"jsonrpc": "2.0", "id": 20,
                                  "method": "tools/call",
                                  "params": {"name": "catalog_search",
                                             "arguments": {"q": ""}}},
                                 token=_td["access_token"])
            check("P7 consent-flow token works end-to-end on /mcp",
                  _st2 == 200 and not ((d2 or {}).get("result") or {}).get("isError"),
                  _st2)

        # ═══ R. grants: view + revoke + credential rotation ═══
        _gl = _tc.get("/api/oauth/grants")
        _gd = _gl.json() if _gl.status_code == 200 else {}
        _entry = next((g for g in _gd.get("grants", [])
                       if g.get("client_id") == CIMD_URL), None)
        check("R1 grants list shows the consented client (host + merged scopes)",
              _gl.status_code == 200 and _gd.get("ok") is True and _entry
              and _entry["client_host"] == f"plugin-{_u}.example"
              and _entry["scope"] == "tools:call files:read",
              str(_gd)[:140])

        _rv = _tc.post("/api/oauth/grants/revoke",
                       json={"client_id": CIMD_URL})
        check("R2 revoking the client's grant → ok",
              _rv.status_code == 200 and _rv.json().get("ok") is True,
              _rv.status_code)
        _st3, _h3, d3 = _mcp({"jsonrpc": "2.0", "id": 21,
                              "method": "tools/call",
                              "params": {"name": "catalog_search",
                                         "arguments": {"q": ""}}},
                             token=_td["access_token"])
        check("R3 revoked access token → 401 on /mcp", _st3 == 401, _st3)
        try:
            _oa.refresh_access_token(_td["refresh_token"], CIMD_URL, RESOURCE)
            _r3b = False
        except ValueError as _e3b:
            _r3b = str(_e3b) == "invalid_grant"
        check("R3b revoked refresh token → invalid_grant", _r3b)

        _rv2 = _tc.post("/api/oauth/grants/revoke",
                        json={"client_id": CIMD_URL})
        check("R4 re-revoke with no live rows → 404 not_found",
              _rv2.status_code == 404
              and _rv2.json().get("error") == "not_found", _rv2.status_code)

        # another user's grant is untouchable from this session
        UID2 = _mkuser(_u + "b")
        CIMD_URL2 = f"https://other-{_u}.example/oauth.json"
        REDIRECT2 = f"https://other-{_u}.example/callback"
        _code_b = _oa.create_authorization_code(
            UID2, CIMD_URL2, REDIRECT2, challenge, "tools:call", RESOURCE)
        _toks_b = _oa.exchange_authorization_code(
            _code_b, CIMD_URL2, REDIRECT2, verifier, RESOURCE)
        _rv3 = _tc.post("/api/oauth/grants/revoke",
                        json={"client_id": CIMD_URL2})
        check("R5 revoking another user's client grant → 404",
              _rv3.status_code == 404, _rv3.status_code)
        check("R5b the other user's token still authenticates",
              _oa.authenticate_access_token(
                  _toks_b["access_token"], RESOURCE) is not None)

        # /oauth/token honors its per-IP gate over HTTP (burn the budget
        # for the TestClient peer address, then one real POST)
        _rp._token_hits.clear()
        for _ in range(_rp._TOKEN_LIMIT):
            _rp._token_throttled(_fake_req(ip="testclient"))
        _t429 = _tc.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": CIMD_URL,
            "code": "fsoa_x", "redirect_uri": REDIRECT,
            "code_verifier": verifier, "resource": RESOURCE})
        check("C7 real /oauth/token over the limit → 429 temporarily_unavailable",
              _t429.status_code == 429
              and _t429.json().get("error") == "temporarily_unavailable",
              _t429.status_code)
        _rp._token_hits.clear()

        # credential rotation: a fresh grant for UID dies with the
        # password change; the fsk_ key survives
        _code_c = _oa.create_authorization_code(
            UID, CIMD_URL, REDIRECT, challenge, "tools:call", RESOURCE)
        _toks_c = _oa.exchange_authorization_code(
            _code_c, CIMD_URL, REDIRECT, verifier, RESOURCE)
        from identity.auth import change_password as _chg_pw
        _chg_pw(UID, "Test12345", "NewPass1234")
        _st4, _h4, d4 = _mcp({"jsonrpc": "2.0", "id": 22,
                              "method": "tools/call",
                              "params": {"name": "catalog_search",
                                         "arguments": {"q": ""}}},
                             token=_toks_c["access_token"])
        check("R6 password change revokes the user's OAuth tokens → 401",
              _st4 == 401, _st4)
        try:
            _oa.refresh_access_token(_toks_c["refresh_token"],
                                     CIMD_URL, RESOURCE)
            _r6b = False
        except ValueError:
            _r6b = True
        check("R6b refresh after password change → invalid_grant", _r6b)
        st, _h, d = _mcp({"jsonrpc": "2.0", "id": 23,
                          "method": "tools/call",
                          "params": {"name": "catalog_search",
                                     "arguments": {"q": ""}}},
                         token=FSK)
        check("R7 the same user's fsk_ key still works after the change",
              st == 200 and not ((d or {}).get("result") or {}).get("isError"),
              st)
finally:
    _remote.pinned_request = _orig_pinned
    _oa._cimd_cache.clear()

# token endpoint over HTTP: unknown client (fetch will fail for real)
st, _h, d = _req("POST", "/oauth/token", {
    "grant_type": "authorization_code", "client_id": "https://no-such.example/x.json",
    "code": "fsoa_x", "redirect_uri": "https://no-such.example/cb",
    "code_verifier": "v" * 50, "resource": RESOURCE},
    {"Content-Type": "application/x-www-form-urlencoded"})
# form-encoded body needed; send as dict is JSON — do it properly
body = ("grant_type=authorization_code&client_id="
        + _up.quote("https://no-such.example/x.json")
        + "&code=fsoa_x&redirect_uri=" + _up.quote("https://no-such.example/cb")
        + "&code_verifier=" + "v" * 50 + "&resource=" + _up.quote(RESOURCE))
st, _h, d = _req("POST", "/oauth/token", body.encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"})
check("T1 token endpoint: unresolvable CIMD client → 401 invalid_client",
      st == 401 and (d or {}).get("error") == "invalid_client", (st, d))

# ═══ cleanup ═══
with db() as con:
    for _uid_c in (UID, UID2):
        if _uid_c is None:
            continue
        con.execute("delete from oauth_mcp_codes where user_id=%s", (_uid_c,))
        con.execute("delete from oauth_mcp_tokens where user_id=%s", (_uid_c,))
        con.execute("delete from tool_calls where user_id=%s", (_uid_c,))
        con.execute("delete from uploads where user_id=%s", (_uid_c,))
        con.execute("delete from api_keys where user_id=%s", (_uid_c,))
        con.execute("delete from ledger where user_id=%s", (_uid_c,))
    purge_users(con, [u for u in (UID, UID2) if u is not None])
print("cleanup: oauth fixtures removed")

print()
print(f"FAIL: {' — '.join(FAIL) if FAIL else '无'} — ({COUNT} checks)")
_sys.exit(1 if FAIL else 0)
