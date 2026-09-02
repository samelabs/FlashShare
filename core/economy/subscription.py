"""
Subscription state machine (economy domain, v3 spec 2026-09-02).

Creem subscription event family -> user_plans transitions + cycle credit.
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
    if isinstance(val, (int, float)):
        v = int(val)
        return v // 1000 if v > 10**12 else v
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


def _resolve_subscription(con, sub_id: str) -> tuple[dict | None, dict | None]:
    """sub id -> (order row, plan row). Binding happens at checkout.completed;
    if the event arrives before that, retry semantics apply (caller returns 500)."""
    row = orders_repo.plan_order_by_sub_id(con, sub_id)
    if row is None:
        return None, None
    plan = orders_repo.plan_by_id(con, row["item_id"])
    return dict(row), dict(plan) if plan else None


def bind_checkout(obj: dict) -> dict:
    """checkout.completed (subscription first purchase): bind sub id -> order,
    then upsert the membership row. Credit is NEVER granted here (paid-only).
    Binding commits in its own transaction; a sync failure afterwards leaves
    the binding in place so Creem retries converge."""
    sub = obj.get("subscription") or {}
    sub_id = str(sub.get("id") or "")
    ch_id = str(obj.get("id") or "")
    from database import db as _db
    with _db() as con:
        row = orders_repo.order_by_provider_id(con, "ch:" + ch_id)
        if row is None:
            raise KeyError("order_not_found")
        if row["kind"] != "plan":
            return {"ignored": True}
        orders_repo.set_provider_order_id(con, row["id"], "sub:" + sub_id)
    return _ev_sync(sub_id, obj)


def handle_event(event_type: str, obj: dict) -> dict:
    """Dispatch a Creem subscription-family event. Returns a small dict for
    the webhook response; raises KeyError when binding is missing (retriable)."""
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
        "subscription.update": _ev_ignored,
    }.get(event_type)
    if handler is None:
        # refund.created / dispute.created reach here via topup logging path
        log.warning("subscription event (no handler): %s sub=%s", event_type, sub_id)
        return {"ignored": True}
    return handler(sub_id, obj)


# ── per-event handlers ──

def _ev_ignored(sub_id: str, obj: dict) -> dict:
    log.info("subscription event ignored: sub=%s", sub_id)
    return {"ignored": True}


def _ev_sync(sub_id: str, obj: dict) -> dict:
    """subscription.active: sync only, never grants (Creem guidance)."""
    with db() as con:
        order, plan = _resolve_subscription(con, sub_id)
    if order is None:
        raise KeyError("order_not_found")
    return {"synced": True}


def _bind_and_load(sub_id: str, obj: dict) -> tuple[dict, dict, dict | None]:
    """Resolve order+plan for a sub id; raise KeyError (retriable 500) if the
    checkout.completed binding has not landed yet."""
    with db() as con:
        order, plan = _resolve_subscription(con, sub_id)
    if order is None or plan is None:
        raise KeyError("order_not_found")
    with db() as con:
        row = orders_repo.user_plan_row(con, order["user_id"], plan["id"])
    return order, plan, dict(row) if row else None


def _ev_paid(sub_id: str, obj: dict) -> dict:
    """The ONLY credit-granting path. period fields are Creem-authoritative."""
    sub = obj.get("subscription") or obj
    period_start = _parse_ts(sub.get("current_period_start_date"))
    period_end = _parse_ts(sub.get("current_period_end_date"))
    if not period_start or not period_end:
        raise KeyError("period_missing")
    order, plan, _ = _bind_and_load(sub_id, obj)
    ts = now()
    status = "active" if period_end > ts else "expired"
    with db() as con:
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], status,
            started_at=order["created_at"], renews_at=period_end, period_start=period_start)
        credits = orders_repo.plan_credits(con, plan["id"])
    if credits > 0:
        from economy.billing import grant
        grant(order["user_id"], credits, "grant_plan",
              ref_type="subscription", ref_id=sub_id,
              idem_key=f"plan:{order['user_id']}:{period_start}",
              note=f"plus cycle {period_start}-{period_end}")
    return {"granted": True, "credits": credits, "status": status}


def _ev_scheduled_cancel(sub_id: str, obj: dict) -> dict:
    """End-of-period cancellation booked — membership stays active till renews_at."""
    order, plan, current = _bind_and_load(sub_id, obj)
    ts = now()
    with db() as con:
        if current:
            orders_repo.set_cancel_scheduled(
                con, order["user_id"], plan["id"], ts)
    return {"scheduled_cancel": True}


def _ev_canceled(sub_id: str, obj: dict) -> dict:
    order, plan, _ = _bind_and_load(sub_id, obj)
    with db() as con:
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], "canceled",
            started_at=order["created_at"], renews_at=None, period_start=None)
    return {"status": "canceled"}


def _ev_past_due(sub_id: str, obj: dict) -> dict:
    order, plan, _ = _bind_and_load(sub_id, obj)
    with db() as con:
        current = orders_repo.user_plan_row(con, order["user_id"], plan["id"])
        started = current["started_at"] if current else order["created_at"]
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], "past_due",
            started_at=started, renews_at=None, period_start=None)
    return {"status": "past_due"}


def _ev_expired(sub_id: str, obj: dict) -> dict:
    order, plan, _ = _bind_and_load(sub_id, obj)
    with db() as con:
        current = orders_repo.user_plan_row(con, order["user_id"], plan["id"])
        started = current["started_at"] if current else order["created_at"]
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], "expired",
            started_at=started, renews_at=None, period_start=None)
    return {"status": "expired"}


def sweep_expired() -> int:
    """Boot/daily fallback: expire memberships whose renews_at passed.
    Status transition only — credits are NEVER clawed back here."""
    ts = now()
    with db() as con:
        n = orders_repo.sweep_expired_plans(con, ts)
    if n:
        log.info("subscription sweep: %d membership(s) expired", n)
    return n


def plan_of(user_id: int) -> dict | None:
    """Identity single point (badge / API / backoffice all read here)."""
    with db() as con:
        row = orders_repo.plan_of(con, user_id)
    if row is None:
        return None
    d = dict(row)
    ts = now()
    live = d["status"] == "active" and (d["renews_at"] or 0) > ts
    return {
        "code": d["code"], "name": d["name"], "status": d["status"],
        "renews_at": d["renews_at"],
        "cancel_scheduled_at": d["cancel_scheduled_at"],
        "member": live,
    }
