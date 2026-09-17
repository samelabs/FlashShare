"""Credits service — the economics core (batch B).

Single-track credits, never expire. The ledger is append-only: every
mutation is a new row; balance is always derivable as SUM(delta). The
authoritative in-transaction balance is carried on each row
(balance_after) so auditing a single row tells you the state at that
moment.

Idempotency: every mutation accepts an idem_key; a unique partial index
on (user_id, idem_key) makes replays a no-op that returns the ORIGINAL
result instead of double-charging.

All functions open their own transaction (with db() as con:) and use
SELECT ... FOR UPDATE on the user row to serialize concurrent mutations
per user. Cross-user concurrency is unaffected.
"""

from database import db
from repo import ledger as ledger_repo
from utils import now


class InsufficientCredits(Exception):
    """Raised when a consume would drive the balance negative."""

    def __init__(self, balance: int, cost: int):
        super().__init__("err_insufficient_credits")
        self.balance = balance
        self.cost = cost


def get_balance(user_id: int) -> int:
    """Current balance = sum of all ledger deltas. 0 for fresh users."""
    with db() as con:
        return ledger_repo.total_balance(con, user_id)


# The spendable pool: platform-internal consumption credits. Every kind
# this service writes (grant/consume/refund/adjust) belongs to exactly one
# journal pool — spendable/credit. Provider earnings and withdrawals are
# CASH movements (provider_earning/usd_cent) written by economy.earnings /
# economy.settlement, never by this module.
SPENDABLE_POOL = "spendable"
CREDIT_UNIT = "credit"


def spendable_balance(user_id: int) -> int:
    """Credits the user may spend (the spendable pool only)."""
    with db() as con:
        return ledger_repo.pool_balance(con, user_id, SPENDABLE_POOL)


def _balance_for_update(con, user_id: int) -> int:
    """Serialize on the users row; returns the SPENDABLE balance
    (consume/refund/adjust only ever touch the spendable pool)."""
    ledger_repo.lock_user(con, user_id)
    return ledger_repo.pool_balance(con, user_id, SPENDABLE_POOL)


def grant(user_id: int, amount: int, kind: str, *,
          ref_type: str | None = None, ref_id: str | None = None,
          idem_key: str | None = None, note: str | None = None) -> dict:
    """Add credits. Replays (same user_id+idem_key) return the original row.

    kind: grant_signup | grant_plan | grant_package | grant_referral | adjust
    """
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    ts = now()
    with db() as con:
        # 幂等检查在行锁之后(批2a 修复): 并发同 idem_key 的后到者在
        # _balance_for_update 的 users 行锁上排队, 先到者提交后才读到
        # prior → 干净重放; 不再有"检查过→插入撞唯一索引"的 500 窗口。
        bal = _balance_for_update(con, user_id)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        row = ledger_repo.insert_row(con, user_id, SPENDABLE_POOL,
                                     CREDIT_UNIT, amount, bal + amount,
                                     kind, ref_type, ref_id, idem_key, note, ts)
    return dict(row)


def consume(user_id: int, cost: int, kind: str, *,
            ref_type: str | None = None, ref_id: str | None = None,
            idem_key: str | None = None, note: str | None = None) -> dict:
    """Deduct credits. Raises InsufficientCredits if balance < cost.

    kind: consume | adjust (negative adjustments go through consume)
    Replays with the same idem_key return the original row (no double charge).
    """
    if cost <= 0:
        raise ValueError("consume cost must be positive")
    ts = now()
    with db() as con:
        bal = _balance_for_update(con, user_id)  # 行锁先行(幂等检查锁内, 批2a)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        if bal < cost:
            raise InsufficientCredits(bal, cost)
        row = ledger_repo.insert_row(con, user_id, SPENDABLE_POOL,
                                     CREDIT_UNIT, -cost, bal - cost,
                                     kind, ref_type, ref_id, idem_key, note, ts)
    return dict(row)


def refund(user_id: int, amount: int, *,
           ref_type: str | None = None, ref_id: str | None = None,
           idem_key: str | None = None, note: str | None = None) -> dict:
    """Refund a prior charge — a NEW ledger row (delta > 0), never an UPDATE.

    Movement kind is fixed to 'refund' (spendable pool).
    """
    if amount <= 0:
        raise ValueError("refund amount must be positive")
    ts = now()
    with db() as con:
        bal = _balance_for_update(con, user_id)  # 行锁先行(幂等检查锁内, 批2a)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        row = ledger_repo.insert_row(con, user_id, SPENDABLE_POOL,
                                     CREDIT_UNIT, amount, bal + amount,
                                     "refund", ref_type, ref_id,
                                     idem_key, note, ts)
    return dict(row)


def lazy_signup_grant(con, user_id: int, amount: int) -> dict | None:
    """REMOVED 2026-08-27: signup bonus decoupled from registration/billing."""
    return None


def history_paged(user_id: int, page: int = 1, size: int = 20) -> tuple[list, int]:
    """(rows_newest_first, total_count) for the account credits page.

    Spendable-pool flow only — grants/topups/consumes/refunds/adjusts.
    Cash movements (provider_earning/withdraw) live on the earnings page.
    """
    with db() as con:
        rows, total = ledger_repo.history_paged(con, user_id, SPENDABLE_POOL,
                                                size, max(0, (page - 1) * size))
    return rows, total
