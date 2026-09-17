"""Provider earnings & withdrawals — the CASH account (usd_cent).

Provider Earnings are a cash ledger, never credits: the earning pool is
pool='provider_earning', unit='usd_cent'. There is NO conversion between
user_cost_credits and provider payout cash — they are independent
commercial fields on the tool contract.

  earnings_summary(uid)  — aggregate view (total earned / withdrawn /
                           available), derived from the cash pool +
                           withdrawals
  request_withdrawal()   — provider asks to cash out available earnings;
                           lands as a withdrawal REQUEST row (status
                           pending) — no ledger mutation until the
                           operator approves. Payout rails (Stripe etc.)
                           attach later; txn_ref stays the operator's
                           external settlement reference.
  process_withdrawal()   — operator verb (/samelabs): approve (cash
                           movement on the provider_earning pool with
                           idem withdrawal:{id}) or reject (request
                           stays, earnings untouched).

Red lines honored:
  - cash and credits never mix: withdrawal movements live only on the
    provider_earning pool; spendable credits are never withdrawable;
  - idempotency everywhere: withdrawal:{id} family; double-approve is
    structurally impossible (status guard + unique idem);
  - no partial state: approve = status flip + ledger row in ONE tx.
"""

from database import db
from repo import economy as economy_repo
from repo import ledger as ledger_repo
from utils import now

# Cash withdrawal policy (independent of any credit unit):
# minimum payout = $10.00 = 1000 usd_cent.
MIN_WITHDRAWAL_MINOR = 1000
WITHDRAWAL_CURRENCY = "USD"
WITHDRAWAL_STATES = ("pending", "approved", "rejected", "paid")

EARNING_POOL = "provider_earning"
EARNING_UNIT = "usd_cent"


def _fmt_usd(minor: int) -> str:
    """usd_cent → display string, e.g. 2500 → '$25'. Integer cents only —
    no floating point anywhere in the cash path."""
    sign = "-" if minor < 0 else ""
    minor = abs(int(minor))
    if minor % 100 == 0:
        return f"{sign}${minor // 100:,}"
    return f"{sign}${minor // 100:,}.{minor % 100:02d}"


def earnings_summary(user_id: int) -> dict:
    """Journal is authoritative: approved withdrawals are already movements
    on the provider_earning pool, so the pool balance IS the available cash.
    Pending requests are not yet movements → held back. Display fields:

      earned_total  gross accrual (sum of payout movements, delta > 0)
      withdrawn     approved+paid requests (display, from the table)
      pending       pending requests
      available     pool balance − pending = withdrawable now
    """
    with db() as con:
        pool = ledger_repo.cash_pool_balance(
            con, user_id, EARNING_POOL, EARNING_UNIT)
        earned_total = con.execute(
            "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger "
            "WHERE user_id = %s AND pool = %s AND delta > 0",
            (user_id, EARNING_POOL)).fetchone()["s"]
        withdrawn = economy_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = economy_repo.withdrawal_sum(con, user_id, ("pending",))
    available = int(pool) - int(pending)
    return {"earned": int(pool), "earned_total": int(earned_total),
            "withdrawn": int(withdrawn),
            "pending": int(pending), "available": max(0, available)}


def per_tool_revenue(user_id: int) -> list:
    """Revenue breakdown per published tool (provider_earning movements
    joined to the tool that generated them — note references the tool_key)."""
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


def request_withdrawal(user_id: int, amount_minor: int,
                       currency: str = WITHDRAWAL_CURRENCY,
                       note: str | None = None) -> dict:
    """Provider asks to cash out. Request only — earnings stay put until
    operator approval (reject leaves the balance untouched)."""
    amount_minor = int(amount_minor)
    if currency != WITHDRAWAL_CURRENCY:
        raise ValueError("withdrawal_currency_unsupported")
    if amount_minor < MIN_WITHDRAWAL_MINOR:
        raise ValueError("withdrawal_min")
    s = earnings_summary(user_id)  # journal-authoritative available
    if amount_minor > s["available"]:
        raise ValueError("withdrawal_insufficient")
    ts = now()
    with db() as con:
        from repo import users as users_repo
        ch = users_repo.payout_channel_of(con, user_id)
        channel = (ch["payout_channel"] or "")[:120] if ch else ""
        row = economy_repo.insert_withdrawal(con, user_id, amount_minor,
                                             currency, (note or "")[:200], ts)
    out = dict(row)
    out["payout_channel"] = channel  # surfaced for the operator queue
    return out


def process_withdrawal(withdrawal_id: int, approve: bool,
                       admin_user_id: int | None = None,
                       txn_ref: str | None = None) -> dict:
    """Operator verb. Approve = cash movement (pool=provider_earning,
    idem withdrawal:{id}) + status flip, ONE transaction. Reject = flip
    only. Re-processing a finished request is a typed no-op error."""
    ts = now()
    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] != "pending":
            raise ValueError("already_processed")
        if approve:
            # EARN-pool gate (same accounting as the request path's
            # earnings_summary): withdrawal pays provider earnings only —
            # spendable credits are NOT withdrawable. Available excluding
            # this pending row = earned - (approved+paid) - other_pending.
            # pool balance already nets approved withdrawals (movements);
            # only OTHER pending requests are still held back
            avail = (
                ledger_repo.cash_pool_balance(
                    con, row["user_id"], EARNING_POOL, EARNING_UNIT)
                - economy_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("pending",), withdrawal_id))
            if int(avail) < int(row["amount_minor"]):
                con.rollback()
                raise ValueError("withdrawal_insufficient")
            # balance_after is the provider_earning pool balance after this
            # movement — earned − (approved+paid withdrawals incl. this one).
            earned = ledger_repo.cash_pool_balance(
                con, row["user_id"], EARNING_POOL, EARNING_UNIT)
            withdrawn_so_far = economy_repo.withdrawal_sum(
                con, row["user_id"], ("approved", "paid"))
            earn_pool_after = earned - withdrawn_so_far - int(row["amount_minor"])
            ledger_repo.insert_row(con, row["user_id"],
                                   EARNING_POOL, EARNING_UNIT,
                                   -int(row["amount_minor"]),
                                   earn_pool_after, "withdraw",
                                   "withdrawal", str(row["id"]),
                                   f"withdrawal:{row['id']}",
                                   f"withdrawal #{row['id']}", ts)
        else:
            pass  # reject: no earnings movement
        out = economy_repo.finish_withdrawal(
            con, withdrawal_id, "approved" if approve else "rejected",
            ts, admin_user_id,
            (txn_ref or None) if approve else None)
    return dict(out)


def revenue_lines(user_id: int, limit: int = 50, offset: int = 0) -> tuple:
    """E2: per-line revenue detail — (rows, total) so the page can paginate.
    The ledger stays the single source; this is a read projection."""
    with db() as con:
        rows, total = ledger_repo.earn_lines_paged(con, user_id, limit, offset)
    out = []
    for r in rows:
        out.append({"delta": int(r["delta"]),
                    "call_id": r["ref_id"],           # ref = tool_calls.id
                    "tool": (r["note"] or "").replace("payout ", ""),
                    "ts": int(r["created_at"])})
    return out, int(total)


def list_all_pending(limit: int = 50) -> list:
    """Operator queue (/samelabs/withdrawals)."""
    with db() as con:
        rows = economy_repo.withdrawals_queue(con, limit)
    return [dict(r) for r in rows]
