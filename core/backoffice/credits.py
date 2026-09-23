"""Operator credits adjustment command (migrated verbatim in behavior
from the retired /samelabs/users/{id}/credits route).

Contract kept exactly:
  - stable action id: the operator UI mints ONE id per edited action and
    resends it verbatim on network retry — the retry replays the same
    ledger idem key instead of double mutating
  - user-row serialization via billing grant/consume
  - ledger idem `adjust:{operator}:{action_id}`
  - one ledger movement ↔ one audit fact, same transaction
"""

from database import db
from repo import operator_audit
from utils import now


def adjust_credits(operator_id: int, target_user_id: int, amount: int,
                   action_id: str, note: str) -> dict:
    """Returns {'ok': True, 'balance_after': N} or raises ValueError with
    a typed token (zero_amount / insufficient via InsufficientCredits)."""
    from economy.billing import grant, consume, InsufficientCredits
    if not action_id or len(action_id) > 64:
        raise ValueError("invalid_request")
    idem = f"adjust:{operator_id}:{action_id}"
    # ledger movement + audit fact in ONE transaction; same action_id
    # replay → billing idem returns the ORIGINAL row → zero new audit.
    try:
        with db() as con:
            if amount > 0:
                row = grant(target_user_id, amount, "adjust",
                            note=note or "samelabs adjust",
                            idem_key=idem, con=con)
            elif amount < 0:
                row = consume(target_user_id, -amount, "adjust",
                              note=note or "samelabs adjust",
                              idem_key=idem, con=con)
            else:
                raise ValueError("zero_amount")
            # one ledger movement ↔ one audit fact: a replayed action
            # resolves to the SAME ledger row id → audit already exists
            already = operator_audit.exists_for_ledger(con, int(row["id"]))
            if not already:
                delta = int(row["delta"])
                bal_after = int(row["balance_after"])
                operator_audit.record(
                    con, operator_id, "credits.adjust", "user_credits",
                    str(target_user_id),
                    {"user_id": target_user_id,
                     "balance_before": bal_after - delta,
                     "balance_after": bal_after},
                    {"user_id": target_user_id, "delta": delta,
                     "balance_before": bal_after - delta,
                     "balance_after": bal_after,
                     "ledger_id": int(row["id"])}, now())
    except InsufficientCredits as exc:
        raise ValueError("insufficient") from exc
    return {"ok": True, "balance_after": int(row["balance_after"])}
