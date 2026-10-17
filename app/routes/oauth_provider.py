"""OAuth 2.1 authorization server for the main /mcp surface (CIMD)."""

import asyncio
import secrets
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.web_ctx import ctx_base
from config import BASE_URL
from identity.oauth_provider import (
    DEFAULT_SCOPE, ISSUER, MCP_RESOURCE, SCOPES,
    cimd_fetch, create_authorization_code, exchange_authorization_code,
    refresh_access_token, validate_authorization,
)

router = APIRouter()
_CONSENT_COOKIE = "fs_oauth_consent"
_CONSENT_TTL = 10 * 60
_AUTHZ_LIMIT = 60
_AUTHZ_WINDOW = 60 * 60
_authz_hits: dict = {}


def _authorize_throttled(request: Request) -> bool:
    """Per-IP gate on /oauth/authorize: every request can trigger one
    outbound CIMD fetch (on cache miss) and a codes-row write — an
    unauthenticated amplifier otherwise."""
    import time
    from app.web_auth import client_ip
    ts = int(time.time())
    key = client_ip(request)
    hits = [t for t in _authz_hits.get(key, []) if ts - t < _AUTHZ_WINDOW]
    if len(hits) >= _AUTHZ_LIMIT:
        _authz_hits[key] = hits
        return True
    hits.append(ts)
    _authz_hits[key] = hits
    if len(_authz_hits) > 10000:
        cutoff = ts - _AUTHZ_WINDOW
        for item in list(_authz_hits):
            _authz_hits[item] = [t for t in _authz_hits[item] if t >= cutoff]
            if not _authz_hits[item]:
                del _authz_hits[item]
    return False


def _oauth_json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        body, status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# /oauth/token is anonymous and every request may trigger one outbound
# CIMD fetch (on cache miss) — its own per-IP gate, separate store from
# the authorize throttle.
_TOKEN_LIMIT = 120
_TOKEN_WINDOW = 60 * 60
_token_hits: dict = {}


def _token_throttled(request: Request) -> bool:
    import time
    from app.web_auth import client_ip
    ts = int(time.time())
    key = client_ip(request)
    hits = [t for t in _token_hits.get(key, []) if ts - t < _TOKEN_WINDOW]
    if len(hits) >= _TOKEN_LIMIT:
        _token_hits[key] = hits
        return True
    hits.append(ts)
    _token_hits[key] = hits
    if len(_token_hits) > 10000:
        cutoff = ts - _TOKEN_WINDOW
        for item in list(_token_hits):
            _token_hits[item] = [t for t in _token_hits[item] if t >= cutoff]
            if not _token_hits[item]:
                del _token_hits[item]
    return False


def _redirect_uri_with(uri: str, **params) -> str:
    parts = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend(
        (key, str(value)) for key, value in params.items()
        if value is not None)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query), parts.fragment))


def _auth_params(values) -> dict:
    return {
        "response_type": str(values.get("response_type") or ""),
        "client_id": str(values.get("client_id") or ""),
        "redirect_uri": str(values.get("redirect_uri") or ""),
        "scope": str(values.get("scope") or DEFAULT_SCOPE),
        "state": str(values.get("state") or ""),
        "code_challenge": str(values.get("code_challenge") or ""),
        "code_challenge_method": str(
            values.get("code_challenge_method") or ""),
        "resource": str(values.get("resource") or ""),
    }


def _validated(params: dict):
    if params["response_type"] != "code":
        raise ValueError("unsupported_response_type")
    return validate_authorization(
        params["client_id"], params["redirect_uri"], params["scope"],
        params["resource"], params["code_challenge"],
        params["code_challenge_method"])


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    # Two discovery forms per RFC 9728: the root form and the
    # path-inserted form. The protected resource IS /mcp (a path under
    # this host), and some MCP clients derive the metadata URL by
    # inserting the well-known prefix before the resource path — a 404
    # there reads as "no OAuth" to them.
    return {
        "resource": MCP_RESOURCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{BASE_URL}/agents",
        "resource_policy_uri": f"{BASE_URL}/privacy",
        "resource_tos_uri": f"{BASE_URL}/terms",
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "authorization_response_iss_parameter_supported": True,
        "token_endpoint_auth_methods_supported": ["none"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(SCOPES),
    }


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    if _authorize_throttled(request):
        return _oauth_json(
            {"error": "temporarily_unavailable",
             "error_description": "Too many authorization requests."},
            429)
    params = _auth_params(request.query_params)
    try:
        client_row, scope = await asyncio.to_thread(_validated, params)
    except ValueError as exc:
        return _oauth_json(
            {
                "error": str(exc),
                "error_description": "Invalid authorization request.",
            },
            400)

    user = getattr(request.state, "user", None)
    if not user or getattr(request.state, "auth_via", "") != "session":
        relative = request.url.path
        if request.url.query:
            relative += "?" + request.url.query
        return RedirectResponse(
            "/auth/login?next=" + urllib.parse.quote(relative, safe=""), 302)

    nonce = secrets.token_urlsafe(32)
    from app.main import templates
    ctx = ctx_base(request)
    requested = set(scope.split())
    # Anti-impersonation context: a client may CLAIM any display name —
    # the consent page must also show where the client document lives
    # and where the browser returns after consenting.
    client_host = urllib.parse.urlsplit(params["client_id"]).hostname or ""
    redirect_host = urllib.parse.urlsplit(
        params["redirect_uri"]).hostname or ""
    ctx.update({
        "shell_class": "",
        "client_name": client_row["client_name"],
        "client_host": client_host,
        "redirect_host": redirect_host,
        "oauth_params": {**params, "scope": scope},
        "oauth_nonce": nonce,
        "oauth_call": "tools:call" in requested,
        "oauth_read": "files:read" in requested,
        "oauth_delete": "files:delete" in requested,
        "oauth_user": user.get("display_name") or user.get("username"),
    })
    response = templates.TemplateResponse(
        request, "oauth/consent.html", ctx)
    response.set_cookie(
        _CONSENT_COOKIE, nonce, max_age=_CONSENT_TTL, path="/oauth",
        httponly=True, secure=True, samesite="lax")
    return response


@router.post("/oauth/authorize")
async def oauth_authorize_submit(request: Request):
    if _authorize_throttled(request):
        return _oauth_json(
            {"error": "temporarily_unavailable",
             "error_description": "Too many authorization requests."},
            429)
    user = getattr(request.state, "user", None)
    if not user or getattr(request.state, "auth_via", "") != "session":
        return RedirectResponse("/auth/login", 302)

    form = await request.form()
    params = _auth_params(form)
    try:
        _client_row, scope = await asyncio.to_thread(_validated, params)
    except ValueError as exc:
        return _oauth_json(
            {
                "error": str(exc),
                "error_description": "Invalid authorization request.",
            },
            400)

    nonce = str(form.get("consent_nonce") or "")
    cookie_nonce = request.cookies.get(_CONSENT_COOKIE, "")
    if (not nonce or not cookie_nonce
            or not secrets.compare_digest(nonce, cookie_nonce)):
        return _oauth_json(
            {
                "error": "invalid_request",
                "error_description": "Authorization page expired.",
            },
            400)

    state = params["state"] or None
    if str(form.get("action") or "deny") != "allow":
        target = _redirect_uri_with(
            params["redirect_uri"],
            error="access_denied",
            state=state,
            iss=ISSUER)
    else:
        code = create_authorization_code(
            int(user["id"]),
            params["client_id"],
            params["redirect_uri"],
            params["code_challenge"],
            scope,
            params["resource"])
        target = _redirect_uri_with(
            params["redirect_uri"],
            code=code,
            state=state,
            iss=ISSUER)

    response = RedirectResponse(target, 302)
    response.delete_cookie(_CONSENT_COOKIE, path="/oauth")
    return response


@router.post("/oauth/token")
async def oauth_token(request: Request):
    if _token_throttled(request):
        return _oauth_json(
            {"error": "temporarily_unavailable",
             "error_description": "Too many token requests."},
            429)
    form = await request.form()
    if request.headers.get("authorization"):
        return _oauth_json(
            {
                "error": "invalid_client",
                "error_description": (
                    "This authorization server uses public clients."),
            },
            401)

    grant_type = str(form.get("grant_type") or "")
    client_id = str(form.get("client_id") or "")
    resource = str(form.get("resource") or "")
    if not await asyncio.to_thread(cimd_fetch, client_id):
        return _oauth_json({"error": "invalid_client"}, 401)

    try:
        if grant_type == "authorization_code":
            output = exchange_authorization_code(
                str(form.get("code") or ""),
                client_id,
                str(form.get("redirect_uri") or ""),
                str(form.get("code_verifier") or ""),
                resource)
        elif grant_type == "refresh_token":
            output = refresh_access_token(
                str(form.get("refresh_token") or ""),
                client_id,
                resource)
        else:
            return _oauth_json(
                {"error": "unsupported_grant_type"}, 400)
    except ValueError as exc:
        return _oauth_json({"error": str(exc)}, 400)
    return _oauth_json(output)
