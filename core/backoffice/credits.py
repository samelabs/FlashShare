"""Operator credits adjustment command (migrated verbatim in behavior
from the retired /samelabs/users/{id}/credits route).

Contract kept exactly:
  - stable action id: the operator UI mints ONE id per edited action and
    resends it verbatim on network retry — the retry replays the same
    ledger idem key instead of double mutating
  - user-row serialization via billing grant/consume
  - ledger idem `adjust:{operator}:{action_id}`
  - one ledger movement ↔ one audit fact, same transaction
  - typed rejects: unknown target user → KeyError('user_not_found');
    |amount| > MAX_ADJUST → ValueError('amount_out_of_range'); the same
    action id replayed with a DIFFERENT amount → ValueError(
    'action_id_conflict') (never a silent replay of the original row)
"""

from database import db
from repo import operator_audit
from repo import ledger as ledger_repo
from repo import users as users_repo
from utils import now

# Upper bound for one operator adjustment (Credits). Keeps the ledger
# BIGINT arithmetic far from overflow and rejects fat-finger amounts.
MAX_ADJUST = 1_000_000_000


def adjust_credits(operator_id: int, target_user_id: int, amount: int,
                   action_id: str, note: str) -> dict:
    """Returns {'ok': True, 'balance_after': N, 'replayed': bool} or
    raises KeyError('user_not_found') / ValueError with a typed token
    (invalid_request / zero_amount / amount_out_of_range /
    action_id_conflict / insufficient)."""
    from economy.billing import grant, consume, InsufficientCredits
    if not action_id or len(action_id) > 64:
        raise ValueError("invalid_request")
    if amount == 0:
        raise ValueError("zero_amount")
    if abs(amount) > MAX_ADJUST:
        raise ValueError("amount_out_of_range")
    idem = f"adjust:{operator_id}:{action_id}"
    # ledger movement + audit fact in ONE transaction; same action_id
    # replay → billing idem returns the ORIGINAL row → zero new audit.
    try:
        with db() as con:
            # target existence + user-row serialization BEFORE the idem
            # read (the billing primitives take the same row lock again)
            ledger_repo.lock_user(con, target_user_id)
            if users_repo.by_id(con, target_user_id) is None:
                raise KeyError("user_not_found")
            prior = ledger_repo.by_idem_key(con, target_user_id, idem)
            if prior is not None and int(prior["delta"]) != amount:
                # same stable action id, different edited amount: the
                # original movement stands; the new amount is NOT applied
                raise ValueError("action_id_conflict")
            if amount > 0:
                row = grant(target_user_id, amount, "adjust",
                            note=note or "samelabs adjust",
                            idem_key=idem, con=con)
            else:
                row = consume(target_user_id, -amount, "adjust",
                              note=note or "samelabs adjust",
                              idem_key=idem, con=con)
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
    return {"ok": True, "balance_after": int(row["balance_after"]),
            "replayed": prior is not None}
