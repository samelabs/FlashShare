"""governance/commission — 平台佣金率 (批2e 从路由/gate 上收为治理参数)."""

from database import db
from repo.orders import setting

DEFAULT_COMMISSION_RATE = 30


def commission_rate() -> int:
    """Platform commission percent (0-100) from settings; default 30."""
    with db() as con:
        row = setting(con, "commission_rate")
    try:
        return max(0, min(100, int(row["value"]))) if row else DEFAULT_COMMISSION_RATE
    except (ValueError, TypeError):
        return DEFAULT_COMMISSION_RATE
