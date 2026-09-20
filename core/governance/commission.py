"""governance/commission — 平台佣金率 (批2e 从路由/gate 上收为治理参数)."""

from repo.orders import setting

DEFAULT_COMMISSION_RATE = 30


def commission_rate(con) -> int:
    """Platform commission percent (0-100) from settings; default 30.

    Reads within the CALLER's transaction (claim-time snapshot: the
    config read and the tool_call INSERT share one DB transaction).
    Fallback/clamp semantics: missing → 30, invalid → 30,
    valid → clamp 0..100.
    """
    row = setting(con, "commission_rate")
    try:
        return max(0, min(100, int(row["value"]))) if row else DEFAULT_COMMISSION_RATE
    except (ValueError, TypeError):
        return DEFAULT_COMMISSION_RATE
