"""Top-up endpoints: Creem checkout + webhook."""
import json

from fastapi import APIRouter

router = APIRouter()

from fastapi import Request
from fastapi.responses import JSONResponse

from app.web_auth import require_session_auth as _require_auth  # 单点 (治理)
from database import db  # checkout/subscribe 事务 (审计T1: 8处 with db() 此前零导入)


@router.post("/api/topup/checkout")
async def topup_checkout(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    from economy import creem
    if not creem.creem_enabled():
        return JSONResponse({"ok": False, "error": "topup_unavailable"}, 503)
    try:
        raw = await request.json()
        package_id = int(raw.get("package_id", 0))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import package_by_id_active
    from utils import now
    with db() as con:
        pkg = package_by_id_active(con, package_id)
    # 前置合同 (本地拒绝, 不让 provider 4xx 兜底): shared Product + 固定
    # currency + custom_price 官方范围 — 全部在创建 order 之前。
    if not pkg or not creem.credits_ready():
        return JSONResponse({"ok": False, "error": "package_not_ready"}, 422)
    if (pkg["currency"] or "").upper() != creem.CREEM_CREDITS_CURRENCY:
        return JSONResponse({"ok": False, "error": "package_not_ready"}, 422)
    if not creem.custom_price_in_range(pkg["price_cents"]):
        return JSONResponse({"ok": False, "error": "package_not_ready"}, 422)
    from repo.orders import insert_order, mark_failed, set_provider_order_id
    ts = now()
    with db() as con:
        # purchase fact freeze: amount + currency + credits quantity —
        # later package edits never rewrite a checked-out order
        order = insert_order(con, user["id"], "package", pkg["id"], "creem",
                             pkg["price_cents"], pkg["currency"], ts,
                             credits_snapshot=int(pkg["credits"]))
    try:
        out = creem.create_credits_checkout(order["id"], pkg["price_cents"], user)
    except Exception:
        with db() as con:
            mark_failed(con, order["id"])
        return JSONResponse({"ok": False, "error": "checkout_failed"}, 502)
    with db() as con:
        set_provider_order_id(con, order["id"], "ch:" + out["checkout_id"])
    return out


@router.post("/api/topup/subscribe")
async def subscribe_checkout(request: Request):
    """订阅 checkout (v3 spec): plan 必须绑 creem_product_id 才可售。"""
    user, err = _require_auth(request)
    if err:
        return err
    from economy import creem
    if not creem.creem_enabled():
        return JSONResponse({"ok": False, "error": "topup_unavailable"}, 503)
    try:
        raw = await request.json()
        plan_id = int(raw.get("plan_id", 0))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import (plan_by_id_active, insert_order, set_provider_order_id,
                             membership_row, open_plan_order,
                             lock_user_for_checkout, mark_failed)
    with db() as con:
        plan = plan_by_id_active(con, plan_id)
    if not plan or not plan["creem_product_id"]:
        return JSONResponse({"ok": False, "error": "plan_not_ready"}, 422)
    # 收费前配置 gate: 可售性单点 = Tier.sellable (active+定价+绑品+权益
    # 配齐), 与 upgrade/downgrade/页面同一 authority。
    from governance import plan_catalog
    _tier = plan_catalog.by_code(plan["code"])
    if not _tier or _tier.is_free or not _tier.sellable:
        return JSONResponse({"ok": False, "error": "plan_not_ready"}, 422)
    # 一个 user 同时最多一个 open plan checkout — users 行 FOR UPDATE
    # 串行化同 user 并发 subscribe。检查与 INSERT 同一事务: pending order
    # 在锁释放前落库 → 并发 B 等锁后必然看到 A 的 pending, 不再 INSERT。
    # provider HTTP 一律在事务外 (不持 DB lock 调外部 API)。
    from utils import now as _now
    from economy.subscription import membership_is_occupied
    _ts = _now()
    with db() as con:
        lock_user_for_checkout(con, user["id"])
        current = membership_row(con, user["id"])
        if current is not None and membership_is_occupied(
                current["status"], current["renews_at"], _ts):
            return JSONResponse({
                "ok": False,
                "error": "plan_change_required",
                "current_plan": current["code"],
                "requested_plan": plan["code"],
            }, 409)
        pending = open_plan_order(con, user["id"])
        order = None
        if pending is None:
            order = insert_order(con, user["id"], "plan", plan["id"], "creem",
                                 plan["price_cents"], plan["currency"], _ts)
    # open pending checkout 收敛 (事务外):
    if order is None:
        prov_id = pending["provider_order_id"] or ""
        if not prov_id.startswith("ch:"):
            # provider id 尚未落地 → in-progress, 不建第二单
            return JSONResponse({"ok": False, "error": "subscription_in_progress"}, 409)
        try:
            co = creem.retrieve_checkout(prov_id[3:])
        except Exception:
            return JSONResponse({"ok": False, "error": "subscription_in_progress"}, 409)
        st = str(co.get("status") or "")
        if st == "completed":
            # 不建新 checkout — webhook 侧 membership 收敛
            return JSONResponse({"ok": False, "error": "subscription_in_progress"}, 409)
        if st in ("pending", "processing") and co.get("checkout_url"):
            return {"ok": True, "checkout_url": co["checkout_url"],
                    "checkout_id": prov_id[3:], "reused": True}
        # expired (或其它终态) → 终结旧 order, 走下方重新创建
        with db() as con:
            mark_failed(con, pending["id"])
        with db() as con:
            lock_user_for_checkout(con, user["id"])
            pending2 = open_plan_order(con, user["id"])
            if pending2 is not None:
                return JSONResponse({"ok": False, "error": "subscription_in_progress"}, 409)
            order = insert_order(con, user["id"], "plan", plan["id"], "creem",
                                 plan["price_cents"], plan["currency"], _ts)
    try:
        out = creem.create_subscription_checkout(order["id"], dict(plan), user)
    except Exception:
        with db() as con:
            mark_failed(con, order["id"])
        return JSONResponse({"ok": False, "error": "checkout_failed"}, 502)
    with db() as con:
        set_provider_order_id(con, order["id"], "ch:" + out["checkout_id"])
    return out


@router.post("/api/topup/plan-upgrade")
async def plan_upgrade(request: Request):
    """Plus → Pro (立即生效): 调 Creem upgrade (proration-charge-
    immediately)。本地 Membership 不伪造 — subscription.update webhook
    → product.id → pro 收敛, 升级补差 credits 由 update 事件补授。"""
    user, err = _require_auth(request)
    if err:
        return err
    from economy import creem
    if not creem.creem_enabled():
        return JSONResponse({"ok": False, "error": "topup_unavailable"}, 503)
    try:
        raw = await request.json()
        target_code = str(raw.get("plan", "")).strip().lower()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from governance import plan_catalog
    _target = plan_catalog.by_code(target_code)
    if _target is None or _target.is_free:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from utils import now as _now
    from economy.subscription import membership_is_effective
    from repo.orders import membership_row, plan_by_code
    with db() as con:
        current = membership_row(con, user["id"])
    # plan-change eligibility = effective paid (与 plan_of().member 同一定义);
    # active+renews None (bind 未 paid) 占槽但不可升降级。
    if current is None or not membership_is_effective(
            current["status"], current["renews_at"], _now()):
        return JSONResponse({"ok": False, "error": "no_active_membership"}, 409)
    cur_code = current["code"]
    if plan_catalog.direction(_target, plan_catalog.by_code(cur_code)) <= 0:
        return JSONResponse({"ok": False, "error": "not_an_upgrade"}, 409)
    # target readiness 单点 = Tier.sellable (active+定价+绑品+权益配齐);
    # 不再单独检查 active/product — 与 subscribe/页面同一 authority。
    if not _target.sellable:
        return JSONResponse({"ok": False, "error": "plan_not_ready"}, 422)
    with db() as con:
        target = plan_by_code(con, target_code)
    sub_id = current["provider_subscription_id"]
    if sub_id.startswith("sub:"):
        sub_id = sub_id[4:]
    try:
        # 升级域语义: proration-charge-immediately — product 立即 Pro,
        # prorated difference charged immediately, original billing date
        # remains (本周期 credits 由 update 事件补差 100+400=500)
        creem.upgrade_subscription(sub_id, target["creem_product_id"],
                                   update_behavior="proration-charge-immediately")
    except Exception:
        return JSONResponse({"ok": False, "error": "upgrade_failed"}, 502)
    return {"ok": True, "upgrade": "initiated",
            "from": cur_code, "to": target_code}


@router.post("/api/topup/plan-downgrade")
async def plan_downgrade(request: Request):
    """Pro → Plus (next renewal): Creem 官方 product-change endpoint 同样
    承载 downgrade — update_behavior=proration-none (官方语义: no
    proration, change takes effect next billing cycle, no immediate
    charge)。先同步 Creem; provider 成功后才写 pending_downgrades
    (application expectation / local continuity fact, 非 Payment
    authority)。Creem 失败 → ZERO pending。当前周期 Membership/entitlements
    保持 Pro; 周期边界 provider product 必须实际为 Plus 才切换。"""
    user, err = _require_auth(request)
    if err:
        return err
    from economy import creem
    if not creem.creem_enabled():
        return JSONResponse({"ok": False, "error": "topup_unavailable"}, 503)
    try:
        raw = await request.json()
        target_code = str(raw.get("plan", "")).strip().lower()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from governance import plan_catalog
    _target = plan_catalog.by_code(target_code)
    if _target is None or _target.is_free:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from utils import now as _now
    from economy.subscription import membership_is_effective, schedule_downgrade
    from repo.orders import membership_row, plan_by_code
    with db() as con:
        current = membership_row(con, user["id"])
    # plan-change eligibility = effective paid (与 plan_of().member 同一定义)
    if current is None or not membership_is_effective(
            current["status"], current["renews_at"], _now()):
        return JSONResponse({"ok": False, "error": "no_active_membership"}, 409)
    cur_code = current["code"]
    if plan_catalog.direction(_target, plan_catalog.by_code(cur_code)) >= 0:
        return JSONResponse({"ok": False, "error": "not_a_downgrade"}, 409)
    # target readiness 单点 = Tier.sellable (与 subscribe/页面同一 authority)
    if not _target.sellable:
        return JSONResponse({"ok": False, "error": "plan_not_ready"}, 422)
    with db() as con:
        target = plan_by_code(con, target_code)
    sub_id = current["provider_subscription_id"]
    if sub_id.startswith("sub:"):
        sub_id = sub_id[4:]
    try:
        creem.upgrade_subscription(sub_id, target["creem_product_id"],
                                   update_behavior="proration-none")
    except Exception:
        return JSONResponse({"ok": False, "error": "downgrade_failed"}, 502)
    schedule_downgrade(user["id"], target["id"])
    return {"ok": True, "downgrade": "scheduled",
            "from": cur_code, "to": target_code,
            "effective_at": current["renews_at"]}


@router.post("/api/topup/portal")
async def subscription_portal(request: Request):
    """Self-service subscription management (cancel / payment method /
    invoices) via the Creem customer portal of the CURRENT Membership's
    provider subscription. Local Membership state still converges only
    through the subscription.* webhooks."""
    user, err = _require_auth(request)
    if err:
        return err
    from economy import creem
    if not creem.creem_enabled():
        return JSONResponse({"ok": False, "error": "topup_unavailable"}, 503)
    from repo.orders import membership_row
    with db() as con:
        current = membership_row(con, user["id"])
    sub_ref = (current or {}).get("provider_subscription_id") or ""
    if not sub_ref.startswith("sub:"):
        return JSONResponse({"ok": False, "error": "no_subscription"}, 409)
    import asyncio as _asyncio
    from economy.reconcile import fetch_subscription
    sub = await _asyncio.to_thread(fetch_subscription, sub_ref[4:])
    cust = (sub or {}).get("customer")
    customer_id = str((cust.get("id") if isinstance(cust, dict) else cust) or "")
    if not customer_id:
        return JSONResponse({"ok": False, "error": "portal_unavailable"}, 502)
    try:
        url = await _asyncio.to_thread(creem.customer_portal_link, customer_id)
    except Exception:
        return JSONResponse({"ok": False, "error": "portal_unavailable"}, 502)
    return {"ok": True, "url": url}


MAX_WEBHOOK_BODY = 262144  # 256KB — Creem 事件远小于此; 挡匿名内存放大 (审计🟡9)


@router.post("/webhooks/creem")
async def creem_webhook(request: Request):
    from economy import creem
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > MAX_WEBHOOK_BODY:
        return JSONResponse({"ok": False, "error": "payload_too_large"}, 413)
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BODY:
        return JSONResponse({"ok": False, "error": "payload_too_large"}, 413)
    sig = request.headers.get("creem-signature", "")
    if not creem.verify_signature(raw, sig):
        return JSONResponse({"ok": False, "error": "bad_signature"}, 401)
    try:
        event = json.loads(raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, 400)
    event_type = str(event.get("eventType") or "")
    obj = event.get("object") or {}

    # ── subscription family → economy.subscription state machine ──
    if event_type.startswith("subscription."):
        from economy import subscription
        try:
            res = subscription.handle_event(event_type, obj)
        except KeyError as exc:
            # 绑定未落地 / 周期字段缺失 — 乱序到达, 让 Creem 重试
            import logging
            logging.getLogger("flashshare").warning(
                "creem webhook %s: %s — retriable", event_type, exc)
            return JSONResponse({"ok": False, "error": str(exc.args[0])}, 500)
        return {"ok": True, **res}

    if event_type == "refund.created" or event_type == "dispute.created":
        # exact economic reversal (single domain handler for both) —
        # the route only verifies signature + parses, never adjudicates
        from economy import reverse as _reverse
        try:
            res = _reverse.reverse_payment(event_type, event)
        except KeyError as exc:
            # missing proof / provider fetch failure / unknown txn /
            # partial refund unresolved / status mismatch → 500 so
            # Creem retry + failed-event surface keeps the entry point
            import logging
            logging.getLogger("flashshare").warning(
                "creem %s: %s — retriable, zero mutation", event_type, exc)
            return JSONResponse(
                {"ok": False, "error": str(exc.args[0])}, 500)
        return {"ok": True, **res}

    if event_type != "checkout.completed":
        return {"ok": True, "ignored": True}

    # checkout.completed: subscription first-purchase vs plain package order
    if obj.get("subscription"):
        # 订阅首购: 绑定 sub id → order + 建档 (授信只由 subscription.paid 驱动)。
        # 域函数内部保证绑定先提交; sync 失败 → 500 重试, 绑定已在可收敛。
        from economy import subscription as _sub
        try:
            res = _sub.bind_checkout(obj)
        except KeyError as exc:
            import logging
            logging.getLogger("flashshare").warning(
                "creem webhook checkout(sub): %s — retriable", exc)
            return JSONResponse({"ok": False, "error": str(exc.args[0])}, 500)
        return {"ok": True, **res}

    order = (obj.get("order") or {})
    try:
        res = creem.fulfill(order.get("id") or "", obj.get("id") or "",
                            int(order.get("amount") or 0),
                            product_id=str(order.get("product") or ""),
                            currency=str(order.get("currency") or ""))
    except KeyError as exc:
        # 🔴5 修复(批2a): order_not_found 可能是 checkout→provider_order_id
        # 写入窗口内的乱序到达。返回 500 让 Creem 重试(webhook 重试政策),
        # 重试时 provider_order_id 已落库即可对上。原实现 200-ack 会把
        # 乱序事件变成永久丢失(用户付款不到账)。
        # amount_mismatch(订阅S2): Creem 金额与订单不符 → 同样 500, 重试耗尽后
        # 停止 → 不授信, 运营对账。日志区分两类。
        import logging
        log = logging.getLogger("flashshare")
        if "amount_mismatch" in str(exc):
            log.warning("creem webhook: amount mismatch (prov=%s) — NOT granting",
                        order.get("id"))
        else:
            log.warning("creem webhook: order not found (prov=%s ch=%s) — retriable",
                        order.get("id"), obj.get("id"))
        return JSONResponse({"ok": False, "error": str(exc.args[0])}, 500)
    return {"ok": True, **res}
