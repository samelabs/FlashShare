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
from repo import catalog as catalog_repo
from repo import ledger as ledger_repo
from utils import now

MIN_WITHDRAWAL = 10000         # credits = $10 (CREDITS_PER_USD=1000)
CREDITS_PER_USD = 1000         # 1 USD = 1000 credits; earnings shown in USD
WITHDRAWAL_STATES = ("pending", "approved", "rejected", "paid")


def ensure_schema() -> None:
    """Schema now lives in database.py _SCHEMA_SQL (single source of truth,
    批1归一). Kept as a no-op shim for existing callers (main.startup);
    the withdrawals DDL here would be a second truth source.
    """
    return None


def earnings_summary(user_id: int) -> dict:
    """Available = earned (provider_earn) − withdrawn (approved+paid) −
    pending requests. Derived live from the ledger; no cached counters."""
    with db() as con:
        earned = ledger_repo.sum_kind(con, user_id, "provider_earn")
        withdrawn = catalog_repo.withdrawal_sum(
            con, user_id, ("approved", "paid"))
        pending = catalog_repo.withdrawal_sum(con, user_id, ("pending",))
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
        rows = catalog_repo.withdrawals_by_user(con, user_id, limit, offset)
    return [dict(r) for r in rows]


def withdrawal_count(user_id: int) -> int:
    with db() as con:
        return catalog_repo.withdrawal_count(con, user_id)


def request_withdrawal(user_id: int, amount: int, note: str | None = None) -> dict:
    """Provider asks to cash out. Request only — credits stay put until
    operator approval (reject leaves the balance untouched)."""
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
        row = catalog_repo.insert_withdrawal(con, user_id, amount,
                                             (note or "")[:200], ts)
    out = dict(row)
    out["payout_channel"] = channel  # surfaced for the operator queue
    return out


def process_withdrawal(withdrawal_id: int, approve: bool,
                       admin_user_id: int | None = None,
                       txn_ref: str | None = None) -> dict:
    """Operator verb. Approve = ledger consume (kind='withdraw',
    idem withdrawal:{id}) + status flip, ONE transaction. Reject = flip
    only. Re-processing a finished request is a typed no-op error."""
    from billing import consume
    ts = now()
    with db() as con:
        row = catalog_repo.withdrawal_for_update(con, withdrawal_id)
        if not row:
            raise KeyError("not_found")
        if row["status"] != "pending":
            raise ValueError("already_processed")
        if approve:
            new_status = "approved"
            # EARN-pool gate (same accounting as the request path's
            # earnings_summary): withdrawal pays provider earnings only —
            # top-up credits are NOT withdrawable. Available excluding this
            # pending row = earned - (approved+paid) - other_pending.
            avail = (
                ledger_repo.sum_kind(con, row["user_id"], "provider_earn")
                - catalog_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("approved", "paid"), withdrawal_id)
                - catalog_repo.withdrawal_sum_excluding(
                    con, row["user_id"], ("pending",), withdrawal_id))
            if int(avail) < int(row["amount"]):
                con.rollback()
                raise ValueError("withdrawal_insufficient")
            # EARN 池基线: balance_after 记 provider_earn 池口径 (批2 修复 🔴3,
            # 现阶段保持现行为=全池口径, 重建时统一)
            bal = ledger_repo.total_balance(con, row["user_id"])
            ledger_repo.insert_row(con, row["user_id"], -int(row["amount"]),
                                   int(bal) - int(row["amount"]), "withdraw",
                                   "withdrawal", str(row["id"]),
                                   f"withdrawal:{row['id']}",
                                   f"withdrawal #{row['id']}", ts)
        else:
            new_status = "rejected"
        out = catalog_repo.finish_withdrawal(
            con, withdrawal_id, new_status, ts, admin_user_id,
            (txn_ref or None) if approve else None)
    return dict(out)


def revenue_lines(user_id: int, limit: int = 50, offset: int = 0) -> tuple:
    """E2: per-line revenue detail — (rows, total) so the page can paginate.
    The ledger stays the single source; this is a read projection."""
    with db() as con:
        rows, total = ledger_repo.earn_lines_paged(con, user_id, limit, offset)
    out = []
    for r in rows:
        out.append({"delta": int(r["delta"]), "ref": r["ref_id"],
                    "tool": (r["note"] or "").replace("payout ", ""),
                    "ts": int(r["created_at"])})
    return out, int(total)


def list_all_pending(limit: int = 50) -> list:
    """Operator queue (/samelabs/withdrawals)."""
    with db() as con:
        rows = catalog_repo.withdrawals_queue(con, limit)
    return [dict(r) for r in rows]

def to_usd(credits: int) -> str:
    """Credits → display USD string, e.g. 25000 → '$25'."""
    return f"${credits / CREDITS_PER_USD:,.2f}".rstrip('0').rstrip('.') if credits % CREDITS_PER_USD else f"${credits // CREDITS_PER_USD:,}"
