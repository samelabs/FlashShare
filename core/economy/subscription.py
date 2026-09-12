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
    """Creem payload dates are ISO8601 or epoch-ms — normalise to epoch s.
    加固 (审计🟡7): 无时区串按 UTC (幂等键不受部署机时区影响); nan/inf/超界归 None。"""
    if val is None:
        return None
    import datetime
    try:
        if isinstance(val, (int, float)):
            v = int(val)
            if v != v or v in (1 << 63, -(1 << 63)):  # nan check / i64 边界
                return None
            v = v // 1000 if v > 10**12 else v
            return v if 0 < v < 4102444800 else None  # (1970, 2100) 钳制
        dt = datetime.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        epoch = int(dt.timestamp())
        return epoch if 0 < epoch < 4102444800 else None
    except (ValueError, OverflowError, TypeError):
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
            # 重放: 绑定已完成 (ch: 已被覆写为 sub:) → 幂等 ack, 不再 500 重试环
            already = orders_repo.plan_order_by_sub_id(con, sub_id)
            if already is not None:
                return {"bound": True, "replay": True}
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


def apply_paid_period(sub_id: str, provider_subscription: dict, *,
                      scheduled_cancel: bool = False) -> dict:
    """The ONLY paid-period implementation (shared by webhook + reconcile).

    period fields are Creem-authoritative. 崩溃一致性顺序保持:
    grant(幂等, idem_key plan:{uid}:{period_start}) 落账后才能写
    last_grant_period_start (upsert) — 先写后授会在崩溃窗口里留下
    "已授未到账"的撒谎留痕。

    scheduled_cancel=True 时使用与 scheduled-cancel handler 一致的
    marker 语义 (cancel_scheduled_at=now); 否则保留既有 marker 不清除。
    """
    sub = provider_subscription.get("subscription") or provider_subscription
    period_start = _parse_ts(sub.get("current_period_start_date"))
    period_end = _parse_ts(sub.get("current_period_end_date"))
    if not period_start or not period_end:
        raise KeyError("period_missing")
    order, plan, current = _bind_and_load(sub_id, provider_subscription)
    ts = now()
    status = "active" if period_end > ts else "expired"
    credits = 0
    with db() as con:
        credits = orders_repo.plan_credits(con, plan["id"])
    if credits > 0:
        from economy.billing import grant
        grant(order["user_id"], credits, "grant_plan",
              ref_type="subscription", ref_id=sub_id,
              idem_key=f"plan:{order['user_id']}:{period_start}",
              note=f"plus cycle {period_start}-{period_end}")
    cancel_ts = None
    if scheduled_cancel:
        cancel_ts = ts
    elif current and current.get("cancel_scheduled_at"):
        cancel_ts = int(current["cancel_scheduled_at"])  # 保留既有 marker
    with db() as con:
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], status,
            started_at=order["created_at"], renews_at=period_end,
            period_start=period_start, cancel_scheduled_at=cancel_ts)
    return {"granted": True, "credits": credits, "status": status}


def _ev_paid(sub_id: str, obj: dict) -> dict:
    """Webhook paid event — delegates to the shared paid-period path."""
    return apply_paid_period(sub_id, obj)


def _ev_scheduled_cancel(sub_id: str, obj: dict) -> dict:
    """End-of-period cancellation booked — membership stays active till renews_at."""
    order, plan, current = _bind_and_load(sub_id, obj)
    ts = now()
    with db() as con:
        if current:
            orders_repo.set_cancel_scheduled(
                con, order["user_id"], plan["id"], ts)
    return {"scheduled_cancel": True}


def _period_end_of(obj: dict, sub_id: str) -> int | None:
    """payload 的当期周期 end; 缺失时回落到库中现值 (规范四: 当期权益到 period_end)。"""
    sub = obj.get("subscription") or obj
    return _parse_ts(sub.get("current_period_end_date"))


def _ev_canceled(sub_id: str, obj: dict) -> dict:
    """当期权益保留至 period_end (规范四) — 不抹 renews_at。"""
    order, plan, current = _bind_and_load(sub_id, obj)
    renews = _period_end_of(obj, sub_id) or (current["renews_at"] if current else None)
    with db() as con:
        started = current["started_at"] if current else order["created_at"]
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], "canceled",
            started_at=started, renews_at=renews, period_start=None)
    return {"status": "canceled"}


def _ev_past_due(sub_id: str, obj: dict) -> dict:
    order, plan, _ = _bind_and_load(sub_id, obj)
    with db() as con:
        current = orders_repo.user_plan_row(con, order["user_id"], plan["id"])
        started = current["started_at"] if current else order["created_at"]
        # 宽限: 权益保留至原 renews_at (不抹) — 补扣成功由 paid 推进周期
        renews = current["renews_at"] if current else None
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], "past_due",
            started_at=started, renews_at=renews, period_start=None)
    return {"status": "past_due"}


def _ev_expired(sub_id: str, obj: dict) -> dict:
    order, plan, _ = _bind_and_load(sub_id, obj)
    with db() as con:
        current = orders_repo.user_plan_row(con, order["user_id"], plan["id"])
        started = current["started_at"] if current else order["created_at"]
        renews = _period_end_of(obj, sub_id) or (current["renews_at"] if current else None)
        orders_repo.upsert_user_plan(
            con, order["user_id"], plan["id"], "expired",
            started_at=started, renews_at=renews, period_start=None)
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


class MembershipAmbiguousError(RuntimeError):
    """多行 membership 同时 live — 无法 deterministic 选择, fail closed。

    Read model 各消费面 (projection/API/Web/MCP) 必须向上传播, 不得
    谎称某个 tier; authorization (entitlements.resolve) 既有 exception →
    Free fail-safe 边界保持。candidates 只进 debug 信息, 不进 public shape。
    """


def _is_live(row: dict, ts: int) -> bool:
    # 规范四: canceled(当期结束前)/past_due(宽限期) 权益保留至 renews_at;
    # expired 永久失效。renews_at 是权威边界。
    return (row["status"] in ("active", "canceled", "past_due")
            and (row["renews_at"] or 0) > ts)


def plan_of(user_id: int) -> dict | None:
    """Identity single point (badge / API / backoffice all read here).

    Read invariant: 唯一 live → 返回; 多个 live → MembershipAmbiguousError
    (绝不 silently pick); 无 live → repo 时间排序第一行作最近历史 fact
    (member=false, 不产生 entitlement)。public shape 不变。
    """
    ts = now()
    with db() as con:
        rows = orders_repo.membership_rows(con, user_id)
    if not rows:
        return None
    dicts = [dict(r) for r in rows]
    live = [d for d in dicts if _is_live(d, ts)]
    if len(live) > 1:
        raise MembershipAmbiguousError(
            f"user {user_id} has {len(live)} live memberships: "
            f"{[(d['code'], d['plan_id']) for d in live]}")
    chosen = live[0] if live else dicts[0]
    member = bool(live)
    return {
        "code": chosen["code"], "name": chosen["name"],
        "status": chosen["status"], "renews_at": chosen["renews_at"],
        "cancel_scheduled_at": chosen["cancel_scheduled_at"],
        "member": member,
    }
