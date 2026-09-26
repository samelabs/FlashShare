"""Provider earnings & withdrawals (C2C settlement surface, phase 3b).

Denomination (S3): provider/platform cash amounts are integer USD-millis
(1000 = $1.00) — provider_earn, withdrawals.amount, and the earn pool
are USD-millis. Consumer Spendable Credits are a separate consumer-side
unit and never appear here as "earnings". The single conversion
authority is economy.money (settlement boundary + USD display + USD
decimal parsing).

The ledger stays the single source of truth: provider_earn rows ARE the
earning record. This module adds:

  earnings_summary(uid)  — aggregate view (total earned / withdrawn /
                           available), derived from ledger + withdrawals
  request_withdrawal()   — provider asks to cash out available
                           USD-millis; lands as a withdrawal REQUEST row
                           (status pending) — no ledger mutation until
                           the operator approves. Payout rails (Stripe
                           etc.) attach later; the bookkeeping loop is
                           closed NOW: request → approve → deduct
                           (ledger) → mark paid.
  approve_withdrawal()   — operator verb: pending → approved, exactly
                           one withdraw ledger row (idem withdrawal:{id})
  reject_withdrawal()    — pending → rejected, zero ledger mutation
  mark_withdrawal_paid() — approved → paid with external payout_ref,
                           zero ledger mutation

Red lines honored:
  - consumer credits and cash NEVER mix in one row: withdrawals are
    ledger rows (kind='withdraw') referencing the request id; the cash
    amount unit is USD-millis everywhere in this pool.
  - idempotency everywhere: withdrawal:{id} family; double-approve is
    structurally impossible (status guard + unique idem).
  - no partial state: approve = status flip + ledger row in ONE tx.
"""

from database import db
from economy.money import CENT_MILLIS
from repo import economy as economy_repo
from repo import ledger as ledger_repo
from utils import now

MIN_WITHDRAWAL_MILLIS = 10000    # USD-millis = $10.00 minimum withdrawal
WITHDRAWAL_STATES = ("pending", "approved", "rejected", "paid",
                     "payout_failed")


def earnings_summary(user_id: int) -> dict:
    """Available = earned (provider_earn) − withdrawn (approved+paid) −
    pending requests. All amounts are USD-millis. Derived live from the
    ledger; no cached counters."""
    with db() as con:
        earned = ledger_repo.sum_kind(con, user_id, "provider_earn")
        withdrawn = economy_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = economy_repo.withdrawal_sum(con, user_id, ("pending",))
    available = int(earned) - int(withdrawn) - int(pending)
    return {"earned": int(earned), "withdrawn": int(withdrawn),
            "pending": int(pending), "available": max(0, available)}


def per_tool_revenue(user_id: int) -> list:
    """Revenue breakdown per published tool. Tool identity is relation-
    derived: ledger(kind=provider_earn, ref_type='tool_call', ref_id=
    tool_calls.id) → tc.tool_id. note is never parsed for identity."""
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
    """Provider asks to cash out. amount is USD-millis (whole-cent only —
    the route parses the USD decimal string via economy.money; this
    layer never sees floats). Request only — the earn pool is untouched
    until operator approval (reject never debits).

    S4: the request freezes the CURRENT users.payout_channel into
    withdrawals.payout_channel_snapshot inside the same transaction.
    Empty channel → typed error payout_channel_required, no row created.
    Later profile edits never change an existing request.

    Concurrency boundary: the users row lock (SELECT FOR UPDATE) is the
    SAME cash-pool anchor Tool settlement uses — same-provider requests,
    earning writes and approvals all serialize here.
    """
    from economy.money import strict_int
    amount = strict_int(amount)   # real int only — bool/float/str/None fail
    if amount % CENT_MILLIS:
        raise ValueError("sub_cent_amount")     # whole-cent only, no rounding
    if amount < MIN_WITHDRAWAL_MILLIS:
        raise ValueError("withdrawal_min")
    ts = now()
    with db() as con:
        ledger_repo.lock_user(con, user_id)   # cash-pool serialization anchor
        from repo import users as users_repo
        ch = users_repo.payout_channel_of(con, user_id)
        channel = (ch["payout_channel"] or "").strip()[:120] if ch else ""
        if not channel:
            raise ValueError("payout_channel_required")
        earned = ledger_repo.sum_kind(con, user_id, "provider_earn")
        withdrawn = economy_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = economy_repo.withdrawal_sum(con, user_id, ("pending",))
        available = int(earned) - int(withdrawn) - int(pending)
        if amount > available:
            raise ValueError("withdrawal_insufficient")
        row = economy_repo.insert_withdrawal(
            con, user_id, amount, (note or "")[:200], ts, channel)
    return dict(row)


def _snap_w(r) -> dict:
    # withdrawal allowlist ONLY — never payout channel / payout_ref
    # secrets; the request-time channel snapshot stays on the row.
    return {"id": int(r["id"]), "user_id": int(r["user_id"]),
            "amount": int(r["amount"]), "status": str(r["status"])}


def approve_withdrawal(withdrawal_id: int,
                       admin_user_id: int) -> dict:
    """pending → approved. Exactly one withdraw ledger row (idem
    withdrawal:{id}); review fields set; paid fields untouched.

    Replay: approve on an already-approved row is a NO-OP success
    (returns the row; zero ledger, zero audit). Any other state →
    typed transition conflict.
    """
    from repo import operator_audit
    ts = now()
    with db() as con:
        # 1. lock the withdrawal row
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        # replay: approved × approve → existing row, zero mutation
        if row["status"] == "approved":
            return dict(row)
        if row["status"] != "pending":
            raise ValueError("transition_conflict")
        # 2. lock provider users row — the earn-pool serialization
        #    anchor shared with settlement + requests
        ledger_repo.lock_user(con, int(row["user_id"]))
        before = _snap_w(row)
        # 3-5. re-read pool facts; verify still fully funded
        avail = (
            ledger_repo.sum_kind(con, row["user_id"], "provider_earn")
            - economy_repo.withdrawal_sum_excluding(
                con, row["user_id"], ("approved", "paid"), withdrawal_id)
            - economy_repo.withdrawal_sum_excluding(
                con, row["user_id"], ("pending",), withdrawal_id))
        if int(avail) < int(row["amount"]):
            con.rollback()
            raise ValueError("withdrawal_insufficient")
        # 6. exactly one withdraw ledger row (unique idem backs this)
        earn_pool_after = ledger_repo.earn_pool_balance(
            con, row["user_id"]) - int(row["amount"])
        ledger_repo.insert_row(con, row["user_id"], -int(row["amount"]),
                               earn_pool_after, "withdraw",
                               "withdrawal", str(row["id"]),
                               f"withdrawal:{row['id']}",
                               f"withdrawal #{row['id']}", ts)
        # 7. status → approved (review fields only)
        out = economy_repo.mark_reviewed(
            con, withdrawal_id, "approved", ts, int(admin_user_id))
        # 9. audit inside the same transaction
        operator_audit.record(
            con, int(admin_user_id), "withdrawal.approve",
            "withdrawal", str(withdrawal_id), before,
            {**before, "status": "approved",
             "reviewed_by": int(admin_user_id)}, ts)
    return dict(out)


def reject_withdrawal(withdrawal_id: int,
                      admin_user_id: int) -> dict:
    """pending → rejected. ZERO ledger mutation.

    Replay: reject on an already-rejected row is a NO-OP success
    (zero mutation, zero duplicate audit).
    """
    from repo import operator_audit
    ts = now()
    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] == "rejected":
            return dict(row)               # replay: no-op success
        if row["status"] != "pending":
            raise ValueError("transition_conflict")
        before = _snap_w(row)
        out = economy_repo.mark_reviewed(
            con, withdrawal_id, "rejected", ts, int(admin_user_id))
        operator_audit.record(
            con, int(admin_user_id), "withdrawal.reject",
            "withdrawal", str(withdrawal_id), before,
            {**before, "status": "rejected",
             "reviewed_by": int(admin_user_id)}, ts)
    return dict(out)


def mark_withdrawal_paid(withdrawal_id: int, payout_ref: str,
                         admin_user_id: int) -> dict:
    """approved → paid. payout_ref = actual non-empty external settlement
    evidence (bounded length, stored verbatim). ZERO ledger mutation —
    the single debit happened at approval.

    Replay: paid + SAME payout_ref → NO-OP success (zero mutation,
    zero duplicate audit). paid + different payout_ref → typed
    payout_ref_conflict (original evidence is never overwritten).
    pending → paid is FORBIDDEN (transition_conflict).
    """
    from repo import operator_audit
    # strict: only a real JSON string may carry external settlement
    # evidence — numbers/bools/null/containers are NEVER coerced
    if not isinstance(payout_ref, str):
        raise ValueError("invalid_payout_ref_type")
    ref = payout_ref.strip()
    if not ref or len(ref) > 200:
        raise ValueError("invalid_payout_ref")
    ts = now()
    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] == "paid":
            if (row["payout_ref"] or "") == ref:
                return dict(row)           # replay: same evidence, no-op
            raise ValueError("payout_ref_conflict")
        if row["status"] != "approved":
            raise ValueError("transition_conflict")
        before = _snap_w(row)
        out = economy_repo.mark_paid(
            con, withdrawal_id, ts, int(admin_user_id), ref)
        operator_audit.record(
            con, int(admin_user_id), "withdrawal.paid",
            "withdrawal", str(withdrawal_id), before,
            {**before, "status": "paid", "paid_by": int(admin_user_id)}, ts)
    return dict(out)


def mark_withdrawal_payout_failed(withdrawal_id: int, reason: str,
                                  admin_user_id: int) -> dict:
    """approved → payout_failed (B2b-1): the external payout could not be
    completed. ONE transaction: status flip + earn-pool return (a positive
    provider_earn ledger row, idem `payout-failed:{id}`) + audit. The earn
    pool math never changes shape — earnings_summary already excludes
    non-(approved|paid) withdrawals from `withdrawn`, so the returned
    amount becomes available again purely through the ledger row.

    Replay: payout_failed + replay → NO-OP success (zero new ledger, zero
    duplicate audit; the idem key backs the same guarantee under any
    interleaving). pending/rejected/paid/payout_failed-source states are
    refused (transition_conflict / not_found).
    reason is REQUIRED operator evidence (bounded, stored in the audit).
    """
    from repo import operator_audit
    if not isinstance(reason, str):
        raise ValueError("invalid_payout_reason_type")
    why = reason.strip()
    if not why or len(why) > 200:
        raise ValueError("invalid_payout_reason")
    ts = now()
    with db() as con:
        row = economy_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        # replay: already payout_failed → no-op success, zero mutation
        if row["status"] == "payout_failed":
            return dict(row)
        if row["status"] != "approved":
            raise ValueError("transition_conflict")
        before = _snap_w(row)
        # cash-pool serialization anchor — same lock the approval took
        ledger_repo.lock_user(con, int(row["user_id"]))
        idem = f"payout-failed:{withdrawal_id}"
        pool_after = ledger_repo.earn_pool_balance(
            con, row["user_id"]) + int(row["amount"])
        # the return leg reverses the withdrawal's own negative movement
        # (same 'withdraw' kind, positive delta): the pool arithmetic
        # (provider_earn − withdraw) restores exactly, and the lifetime
        # 'earned' aggregate keeps counting the ORIGINAL earning once —
        # a provider_earn-kind return row would double-count it in
        # earnings_summary.
        ledger_repo.insert_row(
            con, int(row["user_id"]), int(row["amount"]), pool_after,
            "withdraw", "withdrawal", str(withdrawal_id), idem,
            f"withdrawal #{withdrawal_id} payout failed", ts)
        out = economy_repo.mark_payout_failed(con, withdrawal_id)
        operator_audit.record(
            con, int(admin_user_id), "withdrawal.payout_failed",
            "withdrawal", str(withdrawal_id), before,
            {**before, "status": "payout_failed", "reason": why,
             "resolved_by": int(admin_user_id)}, ts)
    return dict(out)


def revenue_lines(user_id: int, limit: int = 50, offset: int = 0) -> tuple:
    """E2: per-line revenue detail — (rows, total) so the page can paginate.
    The ledger stays the single source; this is a read projection."""
    with db() as con:
        rows, total = ledger_repo.earn_lines_paged(con, user_id, limit, offset)
    out = []
    for r in rows:
        tool_id = r["tool_id"]
        out.append({"delta": int(r["delta"]),
                    "call_id": r["ref_id"],           # ref = tool_calls.id (批4 #17)
                    "tool": tool_id if tool_id is not None else "?",
                    "ts": int(r["created_at"])})
    return out, int(total)
