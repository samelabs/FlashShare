"""OAuth 2.1 authorization server for the reviewable /mcp/openai surface."""

import json
import secrets
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.web_auth import client_ip
from app.web_ctx import ctx_base
from config import BASE_URL
from identity.oauth_provider import (
    DEFAULT_SCOPE, ISSUER, OPENAI_MCP_RESOURCE, SCOPES,
    client, create_authorization_code, exchange_authorization_code,
    refresh_access_token, register_client, validate_authorization,
)

router = APIRouter()
_CONSENT_COOKIE = "fs_oauth_consent"
_CONSENT_TTL = 10 * 60
_REG_LIMIT = 30
_REG_WINDOW = 60 * 60
_reg_hits = {}


def _oauth_json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        body, status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _client_ip_throttled(request: Request) -> bool:
    import time
    ts = int(time.time())
    key = client_ip(request)
    hits = [t for t in _reg_hits.get(key, []) if ts - t < _REG_WINDOW]
    if len(hits) >= _REG_LIMIT:
        _reg_hits[key] = hits
        return True
    hits.append(ts)
    _reg_hits[key] = hits
    if len(_reg_hits) > 10000:
        cutoff = ts - _REG_WINDOW
        for item in list(_reg_hits):
            _reg_hits[item] = [t for t in _reg_hits[item] if t >= cutoff]
            if not _reg_hits[item]:
                del _reg_hits[item]
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
async def protected_resource_metadata():
    return {
        "resource": OPENAI_MCP_RESOURCE,
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
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "authorization_response_iss_parameter_supported": True,
        "token_endpoint_auth_methods_supported": ["none"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(SCOPES),
    }


@router.post("/oauth/register")
async def oauth_register(request: Request):
    if _client_ip_throttled(request):
        return _oauth_json(
            {
                "error": "temporarily_unavailable",
                "error_description": "Too many client registrations.",
            },
            429)
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > 65536:
                return _oauth_json({"error": "invalid_client_metadata"}, 413)
        except ValueError:
            pass
    try:
        raw = await request.body()
        if len(raw) > 65536:
            return _oauth_json({"error": "invalid_client_metadata"}, 413)
        output = register_client(json.loads(raw or b"{}"))
    except ValueError as exc:
        return _oauth_json(
            {
                "error": str(exc),
                "error_description": "Client registration was rejected.",
            },
            400)
    except Exception:
        return _oauth_json(
            {
                "error": "invalid_client_metadata",
                "error_description": "Client registration was rejected.",
            },
            400)
    return _oauth_json(output, 201)


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    params = _auth_params(request.query_params)
    try:
        client_row, scope = _validated(params)
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
    ctx.update({
        "shell_class": "",
        "client_name": client_row["client_name"],
        "oauth_params": {**params, "scope": scope},
        "oauth_nonce": nonce,
        "oauth_read": "files:read" in requested,
        "oauth_write": "files:write" in requested,
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
    user = getattr(request.state, "user", None)
    if not user or getattr(request.state, "auth_via", "") != "session":
        return RedirectResponse("/auth/login", 302)

    form = await request.form()
    params = _auth_params(form)
    try:
        _client_row, scope = _validated(params)
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
    if not client(client_id):
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
