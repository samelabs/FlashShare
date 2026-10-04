#!/usr/bin/env python3
"""OpenAI public MCP projection: OAuth 2.1 + fixed review surface."""

import base64
import hashlib
import http.client as _hc
import json
import os
import re
import sys
import urllib.parse
import uuid

_TEST_PORT = os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n")
    sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        sys.stderr.write("ERROR: production port not allowed.\n")
        sys.exit(2)
except ValueError:
    sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n")
    sys.exit(2)

TEST_DB = os.environ.get("TEST_DB_URL")
if not TEST_DB:
    sys.stderr.write("ERROR: TEST_DB_URL is required.\n")
    sys.exit(2)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "core"))
os.environ.setdefault(
    "DATABASE_URL",
    TEST_DB.replace("postgresql://", "postgresql+psycopg://"))

from database import db  # noqa: E402
from tests.support.cleanup import purge_users  # noqa: E402

HOST = ("127.0.0.1", int(_TEST_PORT))
FAIL = []
COUNT = 0
created_users = []


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name,
          ("— " + str(detail)[:220]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


def req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    hdr = dict(headers or {})
    payload = body
    if isinstance(body, dict):
        payload = json.dumps(body).encode()
        hdr.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        payload = body.encode()
    conn.request(method, path, payload, hdr)
    response = conn.getresponse()
    data = response.read()
    response_headers = {k.lower(): v for k, v in response.getheaders()}
    set_cookie = response.getheader("Set-Cookie") or ""
    status = response.status
    conn.close()
    try:
        parsed = json.loads(data) if data else None
    except Exception:
        parsed = data.decode(errors="replace")
    return status, response_headers, parsed, set_cookie


def form(fields):
    return urllib.parse.urlencode(fields)


def rpc(mid, method, params=None):
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "method": method,
        "params": params or {},
    }


uid = "oa" + uuid.uuid4().hex[:8]
email = uid + "@example.test"
password = "Canonical-pass-518!"

try:
    status, _, resource_meta, _ = req(
        "GET", "/.well-known/oauth-protected-resource")
    check("protected resource metadata 200", status == 200, status)
    check(
        "resource identifies /mcp/openai",
        isinstance(resource_meta, dict)
        and resource_meta.get("resource", "").endswith("/mcp/openai"),
        resource_meta)

    status, _, auth_meta, _ = req(
        "GET", "/.well-known/oauth-authorization-server")
    check("authorization metadata 200", status == 200, status)
    check(
        "PKCE S256 advertised",
        isinstance(auth_meta, dict)
        and "S256" in auth_meta.get("code_challenge_methods_supported", []),
        auth_meta)
    check(
        "public token auth advertised",
        isinstance(auth_meta, dict)
        and "none" in auth_meta.get(
            "token_endpoint_auth_methods_supported", []),
        auth_meta)

    status, _, support_page, _ = req("GET", "/support")
    check(
        "dedicated support page is public",
        status == 200 and "ad@live.it" in str(support_page),
        (status, str(support_page)[:180]))

    bad_registration = {
        "client_name": "Bad",
        "redirect_uris": ["https://evil.example/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    status, _, output, _ = req(
        "POST", "/oauth/register", bad_registration)
    check(
        "DCR rejects non-OpenAI callback",
        status == 400,
        (status, output))

    registration = {
        "client_name": "OpenAI Canonical",
        "redirect_uris": [
            "https://chatgpt.com/connector_platform_oauth_redirect"
        ],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    status, _, client_doc, _ = req(
        "POST", "/oauth/register", registration)
    check(
        "DCR succeeds",
        status == 201
        and isinstance(client_doc, dict)
        and bool(client_doc.get("client_id")),
        (status, client_doc))
    client_id = (
        client_doc.get("client_id", "")
        if isinstance(client_doc, dict) else "")

    status, _, _, set_cookie = req(
        "POST",
        "/auth/register",
        form({
            "username": uid,
            "email": email,
            "password": password,
            "password_confirm": password,
            "next": "/",
        }),
        {"Content-Type": "application/x-www-form-urlencoded"})
    check("test user registers", status in (302, 303), status)
    session_cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
    check(
        "registration minted browser session",
        bool(session_cookie),
        set_cookie)

    with db() as con:
        row = con.execute(
            "SELECT id FROM users WHERE username = %s", (uid,)).fetchone()
        if row:
            created_users.append(int(row["id"]))

    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = "state-" + uuid.uuid4().hex
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": registration["redirect_uris"][0],
        "scope": "files:read files:write",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource_meta.get("resource", ""),
    })
    status, _, page, consent_set_cookie = req(
        "GET",
        "/oauth/authorize?" + query,
        headers={"Cookie": session_cookie})
    check(
        "authorization renders explicit consent",
        status == 200 and "OpenAI Canonical" in str(page),
        (status, str(page)[:180]))
    nonce_match = re.search(
        r'name="consent_nonce" value="([^"]+)"', str(page))
    nonce = nonce_match.group(1) if nonce_match else ""
    consent_cookie = (
        consent_set_cookie.split(";", 1)[0]
        if consent_set_cookie else "")
    check(
        "consent nonce issued",
        bool(nonce and consent_cookie),
        (nonce, consent_set_cookie))

    approval = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": registration["redirect_uris"][0],
        "scope": "files:read files:write",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource_meta.get("resource", ""),
        "consent_nonce": nonce,
        "action": "allow",
    }
    status, headers, _, _ = req(
        "POST",
        "/oauth/authorize",
        form(approval),
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": session_cookie + "; " + consent_cookie,
        })
    location = headers.get("location", "")
    parsed = urllib.parse.urlsplit(location)
    query_params = urllib.parse.parse_qs(parsed.query)
    code = (query_params.get("code") or [""])[0]
    check(
        "consent redirects with code/state/issuer",
        status in (302, 303)
        and bool(code)
        and (query_params.get("state") or [""])[0] == state
        and (query_params.get("iss") or [""])[0]
        == auth_meta.get("issuer"),
        location)

    status, _, token_doc, _ = req(
        "POST",
        "/oauth/token",
        form({
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": registration["redirect_uris"][0],
            "code_verifier": verifier,
            "resource": resource_meta.get("resource", ""),
        }),
        {"Content-Type": "application/x-www-form-urlencoded"})
    access = (
        token_doc.get("access_token", "")
        if isinstance(token_doc, dict) else "")
    refresh = (
        token_doc.get("refresh_token", "")
        if isinstance(token_doc, dict) else "")
    check(
        "code exchange issues OAuth tokens",
        status == 200
        and access.startswith("fsot_")
        and refresh.startswith("fsor_"),
        (status, token_doc))

    status, _, initialized, _ = req(
        "POST",
        "/mcp/openai",
        rpc(1, "initialize", {"protocolVersion": "2025-11-25"}))
    instructions = (
        initialized.get("result", {}).get("instructions", "")
        if isinstance(initialized, dict) else "")
    check(
        "public MCP initializes anonymously",
        status == 200,
        (status, initialized))
    forbidden_copy = (
        "credit", "pricing", "price", "top up", "subscribe", "free")
    check(
        "public instructions contain no commerce copy",
        not any(word in instructions.lower() for word in forbidden_copy),
        instructions)

    auth_headers = {
        "Authorization": "Bearer " + access,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
    }
    status, _, listed, _ = req(
        "POST",
        "/mcp/openai",
        rpc(2, "tools/list"),
        auth_headers)
    tools_list = (
        listed.get("result", {}).get("tools", [])
        if isinstance(listed, dict) else [])
    names = {tool.get("name") for tool in tools_list}
    expected = {
        "create_upload",
        "list_uploads",
        "delete_upload",
        "share",
        "beautify",
        "compress",
        "crop_white",
        "ico",
        "img2pdf",
        "pdf_merge",
        "zip_pack",
    }
    check(
        "fixed review tools present",
        expected.issubset(names),
        sorted(names))
    check(
        "dynamic/router tools absent",
        not ({
            "register", "catalog_search", "pin_tool", "unpin_tool", "privacy"
        } & names),
        sorted(names))

    annotation_fields = (
        "readOnlyHint",
        "destructiveHint",
        "openWorldHint",
        "idempotentHint",
    )
    annotations_ok = all(
        isinstance(tool.get("annotations", {}).get(key), bool)
        for tool in tools_list
        for key in annotation_fields)
    security_ok = all(
        bool(tool.get("securitySchemes")) for tool in tools_list)
    check(
        "all tools have explicit safety annotations",
        annotations_ok,
        tools_list)
    check(
        "all tools declare OAuth security",
        security_ok,
        tools_list)

    descriptions = " ".join(
        str(tool.get("description", "")) for tool in tools_list).lower()
    check(
        "tool descriptions contain no commerce copy",
        not any(word in descriptions for word in forbidden_copy),
        descriptions[:500])

    status, _, denied, _ = req(
        "POST",
        "/mcp/openai",
        rpc(3, "tools/call", {
            "name": "catalog_search",
            "arguments": {"q": ""},
        }),
        auth_headers)
    check(
        "catalog_search cannot be called",
        isinstance(denied, dict)
        and denied.get("error", {}).get("code") == -32602,
        denied)

    status, headers, _, _ = req(
        "POST",
        "/mcp/openai",
        rpc(4, "tools/list"),
        {
            "Authorization": "Bearer fsot_invalid",
            "Content-Type": "application/json",
        })
    check("invalid token is HTTP 401", status == 401, status)
    check(
        "401 advertises protected resource metadata",
        "oauth-protected-resource"
        in headers.get("www-authenticate", ""),
        headers.get("www-authenticate"))

    status, _, refreshed, _ = req(
        "POST",
        "/oauth/token",
        form({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
            "resource": resource_meta.get("resource", ""),
        }),
        {"Content-Type": "application/x-www-form-urlencoded"})
    check(
        "refresh rotates both OAuth credentials",
        status == 200
        and isinstance(refreshed, dict)
        and refreshed.get("access_token") != access
        and refreshed.get("refresh_token") != refresh,
        (status, refreshed))

finally:
    if created_users:
        with db() as con:
            purge_users(con, created_users)

print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    sys.exit(1)
