"""Activities module — configurable credit-grant campaigns (2026-08-25).

Replaces hardcoded SIGNUP_GRANT_CREDITS / DAILY_LOGIN_CREDITS with
data-driven activity rows managed from /samelabs/activities:

    code          unique key ('signup', 'daily_login', ...)
    activity_type 'once'  — one grant per user, ever
                  'daily' — one grant per user per UTC day
    amount        credits per grant
    active        operator switch (inactive → all grants skip)
    starts_at/ends_at  optional window (NULL = unbounded)
    idem_policy   derived from activity_type — see _idem_key()
    label_json    optional {lang: display} override for the account page

The ledger discipline is UNCHANGED: every grant is a billing-layer mutation
with idempotency (user_id + idem_key unique index). This module only moves
"how much / when / idempotency key" from code into data.

Migration contract: seed rows reproduce the historical constants exactly —
signup 100 (kind grant_signup), daily_login 5 (kind grant_daily) — so
existing ledger history keeps its meaning and both idem-key families
('signup:{uid}', 'daily:{uid}:{yyyymmdd}') stay collision-free.
"""

import json
import time

from database import db
from utils import now

ACTIVITY_TYPES = ("once", "daily")


def _seed_rows() -> list:
    ts = now()
    return [
        {
            "code": "signup",
            "activity_type": "once",
            "amount": 100,
            "kind": "grant_signup",
            "label_json": None,
            "starts_at": None,
            "ends_at": None,
        },
        {
            "code": "daily_login",
            "activity_type": "daily",
            "amount": 5,
            "kind": "grant_daily",
            "label_json": None,
            "starts_at": None,
            "ends_at": None,
        },
    ]


def ensure_schema_and_seed() -> dict:
    """CREATE TABLE IF NOT EXISTS + idempotent seed. Called at startup."""
    ts = now()
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
              id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              code          TEXT NOT NULL UNIQUE,
              activity_type TEXT NOT NULL,
              amount        BIGINT NOT NULL,
              kind          TEXT NOT NULL,
              active        BOOLEAN NOT NULL DEFAULT TRUE,
              starts_at     BIGINT,
              ends_at       BIGINT,
              label_json    TEXT,
              updated_at    BIGINT NOT NULL,
              updated_by    BIGINT
            )
            """
        )
        seeded = 0
        for row in _seed_rows():
            cur = con.execute(
                """
                INSERT INTO activities (code, activity_type, amount, kind,
                                        active, starts_at, ends_at, label_json,
                                        updated_at)
                VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (row["code"], row["activity_type"], row["amount"], row["kind"],
                 row["starts_at"], row["ends_at"], row["label_json"], ts),
            )
            seeded += cur.rowcount
    return {"seeded": seeded}


# ── Reads ──

def get_activity(code: str) -> dict | None:
    with db() as con:
        row = con.execute(
            "SELECT * FROM activities WHERE code = %s", (code,)
        ).fetchone()
    return dict(row) if row else None


def list_activities() -> list:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM activities ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def _in_window(act: dict) -> bool:
    ts = now()
    if act["starts_at"] and ts < act["starts_at"]:
        return False
    if act["ends_at"] and ts > act["ends_at"]:
        return False
    return True


def _idem_key(act: dict, user_id: int) -> str:
    """Idempotency key — derived from activity_type.

    The two SEEDED activities keep their historical billing.py key
    families byte-for-byte (ledger lineage):
      signup     → 'signup:{uid}'             (original family)
      daily_login→ 'daily:{uid}:{yyyymmdd}'   (original family)
    Any OTHER activity uses '{code}:{uid}'[:'{day}'] — a per-code family —
    so two once-type campaigns never collide on the same key (a shared
    'signup:{uid}' key would make the second campaign a permanent no-op)."""
    historical = act.get("code") in ("signup", "daily_login")
    if act["activity_type"] == "daily":
        day = time.strftime("%Y%m%d", time.gmtime())
        if historical:
            return f"daily:{user_id}:{day}"
        return f"{act['code']}:{user_id}:{day}"
    if historical:
        return f"signup:{user_id}"
    return f"{act['code']}:{user_id}"


# ── Grant entry (single point for every consumer) ──

def apply_activity(code: str, user_id: int) -> dict | None:
    """Apply an activity grant for a user. Returns the ledger row on
    success, None when skipped (inactive / out of window / already
    granted). Never raises past billing semantics — callers treat grants
    as best-effort, matching the legacy _grant_daily behavior.
    """
    act = get_activity(code)
    if not act or not act["active"] or not _in_window(act):
        return None
    idem = _idem_key(act, user_id)
    from billing import grant
    try:
        return grant(user_id, int(act["amount"]), act["kind"],
                     idem_key=idem, note=f"activity:{act['code']}")
    except Exception:
        return None


def granted_today(code: str, user_id: int) -> bool:
    """True when a 'daily' activity already landed this UTC day
    (account page chip state). 'once' activities: True after the grant."""
    act = get_activity(code)
    if not act:
        return False
    with db() as con:
        prior = con.execute(
            "SELECT 1 FROM ledger WHERE user_id = %s AND idem_key = %s LIMIT 1",
            (user_id, _idem_key(act, user_id)),
        ).fetchone()
    return prior is not None


# ── Operator mutations (/samelabs) ──

def set_activity(code: str, amount: int | None, active: bool | None,
                 starts_at: int | None, ends_at: int | None,
                 admin_user_id: int | None = None) -> dict:
    """Update an activity row. None fields are left untouched (partial
    update semantics, same UX as tool pricing). Clearing a window bound
    is expressed as 0 (NULL-ify), since JSON null vs absent is ambiguous
    for the operator UI."""
    act = get_activity(code)
    if not act:
        raise KeyError(code)
    if amount is not None and (amount < 0 or amount > 1000000):
        raise ValueError("amount out of range")
    if starts_at is not None and ends_at is not None and starts_at and ends_at \
            and starts_at > ends_at:
        raise ValueError("starts_at > ends_at")
    ts = now()
    with db() as con:
        row = con.execute(
            """
            UPDATE activities SET
              amount     = COALESCE(%s, amount),
              active     = COALESCE(%s, active),
              starts_at  = CASE WHEN %s THEN NULL ELSE COALESCE(%s, starts_at) END,
              ends_at    = CASE WHEN %s THEN NULL ELSE COALESCE(%s, ends_at) END,
              updated_at = %s,
              updated_by = %s
            WHERE code = %s
            RETURNING *
            """,
            (amount, active,
             starts_at == 0, starts_at, ends_at == 0, ends_at,
             ts, admin_user_id, code),
        ).fetchone()
    return dict(row)


def activity_label(act: dict, lang: str = "zh") -> str:
    """Display label: label_json override → builtin fallback per code."""
    if act.get("label_json"):
        try:
            labels = json.loads(act["label_json"])
            if labels.get(lang):
                return labels[lang]
            if labels.get("en"):
                return labels["en"]
        except Exception:
            pass
    builtin = {
        "signup": {"zh": "注册赠送", "en": "Signup bonus"},
        "daily_login": {"zh": "每日登录", "en": "Daily login"},
    }
    return builtin.get(act["code"], {}).get(lang) or act["code"]
