"""
Payment reverse domain — refund.created / dispute.created → exact
economic reversal (single handler for both events).

Chain: signed event → fetch provider Transaction (never trust the
webhook's simplified object) → validate status → locate the ORIGINAL
economic fact → exact negative revoke via billing.revoke (the single
negative payment-reversal writer).

Unit model: 1 USD = 1000 Credits. A successful provider payment created
Credits; its refund/chargeback creates the equal negative. Balance may
go negative — spent value does not block reversal. No secondary
monetary store, no debt table, no financial_hold.

Idempotency: payment-reversal:{transaction_id} — refund replay, dispute
replay, and refund-then-dispute on the same transaction all converge to
ONE reversal.
"""
import logging

from database import db
from repo import orders as orders_repo
from utils import now

log = logging.getLogger("flashshare")

from repo.orders import mark_refunded, mark_disputed  # noqa: E402


def reverse_payment(event_type: str, event: dict) -> dict:
    """refund.created / dispute.created → exact reversal.

    Raises KeyError (→ 500 retriable) on: missing proof fields, provider
    fetch failure, unknown transaction, or un-locatable original economic
    fact. A PARTIAL refund is queued for operator review and acknowledged
    (200) — zero mutation. Returns a small dict on success or an
    idempotent no-op.
    """
    from economy import reconcile as _reconcile

    event_id = str(event.get("id") or "")
    obj = event.get("object") or {}
    if not isinstance(obj, dict):
        obj = {}
    # Real Creem shape: event.id=evt_xxx; refund object.id=ref_xxx /
    # dispute object.id=disp_xxx; the transaction id lives ONLY in
    # object.transaction.id — never fall back to the refund/dispute
    # object id (it names a different provider entity).
    expected_obj = "refund" if event_type == "refund.created" else "dispute"
    if str(obj.get("object") or "") != expected_obj:
        raise KeyError("reversal_object_type_mismatch")
    txn_obj = obj.get("transaction")
    txn_id = str(txn_obj.get("id") or "") if isinstance(txn_obj, dict) else ""
    if not event_id or not txn_id:
        raise KeyError("reversal_proof_missing")

    # ── provider transaction is the ONLY economic authority ──
    # Never adjudicate on the webhook's simplified object.
    txn = _reconcile.fetch_transaction(txn_id)
    if txn is None:
        log.warning("reverse %s: transaction %s fetch failed — retriable",
                    event_type, txn_id)
        raise KeyError("transaction_fetch_failed")

    status = str(txn.get("status") or "")
    if event_type == "refund.created":
        if status == "partially_refunded":
            # Owner rule (B2b-1): a partial refund is a HUMAN decision —
            # ZERO Credits mutation, ZERO Membership mutation. The event
            # lands in the operator review queue (idempotent per event id)
            # and the webhook acknowledges with 200: replaying it can
            # never converge anything, so retry pressure is pure noise.
            from repo import payment_reviews as _reviews
            with db() as con:
                queued = _reviews.insert_review(
                    con, event_type, event_id, txn_id,
                    {"provider_status": status,
                     "provider_type": str(txn.get("type") or "")},
                    now())
            log.warning(
                "reverse refund.created: txn=%s PARTIAL refund — %s, "
                "operator review required (event=%s)",
                txn_id,
                "queued" if queued else "already queued (replay)",
                event_id)
            return {"reversed": False, "credits": 0,
                    "kind": "review", "reason": "partial_refund",
                    "queued": bool(queued)}
        if status != "refunded":
            raise KeyError("refund_status_not_refunded")
    else:  # dispute.created
        if status != "chargeback":
            raise KeyError("dispute_status_not_chargeback")

    ttype = str(txn.get("type") or "")
    _is_dispute = event_type == "dispute.created"
    if ttype == "payment":
        return _reverse_package(txn, _is_dispute)
    if ttype == "invoice":
        return _reverse_subscription(txn)
    raise KeyError("transaction_type_unknown")


def _reverse_package(txn: dict, _is_dispute: bool = False) -> dict:
    """one-time credits package: reverse the original grant_package fact."""
    txn_id = str(txn.get("id") or "")
    order_ref = str(txn.get("order") or "")
    if isinstance(txn.get("order"), dict):
        order_ref = str(txn["order"].get("id") or "")
    if not order_ref:
        raise KeyError("package_order_missing")
    with db() as con:
        row = orders_repo.order_by_provider_id(con, order_ref)
        if row is None or row["kind"] != "package":
            raise KeyError("package_order_not_found")
        # original fact: the actual grant ledger row (idem order:{id})
        from repo import ledger as ledger_repo
        grant_row = ledger_repo.by_idem_key(con, int(row["user_id"]),
                                            f"order:{row['id']}")
    # PRE-STATE GATE: a reversal may only land on an order that was
    # actually PAID (the payment it reverses provably completed).
    # pending/failed means the provider reversal references a purchase
    # 518 never saw complete — an unresolved fact, not something to
    # paper over with a terminal marker.
    if row["status"] not in ("paid", "refunded", "disputed"):
        raise KeyError("reversal_without_paid_proof")
    snapshot = int(row["credits_snapshot"] or 0)
    # Economic reversal requires the EXACT original positive grant:
    # idem order:{id}, delta == credits_snapshot > 0. Missing grant,
    # non-positive delta, or a grant that disagrees with the frozen
    # purchase fact is an unprovable state — fail loud with ZERO
    # revoke and ZERO Order mutation (no "no_grant" terminal lie).
    if grant_row is None:
        raise KeyError("original_grant_missing")
    amount = int(grant_row["delta"])
    if amount <= 0:
        raise KeyError("original_grant_non_positive")
    if snapshot <= 0:
        raise KeyError("credits_snapshot_non_positive")
    if amount != snapshot:
        # grant vs frozen purchase fact disagree — fail loud, never guess
        raise KeyError("grant_snapshot_mismatch")
    from economy.billing import revoke
    from repo import ledger as ledger_repo
    with db() as con:
        prior = ledger_repo.by_idem_key(con, int(row["user_id"]),
                                        f"payment-reversal:{txn_id}")
    revoke(int(row["user_id"]), amount,
           ref_type="creem_transaction", ref_id=txn_id,
           idem_key=f"payment-reversal:{txn_id}",
           note=f"package refund order:{row['id']} -{amount}")
    with db() as con:
        # terminal state settles on every arrival: first reversal writes
        # refunded/disputed; a same-txn replay (idem hit in revoke) still
        # converges the order state — dispute after refund flips the
        # terminal marker without a second economic movement.
        _terminal = mark_disputed if _is_dispute else mark_refunded
        _terminal(con, int(row["id"]), now())
    first_time = prior is None
    return {"reversed": first_time, "credits": -amount if first_time else 0,
            "kind": "package"}


def _reverse_subscription(txn: dict) -> dict:
    """recurring invoice: reverse the exact grants linked to this txn."""
    txn_id = str(txn.get("id") or "")
    sub_id = str(txn.get("subscription") or "")
    from economy.subscription import _parse_ts
    period_start = _parse_ts(txn.get("period_start"))
    period_end = _parse_ts(txn.get("period_end"))
    if not sub_id or not period_start:
        raise KeyError("subscription_ref_missing")
    if not period_end or period_end <= period_start:
        # incomplete/disordered period proof — fail closed, zero mutation
        raise KeyError("subscription_period_proof_invalid")

    # locate the ORIGINAL user via the subscription's plan order — never
    # via current Membership (a historical period may no longer be the
    # user's current subscription)
    with db() as con:
        order = orders_repo.plan_order_by_sub_id(con, sub_id)
    if order is None:
        raise KeyError("subscription_order_not_found")
    user_id = int(order["user_id"])

    # exact grants for THIS transaction: kind ∈ (grant_plan,
    # grant_plan_upgrade) AND ref_type='creem_transaction' AND ref_id=txn
    from repo import ledger as ledger_repo
    with db() as con:
        rows = ledger_repo.subscription_grants_by_transaction(con, user_id,
                                                              txn_id)
    total = sum(int(r["delta"]) for r in rows) if rows else 0
    # Which payment is this? Decided by WHO FUNDED THE PERIOD'S BASE
    # CYCLE — never by whether THIS transaction's own grant landed (its
    # webhook may have been lost):
    #   the period's base grant (idem plan:{uid}:{ps}) exists and was
    #   paid by a DIFFERENT transaction → this txn is an upgrade/proration
    #   charge: reverse only what it minted (possibly zero when its
    #   callback was lost) and NEVER end the base-paid period;
    #   the base grant carries THIS txn's ref (or the period has no base
    #   funding from anyone else) → the base cycle payment itself:
    #   a reversal of the CURRENT paid period ends its validity.
    with db() as con:
        base = ledger_repo.by_idem_key(
            con, user_id, f"plan:{user_id}:{period_start}")
    base_ref = (base or {}).get("ref_id") if base else None
    is_base_payment = base_ref is None or str(base_ref) == txn_id
    if not is_base_payment:
        # upgrade charge (identified by the period's base-payment fact,
        # not this txn's grants): revoke only what it minted — with a
        # LOST upgrade callback that is zero (nothing was ever granted
        # for this transaction) and no revoke happens at all.
        if total > 0:
            from economy.billing import revoke as _revoke_upgrade
            _revoke_upgrade(user_id, total,
                            ref_type="creem_transaction", ref_id=txn_id,
                            idem_key=f"payment-reversal:{txn_id}",
                            note=f"subscription upgrade reversal sub:{sub_id} -{total}")
        return {"reversed": total > 0, "credits": -total if total > 0 else 0,
                "kind": "upgrade"}
    if total <= 0:
        # This transaction never minted Credits (a legal zero-credit
        # Plan, or already reversed and replayed). ZERO revoke happens —
        # but a reversal of the CURRENT paid period still ends the
        # membership's paid validity: the economic fact is the payment
        # transaction, not the (possibly zero) credits movement.
        _expire_if_current_period(user_id, sub_id, period_start, txn)
        return {"reversed": False, "credits": 0, "reason": "no_grant"}

    from economy.billing import revoke
    from repo import ledger as ledger_repo
    with db() as con:
        prior = ledger_repo.by_idem_key(con, user_id,
                                        f"payment-reversal:{txn_id}")
    revoke(user_id, total,
           ref_type="creem_transaction", ref_id=txn_id,
           idem_key=f"payment-reversal:{txn_id}",
           note=f"subscription reversal sub:{sub_id} -{total}")
    _expire_if_current_period(user_id, sub_id, period_start, txn)
    first_time = prior is None
    return {"reversed": first_time, "credits": -total if first_time else 0,
            "kind": "subscription"}


def _expire_if_current_period(user_id: int, sub_id: str,
                              period_start: int, txn: dict) -> None:
    """Current paid period reversal → Membership expires NOW.

    Current period iff Membership.provider_subscription_id == sub:{id}
    AND last_grant_period_start == txn.period_start. A historical
    period reversal leaves the current Membership untouched.
    """
    from economy.subscription import _parse_ts as _pts
    from economy.subscription import membership_is_effective
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None or int(m["user_id"]) != user_id:
            return
        if int(m["last_grant_period_start"] or 0) != period_start:
            return  # historical period — current access unaffected
        if not membership_is_effective(m["status"], m["renews_at"], now()):
            return
        # economic validity of the current paid period is gone
        orders_repo.upsert_membership_state(
            con, sub_id, "expired",
            started_at=m["started_at"], renews_at=now(),
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    log.info("reverse: current-period payment reversed → membership "
             "sub:%s expired immediately", sub_id)
