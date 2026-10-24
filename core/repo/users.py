"""repo: users — 用户表数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
调用方: core/identity 域服务 (现 identity.auth.py/identity.apikeys.py/earn.py)。
"""

from typing import Any, Optional


def by_id(con, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()


def by_username_or_email(con, login: str, email: str) -> Optional[dict]:
    """username identity is case-insensitive (one handle per folded name,
    users_username_lower_uk); email is the normalized (lowercase) form."""
    return con.execute(
        "SELECT id, password_hash, status FROM users"
        " WHERE lower(username) = lower(%s) OR lower(email) = %s",
        (login, email)).fetchone()


def by_x_user_id(con, x_id: str) -> Optional[dict]:
    return con.execute(
        "SELECT id, status FROM users WHERE x_user_id = %s", (x_id,)).fetchone()


def username_exists(con, username: str) -> bool:
    """Folded (case-insensitive) handle check — one identity per
    lower(username), backed by users_username_lower_uk."""
    return con.execute(
        "SELECT 1 FROM users WHERE lower(username) = lower(%s)",
        (username,)).fetchone() is not None


def email_taken_by_other(con, email: str, user_id: int) -> bool:
    """email is the normalized (lowercase) form; case-insensitive match."""
    return con.execute(
        "SELECT 1 FROM users WHERE lower(email) = %s AND id != %s",
        (email, user_id)).fetchone() is not None


def insert_local(con, username: str, email: str, pw_hash: str,
                 display_name: str, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO users (username, email, password_hash, display_name,
                           status, created_at, last_login_at, updated_at)
        VALUES (%s, %s, %s, %s, 'active', %s, %s, %s)
        RETURNING id
        """,
        (username, email, pw_hash, display_name, ts, ts, ts)).fetchone()


def insert_x_user(con, username: str, display_name: str, avatar_url: str,
                  x_id: str, x_username: str, x_display_name: str,
                  x_profile_url: str, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO users
          (username, display_name, avatar_url,
           x_user_id, x_username, x_display_name, x_profile_url,
           status, created_at, last_login_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
        RETURNING id
        """,
        (username, display_name, avatar_url,
         x_id, x_username, x_display_name, x_profile_url,
         ts, ts, ts)).fetchone()


def update_x_profile(con, user_id: int, x_username: str, x_display_name: str,
                     avatar_url: str, x_profile_url: str, ts: int) -> None:
    """X identity refresh on login/profile sync.

    avatar_url is THE avatar authority: a fresh non-empty X image overwrites
    it; an empty X image never clears the current value.
    """
    con.execute(
        """
        UPDATE users SET
            x_username = %s, x_display_name = %s, x_profile_url = %s,
            avatar_url = COALESCE(NULLIF(%s, ''), avatar_url),
            last_login_at = %s, updated_at = %s
        WHERE id = %s
        """,
        (x_username, x_display_name, x_profile_url,
         avatar_url, ts, ts, user_id))


def bind_x(con, user_id: int, x_id: str, x_username: str, x_display_name: str,
           avatar_url: str, x_profile_url: str, ts: int) -> None:
    """Bind X to an existing account: same avatar rule as refresh — a fresh
    non-empty X image overwrites, an empty one keeps the current avatar."""
    con.execute(
        """
        UPDATE users SET
            x_user_id = %s, x_username = %s, x_display_name = %s,
            x_profile_url = %s,
            avatar_url = COALESCE(NULLIF(%s, ''), avatar_url),
            updated_at = %s
        WHERE id = %s
        """,
        (x_id, x_username, x_display_name,
         x_profile_url, avatar_url,
         ts, user_id))


def unbind_x(con, user_id: int, ts: int) -> None:
    # X identity AND the avatar it provided leave together; the UI falls
    # back to initials.
    con.execute(
        """
        UPDATE users SET
            x_user_id = NULL, x_username = NULL, x_display_name = NULL,
            avatar_url = NULL, x_profile_url = NULL,
            updated_at = %s
        WHERE id = %s
        """,
        (ts, user_id))


def update_password_hash(con, user_id: int, pw_hash: str, ts: int) -> None:
    """Unconditional password overwrite — operator reset surface only
    (an operator reset is an authority action and must clobber).
    Self-service change/set use the compare-and-set variants below."""
    con.execute(
        "UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s",
        (pw_hash, ts, user_id))


def update_password_hash_if_current(con, user_id: int, pw_hash: str,
                                    old_hash: str, ts: int) -> int:
    """CAS write for change_password: only the transaction whose verified
    old hash still matches wins; a concurrent writer makes this affect
    0 rows instead of being blindly overwritten."""
    return con.execute(
        "UPDATE users SET password_hash = %s, updated_at = %s"
        " WHERE id = %s AND password_hash = %s",
        (pw_hash, ts, user_id, old_hash)).rowcount


def update_password_hash_if_unset(con, user_id: int, pw_hash: str,
                                  ts: int) -> int:
    """CAS write for set_password: only the first set of a previously
    NULL hash wins; a concurrent setter makes this affect 0 rows."""
    return con.execute(
        "UPDATE users SET password_hash = %s, updated_at = %s"
        " WHERE id = %s AND password_hash IS NULL",
        (pw_hash, ts, user_id)).rowcount


def set_status(con, user_id: int, status: str, ts: int) -> dict:
    """Account status write (operator suspension surface). The identity
    chain reads status live: 'suspended' blocks login, sessions, API
    keys and (via catalog.resolve) tool visibility."""
    return con.execute(
        "UPDATE users SET status = %s, updated_at = %s WHERE id = %s"
        " RETURNING *",
        (status, ts, user_id)).fetchone()


def touch_login(con, user_id: int, ts: int) -> None:
    con.execute(
        "UPDATE users SET last_login_at = %s, updated_at = %s WHERE id = %s",
        (ts, ts, user_id))


def update_display_name(con, user_id: int, display_name: str, ts: int) -> None:
    con.execute(
        "UPDATE users SET display_name = %s, updated_at = %s WHERE id = %s",
        (display_name, ts, user_id))


def update_bio(con, user_id: int, bio: Any, ts: int) -> None:
    con.execute(
        "UPDATE users SET bio = %s, updated_at = %s WHERE id = %s",
        (bio, ts, user_id))


def update_payout_channel(con, user_id: int, channel: Any, ts: int) -> None:
    con.execute(
        "UPDATE users SET payout_channel = %s, updated_at = %s WHERE id = %s",
        (channel, ts, user_id))


def update_email(con, user_id: int, email: str, ts: int) -> None:
    con.execute(
        "UPDATE users SET email = %s, updated_at = %s WHERE id = %s",
        (email, ts, user_id))


def payout_channel_of(con, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT payout_channel FROM users WHERE id = %s", (user_id,)).fetchone()


def username_of(con, user_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT username, display_name FROM users WHERE id = %s",
        (user_id,)).fetchone()


def id_by_username(con, username: str) -> Optional[dict]:
    """用户页路由/platform lookup: username → {id, status, username,
    display_name} (canonical stored handle for display; display_name is
    public-by-design — the same field the header identity shows; no
    other columns leak).
    username 身份按折叠名匹配 (与注册判重同一语义)。"""
    return con.execute(
        "SELECT id, status, username, display_name FROM users"
        " WHERE lower(username) = lower(%s)",
        (username,)).fetchone()
