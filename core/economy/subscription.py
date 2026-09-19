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


def bind_checkout(obj: dict, order_id: int | None = None) -> dict:
    """checkout.completed (subscription first purchase), ONE DB
    transaction: lock the exact plan order → guarded pending/failed→paid
    with provider_order_id='sub:<id>' → bind Membership if missing →
    COMMIT. A Membership failure rolls the order status AND identity
    back together — 'order sub-bound but pending' or 'Membership bound
    but order failed' cannot exist. Credit is NEVER granted here
    (paid-only). Replay (paid + same sub + membership) is idempotent.
    The order is located via the SIGNED request_id when provided; the
    ch:/sub: lookups remain only for replay convergence of historical
    sequences."""
    sub = obj.get("subscription") or {}
    sub_id = str(sub.get("id") or "")
    ch_id = str(obj.get("id") or "")
    from database import db as _db
    from repo.orders import complete_plan_checkout
    if order_id is not None:
        # request_id path (canonical): one transaction end to end
        with _db() as con:
            row = con.execute(
                "SELECT * FROM orders WHERE id=%s FOR UPDATE",
                (order_id,)).fetchone()
            if row is None:
                raise KeyError("order_not_found")
            if row["kind"] != "plan":
                return {"ignored": True}
            # ── checkout payment-contract proof ──
            # The signed completion must match the local purchase fact:
            # the plan row's CURRENT product/price is authoritative here
            # (first checkout: rebind is refused while any membership
            # could exist — and this plan has none yet — so live == the
            # fact frozen at checkout). Mismatched amount/product is a
            # provider-vs-local divergence, never a silent bind.
            _plan = orders_repo.plan_by_id(con, int(row["item_id"]))
            if _plan is None:
                raise KeyError("plan_not_found")
            _w_order = obj.get("order") or {}
            _w_amount = int(_w_order.get("amount") or 0)
            _w_product = str(_w_order.get("product") or "")
            if _w_amount != int(row["amount_cents"]):
                raise KeyError("checkout_amount_mismatch")
            if _plan.get("creem_product_id") and _w_product \
                    and _w_product != _plan["creem_product_id"]:
                raise KeyError("checkout_product_mismatch")
            cur = row["provider_order_id"] or ""
            # identity validation: NULL / ch:<this checkout> / sub:<this sub>
            if cur and cur != f"sub:{sub_id}" and cur != f"ch:{ch_id}":
                raise KeyError("provider_identity_conflict")
            complete_plan_checkout(con, int(row["id"]), f"sub:{sub_id}", now())
            if orders_repo.membership_by_sub(con, sub_id) is None:
                orders_repo.bind_membership(
                    con, row["user_id"], row["item_id"], sub_id, "active",
                    started_at=row["created_at"], renews_at=None)
        return _ev_sync(sub_id, obj)
    with _db() as con:
        row = orders_repo.order_by_provider_id(con, "ch:" + ch_id)
        if row is None:
            # 重放: 绑定已完成 (ch: 已被覆写为 sub:) → 幂等; membership 若
            # 仍缺失 (旧数据/迁移前序列) 则此刻补建, 不再 500 重试环
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
    # 首购建档: memberships 单行 (user 唯一); 已有 current membership 的
    # 二次首购路径由 /subscribe 入口拦截 (plan_change_required), 正常
    # 不会到达这里; 到达则 PK 冲突 fail loud (不变量兜底)。
    sub = obj.get("subscription") or {}
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
        "subscription.update": _ev_update,
    }.get(event_type)
    if handler is None:
        # refund.created / dispute.created reach here via topup logging path
        log.warning("subscription event (no handler): %s sub=%s", event_type, sub_id)
        return {"ignored": True}
    return handler(sub_id, obj)


# ── per-event handlers ──

def schedule_downgrade(user_id: int, target_plan_id: int) -> dict:
    """Pro → Plus next renewal: 只记录 pending fact (effective_at = 当前
    renews_at), 不改当前 Membership/entitlements; 周期边界由下一周期
    paid/reconcile 应用。"""
    with db() as con:
        m = orders_repo.membership_row(con, user_id)
        if m is None:
            raise KeyError("membership_not_found")
        # effective_at 的唯一合法来源是当前 paid 周期边界; renews_at 缺失
        # (bind 未 paid) 不允许排期 — 绝不落 effective_at=0。
        if m["renews_at"] is None:
            raise ValueError("renews_at_missing")
        orders_repo.set_pending_downgrade(
            con, user_id, target_plan_id, int(m["renews_at"]))
    return {"scheduled": True, "effective_at": int(m["renews_at"])}


def membership_is_effective(status: str, renews_at: int | None, ts: int) -> bool:
    """effective/current paid predicate — plan-change eligibility 单点。

    与 plan_of().member 同一定义: status ∈ (active, canceled, past_due)
    且 renews_at 非空且 > ts。occupied 但非 effective (active+renews None)
    → 占槽 (不可二次 checkout) 但不可升降级、不算会员权益。"""
    if status not in ("active", "canceled", "past_due"):
        return False
    return renews_at is not None and renews_at > ts


def membership_is_occupied(status: str, renews_at: int | None, ts: int) -> bool:
    """binding-slot 单一语义 (duplicate-checkout 防线专用; plan-change
    eligibility 必须用 membership_is_effective):

    occupied = status ∈ (active, canceled, past_due) 且
               (renews_at is None 或 renews_at > ts)
    definitively non-live = expired, 或 live-state 但 renews_at ≤ ts
    (bind 后未 paid 的 active+renews None 同样占用)。
    """
    if status not in ("active", "canceled", "past_due"):
        return False
    return renews_at is None or renews_at > ts


def extract_product_id(sub: dict) -> str:
    """Normalize the two current Creem subscription product representations
    used by webhook/API sources:
        sub["product"] = {"id": "prod_x"}  → "prod_x"
        sub["product"] = "prod_x"          → "prod_x"
    Missing/other shapes → "" (caller fails loud)."""
    product = sub.get("product")
    if isinstance(product, dict):
        return str(product.get("id") or "")
    if isinstance(product, str):
        return product
    return ""


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
    """Provider subscription → (order, plan, membership) 定位。

    Membership 定位以 memberships.provider_subscription_id 为权威
    (不再从原始 Order.item_id 推断当前 plan); checkout.completed 绑定
    尚未落地时 KeyError (retriable 500)。
    """
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is not None:
            order, plan = _resolve_subscription(con, sub_id)
            if order is None:
                raise KeyError("order_not_found")
            plan = orders_repo.plan_by_id(con, m["plan_id"])
            return dict(order), dict(plan), dict(m)
        # current webhook/order race path: 事件早于 membership 行
        # (首购序列由 bind_checkout 建立)
        order, plan = _resolve_subscription(con, sub_id)
        if order is None or plan is None:
            raise KeyError("order_not_found")
        return dict(order), dict(plan), None


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
    # paid proof identity — grants MUST carry the paid transaction ref
    txn_ref = str(sub.get("last_transaction_id") or "")
    if not txn_ref:
        raise KeyError("paid_proof_missing:last_transaction_id")
    order, plan, current = _bind_and_load(sub_id, provider_subscription)
    # ── reversed-period gate ──
    # A durable economic reversal (revoke, idem payment-reversal:{txn})
    # of a payment that funded THIS period invalidates its paid validity:
    # an old subscription.paid replay must grant zero and leave an
    # expired Membership expired. Scope is the PERIOD only — a later,
    # independently paid new period grants normally.
    with db() as con:
        if orders_repo.period_has_payment_reversal(
                con, int(order["user_id"]), period_start):
            raise KeyError("paid_period_reversed")
    ts = now()
    status = "active" if period_end > ts else "expired"
    # ── paid product identity proof ──
    # A provider-confirmed paid period carries a product; that product
    # must map to a canonical Plan AND be consistent with the local
    # subscription state — never grant a plan the provider did not bill:
    #   current Membership exists → provider product must equal the
    #     Membership plan's product (a higher-tier product before the
    #     subscription.update sync fails loud for replay after sync;
    #     pending-downgrade handling below is the only switch path)
    #   no Membership yet (first period, order-located) → provider
    #     product must equal the original order's plan product
    #   unknown product (no canonical mapping) → fail loud, zero grant
    provider_product = extract_product_id(sub)
    if not provider_product:
        raise KeyError("paid_product_missing")
    if current is not None:
        with db() as con:
            cur_plan = orders_repo.plan_by_id(con, current["plan_id"])
        if cur_plan is None or provider_product != cur_plan.get("creem_product_id"):
            from governance import plan_catalog
            mapped = plan_catalog.by_product(provider_product)
            if mapped is None:
                raise KeyError("paid_product_unknown")
            # known but different tier — downgrade recovery is adjudicated
            # below (pending / next-period lower tier); anything else
            # (including a higher tier before subscription.update sync)
            # fails loud for a later replay after Membership converges.
            direction = plan_catalog.direction(
                mapped, plan_catalog.by_code(cur_plan["code"])) if cur_plan else 0
            if direction >= 0:
                raise KeyError("paid_product_not_synced")
    else:
        # first paid period located via the original order
        if not plan.get("creem_product_id") \
                or provider_product != plan["creem_product_id"]:
            raise KeyError("paid_product_order_mismatch")
    credits = 0
    with db() as con:
        credits = orders_repo.plan_credits(con, plan["id"])
    # downgrade 周期边界 — provider paid snapshot 是最终权威:
    #   a) pending due + provider product == target → 切换 + grant target 周期
    #   b) pending due + provider product != target → fail loud, 不切不清
    #   c) pending 缺失 (route crash / update 未消费) + 新周期
    #      (period_start >= 旧 renews_at) + provider product 是 canonical
    #      下行 tier → provider snapshot 自身即权威证明, 恢复连续性事实:
    #      Membership → target, grant target 周期 credits (Plus 100, 非 500)。
    #      pending 表不凌驾 provider paid snapshot。
    target_plan_id = None
    with db() as con:
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
            from governance import plan_catalog
            by_product_tier = plan_catalog.by_product(provider_product)
            if by_product_tier is not None:
                direction = plan_catalog.direction(
                    by_product_tier, plan_catalog.by_code(cur_plan["code"]))
                old_renews = int(m_cur["renews_at"] or 0)
                if direction < 0 and old_renews and period_start >= old_renews:
                    target_plan_id = int(by_product_tier.id)
    if target_plan_id is not None:
        with db() as con:
            plan = orders_repo.plan_by_id(con, target_plan_id)
            credits = orders_repo.plan_credits(con, target_plan_id)
    # 周期 plan-derived credits 语义: 本 billing period 已获得的 plan credits
    # (grant_plan + grant_plan_upgrade, 按 idem 前缀聚合) 补齐到当前 plan
    # 的 canonical 值 — 升级补差后再收到本周期 paid 重放/新周期判断时,
    # 总额始终收敛到该周期应得值, 绝不重复全额。
    # REPLAY STABILITY: once this period carries granted facts
    # (already > 0) the LIVE plan.monthly_credits is no longer an
    # authority for it — a later operator credit edit must never make a
    # same-transaction replay mint the difference. Only a verified
    # switch to a genuinely different plan (target_plan_id above) may
    # change the period's total; otherwise plan_total freezes at the
    # already-granted amount.
    with db() as con:
        already = orders_repo.period_plan_credits(con, order["user_id"], period_start)
    # REPLAY vs SWITCH, decided by the paid proof itself:
    #   replay — THIS transaction already funded a grant this period
    #     (its idem/base row exists): the live plan.monthly_credits is
    #     no longer an authority for the period; freeze at the granted
    #     amount so a later operator credit edit can never mint the
    #     difference on a same-transaction replay.
    #   switch — a NEW transaction under a genuinely different plan
    #     (upgrade top-up path): the top-up to the new plan's total is
    #     legitimate and follows the existing grant_plan_upgrade shape.
    _is_replay = False
    if already > 0 and target_plan_id is None:
        with db() as con:
            _seen = con.execute(
                "SELECT 1 FROM ledger WHERE user_id=%s AND kind IN"
                " ('grant_plan','grant_plan_upgrade') AND ref_id=%s LIMIT 1",
                (order["user_id"], txn_ref)).fetchone()
        _is_replay = _seen is not None
    if _is_replay:
        plan_total = already
    else:
        plan_total = credits
    if plan_total > already and plan_total > 0:
        # already == 0 → base cycle grant; already > 0 → the period was
        # partially credited (e.g. mid-cycle upgrade) → top-up the exact
        # difference as grant_plan_upgrade. Both kinds carry the paid
        # transaction ref (ref_type=creem_transaction) — the single fact
        # a later refund/chargeback reverses. Base idem plan:{uid}:{ps};
        # upgrade idem plan-upgrade:{uid}:{ps}:{plan} (two distinct idem
        # shapes → a base row never swallows an upgrade top-up).
        from economy.billing import grant
        if already > 0:
            grant(order["user_id"], plan_total - already,
                  "grant_plan_upgrade",
                  ref_type="creem_transaction", ref_id=txn_ref,
                  idem_key=f"plan-upgrade:{order['user_id']}:{period_start}:{plan['code']}",
                  note=f"{plan['code']} upgrade top-up {period_start}-{period_end}")
        else:
            grant(order["user_id"], plan_total, "grant_plan",
                  ref_type="creem_transaction", ref_id=txn_ref,
                  idem_key=f"plan:{order['user_id']}:{period_start}",
                  note=f"{plan['code']} cycle {period_start}-{period_end}")
    cancel_ts = None
    if scheduled_cancel:
        cancel_ts = ts
    elif current and current.get("cancel_scheduled_at"):
        cancel_ts = int(current["cancel_scheduled_at"])  # 保留既有 marker
    started = (current or {}).get("started_at") or order["created_at"]
    with db() as con:
        orders_repo.upsert_membership_state(
            con, sub_id, status,
            started_at=started, renews_at=period_end,
            plan_id=plan["id"],
            period_start=period_start, cancel_scheduled_at=cancel_ts)
    return {"granted": True, "credits": credits, "status": status}


def _ev_paid(sub_id: str, obj: dict) -> dict:
    """Webhook paid event — provider direct successful-payment proof.

    The signed subscription.paid event IS the provider's proof, but it
    must still carry the proof fields: subscription id, current period
    start/end, product identity and last_transaction_id. A payload
    missing proof fields fails loud (KeyError → 500 → Creem retries);
    an active status alone NEVER substitutes for proof.
    """
    sub = obj.get("subscription") or obj
    missing = []
    if not sub_id:
        missing.append("subscription_id")
    if not _parse_ts(sub.get("current_period_start_date")):
        missing.append("period_start")
    if not _parse_ts(sub.get("current_period_end_date")):
        missing.append("period_end")
    if not extract_product_id(sub):
        missing.append("product")
    if not str(sub.get("last_transaction_id") or ""):
        missing.append("last_transaction_id")
    if missing:
        raise KeyError("paid_proof_missing:" + ",".join(missing))
    return apply_paid_period(sub_id, obj)


def _ev_update(sub_id: str, obj: dict) -> dict:
    """subscription.update: provider product 变化 → 同步 current Membership.plan。

    provider payload 的 product/product_id → 本地唯一 plan (creem_product_id
    绑定); 无唯一本地映射 → KeyError fail loud (retriable), 绝不猜 plan、
    不回落原始 order.item_id。plan change 后 plan_of()/projection/
    entitlements 自然跟随新 tier。
    """
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
            from governance import plan_catalog
            cur_plan = orders_repo.plan_by_id(con, m["plan_id"])
            direction = plan_catalog.direction(
                plan_catalog.by_code(plan["code"]),
                plan_catalog.by_code(cur_plan["code"])) if cur_plan else 1
            if direction > 0:
                # upgrade (Plus→Pro): Membership/Product sync ONLY.
                # Zero Credits here — upgrade 补差授权已收进唯一
                # apply_paid_period(): 只有 provider 的成功支付证明
                # (subscription.paid 的 paid transaction) 才产生钱。
                # Creem upgrade 的 proration-charge-immediately 会触发
                # 新 paid event → replay 经 apply_paid_period 补差。
                orders_repo.clear_pending_downgrade(con, m["user_id"])
                orders_repo.upsert_membership_state(
                    con, sub_id, m["status"],
                    started_at=m["started_at"], renews_at=m["renews_at"],
                    plan_id=plan["id"],
                    period_start=m["last_grant_period_start"],
                    cancel_scheduled_at=m["cancel_scheduled_at"])
                return {"updated": True, "plan": plan["code"]}
            if direction < 0:
                # downgrade (Pro→Plus): 方向由 canonical tier order 判定,
                # 与 pending row 是否已写入无关 (webhook-before-pending race
                # 免疫)。provider Product 已切是事实, 但当前 Pro 周期已付 →
                # Membership/Entitlements 保持 Pro 到 period_end; 自建/幂等
                # 收敛同一 pending fact (route 的 schedule_downgrade 之后
                # 到达时为同值 upsert)。
                orders_repo.set_pending_downgrade(
                    con, m["user_id"], plan["id"], int(m["renews_at"] or 0))
                return {"updated": False, "deferred": "pending_downgrade",
                        "pending_target": plan["code"]}
        # 同 plan 的 update: 状态/周期字段同步 (无 tier 变化)
    return {"updated": True, "plan": plan["code"]}


def _ev_scheduled_cancel(sub_id: str, obj: dict) -> dict:
    """End-of-period cancellation booked — membership stays active till renews_at."""
    sub = obj.get("subscription") or obj
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id)
        if m is None:
            raise KeyError("membership_not_found")
        started = m["started_at"]
        renews = _parse_ts(sub.get("current_period_end_date")) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, m["status"],
            started_at=started, renews_at=renews,
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=now())
    return {"scheduled_cancel": True}


def _period_end_of(obj: dict, sub_id: str) -> int | None:
    """payload 的当期周期 end; 缺失时回落到库中现值 (规范四: 当期权益到 period_end)。"""
    sub = obj.get("subscription") or obj
    return _parse_ts(sub.get("current_period_end_date"))


def _ev_canceled(sub_id: str, obj: dict) -> dict:
    """当期权益保留至 period_end (规范四) — 不抹 renews_at。"""
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
        # 宽限: 权益保留至原 renews_at (不抹) — 补扣成功由 paid 推进周期
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
    """Boot/daily fallback: expire memberships whose renews_at passed.
    Status transition only — credits are NEVER clawed back here."""
    ts = now()
    with db() as con:
        n = orders_repo.sweep_expired_plans(con, ts)
    if n:
        log.info("subscription sweep: %d membership(s) expired", n)
    return n


def plan_of(user_id: int) -> dict | None:
    """Identity single point (badge / API / backoffice all read here).

    memberships.user_id PK 保证至多一行 — 无多行选择逻辑。member 语义
    不变: 规范四 (canceled 当期内/past_due 宽限期保留至 renews_at)。
    """
    with db() as con:
        row = orders_repo.membership_row(con, user_id)
    if row is None:
        return None
    d = dict(row)
    ts = now()
    live = membership_is_effective(d["status"], d["renews_at"], ts)
    return {
        "code": d["code"], "name": d["name"],
        "badge": d["name"].upper(),  # header 徽标文案 (行 name 派生, 数据驱动)
        "status": d["status"],
        "renews_at": d["renews_at"],
        "cancel_scheduled_at": d["cancel_scheduled_at"],
        "member": live,
    }
