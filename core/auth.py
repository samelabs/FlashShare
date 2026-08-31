import base64
import json
import re
import urllib.parse
import urllib.request
import urllib.error

from config import BASE_URL, X_CLIENT_ID, X_CLIENT_SECRET
from database import db
from repo import users as users_repo
from security import hash_password, verify_password, needs_rehash
from utils import now


# ── Validation ──────────────────────────────────────────

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_username(username: str) -> str | None:
    if not username:
        return "err_username_empty"
    if len(username) < 5:
        return "err_username_short"
    if len(username) > 32:
        return "err_username_long"
    if not USERNAME_RE.match(username):
        return "err_username_invalid"
    return None


def validate_email(email: str) -> str | None:
    if not email:
        return "err_email_empty"
    if len(email) > 254:
        return "err_email_invalid"
    if not EMAIL_RE.match(email):
        return "err_email_invalid"
    return None


def validate_password(password: str) -> str | None:
    if not password:
        return "err_password_empty"
    if len(password) < 8:
        return "err_password_short"
    if len(password) > 128:
        return "err_password_long"
    return None


# ── Local account ───────────────────────────────────────

def username_taken(username: str) -> bool:
    with db() as con:
        return users_repo.username_exists(con, username)


def register_user(username: str, email: str, password: str) -> int:
    """Create a new local account. Caller must validate inputs first."""
    ts = now()
    pw_hash = hash_password(password)
    with db() as con:
        row = users_repo.insert_local(con, username, email, pw_hash,
                                      username, ts)
        return row["id"]


def authenticate_local(login: str, password: str) -> int | None:
    """Authenticate by username or email. Returns user id or None."""
    with db() as con:
        row = users_repo.by_username_or_email(con, login)
    if not row or not row["password_hash"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    ts = now()
    # Auto-upgrade legacy SHA-256 hashes to bcrypt on successful login
    if needs_rehash(row["password_hash"]):
        new_hash = hash_password(password)
        with db() as con:
            users_repo.update_password_hash(con, row["id"], new_hash, ts)
    with db() as con:
        users_repo.touch_login(con, row["id"], ts)
    return row["id"]


# ── X OAuth ─────────────────────────────────────────────

def exchange_x_token(code: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "grant_type": "authorization_code",
        "client_id": X_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/auth/x/callback",
        "code_verifier": verifier,
    }).encode("utf-8")
    basic = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.x.com/2/oauth2/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"X token exchange failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"X token endpoint unreachable: {exc.reason}") from exc


def fetch_x_user(access_token: str) -> dict:
    req = urllib.request.Request(
        "https://api.x.com/2/users/me?user.fields=profile_image_url",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"X user lookup failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"X user endpoint unreachable: {exc.reason}") from exc
    return payload["data"]


def upsert_x_user(x_user: dict, bind_user_id: int | None = None) -> int:
    """Process X OAuth callback.

    Args:
        x_user: dict with id, name, username, profile_image_url from X API.
        bind_user_id: If set, bind X to this existing user instead of creating new.

    Returns user id.

    Raises ValueError if X account is linked to a different user.
    """
    ts = now()
    x_id = x_user["id"]
    x_username = x_user.get("username", "")
    x_display_name = x_user.get("name", "")
    x_avatar_url = x_user.get("profile_image_url", "")
    x_profile_url = f"https://x.com/{x_username}" if x_username else ""

    with db() as con:
        existing = users_repo.by_x_user_id(con, x_id)

        if existing:
            # X account already linked to a user
            if bind_user_id and existing["id"] != bind_user_id:
                raise ValueError("err_x_bound_other")
            user_id = existing["id"]
            users_repo.update_x_profile(con, user_id, x_username,
                                        x_display_name, x_avatar_url,
                                        x_profile_url, ts)
            return user_id

        if bind_user_id:
            # Bind X to existing local account
            row = users_repo.by_id(con, bind_user_id)
            if not row:
                raise ValueError("err_user_not_found")
            users_repo.bind_x(con, bind_user_id, x_id, x_username,
                              x_display_name, x_avatar_url, x_profile_url, ts)
            return bind_user_id

        # New user via X login — create account with X identity
        # X guarantees username ≤15 chars and numeric ids, but external API
        # data is not trusted: clamp to the 32-char column/validation limit.
        username = (x_username or "")[:32]
        if not username or len(username) < 5:
            username = f"x_{x_id}"[:32]
        # Ensure username uniqueness
        base_name = username
        suffix = 1
        while users_repo.username_exists(con, username):
            username = f"{base_name}_{suffix}"[:32]
            suffix += 1

        row = users_repo.insert_x_user(
            con, username, x_display_name or username, x_avatar_url,
            x_id, x_username, x_display_name, x_avatar_url, x_profile_url, ts)
        return row["id"]


def unbind_x(user_id: int) -> None:
    """Remove X binding from user. User must have local credentials."""
    with db() as con:
        row = users_repo.by_id(con, user_id)
        if not row:
            raise ValueError("err_user_not_found")
        if not row["password_hash"] or not row["email"]:
            raise ValueError("err_set_email_before_unbind")
        users_repo.unbind_x(con, user_id, now())


# ── Profile ─────────────────────────────────────────────

def update_profile(user_id: int, display_name: str | None = None,
                   bio: str | None = None, email: str | None = None,
                   payout_channel: str | None = None) -> None:
    ts = now()
    with db() as con:
        if display_name is not None:
            display_name = display_name.strip()[:64]
            if not display_name:
                raise ValueError("err_display_name_empty")
            users_repo.update_display_name(con, user_id, display_name, ts)
        if bio is not None:
            users_repo.update_bio(con, user_id, bio.strip()[:500] or None, ts)
        if payout_channel is not None:
            # withdrawal destination (operator reads it when paying out)
            users_repo.update_payout_channel(
                con, user_id, payout_channel.strip()[:120] or None, ts)
        if email is not None:
            email = email.strip().lower()[:254]
            if not email:
                raise ValueError("err_email_empty")
            if users_repo.email_taken_by_other(con, email, user_id):
                raise ValueError("err_email_used")
            users_repo.update_email(con, user_id, email, ts)


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    with db() as con:
        row = users_repo.by_id(con, user_id)
    if not row or not row["password_hash"]:
        raise ValueError("err_no_password_set")
    if not verify_password(old_password, row["password_hash"]):
        raise ValueError("err_old_password_wrong")
    err = validate_password(new_password)
    if err:
        raise ValueError(err)
    with db() as con:
        users_repo.update_password_hash(
            con, user_id, hash_password(new_password), now())


def set_password(user_id: int, new_password: str) -> None:
    """Set password for OAuth-only users who have no password."""
    with db() as con:
        row = users_repo.by_id(con, user_id)
    if row and row["password_hash"]:
        raise ValueError("err_password_already_set")
    err = validate_password(new_password)
    if err:
        raise ValueError(err)
    with db() as con:
        users_repo.update_password_hash(
            con, user_id, hash_password(new_password), now())


# ── Serialization ───────────────────────────────────────

def public_user(row) -> dict:
    data = {
        "username": row["username"],
        "email": row["email"],
        "displayName": row["display_name"],
        "avatarUrl": row["avatar_url"],
        "bio": row["bio"],
        "payoutChannel": row.get("payout_channel"),
        "hasPassword": bool(row["password_hash"]),
        "xUsername": row["x_username"],
        "xProfileUrl": row["x_profile_url"],
    }
    # Backward compat
    data["profileUrl"] = row["x_profile_url"]
    return data


def has_password(user_id: int) -> bool:
    with db() as con:
        row = users_repo.by_id(con, user_id)
    return bool(row and row["password_hash"])
