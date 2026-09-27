"""Operator user commands : password reset, provider suspension.

Both verbs are audited and protect the platform row and backoffice
accounts. Password reset NEVER records the credential itself; suspension
drives login/key/catalog surface enforcement through the single status
field ('suspended') the identity chain already reads.
"""

import secrets
import string

from database import db
from repo import operator_audit, users as users_repo, sessions as sessions_repo
from utils import now

PLATFORM_USERNAME = "platform"
_TMP_LEN = 16
_TMP_ALPHABET = string.ascii_letters + string.digits


def _protected(row: dict) -> bool:
    """Platform row and backoffice accounts can neither be reset nor
    suspended: the platform is the bookkeeping subject, and suspending
    an operator would lock the control plane itself."""
    from app.routes.samelabs.common import BACKOFFICE_STATUSES
    return row["username"] == PLATFORM_USERNAME \
        or row["status"] in BACKOFFICE_STATUSES


def reset_user_password(operator_id: int, user_id: int) -> tuple[int, str]:
    """Generate a one-time temporary password, hash it onto the account,
    and revoke every browser session. API keys survive (independent
    program credentials). Returns (user_id, temp_password) — shown to
    the operator ONCE, never stored or audited in cleartext."""
    from identity.auth import hash_password
    ts = now()
    temp = "".join(secrets.choice(_TMP_ALPHABET) for _ in range(_TMP_LEN))
    with db() as con:
        row = users_repo.by_id(con, user_id)
        if row is None:
            raise KeyError("not_found")
        if _protected(row):
            raise ValueError("user_protected")
        before = {"status": row["status"],
                  "pw_present": bool(row["password_hash"])}
        users_repo.update_password_hash(
            con, user_id, hash_password(temp), ts)
        sessions_repo.delete_user_sessions(con, user_id)
        operator_audit.record(
            con, operator_id, "user.password.reset", "user", str(user_id),
            before, {**before, "pw_reset": True,
                     "logins_revoked": True}, ts)
    return user_id, temp


def set_user_suspended(operator_id: int, user_id: int,
                       suspended: bool) -> dict:
    """Provider suspension toggle. Suspended users cannot log in (local +
    X), their API keys stop authenticating, and resolve()/catalog hide
    their tools from every surface; new calls are refused before any
    charge. Completed-call earnings are never touched. Restore returns
    everything to normal (no re-grant needed — facts never moved)."""
    ts = now()
    target_status = "suspended" if suspended else "active"
    with db() as con:
        row = users_repo.by_id(con, user_id)
        if row is None:
            raise KeyError("not_found")
        if _protected(row):
            raise ValueError("user_protected")
        if row["status"] == target_status:
            return dict(row)   # replay: no-op success
        before = {"status": row["status"]}
        out = users_repo.set_status(con, user_id, target_status, ts)
        if suspended:
            # kill live browser sessions at the same instant
            sessions_repo.delete_user_sessions(con, user_id)
        operator_audit.record(
            con, operator_id,
            "user.suspend" if suspended else "user.unsuspend",
            "user", str(user_id), before,
            {"status": target_status}, ts)
    return dict(out)
