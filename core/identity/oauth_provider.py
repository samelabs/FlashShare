"""OAuth 2.1 authorization server for the main /mcp surface.

Token format is opaque prefixed strings, hashed at rest — revocable,
no key management. PKCE S256 is mandatory (public clients). Client
identity is CIMD (Client ID Metadata Document, SEP-991): the client_id
IS an HTTPS URL; this server fetches the document on demand through the
pinned SSRF-guarded transport, verifies it self-references, and caches
it briefly. No registration endpoint, no client table — the URL is the
identifier.

Tokens are bound to the resource (RFC 8707): audience is exactly
BASE_URL/mcp, the one northbound machine surface.
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

from identity.capabilities import normalize_scope

ISSUER = BASE_URL
MCP_RESOURCE = f"{BASE_URL}/mcp"
SCOPES = ("tools:call", "files:read", "files:delete")
DEFAULT_SCOPE = " ".join(SCOPES)

CODE_TTL = 10 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60

_ACCESS_PREFIX = "fsot_"
_REFRESH_PREFIX = "fsor_"
_CODE_PREFIX = "fsoa_"
_PKCE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")

# CIMD fetch policy: small docs, short timeout, brief cache. Failures
# (fetch error / rejected document) are cached too, on a shorter TTL —
# /oauth endpoints are anonymous, an unreachable or invalid client must
# not be re-fetched on every request.
CIMD_MAX_BYTES = 64 * 1024
CIMD_TIMEOUT_S = 8
CIMD_CACHE_TTL = 300
CIMD_NEGATIVE_TTL = 60

_cimd_cache: dict = {}


def _cimd_negative(client_id: str) -> None:
    """Cache a failed fetch/validation briefly and report absence."""
    _cimd_cache[client_id] = (now() + CIMD_NEGATIVE_TTL, None)
    return None


def _opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _valid_cimd_url(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        p = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (not p.fragment and p.scheme == "https" and bool(p.netloc)
            and p.username is None and p.password is None)


def _valid_redirect_uri(value) -> bool:
    """CIMD redirect entries: https anywhere, or http on loopback (the
    standard local-client callback MCP clients use)."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        p = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if p.fragment or not p.scheme or not p.netloc:
        return False
    host = (p.hostname or "").lower()
    loopback = host in ("localhost", "127.0.0.1", "::1")
    return p.scheme == "https" or (p.scheme == "http" and loopback)


def cimd_fetch(client_id: str) -> dict | None:
    """Fetch + validate a Client ID Metadata Document. Returns
    {client_id, client_name, redirect_uris, logo_uri?} or None (None is
    cached for CIMD_NEGATIVE_TTL). The fetch goes through the pinned
    SSRF-guarded transport (https only, every resolved address public,
    connect pinned, redirects re-validated) — CIMD's headline risk is
    the outbound request."""
    if not _valid_cimd_url(client_id):
        return None
    hit = _cimd_cache.get(client_id)
    if hit and hit[0] > now():
        return hit[1]
    from remote import pinned_request
    try:
        status, body, _ctype, _cd = pinned_request(client_id, method="GET",
                                                   timeout_s=CIMD_TIMEOUT_S,
                                                   max_bytes=CIMD_MAX_BYTES)
    except Exception:
        return _cimd_negative(client_id)
    if status != 200 or not body:
        return _cimd_negative(client_id)
    import json as _json
    try:
        doc = _json.loads(body)
    except Exception:
        return _cimd_negative(client_id)
    if not isinstance(doc, dict):
        return _cimd_negative(client_id)
    # self-reference: the document MUST claim the exact URL it was
    # fetched from (the anti-impersonation rule)
    if doc.get("client_id") != client_id:
        return _cimd_negative(client_id)
    uris = doc.get("redirect_uris")
    if (not isinstance(uris, list) or not uris or len(uris) > 10
            or any(not _valid_redirect_uri(u) for u in uris)):
        return _cimd_negative(client_id)
    name = str(doc.get("client_name") or "").strip()[:80]
    if not name:
        return _cimd_negative(client_id)
    if doc.get("token_endpoint_auth_method", "none") != "none":
        return _cimd_negative(client_id)  # public clients only in this MVP
    logo = doc.get("logo_uri")
    out = {
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": list(dict.fromkeys(uris)),
    }
    if isinstance(logo, str) and logo.startswith("https://"):
        out["logo_uri"] = logo[:2048]
    _cimd_cache[client_id] = (now() + CIMD_CACHE_TTL, out)
    return out


def validate_authorization(client_id: str, redirect_uri: str, scope,
                           resource: str, code_challenge: str,
                           code_challenge_method: str) -> tuple:
    """Authorize-request validation: CIMD lookup + exact redirect match
    + resource binding + mandatory PKCE S256. Returns the verified
    client document (consent renders from it) and the canonical scope."""
    doc = cimd_fetch(str(client_id or ""))
    if not doc:
        raise ValueError("invalid_client")
    if redirect_uri not in doc["redirect_uris"]:
        raise ValueError("invalid_redirect_uri")
    if resource != MCP_RESOURCE:
        raise ValueError("invalid_target")
    if code_challenge_method != "S256" or not code_challenge:
        raise ValueError("invalid_request")
    return doc, normalize_scope(scope or DEFAULT_SCOPE)


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
    if resource != MCP_RESOURCE:
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
    if resource != MCP_RESOURCE:
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
    """Bearer validation for /mcp: returns {user, scopes, client_id} or
    None. Touches last-use telemetry (non-fatal)."""
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
    client_id = user.pop("oauth_client_id", None)
    return {"user": user, "scopes": scopes, "client_id": client_id}


def list_grants(user_id: int) -> list:
    """The user's live grants for the account page. Each item carries
    the client's hostname for display and the merged scope union of its
    live tokens (canonical order); the raw client_id travels only in
    the revoke request and is never rendered."""
    ts = now()
    with db() as con:
        rows = repo.active_grants(con, user_id, ts)
    out = []
    for row in rows:
        client_id = row["client_id"]
        try:
            host = urllib.parse.urlsplit(client_id).hostname or ""
        except ValueError:
            host = ""
        scopes = set()
        for granted in row["scopes"] or []:
            scopes.update(str(granted).split())
        ordered = [s for s in SCOPES if s in scopes]
        ordered += sorted(scopes.difference(SCOPES))
        out.append({
            "client_id": client_id,
            "client_host": host,
            "scope": " ".join(ordered),
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        })
    return out


def revoke_grant(user_id: int, client_id: str) -> bool:
    """Revoke the user's grant for one client. False when the user held
    no live grant from that client."""
    ts = now()
    with db() as con:
        return repo.revoke_client(con, user_id, client_id, ts) > 0
