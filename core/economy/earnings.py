"""Creator earnings & withdrawals.

The ledger remains shared infrastructure, but this domain's unit is USD cents:
provider_earn and withdraw rows are money, not spendable Credits.
"""

from database import db
from repo import economy as economy_repo
from repo import ledger as ledger_repo
from utils import now

MIN_WITHDRAWAL = 1000          # cents = $10
WITHDRAWAL_STATES = ("pending", "approved", "rejected", "paid")


def ensure_schema() -> None:
    return None


def earnings_summary(user_id: int) -> dict:
    """Amounts are integer USD cents."""
    with db() as con:
        earned = ledger_repo.sum_kind(con, user_id, "provider_earn")
        withdrawn = economy_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = economy_repo.withdrawal_sum(con, user_id, ("pending",))
    available = int(earned) - int(withdrawn) - int(pending)
    return {"earned": int(earned), "withdrawn": int(withdrawn),
            "pending": int(pending), "available": max(0, available)}


def per_tool_revenue(user_id: int) -> list:
    with db() as con:
        rows = ledger_repo.earn_by_tool(con, user_id, limit=100)
    out = []
    for r in rows:
        key = r["tool_key"] or ""
        label = key.split("/")[-1] if key else "?"
        out.append({"tool": label, "tool_key": key,
                    "earned": int(r["earned"]), "payouts": r["payouts"]})
    return out


def list_withdrawals(user_id: int, limit: int = 20, offset: int = 0) -> list:
    with db() as con:
        rows = economy_repo.withdrawals_by_user(con, user_id, limit, offset)
    return [dict(r) for r in rows]


def withdrawal_count(user_id: int) -> int:
    with db() as con:
        return economy_repo.withdrawal_count(con, user_id)


def request_withdrawal(user_id: int, amount: int, note: str | None = None) -> dict:
    """Request withdrawal of creator Earnings. amount is USD cents."""
    amount = int(amount)
    if amount < MIN_WITHDRAWAL:
        raise ValueError("withdrawal_min")
    s = earnings_summary(user_id)
    if amount > s["available"]:
        raise ValueError("withdrawal_insufficient")
    ts = now()
    with db() as con:
        from repo import users as users_repo
        ch = users_repo.payout_channel_of(con, user_id)
        channel = (ch["payout_channel"] or "")[:120] if ch else ""
        row = economy_repo.insert_withdrawal(con, user_id, amount,
                                             (note or "")[:200], ts)
    out = dict(row)
    out["payout_channel"] = channel
    return out


def process_withdrawal(withdrawal_id: int, approve: bool,
                       admin_user_id: int | None = None,
                       txn_ref: str | None = None) -> dict:
    """Operator bookkeeping step. Earnings amounts are USD cents."""
    ts = now()
    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] != "pending":
            raise ValueError("already_processed")
        if approve:
            new_status = "approved"
            avail = (
                ledger_repo.sum_kind(con, row["user_id"], "provider_earn")
                - economy_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("approved", "paid"), withdrawal_id)
                - economy_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("pending",), withdrawal_id))
            if int(avail) < int(row["amount"]):
                con.rollback()
                raise ValueError("withdrawal_insufficient")
            earned = ledger_repo.sum_kind(con, row["user_id"], "provider_earn")
            withdrawn_so_far = economy_repo.withdrawal_sum(
                con, row["user_id"], ("approved", "paid"))
            earn_pool_after = earned - withdrawn_so_far - int(row["amount"])
            ledger_repo.insert_row(con, row["user_id"], -int(row["amount"]),
                                   earn_pool_after, "withdraw",
                                   "withdrawal", str(row["id"]),
                                   f"withdrawal:{row['id']}",
                                   f"withdrawal #{row['id']}", ts)
        else:
            new_status = "rejected"
        out = economy_repo.finish_withdrawal(
            con, withdrawal_id, new_status, ts, admin_user_id,
            (txn_ref or None) if approve else None)
    return dict(out)


def revenue_lines(user_id: int, limit: int = 50, offset: int = 0) -> tuple:
    with db() as con:
        rows, total = ledger_repo.earn_lines_paged(con, user_id, limit, offset)
    out = []
    for r in rows:
        out.append({"delta": int(r["delta"]),
                    "call_id": r["ref_id"],
                    "tool": (r["note"] or "").replace("payout ", ""),
                    "ts": int(r["created_at"])})
    return out, int(total)


def list_all_pending(limit: int = 50) -> list:
    with db() as con:
        rows = economy_repo.withdrawals_queue(con, limit)
    return [dict(r) for r in rows]


def to_usd(cents: int) -> str:
    """Integer USD cents -> display string."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    dollars, remainder = divmod(cents, 100)
    if remainder:
        return f"{sign}${dollars:,}.{remainder:02d}"
    return f"{sign}${dollars:,}"
