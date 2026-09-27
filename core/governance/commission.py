"""governance/commission — 平台佣金率 (the execution-domain split 从路由/gate 上收为治理参数).

Fail-closed: the rate is REAL config, not a guess. Missing,
non-integer, or out-of-range → None — the caller (metering gate) must
refuse the call BEFORE charging. No fallback, no clamping: a wrong
commission silently changes the developer/platform split, so an
unprovable rate must never settle a payout.
"""

from repo.orders import setting

RATE_KEY = "commission_rate"
BOOTSTRAP_RATE = 30


def commission_rate(con) -> int | None:
    """Platform commission percent (0..100) from settings, or None when
    unconfigured/illegal (fail-closed — the metering gate refuses the
    call before consume; system tools never read this for settlement).

    Reads within the CALLER's transaction (claim-time snapshot: the
    config read and the tool_call INSERT share one DB transaction).
    """
    row = setting(con, RATE_KEY)
    if row is None:
        return None
    raw = row["value"]
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        rate = int(raw.strip())
    except (ValueError, TypeError):
        return None
    return rate if 0 <= rate <= 100 else None


def set_commission_rate(con, rate: int, ts: int, updated_by: int | None) -> dict:
    """Operator write: 0..100 integer only. The caller owns validation
    ordering (this is the single settings write for the rate) and the
    operator_audit record."""
    if not isinstance(rate, int) or isinstance(rate, bool) \
            or not 0 <= rate <= 100:
        raise ValueError("invalid_commission_rate")
    return con.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (key) DO UPDATE
          SET value = excluded.value, updated_at = excluded.updated_at,
              updated_by = excluded.updated_by
        RETURNING *
        """,
        (RATE_KEY, str(rate), ts, updated_by)).fetchone()
