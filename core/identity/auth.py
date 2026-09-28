import base64
import json
import re
import secrets
import urllib.parse
import urllib.request
import urllib.error

from config import BASE_URL, X_CLIENT_ID, X_CLIENT_SECRET
from database import db
import psycopg2.errors as _pgerr

_UniqueViolation = _pgerr.UniqueViolation

from repo import users as users_repo
from repo import sessions as sessions_repo
from security import hash_password, verify_password
from utils import now


# ── Validation ──────────────────────────────────────────

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Platform-identity namespace: registration and X auto-naming never take
# these handles (case-insensitive — usernames are unique per folded name).
# The platform account bootstrap (governance.platform) inserts directly
# and does not pass through this vocabulary.
RESERVED_USERNAMES = frozenset({
    "platform", "admin", "administrator", "samelabs", "system",
    "support", "official", "518plus", "security", "staff",
    "operator", "moderator",
})


def validate_username(username: str) -> str | None:
    if not username:
        return "err_username_empty"
    if len(username) < 5:
        return "err_username_short"
    if len(username) > 32:
        return "err_username_long"
    if not USERNAME_RE.match(username):
        return "err_username_invalid"
    if username.lower() in RESERVED_USERNAMES:
        return "err_username_reserved"
    return None


def normalize_email(email: str | None) -> str:
    """Email identity is case-insensitive: every write and lookup uses
    the stripped, lowercased form."""
    return (email or "").strip().lower()


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
    """Create a new local account. Caller must validate inputs first.

    Raises ValueError("err_email_taken") when the normalized email is
    already held (case-insensitive, covers pre-normalization rows).
    """
    ts = now()
    email = normalize_email(email)
    pw_hash = hash_password(password)
    with db() as con:
        if users_repo.email_taken_by_other(con, email, 0):
            raise ValueError("err_email_taken")
        try:
            row = users_repo.insert_local(con, username, email, pw_hash,
                                          username, ts)
        except _UniqueViolation as exc:
            # lost the race against a folded unique index (username or
            # email): converges to the same typed contract, never a 500
            constraint = str(getattr(
                getattr(exc, "diag", None), "constraint_name", "") or "")
            if "username" in constraint:
                raise ValueError("err_username_taken") from exc
            raise ValueError("err_email_taken") from exc
        return row["id"]


def authenticate_local(login: str, password: str) -> int | None:
    """Authenticate by username/email. Suspended accounts never authenticate."""
    with db() as con:
        row = users_repo.by_username_or_email(con, login, normalize_email(login))
    if not row or row.get("status") == "suspended" or not row["password_hash"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    with db() as con:
        users_repo.touch_login(con, row["id"], now())
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
    """Process X OAuth callback; suspended identities cannot login or bind.

    Avatar authority = users.avatar_url (single field): signup writes the
    X image verbatim (may be empty); login/bind refresh overwrites only
    with a fresh non-empty image and never clears on empty.
    """
    ts = now()
    x_id = x_user["id"]
    x_username = x_user.get("username", "")
    x_display_name = x_user.get("name", "")
    avatar_url = x_user.get("profile_image_url", "")
    x_profile_url = f"https://x.com/{x_username}" if x_username else ""

    with db() as con:
        existing = users_repo.by_x_user_id(con, x_id)

        if existing:
            if existing.get("status") == "suspended":
                raise ValueError("err_user_suspended")
            if bind_user_id and existing["id"] != bind_user_id:
                raise ValueError("err_x_bound_other")
            user_id = existing["id"]
            users_repo.update_x_profile(con, user_id, x_username,
                                        x_display_name, avatar_url,
                                        x_profile_url, ts)
            return user_id

        if bind_user_id:
            row = users_repo.by_id(con, bind_user_id)
            if not row:
                raise ValueError("err_user_not_found")
            if row.get("status") == "suspended":
                raise ValueError("err_user_suspended")
            try:
                users_repo.bind_x(con, bind_user_id, x_id, x_username,
                                  x_display_name, avatar_url,
                                  x_profile_url, ts)
            except _UniqueViolation:
                # two users raced to bind the same X identity: the loser
                # hits the x_user_id UNIQUE constraint — same typed
                # contract as the sequential check, never a 500.
                winner = users_repo.by_x_user_id(con, x_id)
                if winner is not None and winner["id"] != bind_user_id:
                    raise ValueError("err_x_bound_other")
                raise
            return bind_user_id

        username = (x_username or "")[:32]
        if not username or len(username) < 5:
            username = f"x_{x_id}"[:32]
        if username.lower() in RESERVED_USERNAMES:
            # a reserved handle must not become an account namespace —
            # fall back to the neutral X-identity name
            username = f"x_{x_id}"[:32]
        base_name = username
        suffix = 1
        while users_repo.username_exists(con, username):
            username = f"{base_name}_{suffix}"[:32]
            suffix += 1

        try:
            row = users_repo.insert_x_user(
                con, username, x_display_name or username, avatar_url,
                x_id, x_username, x_display_name, x_profile_url, ts)
            return row["id"]
        except _UniqueViolation:
            # two concurrent first logins of the SAME X identity: the
            # second INSERT loses the x_user_id UNIQUE race — converge
            # to the existing account instead of failing the login.
            existing = users_repo.by_x_user_id(con, x_id)
            if existing is not None:
                return existing["id"]
            raise


def unbind_x(user_id: int) -> None:
    """Remove X binding from user. User must have local credentials."""
    with db() as con:
        row = users_repo.by_id(con, user_id)
        if not row:
            raise ValueError("err_user_not_found")
        if row.get("status") == "suspended":
            raise ValueError("err_user_suspended")
        if not row["password_hash"] or not row["email"]:
            raise ValueError("err_set_email_before_unbind")
        users_repo.unbind_x(con, user_id, now())


# ── Profile ─────────────────────────────────────────────

def update_profile(user_id: int, display_name: str | None = None,
                   bio: str | None = None, email: str | None = None,
                   payout_channel: str | None = None,
                   current_password: str | None = None) -> None:
    """Changing the email of an account that has a password requires the
    current password (X-only accounts without a password are exempt)."""
    ts = now()
    with db() as con:
        if display_name is not None:
            display_name = display_name.strip()[:64]
            if not display_name:
                raise ValueError("err_display_name_empty")
            users_repo.update_display_name(con, user_id, display_name, ts)
        if bio is not None:
            # "" clears the field (empty = cleared, not skipped)
            users_repo.update_bio(con, user_id, bio.strip()[:500], ts)
        if payout_channel is not None:
            users_repo.update_payout_channel(
                con, user_id, payout_channel.strip()[:120], ts)
        if email is not None:
            # Same contract as registration: one validator, no silent
            # truncation — validate_email owns length and shape rules.
            email = normalize_email(email)
            err = validate_email(email)
            if err:
                raise ValueError(err)
            row = users_repo.by_id(con, user_id)
            if not row:
                raise ValueError("err_user_not_found")
            if normalize_email(row["email"]) != email:
                if row["password_hash"]:
                    if not current_password:
                        raise ValueError("err_current_password_required")
                    if not verify_password(current_password, row["password_hash"]):
                        raise ValueError("err_old_password_wrong")
                if users_repo.email_taken_by_other(con, email, user_id):
                    raise ValueError("err_email_used")
            try:
                users_repo.update_email(con, user_id, email, ts)
            except _UniqueViolation:
                raise ValueError("err_email_used")


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    """Rotate the password under a compare-and-set: the UPDATE only lands
    while the stored hash is still the one this call verified, so two
    concurrent changes from the same old password cannot overwrite each
    other — the loser gets the typed old-password error. Sessions are
    cleared only when the write actually happened."""
    with db() as con:
        row = users_repo.by_id(con, user_id)
    if not row or not row["password_hash"]:
        raise ValueError("err_no_password_set")
    old_hash = row["password_hash"]
    if not verify_password(old_password, old_hash):
        raise ValueError("err_old_password_wrong")
    err = validate_password(new_password)
    if err:
        raise ValueError(err)
    new_hash = hash_password(new_password)
    with db() as con:
        if users_repo.update_password_hash_if_current(
                con, user_id, new_hash, old_hash, now()):
            sessions_repo.delete_user_sessions(con, user_id)
        else:
            # lost the race: the hash moved after this call verified it
            raise ValueError("err_old_password_wrong")


def set_password(user_id: int, new_password: str) -> None:
    """First-ever password set (OAuth-only account) under a compare-and-
    set: only the first concurrent setter wins; the loser gets the typed
    already-set error instead of silently overwriting. Sessions are
    cleared only when the write actually happened."""
    err = validate_password(new_password)
    if err:
        raise ValueError(err)
    with db() as con:
        if users_repo.update_password_hash_if_unset(
                con, user_id, hash_password(new_password), now()):
            sessions_repo.delete_user_sessions(con, user_id)
        else:
            raise ValueError("err_password_already_set")


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
    data["profileUrl"] = row["x_profile_url"]
    return data


def has_password(user_id: int) -> bool:
    with db() as con:
        row = users_repo.by_id(con, user_id)
    return bool(row and row["password_hash"])
