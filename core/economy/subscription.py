"""
Subscription state machine (economy domain, v3 spec 2026-09-02).

Creem subscription event family -> memberships transitions + cycle credit.
Credit authority: subscription.paid ONLY (Creem docs guidance). Period
boundaries are Creem-authoritative (current_period_start/end_date in the
payload) — never locally computed. Idempotency layers:
  1. event replay   -> transitions are idempotent (same from+event -> same to)
  2. credit grant   -> idem_key plan:{user_id}:{period_start}
  3. UPDATE writes  -> SET same values (no counters, no side effects)
"""
import logging

from database import db
from repo import orders as orders_repo
from utils import now

log = logging.getLogger("flashshare")

PLAN_STATUSES = ("active", "past_due", "canceled", "expired")


def _parse_ts(val) -> int | None:
    """Creem payload dates are ISO8601 or epoch-ms — normalise to epoch s."""
    if val is None:
        return None
    import datetime
    try:
        if isinstance(val, (int, float)):
            v = int(val)
            if v != v or v in (1 << 63, -(1 << 63)):
                return None
            v = v // 1000 if v > 10**12 else v
            return v if 0 < v < 4102444800 else None
        dt = datetime.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        epoch = int(dt.timestamp())
        return epoch if 0 < epoch < 4102444800 else None
    except (ValueError, OverflowError, TypeError):
        return None


def _resolve_subscription(con, sub_id: str) -> tuple[dict | None, dict | None]:
    row = orders_repo.plan_order_by_sub_id(con, sub_id)
    if row is None:
        return None, None
    plan = orders_repo.plan_by_id(con, row["item_id"])
    return dict(row), dict(plan) if plan else None


def bind_checkout(obj: dict) -> dict:
    sub = obj.get("subscription") or {}
    sub_id = str(sub.get("id") or "")
    ch_id = str(obj.get("id") or "")
    from database import db as _db
    with _db() as con:
        row = orders_repo.order_by_provider_id(con, "ch:" + ch_id)
        if row is None:
            already = orders_repo.plan_order_by_sub_id(con, sub_id)
            if already is not None:
                if orders_repo.membership_by_sub(con, sub_id) is None:
                    orders_repo.bind_membership(
                        con, already["user_id"], already["item_id"], sub_id,
                        "active", started_at=already["created_at"], renews_at=None)
                return {"bound": True, "replay": True}
            raise KeyError("order_not_found")
        if row["kind"] != "plan":
            return {"ignored": True}
        orders_repo.set_provider_order_id(con, row["id"], "sub:" + sub_id)
    with _db() as con:
        row = orders_repo.order_by_provider_id(con, "sub:" + sub_id)
        if row is not None:
            existing = orders_repo.membership_by_sub(con, sub_id)
            if existing is None:
                orders_repo.bind_membership(
                    con, row["user_id"], row["item_id"], sub_id, "active",
                    started_at=row["created_at"], renews_at=None)
            if row["status"] == "pending":
                orders_repo.mark_subscription_checkout_completed(con, row["id"], now())
    return _ev_sync(sub_id, obj)


def handle_event(event_type: str, obj: dict) -> dict:
    sub = obj.get("subscription") or obj
    sub_id = str(sub.get("id") or "")
    handler = {
        "subscription.paid": _ev_paid,
        "subscription.active": _ev_sync,
        "subscription.scheduled_cancel": _ev_scheduled_cancel,
        "subscription.canceled": _ev_canceled,
        "subscription.past_due": _ev_past_due,
        "subscription.expired": _ev_expired,
        "subscription.trialing": _ev_ignored,
        "subscription.paused": _ev_ignored,
        "subscription.update": _ev_update,
    }.get(event_type)
    if handler is None:
        log.warning("subscription event (no handler): %s sub=%s", event_type, sub_id)
        return {"ignored": True}
    return handler(sub_id, obj)


def schedule_downgrade(user_id: int, target_plan_id: int) -> dict:
    with db() as con:
        m = orders_repo.membership_row(con, user_id)
        if m is None:
            raise KeyError("membership_not_found")
        orders_repo.set_pending_downgrade(
            con, user_id, target_plan_id, int(m["renews_at"] or 0))
    return {"scheduled": True, "effective_at": int(m["renews_at"] or 0)}


def membership_is_occupied(status: str, renews_at: int | None, ts: int) -> bool:
    if status not in ("active", "canceled", "past_due"):
        return False
    return renews_at is None or renews_at > ts


def extract_product_id(sub: dict) -> str:
    product = sub.get("product")
    if isinstance(product, dict):
        return str(product.get("id") or "")
    return str(product or sub.get("product_id") or "")


def _ev_ignored(sub_id: str, obj: dict) -> dict:
    log.info("subscription event ignored: sub=%s", sub_id)
    return {"ignored": True}


def _ev_sync(sub_id: str, obj: dict) -> dict:
    with db() as con:
        order, plan = _resolve_subscription(con, sub_id)
    if order is None:
        raise KeyError("order_not_found")
    return {"synced": True}


def _bind_and_load(sub_id: str, obj: dict) -> tuple[dict, dict, dict | None]:
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is not None:
            order, plan = _resolve_subscription(con, sub_id)
            if order is None:
                raise KeyError("order_not_found")
            plan = orders_repo.plan_by_id(con, m["plan_id"])
            return dict(order), dict(plan), dict(m)
        order, plan = _resolve_subscription(con, sub_id)
        if order is None or plan is None:
            raise KeyError("order_not_found")
        return dict(order), dict(plan), None


def apply_paid_period(sub_id: str, provider_subscription: dict, *,
                      scheduled_cancel: bool = False) -> dict:
    """Single paid-period implementation shared by webhook + reconcile."""
    sub = provider_subscription.get("subscription") or provider_subscription
    period_start = _parse_ts(sub.get("current_period_start_date"))
    period_end = _parse_ts(sub.get("current_period_end_date"))
    if not period_start or not period_end:
        raise KeyError("period_missing")
    order, plan, current = _bind_and_load(sub_id, provider_subscription)
    ts = now()
    status = "active" if period_end > ts else "expired"
    with db() as con:
        credits = orders_repo.plan_credits(con, plan["id"])

    target_plan_id = None
    with db() as con:
        provider_product = extract_product_id(
            (provider_subscription.get("subscription") or provider_subscription))
        pend = orders_repo.pending_downgrade(con, order["user_id"])
        m_cur = orders_repo.membership_row(con, order["user_id"])
        cur_plan = orders_repo.plan_by_id(con, m_cur["plan_id"]) if m_cur else None
        if pend is not None and int(pend["effective_at"] or 0) <= period_start:
            tgt = orders_repo.plan_by_id(con, int(pend["target_plan_id"]))
            if tgt is None or not tgt.get("creem_product_id") \
                    or provider_product != tgt["creem_product_id"]:
                raise KeyError("downgrade_product_not_confirmed")
            target_plan_id = int(pend["target_plan_id"])
            orders_repo.clear_pending_downgrade(con, order["user_id"])
        elif pend is None and cur_plan is not None and m_cur is not None:
            from governance.tiers import TIER_ORDER
            by_product = orders_repo.plan_by_product_id(con, provider_product) \
                if provider_product else None
            if by_product is not None:
                direction = (TIER_ORDER.get(by_product["code"], -1)
                             - TIER_ORDER.get(cur_plan["code"], -1))
                old_renews = int(m_cur["renews_at"] or 0)
                if direction < 0 and old_renews and period_start >= old_renews:
                    target_plan_id = int(by_product["id"])
    if target_plan_id is not None:
        with db() as con:
            plan = orders_repo.plan_by_id(con, target_plan_id)
            credits = orders_repo.plan_credits(con, target_plan_id)

    plan_total = int(credits or 0)
    with db() as con:
        already = orders_repo.period_plan_credits(con, order["user_id"], period_start)
    if plan_total > already:
        from economy.billing import grant
        grant(order["user_id"], plan_total - already, "grant_plan",
              ref_type="subscription", ref_id=sub_id,
              idem_key=f"plan:{order['user_id']}:{period_start}",
              note=f"{plan['code']} cycle {period_start}-{period_end}")

    cancel_ts = None
    if scheduled_cancel:
        cancel_ts = ts
    elif current and current.get("cancel_scheduled_at"):
        cancel_ts = int(current["cancel_scheduled_at"])
    started = (current or {}).get("started_at") or order["created_at"]
    with db() as con:
        orders_repo.upsert_membership_state(
            con, sub_id, status,
            started_at=started, renews_at=period_end,
            plan_id=plan["id"],
            period_start=period_start, cancel_scheduled_at=cancel_ts)
    return {"granted": True, "credits": plan_total, "status": status}


def _ev_paid(sub_id: str, obj: dict) -> dict:
    return apply_paid_period(sub_id, obj)


def _ev_update(sub_id: str, obj: dict) -> dict:
    """Provider product change -> converge current Membership plan."""
    sub = obj.get("subscription") or obj
    product = extract_product_id(sub)
    if not product:
        raise KeyError("product_missing")
    with db() as con:
        plan = orders_repo.plan_by_product_id(con, product)
        if plan is None:
            raise KeyError("plan_not_mapped")
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None:
            raise KeyError("membership_not_found")
        if m["plan_id"] != plan["id"]:
            from governance.tiers import TIER_ORDER
            cur_plan = orders_repo.plan_by_id(con, m["plan_id"])
            direction = (TIER_ORDER.get(plan["code"], -1)
                         - TIER_ORDER.get(cur_plan["code"], -1)) if cur_plan else 1
            if direction > 0:
                # Upgrade credit result is the target plan's configured period
                # total minus what this billing period has already granted.
                # This measures the user's real asset, not code-level tier constants.
                period_start = int(m["last_grant_period_start"] or 0)
                if period_start:
                    already = orders_repo.period_plan_credits(
                        con, m["user_id"], period_start)
                    target_total = int(plan["monthly_credits"] or 0)
                    adjustment = max(target_total - already, 0)
                    if adjustment:
                        from economy.billing import grant
                        grant(m["user_id"], adjustment, "grant_plan_upgrade",
                              ref_type="subscription", ref_id=sub_id,
                              idem_key=f"plan-upgrade:{m['user_id']}:{period_start}:{plan['code']}",
                              note=f"upgrade {cur_plan['code']}→{plan['code']} adj {adjustment}")
                orders_repo.clear_pending_downgrade(con, m["user_id"])
                orders_repo.upsert_membership_state(
                    con, sub_id, m["status"],
                    started_at=m["started_at"], renews_at=m["renews_at"],
                    plan_id=plan["id"],
                    period_start=m["last_grant_period_start"],
                    cancel_scheduled_at=m["cancel_scheduled_at"])
                return {"updated": True, "plan": plan["code"]}
            if direction < 0:
                orders_repo.set_pending_downgrade(
                    con, m["user_id"], plan["id"], int(m["renews_at"] or 0))
                return {"updated": False, "deferred": "pending_downgrade",
                        "pending_target": plan["code"]}
    return {"updated": True, "plan": plan["code"]}


def _ev_scheduled_cancel(sub_id: str, obj: dict) -> dict:
    sub = obj.get("subscription") or obj
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None:
            raise KeyError("membership_not_found")
        renews = _parse_ts(sub.get("current_period_end_date")) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, m["status"],
            started_at=m["started_at"], renews_at=renews,
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=now())
    return {"scheduled_cancel": True}


def _period_end_of(obj: dict, sub_id: str) -> int | None:
    sub = obj.get("subscription") or obj
    return _parse_ts(sub.get("current_period_end_date"))


def _ev_canceled(sub_id: str, obj: dict) -> dict:
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None:
            raise KeyError("membership_not_found")
        renews = _period_end_of(obj, sub_id) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, "canceled",
            started_at=m["started_at"], renews_at=renews,
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    return {"status": "canceled"}


def _ev_past_due(sub_id: str, obj: dict) -> dict:
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None:
            raise KeyError("membership_not_found")
        orders_repo.upsert_membership_state(
            con, sub_id, "past_due",
            started_at=m["started_at"], renews_at=m["renews_at"],
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    return {"status": "past_due"}


def _ev_expired(sub_id: str, obj: dict) -> dict:
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None:
            raise KeyError("membership_not_found")
        renews = _period_end_of(obj, sub_id) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, "expired",
            started_at=m["started_at"], renews_at=renews,
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    return {"status": "expired"}


def sweep_expired() -> int:
    ts = now()
    with db() as con:
        n = orders_repo.sweep_expired_plans(con, ts)
    if n:
        log.info("subscription sweep: %d membership(s) expired", n)
    return n


def plan_of(user_id: int) -> dict | None:
    with db() as con:
        row = orders_repo.membership_row(con, user_id)
    if row is None:
        return None
    d = dict(row)
    ts = now()
    live = d["status"] in ("active", "canceled", "past_due") and (d["renews_at"] or 0) > ts
    return {
        "code": d["code"], "name": d["name"], "status": d["status"],
        "renews_at": d["renews_at"],
        "cancel_scheduled_at": d["cancel_scheduled_at"],
        "member": live,
    }
