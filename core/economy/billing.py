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


# Ledger kinds that make up the SPENDABLE pool. provider_earn is NOT here:
# earnings accumulate in their own pool, withdrawable only. This is the
# dual-pool invariant (2026-08-26 global audit): consumption must never
# eat into provider earnings. grant_plan_upgrade is the Plus→Pro mid-cycle
# top-up difference — spendable exactly like the base grant_plan, or an
# upgraded member would see +400 yet be unable to spend it.
SPENDABLE_KINDS = ("grant_signup", "grant_daily", "signup_bonus", "daily",
                   "bonus", "topup", "consume", "refund", "adjust", "grant",
                   "revoke", "manual_topup", "grant_plan", "grant_package",
                   "grant_plan_upgrade")

# The other two pools' kind vocabularies live beside SPENDABLE_KINDS —
# the ONE place the pool split is defined. Earn pool: withdrawable only
# (provider_earn positive, withdraw negative). Platform pool: the
# platform account's own income legs.
EARN_POOL_KINDS = ("provider_earn", "withdraw")
PLATFORM_POOL_KINDS = ("platform_commission", "platform_revenue")


def spendable_balance(user_id: int) -> int:
    """Credits the user may spend (excludes provider_earn & withdraw rows)."""
    with db() as con:
        return ledger_repo.pool_running_balance(con, user_id, SPENDABLE_KINDS)


def _balance_for_update(con, user_id: int) -> int:
    """Serialize on the users row; returns the SPENDABLE balance
    (dual-pool: consume/refund/adjust only ever touch the spendable pool)."""
    ledger_repo.lock_user(con, user_id)
    return ledger_repo.pool_running_balance(con, user_id, SPENDABLE_KINDS)


def grant(user_id: int, amount: int, kind: str, *,
          ref_type: str | None = None, ref_id: str | None = None,
          idem_key: str | None = None, note: str | None = None,
          con=None) -> dict:
    """Add credits. Replays (same user_id+idem_key) return the original row.

    kind: grant_signup | grant_plan | grant_package | grant_referral | adjust
    con 未传: 自开事务 (现有调用方行为完全不变); con 已传: 参与 caller
    事务 (operator audit 与 ledger movement 同事务的窄扩展)。
    """
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    ts = now()
    if con is not None:
        bal = _balance_for_update(con, user_id)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        return dict(ledger_repo.insert_row(
            con, user_id, amount, bal + amount,
            kind, ref_type, ref_id, idem_key, note, ts))
    with db() as con:
        # 幂等检查在行锁之后(idempotency-under-lock fix): 并发同 idem_key 的后到者在
        # _balance_for_update 的 users 行锁上排队, 先到者提交后才读到
        # prior → 干净重放; 不再有"检查过→插入撞唯一索引"的 500 窗口。
        bal = _balance_for_update(con, user_id)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        row = ledger_repo.insert_row(con, user_id, amount, bal + amount,
                                     kind, ref_type, ref_id, idem_key, note, ts)
    return dict(row)


def consume(user_id: int, cost: int, kind: str, *,
            ref_type: str | None = None, ref_id: str | None = None,
            idem_key: str | None = None, note: str | None = None,
            con=None) -> dict:
    """Deduct credits. Raises InsufficientCredits if balance < cost.

    kind: consume | adjust (negative adjustments go through consume)
    Replays with the same idem_key return the original row (no double charge).
    """
    if cost <= 0:
        raise ValueError("consume cost must be positive")
    ts = now()
    if con is not None:
        bal = _balance_for_update(con, user_id)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        if bal < cost:
            raise InsufficientCredits(bal, cost)
        return dict(ledger_repo.insert_row(
            con, user_id, -cost, bal - cost,
            kind, ref_type, ref_id, idem_key, note, ts))
    with db() as con:
        bal = _balance_for_update(con, user_id)  # 行锁先行(幂等检查锁内, the billing lock-order fix)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        if bal < cost:
            raise InsufficientCredits(bal, cost)
        row = ledger_repo.insert_row(con, user_id, -cost, bal - cost,
                                     kind, ref_type, ref_id, idem_key, note, ts)
    return dict(row)


def refund(user_id: int, amount: int, *,
           ref_type: str | None = None, ref_id: str | None = None,
           idem_key: str | None = None, note: str | None = None) -> dict:
    """Refund a prior charge — a NEW ledger row (delta > 0), never an UPDATE.

    kind is fixed to 'refund'. idem_key semantics identical to grant().
    """
    if amount <= 0:
        raise ValueError("refund amount must be positive")
    ts = now()
    with db() as con:
        bal = _balance_for_update(con, user_id)  # 行锁先行(幂等检查锁内, the billing lock-order fix)
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        row = ledger_repo.insert_row(con, user_id, amount, bal + amount,
                                     "refund", ref_type, ref_id,
                                     idem_key, note, ts)
    return dict(row)


def revoke(user_id: int, amount: int, *, ref_type: str | None = None,
           ref_id: str | None = None, idem_key: str | None = None,
           note: str | None = None) -> dict:
    """Economic reversal of a prior payment grant — insert a NEGATIVE
    delta with NO insufficient-balance check: a refund/chargeback must
    revoke the exact granted amount even when the user already spent
    it (balance_after < 0 is the natural debt outcome; no debt table).
    kind is fixed to 'revoke'. Replays (same user_id+idem_key) return
    the original row. The SINGLE payment-reversal writer — nothing
    else may insert negative payment-reversal movements.
    """
    if amount <= 0:
        raise ValueError("revoke amount must be positive")
    ts = now()
    with db() as con:
        bal = _balance_for_update(con, user_id)  # users 行锁 = 串行化边界
        if idem_key:
            prior = ledger_repo.by_idem_key(con, user_id, idem_key)
            if prior:
                return dict(prior)
        row = ledger_repo.insert_row(con, user_id, -amount, bal - amount,
                                     "revoke", ref_type, ref_id,
                                     idem_key, note, ts)
    return dict(row)


def history_page(user_id: int, page: int = 1, size: int = 20,
                 since_ts: int | None = None) -> tuple[list, int]:
    """(rows_newest_first, total_count) for the account credits page.

    Dual-pool (2026-08-26): spendable-side flow only — grants/topups/
    consumes/refunds/adjusts. provider_earn & withdraw live on the
    earnings page. One pool, two honest views. since_ts time-boxes both
    the count and the page (epoch seconds); the count is the caller's
    page-bounding fact and comes back even when the offset page is empty.
    """
    with db() as con:
        total = ledger_repo.history_count(con, user_id, SPENDABLE_KINDS,
                                          since_ts)
        rows = ledger_repo.history_page(
            con, user_id, SPENDABLE_KINDS, size,
            max(0, (page - 1) * size), since_ts)
    return rows, total
