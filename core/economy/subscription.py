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


def _require_checkout_proof(obj: dict, row: dict, plan: dict) -> None:
    """Frozen-contract proof for subscription FIRST checkout completion.
    EVERY completion path validates the same predicate BEFORE any
    Order/Membership mutation; a missing required field is a failure,
    never a skip-the-check. Required:
      checkout id, subscription id, provider order id, provider order
      status == paid, provider order type == recurring, amount ==
      frozen orders.amount_cents, currency == frozen orders.currency,
      product == the Plan's immutable creem_product_id."""
    sub = obj.get("subscription") or {}
    sub_id = str(sub.get("id") or "")
    ch_id = str(obj.get("id") or "")
    w_order = obj.get("order") or {}
    p_order_id = str(w_order.get("id") or "")
    p_status = str(w_order.get("status") or "")
    p_type = str(w_order.get("type") or "")
    amount = int(w_order.get("amount") or 0)
    product = str(w_order.get("product") or "")
    currency = str(w_order.get("currency") or "")
    if not ch_id.strip():
        raise KeyError("checkout_id_missing")
    if not sub_id.strip():
        raise KeyError("subscription_id_missing")
    if not p_order_id.strip():
        raise KeyError("provider_order_id_missing")
    if p_status != "paid":
        raise KeyError("checkout_not_paid")
    if p_type != "recurring":
        raise KeyError("checkout_not_recurring")
    if amount != int(row["amount_cents"]):
        raise KeyError("checkout_amount_mismatch")
    if not currency.strip():
        raise KeyError("currency_missing")
    if currency.upper() != (row["currency"] or "").upper():
        raise KeyError("checkout_currency_mismatch")
    if not product.strip():
        raise KeyError("product_missing")
    plan_product = plan.get("creem_product_id") if plan else None
    if not plan_product or product != plan_product:
        raise KeyError("checkout_product_mismatch")


def bind_checkout(obj: dict, order_id: int) -> dict:
    """checkout.completed (subscription first purchase), ONE DB
    transaction: lock the exact plan order → guarded pending/failed→paid
    with provider_order_id='sub:<id>' → bind Membership if missing →
    COMMIT. A Membership failure rolls the order status AND identity
    back together — 'order sub-bound but pending' or 'Membership bound
    but order failed' cannot exist. Credit is NEVER granted here
    (paid-only). Replay (paid + same sub + membership) is idempotent.
    The order is located via the SIGNED request_id."""
    sub = obj.get("subscription") or {}
    sub_id = str(sub.get("id") or "")
    ch_id = str(obj.get("id") or "")
    from database import db as _db
    from repo.orders import complete_plan_checkout
    # request_id path (canonical): one transaction end to end
    with _db() as con:
        row = orders_repo.order_by_id_for_update(con, order_id)
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
        _require_checkout_proof(obj, row, _plan)
        cur = row["provider_order_id"] or ""
        # identity validation: NULL / ch:<this checkout> / sub:<this sub>
        if cur and cur != f"sub:{sub_id}" and cur != f"ch:{ch_id}":
            raise KeyError("provider_identity_conflict")
        # Membership slot guard — the SAME occupancy predicate the
        # subscribe checkout gate uses (membership_is_occupied):
        #   bound to a DIFFERENT subscription AND occupied (live or
        #     bound-but-unpaid) → never replaced (fail closed);
        #   bound to a different subscription but definitively non-live
        #     (expired / lapsed past renews_at) → this completed first
        #     checkout rebinds the slot (bind_membership rebind path).
        # Only a not-yet-completed order may rebind: a replay of an
        # already-paid checkout never moves the slot back to its
        # (older) subscription.
        _occ = orders_repo.membership_row(con, int(row["user_id"]))
        _rebind = False
        if _occ is not None and _occ["provider_subscription_id"] != f"sub:{sub_id}":
            if row["status"] not in ("pending", "failed") \
                    or membership_is_occupied(_occ["status"],
                                              _occ["renews_at"], now()):
                raise KeyError("membership_occupied")
            _rebind = True
        complete_plan_checkout(con, int(row["id"]), f"sub:{sub_id}", now())
        if orders_repo.membership_by_sub(con, sub_id) is None:
            if _rebind:
                # a scheduled downgrade belongs to the dead subscription;
                # it must not gate the new subscription's first period
                orders_repo.clear_pending_downgrade(con, int(row["user_id"]))
            orders_repo.bind_membership(
                con, row["user_id"], row["item_id"], sub_id, "active",
                started_at=row["created_at"], renews_at=None)
    return {"bound": True, "subscription_id": sub_id}


def handle_event(event_type: str, obj: dict) -> dict:
    """Dispatch a Creem subscription-family event. Returns a small dict for
    the webhook response; raises KeyError when binding is missing (retriable)."""
    sub = obj.get("subscription") or obj
    sub_id = str(sub.get("id") or "")
    handler = {
        "subscription.paid": _ev_paid,
        # subscription.active carries no state the local machine does not
        # already own: status/renews_at/plan come from subscription.paid and
        # the dedicated lifecycle events. The former no-op "sync" handler
        # was deleted — the event is acknowledged as ignored (logged).
        "subscription.active": _ev_ignored,
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
    if target_plan_id is None:
        # Same-transaction replay lookup is EXACT: grants carry
        # ref_type='creem_transaction' — a row under any other ref_type
        # sharing this ref_id must never be mistaken for this paid
        # transaction's grant. 判定 = repo primitive rows 非空与否
        # (存在性/business decision 留 economy 层)。
        # ZERO-CREDIT PLANS have no grant rows for the base cycle —
        # "this transaction was already processed" then lives in the
        # durable period base-payment record (base_txn_id), NOT in a
        # positive-grant precondition: a later operator credit raise
        # must not let an old payment replay mint the difference.
        from repo import ledger as ledger_repo
        with db() as con:
            _seen_rows = ledger_repo.subscription_grants_by_transaction(
                con, order["user_id"], txn_ref)
            _base = orders_repo.period_base_payment(
                con, sub_id, period_start)
        _is_replay = bool(_seen_rows) or (
            _base is not None and str(_base["base_txn_id"]) == txn_ref)
    if _is_replay:
        plan_total = already
    else:
        plan_total = credits
    _moved = 0
    if plan_total > already and plan_total > 0:
        # already == 0 → base cycle grant; already > 0 → the period was
        # partially credited (e.g. mid-cycle upgrade) → top-up the exact
        # difference as grant_plan_upgrade. Both kinds carry the paid
        # transaction ref (ref_type=creem_transaction) — the single fact
        # a later refund/chargeback reverses. Base idem plan:{uid}:{ps};
        # upgrade idem plan-upgrade:{uid}:{ps}:{plan}:{txn_ref} (two
        # distinct idem shapes → a base row never swallows an upgrade
        # top-up; the txn_ref keeps a second, separately-paid upgrade in
        # the same period from replaying the first one's grant row —
        # period_plan_credits aggregates by the prefix, unaffected).
        from economy.billing import grant
        if already > 0:
            _moved = plan_total - already
            grant(order["user_id"], _moved,
                  "grant_plan_upgrade",
                  ref_type="creem_transaction", ref_id=txn_ref,
                  idem_key=f"plan-upgrade:{order['user_id']}:{period_start}:{plan['code']}:{txn_ref}",
                  note=f"{plan['code']} upgrade top-up {period_start}-{period_end}")
        else:
            _moved = plan_total
            grant(order["user_id"], _moved, "grant_plan",
                  ref_type="creem_transaction", ref_id=txn_ref,
                  idem_key=f"plan:{order['user_id']}:{period_start}",
                  note=f"{plan['code']} cycle {period_start}-{period_end}")
        _granted_now = True
    else:
        _granted_now = False
    # cancel marker 预设; 持锁重读后以 locked 行为准 (scheduled_cancel
    # reconcile 语义 = 显式落 now, 否则保留既有 marker)。
    cancel_ts = ts if scheduled_cancel else None
    started = (current or {}).get("started_at") or order["created_at"]
    # ── lifecycle monotonicity ──
    # A paid event for a period the Membership has ALREADY applied
    # (period_start <= last_grant_period_start: same-period replay,
    # late/out-of-order delivery, mid-period upgrade charge) proves
    # payment only; it is not a lifecycle event. Lifecycle state written
    # after that period was applied (canceled / past_due / expired), the
    # newer period boundary AND the newer plan/cancel facts are never
    # regressed by it. 判定基准 = 最终写事务内的加锁行重读: 快照读
    # (current) 与最终写之间可能有其它事件提交 (update / expired 等),
    # 在旧快照上做分支会把已接受的新事实写回旧值 — FOR UPDATE 行与
    # 随后的 upsert 同快照串行, 读到什么就基于什么写。
    with db() as con:
        locked = orders_repo.membership_by_sub(con, sub_id, lock=True)
        renews_write, ps_write = period_end, period_start
        plan_write = plan["id"]
        _last = (locked or {}).get("last_grant_period_start")
        if locked is not None:
            if _last is not None and period_start <= int(_last):
                # already-applied period: the locked row IS the current
                # fact set — plan, status, period and cancel marker all
                # follow it (this payment proves payment only)
                plan_write = int(locked["plan_id"])
                status = locked["status"]
                ps_write = int(_last)
                if locked.get("renews_at") is not None:
                    renews_write = int(locked["renews_at"])
            elif int(locked["plan_id"]) != int(plan_write) \
                    and int(locked["plan_id"]) != int(
                        (current or {}).get("plan_id") or -1):
                # advancing period, but subscription.update accepted a
                # NEWER plan after this event's snapshot — never revert
                # it (the plan this payment verified against is gone)
                plan_write = int(locked["plan_id"])
            if not scheduled_cancel:
                # a marker booked after the snapshot must survive this
                # write — the locked row is the marker authority
                cancel_ts = locked.get("cancel_scheduled_at")
        orders_repo.upsert_membership_state(
            con, sub_id, status,
            renews_at=renews_write,
            plan_id=plan_write,
            period_start=ps_write, cancel_scheduled_at=cancel_ts)
        # durable base-payment record for THIS period — the FIRST
        # subscription.paid to arrive for (sub, period) is its base cycle
        # payment, whatever the credits arithmetic does later (a paid plan
        # with zero monthly credits mints no grant rows, so the ledger
        # cannot answer "who funded the base cycle"). Later same-period
        # charges (upgrades) and replays never overwrite (ON CONFLICT).
        orders_repo.record_period_base_payment(
            con, sub_id, period_start, int(order["user_id"]), txn_ref, ts)
    # Return the TRUE movement of THIS call: a same-transaction replay
    # (or any converged no-op) grants nothing — granted=False, credits=0
    # — even though the live Plan row says otherwise. Reconcile counts
    # and logs follow the actual movement.
    return {"granted": _granted_now, "credits": _moved, "status": status}


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
                    renews_at=m["renews_at"],
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
        else:
            # 同 plan 的 update: provider product == current Membership
            # plan. A pending downgrade whose target the provider no
            # longer carries is void (product switched back before the
            # boundary); leaving it would make the next paid period fail
            # downgrade_product_not_confirmed. Idempotent: DELETE of an
            # absent row is a no-op.
            pend = orders_repo.pending_downgrade(con, m["user_id"])
            if pend is not None and int(pend["target_plan_id"]) != int(plan["id"]):
                orders_repo.clear_pending_downgrade(con, m["user_id"])
                log.info("subscription.update sub=%s: provider product back on"
                         " current plan %s — pending downgrade to plan_id=%s"
                         " cleared", sub_id, plan["code"],
                         pend["target_plan_id"])
                return {"updated": True, "plan": plan["code"],
                        "pending_downgrade_cleared": True}
    return {"updated": True, "plan": plan["code"]}


def _stale_lifecycle(m: dict, obj: dict) -> bool:
    """payload 的周期 start 早于库中 last_grant_period_start → 迟到的
    旧周期事件 (与 apply_paid_period 按 period_start 判单调性同一口径)。
    同一周期内 end 更早的事件 (当前周期被提前终止) 不在此列, 照常生效。"""
    sub = obj.get("subscription") or obj
    event_start = _parse_ts(sub.get("current_period_start_date"))
    last = m["last_grant_period_start"]
    return (event_start is not None and last is not None
            and int(event_start) < int(last))


def _ev_scheduled_cancel(sub_id: str, obj: dict) -> dict:
    """End-of-period cancellation booked — membership stays active till renews_at."""
    sub = obj.get("subscription") or obj
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id, lock=True)
        if m is None:
            raise KeyError("membership_not_found")
        if _stale_lifecycle(m, obj):
            log.info("stale lifecycle event sub:%s (event period end before"
                     " renews_at %s) — ignored", sub_id, m["renews_at"])
            return {"stale": True}
        renews = _parse_ts(sub.get("current_period_end_date")) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, m["status"],
            renews_at=renews,
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
        m = orders_repo.membership_by_sub(con, sub_id, lock=True)
        if m is None:
            raise KeyError("membership_not_found")
        if _stale_lifecycle(m, obj):
            log.info("stale lifecycle event sub:%s (event period end before"
                     " renews_at %s) — ignored", sub_id, m["renews_at"])
            return {"stale": True}
        renews = _period_end_of(obj, sub_id) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, "canceled",
            renews_at=renews,
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    return {"status": "canceled"}


def _ev_past_due(sub_id: str, obj: dict) -> dict:
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id, lock=True)
        if m is None:
            raise KeyError("membership_not_found")
        if _stale_lifecycle(m, obj):
            log.info("stale lifecycle event sub:%s (event period end before"
                     " renews_at %s) — ignored", sub_id, m["renews_at"])
            return {"stale": True}
        # 宽限: 权益保留至原 renews_at (不抹) — 补扣成功由 paid 推进周期
        orders_repo.upsert_membership_state(
            con, sub_id, "past_due",
            renews_at=m["renews_at"],
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    return {"status": "past_due"}


def _ev_expired(sub_id: str, obj: dict) -> dict:
    with db() as con:
        m = orders_repo.membership_by_sub(con, sub_id, lock=True)
        if m is None:
            raise KeyError("membership_not_found")
        if _stale_lifecycle(m, obj):
            log.info("stale lifecycle event sub:%s (event period end before"
                     " renews_at %s) — ignored", sub_id, m["renews_at"])
            return {"stale": True}
        renews = _period_end_of(obj, sub_id) or m["renews_at"]
        orders_repo.upsert_membership_state(
            con, sub_id, "expired",
            renews_at=renews,
            period_start=m["last_grant_period_start"],
            cancel_scheduled_at=m["cancel_scheduled_at"])
    return {"status": "expired"}


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


def effective_member_ids(user_ids: list) -> set:
    """Batch read side of plan_of().member — the verified-badge predicate.

    user_ids → 当前有效会员集合。与 access projection 同一判定
    (membership_is_effective, 规范四: active/canceled/past_due 且
    renews_at > now)，一次往返；无 membership 行的 id 自然不在集合内。
    只读 facts，无缓存 — 返回后数据允许立即变化 (read model 语义)。
    """
    ids = sorted({int(u) for u in user_ids if u is not None})
    if not ids:
        return set()
    ts = now()
    with db() as con:
        rows = orders_repo.membership_effective_rows(con, ids)
    return {int(r["user_id"]) for r in rows
            if membership_is_effective(r["status"], r["renews_at"], ts)}
