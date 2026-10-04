"""OAuth 2.1 identity for the public 518.PLUS MCP projection.

The general /mcp endpoint keeps its fsk_ API-key contract. This module exists
only so a public ChatGPT/Codex plugin can use standards-based authorization
without ever exposing an API key to the model.
"""

import base64
import hashlib
import re
import secrets
import urllib.parse

from config import BASE_URL
from database import db
from repo import oauth_provider as repo
from security import token_hash
from utils import now

ISSUER = BASE_URL
OPENAI_MCP_RESOURCE = f"{BASE_URL}/mcp/openai"
SCOPES = ("files:read", "files:write")
DEFAULT_SCOPE = " ".join(SCOPES)

CODE_TTL = 10 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60

_CLIENT_PREFIX = "fsoc_"
_CODE_PREFIX = "fsoa_"
_ACCESS_PREFIX = "fsot_"
_REFRESH_PREFIX = "fsor_"
_PKCE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def _opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _scope(raw) -> str:
    parts = [p for p in str(raw or DEFAULT_SCOPE).split() if p]
    if not parts:
        parts = list(SCOPES)
    if len(parts) != len(set(parts)) or any(p not in SCOPES for p in parts):
        raise ValueError("invalid_scope")
    return " ".join(p for p in SCOPES if p in parts)


def _valid_redirect_uri(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        p = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if p.fragment or not p.scheme or not p.netloc:
        return False
    return p.scheme == "https" and (p.hostname or "").lower() == "chatgpt.com"


def register_client(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid_client_metadata")
    if payload.get("token_endpoint_auth_method", "none") != "none":
        raise ValueError("invalid_client_metadata")
    redirect_uris = payload.get("redirect_uris")
    if (not isinstance(redirect_uris, list) or not redirect_uris
            or len(redirect_uris) > 10
            or any(not _valid_redirect_uri(uri) for uri in redirect_uris)):
        raise ValueError("invalid_redirect_uri")
    redirect_uris = list(dict.fromkeys(redirect_uris))

    grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
    responses = payload.get("response_types", ["code"])
    if (not isinstance(grants, list)
            or "authorization_code" not in grants
            or any(g not in ("authorization_code", "refresh_token")
                   for g in grants)):
        raise ValueError("invalid_client_metadata")
    if not isinstance(responses, list) or set(responses) != {"code"}:
        raise ValueError("invalid_client_metadata")

    name = str(payload.get("client_name") or "ChatGPT").strip()[:80] or "ChatGPT"
    client_id = _opaque(_CLIENT_PREFIX)
    ts = now()
    with db() as con:
        row = repo.insert_client(con, client_id, name, redirect_uris, ts)
    return {
        "client_id": row["client_id"],
        "client_id_issued_at": ts,
        "client_name": row["client_name"],
        "redirect_uris": row["redirect_uris"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def client(client_id: str):
    with db() as con:
        row = repo.client_by_id(con, str(client_id or ""))
    return dict(row) if row else None


def validate_authorization(client_id: str, redirect_uri: str, scope,
                           resource: str, code_challenge: str,
                           code_challenge_method: str) -> tuple[dict, str]:
    row = client(client_id)
    if not row:
        raise ValueError("invalid_client")
    if redirect_uri not in (row.get("redirect_uris") or []):
        raise ValueError("invalid_redirect_uri")
    if resource != OPENAI_MCP_RESOURCE:
        raise ValueError("invalid_target")
    if code_challenge_method != "S256" or not code_challenge:
        raise ValueError("invalid_request")
    return row, _scope(scope)


def create_authorization_code(user_id: int, client_id: str, redirect_uri: str,
                              code_challenge: str, scope: str,
                              resource: str) -> str:
    code = _opaque(_CODE_PREFIX)
    ts = now()
    with db() as con:
        repo.insert_code(
            con, token_hash(code), user_id, client_id, redirect_uri,
            code_challenge, scope, resource, ts + CODE_TTL, ts)
    return code


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not isinstance(verifier, str) or not _PKCE_RE.fullmatch(verifier):
        return False
    actual = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(actual, str(challenge or ""))


def _token_payload(access: str, refresh: str, scope: str) -> dict:
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


def exchange_authorization_code(code: str, client_id: str, redirect_uri: str,
                                verifier: str, resource: str) -> dict:
    if resource != OPENAI_MCP_RESOURCE:
        raise ValueError("invalid_target")
    access = _opaque(_ACCESS_PREFIX)
    refresh = _opaque(_REFRESH_PREFIX)
    ts = now()
    code_hash = token_hash(str(code or ""))
    with db() as con:
        row = repo.code_for_update(con, code_hash, ts)
        if not row:
            raise ValueError("invalid_grant")
        repo.delete_code(con, code_hash)
        if (row["client_id"] != client_id
                or row["redirect_uri"] != redirect_uri
                or row["resource"] != resource
                or not _pkce_ok(verifier, row["code_challenge"])):
            raise ValueError("invalid_grant")
        repo.insert_token(
            con, token_hash(access), token_hash(refresh), row["user_id"],
            client_id, row["scope"], resource, ts + ACCESS_TTL,
            ts + REFRESH_TTL, ts)
    return _token_payload(access, refresh, row["scope"])


def refresh_access_token(refresh_token: str, client_id: str,
                         resource: str) -> dict:
    if resource != OPENAI_MCP_RESOURCE:
        raise ValueError("invalid_target")
    access = _opaque(_ACCESS_PREFIX)
    refresh = _opaque(_REFRESH_PREFIX)
    ts = now()
    with db() as con:
        row = repo.refresh_for_update(
            con, token_hash(str(refresh_token or "")), client_id, resource, ts)
        if not row or row.get("user_status") == "suspended":
            raise ValueError("invalid_grant")
        repo.rotate_token(
            con, row["id"], token_hash(access), token_hash(refresh),
            ts + ACCESS_TTL, ts + REFRESH_TTL, ts)
    return _token_payload(access, refresh, row["scope"])


def authenticate_access_token(plaintext: str, resource: str):
    if not plaintext or not plaintext.startswith(_ACCESS_PREFIX):
        return None
    access_hash = token_hash(plaintext)
    ts = now()
    with db() as con:
        row = repo.user_by_access_token(con, access_hash, resource, ts)
        if not row:
            return None
        try:
            repo.touch_access_token(con, access_hash, ts)
        except Exception:
            pass
    user = dict(row)
    scopes = set(str(user.pop("oauth_scope", "")).split())
    return {"user": user, "scopes": scopes}
