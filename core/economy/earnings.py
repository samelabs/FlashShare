"""Provider earnings & withdrawals (C2C settlement surface, phase 3b).

The ledger stays the single source of truth: provider_earn rows ARE the
earning record. This module adds:

  earnings_summary(uid)  — aggregate view (total earned / withdrawn /
                           available), derived from ledger + withdrawals
  request_withdrawal()   — provider asks to cash out available credits;
                           lands as a withdrawal REQUEST row (status
                           pending) — no ledger mutation until the
                           operator approves. Payout rails (Stripe etc.)
                           attach later; the bookkeeping loop is closed
                           NOW: request → approve → deduct (ledger) →
                           mark paid.
  process_withdrawal()   — operator verb (/samelabs): approve (ledger
                           consume 'withdraw' with idem withdrawal:{id})
                           or reject (request stays, credits untouched).

Red lines honored:
  - face-value credits and cash NEVER mix in one row: withdrawals are
    ledger consume rows (kind='withdraw') referencing the request id;
    cash amounts are the operator's concern at the rail layer.
  - idempotency everywhere: withdrawal:{id} family; double-approve is
    structurally impossible (status guard + unique idem).
  - no partial state: approve = status flip + ledger row in ONE tx.
"""

from database import db
from repo import economy as economy_repo
from repo import ledger as ledger_repo
from utils import now

MIN_WITHDRAWAL = 10000         # credits = $10 (CREDITS_PER_USD=1000)
CREDITS_PER_USD = 1000         # 1 USD = 1000 credits; earnings shown in USD
WITHDRAWAL_STATES = ("pending", "approved", "rejected", "paid")


def earnings_summary(user_id: int) -> dict:
    """Available = earned (provider_earn) − withdrawn (approved+paid) −
    pending requests. Derived live from the ledger; no cached counters."""
    with db() as con:
        earned = ledger_repo.sum_kind(con, user_id, "provider_earn")
        withdrawn = economy_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = economy_repo.withdrawal_sum(con, user_id, ("pending",))
    available = int(earned) - int(withdrawn) - int(pending)
    return {"earned": int(earned), "withdrawn": int(withdrawn),
            "pending": int(pending), "available": max(0, available)}


def per_tool_revenue(user_id: int) -> list:
    """Revenue breakdown per published tool (provider_earn rows joined to
    the tool that generated them — note references the tool_key)."""
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
    """Provider asks to cash out. Request only — credits stay put until
    operator approval (reject leaves the balance untouched).

    Concurrency boundary: the users row lock (SELECT FOR UPDATE)
    serializes same-provider requests, so available-calculation and the
    pending INSERT commit as one unit — two concurrent requests can
    never both see the same available and over-commit it.
    """
    amount = int(amount)
    if amount < MIN_WITHDRAWAL:
        raise ValueError("withdrawal_min")
    ts = now()
    with db() as con:
        ledger_repo.lock_user(con, user_id)   # serialization boundary
        earned = ledger_repo.sum_kind(con, user_id, "provider_earn")
        withdrawn = economy_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = economy_repo.withdrawal_sum(con, user_id, ("pending",))
        available = int(earned) - int(withdrawn) - int(pending)
        if amount > available:
            raise ValueError("withdrawal_insufficient")
        from repo import users as users_repo
        ch = users_repo.payout_channel_of(con, user_id)
        channel = (ch["payout_channel"] or "")[:120] if ch else ""
        row = economy_repo.insert_withdrawal(con, user_id, amount,
                                             (note or "")[:200], ts,
                                             payout_channel=channel or None)
    out = dict(row)
    out["payout_channel"] = channel  # surfaced for the operator queue
    return out


def process_withdrawal(withdrawal_id: int, approve: bool,
                       admin_user_id: int | None = None,
                       txn_ref: str | None = None) -> dict:
    """Operator verb. Approve = ledger consume (kind='withdraw',
    idem withdrawal:{id}) + status flip, ONE transaction. Reject = flip
    only. Re-processing a finished request is a typed no-op error."""
    from economy.billing import consume
    from repo import operator_audit
    ts = now()

    def _snap_w(r) -> dict:
        # withdrawal allowlist ONLY — never payout channel / txn secrets
        return {"id": int(r["id"]), "user_id": int(r["user_id"]),
                "amount": int(r["amount"]), "status": str(r["status"])}

    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] != "pending":
            raise ValueError("already_processed")
        before = _snap_w(row)
        if approve:
            new_status = "approved"
            # EARN-pool gate (same accounting as the request path's
            # earnings_summary): withdrawal pays provider earnings only —
            # top-up credits are NOT withdrawable. Available excluding this
            # pending row = earned - (approved+paid) - other_pending.
            avail = (
                ledger_repo.sum_kind(con, row["user_id"], "provider_earn")
                - economy_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("approved", "paid"), withdrawal_id)
                - economy_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("pending",), withdrawal_id))
            if int(avail) < int(row["amount"]):
                con.rollback()
                raise ValueError("withdrawal_insufficient")
            # withdraw 属 EARN 池支出; balance_after 与 provider_earn writer
            # 同一单池口径 (earn_pool_balance = provider_earn − withdraw)。
            earn_pool_after = ledger_repo.earn_pool_balance(
                con, row["user_id"]) - int(row["amount"])
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
        # audit INSIDE the same domain transaction — failure rolls the
        # withdrawal mutation (and its ledger consume) back with it.
        operator_audit.record(
            con, int(admin_user_id or 0),
            "withdrawal.approve" if approve else "withdrawal.reject",
            "withdrawal", str(withdrawal_id),
            before, {**before, "status": new_status,
                     "processed_by": int(admin_user_id or 0)}, ts)
    return dict(out)


def settle_withdrawal(withdrawal_id: int, paid: bool,
                      admin_user_id: int, txn_ref: str | None = None) -> dict:
    """Operator verb for an APPROVED withdrawal (the earn pool was already
    debited at approval): paid = payout confirmed (txn_ref required);
    failed = payout did not go through → the exact debit returns to the
    earn pool (positive 'withdraw' movement, idem withdrawal-reversal:{id})
    and the request becomes 'failed' (excluded from withdrawn/pending, so
    the amount is withdrawable again). One transaction with its audit."""
    from repo import operator_audit
    ts = now()
    txn_ref = (txn_ref or "").strip()[:120] or None
    if paid and not txn_ref:
        raise ValueError("txn_ref_required")

    def _snap_w(r) -> dict:
        return {"id": int(r["id"]), "user_id": int(r["user_id"]),
                "amount": int(r["amount"]), "status": str(r["status"])}

    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] != "approved":
            raise ValueError("not_approved")
        before = _snap_w(row)
        if paid:
            new_status = "paid"
        else:
            new_status = "failed"
            amount = int(row["amount"])
            idem = f"withdrawal-reversal:{row['id']}"
            if not ledger_repo.idem_key_exists(con, row["user_id"], idem):
                after_bal = ledger_repo.earn_pool_balance(con, row["user_id"]) + amount
                ledger_repo.insert_row(con, row["user_id"], amount, after_bal,
                                       "withdraw", "withdrawal", str(row["id"]),
                                       idem, f"withdrawal #{row['id']} payout failed", ts)
        out = economy_repo.finish_withdrawal(
            con, withdrawal_id, new_status, ts, admin_user_id,
            txn_ref if paid else None)
        operator_audit.record(
            con, int(admin_user_id),
            "withdrawal.paid" if paid else "withdrawal.fail",
            "withdrawal", str(withdrawal_id),
            before, {**before, "status": new_status,
                     "processed_by": int(admin_user_id)}, ts)
    return dict(out)


def revenue_lines(user_id: int, limit: int = 50, offset: int = 0) -> tuple:
    """E2: per-line revenue detail — (rows, total) so the page can paginate.
    The ledger stays the single source; this is a read projection."""
    with db() as con:
        rows, total = ledger_repo.earn_lines_paged(con, user_id, limit, offset)
    out = []
    for r in rows:
        out.append({"delta": int(r["delta"]),
                    "call_id": r["ref_id"],           # ref = tool_calls.id (批4 #17)
                    "tool": (r["note"] or "").replace("payout ", ""),  # note = tool_key
                    "ts": int(r["created_at"])})
    return out, int(total)


def list_all_pending(limit: int = 50) -> list:
    """Operator queue (/samelabs/withdrawals)."""
    with db() as con:
        rows = economy_repo.withdrawals_queue(con, limit)
    return [dict(r) for r in rows]

def to_usd(credits: int) -> str:
    """Credits → display USD string, e.g. 25000 → '$25'."""
    return f"${credits / CREDITS_PER_USD:,.2f}".rstrip('0').rstrip('.') if credits % CREDITS_PER_USD else f"${credits // CREDITS_PER_USD:,}"
