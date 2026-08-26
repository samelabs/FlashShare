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
from utils import now

MIN_WITHDRAWAL = 100          # credits
WITHDRAWAL_STATES = ("pending", "approved", "rejected", "paid")


def ensure_schema() -> None:
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
              id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              amount      BIGINT NOT NULL CHECK (amount > 0),
              status      TEXT NOT NULL DEFAULT 'pending',
              note        TEXT,
              created_at  BIGINT NOT NULL,
              processed_at BIGINT,
              processed_by BIGINT,
              txn_ref     TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS withdrawals_user_idx ON withdrawals(user_id)")
        con.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS txn_ref TEXT")
        con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS payout_channel TEXT")


def earnings_summary(user_id: int) -> dict:
    """Available = earned (provider_earn) − withdrawn (approved+paid) −
    pending requests. Derived live from the ledger; no cached counters."""
    with db() as con:
        earned = con.execute(
            "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger "
            "WHERE user_id = %s AND kind = 'provider_earn'",
            (user_id,),
        ).fetchone()["s"]
        withdrawn = con.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM withdrawals "
            "WHERE user_id = %s AND status IN ('approved', 'paid')",
            (user_id,),
        ).fetchone()["s"]
        pending = con.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM withdrawals "
            "WHERE user_id = %s AND status = 'pending'",
            (user_id,),
        ).fetchone()["s"]
    available = int(earned) - int(withdrawn) - int(pending)
    return {"earned": int(earned), "withdrawn": int(withdrawn),
            "pending": int(pending), "available": max(0, available)}


def per_tool_revenue(user_id: int) -> list:
    """Revenue breakdown per published tool (provider_earn rows joined to
    the tool that generated them — note references the tool_key)."""
    with db() as con:
        rows = con.execute(
            """SELECT l.note AS tool_key,
                      SUM(l.delta) AS earned,
                      COUNT(*) AS payouts
               FROM ledger l
               WHERE l.user_id = %s AND l.kind = 'provider_earn'
               GROUP BY l.note
               ORDER BY earned DESC""",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        key = r["tool_key"] or ""
        label = key.split("/")[-1] if key else "?"
        out.append({"tool": label, "tool_key": key,
                    "earned": int(r["earned"]), "payouts": r["payouts"]})
    return out


def list_withdrawals(user_id: int, limit: int = 20) -> list:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM withdrawals WHERE user_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (user_id, limit,),
        ).fetchall()
    return [dict(r) for r in rows]


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
        ch = con.execute(
            "SELECT payout_channel FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        channel = (ch["payout_channel"] or "")[:120] if ch else ""
        row = con.execute(
            """INSERT INTO withdrawals
               (user_id, amount, status, note, created_at, txn_ref)
               VALUES (%s, %s, 'pending', %s, %s, NULL) RETURNING *""",
            (user_id, amount, (note or "")[:200], ts,),
        ).fetchone()
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
        row = con.execute(
            "SELECT * FROM withdrawals WHERE id = %s FOR UPDATE",
            (withdrawal_id,),
        ).fetchone()
        if not row:
            raise KeyError("not_found")
        if row["status"] != "pending":
            raise ValueError("already_processed")
        if approve:
            new_status = "approved"
            bal = con.execute(
                "SELECT COALESCE(SUM(delta),0) AS b FROM ledger WHERE user_id = %s",
                (row["user_id"],),
            ).fetchone()["b"]
            if int(bal) < int(row["amount"]):
                con.rollback()
                raise ValueError("withdrawal_insufficient")
            con.execute(
                """INSERT INTO ledger (user_id, delta, balance_after, kind,
                   ref_type, ref_id, idem_key, note, created_at)
                   VALUES (%s, %s, %s, 'withdraw', 'withdrawal', %s, %s, %s, %s)""",
                (row["user_id"], -int(row["amount"]), int(bal) - int(row["amount"]),
                 str(row["id"]), f"withdrawal:{row['id']}",
                 f"withdrawal #{row['id']}", ts,),
            )
        else:
            new_status = "rejected"
        out = con.execute(
            """UPDATE withdrawals SET status = %s, processed_at = %s,
               processed_by = %s, txn_ref = COALESCE(%s, txn_ref)
               WHERE id = %s RETURNING *""",
            (new_status, ts, admin_user_id,
             (txn_ref or None) if approve else None, withdrawal_id,),
        ).fetchone()
    return dict(out)


def revenue_lines(user_id: int, limit: int = 50, offset: int = 0) -> list:
    """E2: per-line revenue detail (provider_earn rows: tool / call / amount /
    time). The ledger stays the single source; this is a read projection."""
    with db() as con:
        rows = con.execute(
            """SELECT l.delta, l.ref_type, l.ref_id, l.note, l.created_at
               FROM ledger l
               WHERE l.user_id = %s AND l.kind = 'provider_earn'
               ORDER BY l.id DESC LIMIT %s OFFSET %s""",
            (user_id, limit, offset,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({"delta": int(r["delta"]), "ref": r["ref_id"],
                    "tool": (r["note"] or "").replace("payout ", ""),
                    "ts": int(r["created_at"])})
    return out


def list_all_pending(limit: int = 50) -> list:
    """Operator queue (/samelabs/withdrawals)."""
    with db() as con:
        rows = con.execute(
            """SELECT w.*, u.username, u.payout_channel FROM withdrawals w
               JOIN users u ON u.id = w.user_id
               ORDER BY (w.status = 'pending') DESC, w.id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
