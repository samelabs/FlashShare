"""Top-up endpoints: Creem checkout + webhook."""

import asyncio
import json

from fastapi import APIRouter

router = APIRouter()

from fastapi import Request
from fastapi.responses import JSONResponse

from app.web_auth import require_session_auth as _require_auth  # 单点 (治理)
from database import db  # checkout/subscribe 事务 (审计T1: 8处 with db() 此前零导入)


def _topup_checkout_impl(user: dict, raw: dict):
    from economy import creem
    package_id = int(raw.get("package_id", 0))
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
    from repo.orders import (insert_order, bind_checkout_id,
                             open_package_order)
    ts = now()
    with db() as con:
        # Purchase intent identity = Order.id. ONE pending package
        # intent per (user, package): an existing pending order
        # (response-loss retry, another browser) is RETRIED as the
        # same logical purchase — same row, same frozen facts, same
        # provider request_id=order:<id>. Terminal orders never block
        # a fresh purchase.
        pend = open_package_order(con, user["id"], int(pkg["id"]))
        if pend is not None:
            order = dict(pend)
        else:
            # purchase fact freeze: amount + currency + credits —
            # later package edits never rewrite a checked-out order
            order = insert_order(con, user["id"], "package", pkg["id"], "creem",
                                 pkg["price_cents"], pkg["currency"], ts,
                                 credits_snapshot=int(pkg["credits"]))
    # Pending retry state machine: everything except a pending order
    # without (or with a reusable ch:) provider identity answers from
    # local facts with ZERO provider create.
    prov = order.get("provider_order_id") or ""
    if order["status"] in ("refunded", "disputed"):
        return JSONResponse({"ok": False, "error": "checkout_closed"}, 409)
    if order["status"] == "paid":
        return {"ok": True, "completed": True}
    if order["status"] == "failed":
        return JSONResponse({"ok": False, "error": "checkout_failed"}, 409)
    if prov.startswith("ch:"):
        # provider entity exists — retrieve instead of re-create
        try:
            co = creem.retrieve_checkout(prov[3:])
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "checkout_unresolved"}, 502)
        st = str(co.get("status") or "")
        if st in ("pending", "processing") and co.get("checkout_url"):
            return {"ok": True, "checkout_url": co["checkout_url"],
                    "checkout_id": prov[3:], "reused": True}
        if st == "completed":
            # Provider completed but the webhook never landed: converge
            # LOCALLY through the SAME fulfillment domain handler —
            # never a second checkout. Contract facts come from the
            # retrieved object; failure answers retriable.
            _prov_order = ((co.get("order") or {}).get("id")
                           or (co.get("order_id") or ""))
            _prov_amount = int((co.get("order") or {}).get("amount")
                               or co.get("amount") or 0)
            _prov_product = str((co.get("order") or {}).get("product")
                                or co.get("product_id") or "")
            _prov_currency = str((co.get("order") or {}).get("currency")
                                 or co.get("currency") or "")
            try:
                res = creem.fulfill_by_request_id(int(order["id"]), str(_prov_order), prov[3:],
                    _prov_amount, product_id=_prov_product,
                    currency=_prov_currency)
                return {"ok": True, "completed": True, "credits": res.get("credits")}
            except KeyError as _exc:
                import logging
                logging.getLogger("flashshare").warning(
                    "pending-retrieve completed convergence failed order=%s: %s",
                    order["id"], _exc)
                return JSONResponse(
                    {"ok": False, "error": "checkout_unresolved"}, 502)
        if st in ("expired", "failed", "closed"):
            from repo.orders import mark_failed as _mf
            with db() as con:
                _mf(con, order["id"])   # provider-terminal proof
            return JSONResponse(
                {"ok": False, "error": f"checkout_{st}"}, 409)
        return JSONResponse({"ok": False, "error": "checkout_unresolved"}, 502)
    # pending + NULL provider id → (re)create; SAME request_id=order:<id>
    # makes the provider side idempotent (response-loss recovery: the
    # retry returns the SAME checkout entity, never a duplicate).
    try:
        out = creem.create_credits_checkout(order["id"],
            int(order["amount_cents"]), user)
    except Exception:
        # Unknown provider outcome (timeout / conn reset / 5xx / ACK
        # loss) is NOT a failure proof: the order stays pending and the
        # caller retries the SAME pending Order (checkout_unresolved).
        return JSONResponse({"ok": False, "error": "checkout_unresolved"}, 502)
    with db() as con:
        bind_checkout_id(con, order["id"], out["checkout_id"])
    return out

@router.post("/api/topup/checkout")
async def topup_checkout(request: Request):
    user, err = await asyncio.to_thread(_require_auth, request)
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
    return await asyncio.to_thread(_topup_checkout_impl, user, raw)


def _subscribe_checkout_impl(user: dict, raw: dict):
    from economy import creem
    plan_id = int(raw.get("plan_id", 0))
    from repo.orders import (plan_by_id_active, insert_order, bind_checkout_id,
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
        # First-checkout intent identity = Order.id. ONE open plan
        # checkout per user: the pending order (however reached —
        # unresolved first attempt with no provider id, mirror
        # rejection, network loss) is RETRIED on the SAME row with the
        # SAME provider request_id=order:<id>; anything further along
        # is in-progress and the pending-convergence/webhook path owns
        # it. Concurrency is serialized by the users row lock held for
        # the whole claim transaction.
        pending = open_plan_order(con, user["id"])
        order = None
        if pending is None:
            order = insert_order(con, user["id"], "plan", plan["id"], "creem",
                                 plan["price_cents"], plan["currency"], _ts)
        elif not (pending["provider_order_id"] or ""):
            # unresolved first attempt (no provider id yet) on the SAME
            # plan → retry the SAME order row / same request_id; a
            # different plan under the single open intent is not this
            # purchase — it stays in_progress until resolved.
            if int(pending["item_id"]) != int(plan["id"]):
                return JSONResponse(
                    {"ok": False, "error": "subscription_in_progress"}, 409)
            order = dict(pending)
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
        # Only an EXPLICIT provider-terminal status releases the open
        # intent (same whitelist as the package path). pending/processing
        # without a URL, blank/unknown/future nonterminal statuses are
        # NOT terminal proof — the pending Order is kept, no second
        # order or checkout is ever created on a guess.
        if st not in ("expired", "failed", "closed"):
            return JSONResponse(
                {"ok": False, "error": "subscription_in_progress"}, 409)
        # terminal (expired/failed/closed) → 终结旧 order, 走下方重新创建
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
        # Unknown provider outcome is not a failure proof — order stays
        # pending; the next subscribe call retries the SAME Order.
        return JSONResponse({"ok": False, "error": "checkout_unresolved"}, 502)
    with db() as con:
        bind_checkout_id(con, order["id"], out["checkout_id"])
    return out

@router.post("/api/topup/subscribe")
async def subscribe_checkout(request: Request):
    """订阅 checkout (v3 spec): plan 必须绑 creem_product_id 才可售。"""
    user, err = await asyncio.to_thread(_require_auth, request)
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
    return await asyncio.to_thread(_subscribe_checkout_impl, user, raw)


def _plan_upgrade_impl(user: dict, raw: dict):
    from economy import creem
    target_code = str(raw.get("plan", "")).strip().lower()
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
    # commercial mirror fail-closed gate — the SAME single point as the
    # first subscription checkout: before any provider product change,
    # the target product must carry this plan's exact price/currency and
    # the recurring/every-month contract. A mismatch or an unverifiable
    # mirror never reaches the provider (thread boundary, like every
    # provider call here); the local commercial state is untouched.
    try:
        _mirror_err = creem.verify_product_mirror(target["creem_product_id"],
            int(target["price_cents"]), str(target["currency"] or "").upper())
    except Exception:
        _mirror_err = "verify_failed"
    if _mirror_err:
        return JSONResponse(
            {"ok": False, "error": "price_mirror_mismatch"}, 502)
    try:
        # 升级域语义: proration-charge-immediately — product 立即 Pro,
        # prorated difference charged immediately, original billing date
        # remains (本周期 credits 由该 charge 的 subscription.paid 补差
        # 100+400=500; subscription.update 只同步 plan, 不授 credits)
        creem.upgrade_subscription(sub_id, target["creem_product_id"],
            update_behavior="proration-charge-immediately")
    except Exception:
        return JSONResponse({"ok": False, "error": "upgrade_failed"}, 502)
    return {"ok": True, "upgrade": "initiated",
            "from": cur_code, "to": target_code}

@router.post("/api/topup/plan-upgrade")
async def plan_upgrade(request: Request):
    """Plus → Pro (立即生效): 调 Creem upgrade (proration-charge-
    immediately)。本地 Membership 不伪造 — subscription.update webhook
    → product.id → pro 收敛 (仅同步 plan, 零 credits); 升级补差 credits
    只由该 proration charge 的 subscription.paid (paid transaction 证明)
    经 apply_paid_period 以 grant_plan_upgrade 补授。"""
    user, err = await asyncio.to_thread(_require_auth, request)
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
    return await asyncio.to_thread(_plan_upgrade_impl, user, raw)


def _plan_downgrade_impl(user: dict, raw: dict):
    from economy import creem
    target_code = str(raw.get("plan", "")).strip().lower()
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
    # commercial mirror fail-closed gate — same single point as upgrade
    # and the first subscription checkout (see plan_upgrade); a mismatch
    # or an unverifiable mirror never reaches the provider.
    try:
        _mirror_err = creem.verify_product_mirror(target["creem_product_id"],
            int(target["price_cents"]), str(target["currency"] or "").upper())
    except Exception:
        _mirror_err = "verify_failed"
    if _mirror_err:
        return JSONResponse(
            {"ok": False, "error": "price_mirror_mismatch"}, 502)
    try:
        creem.upgrade_subscription(sub_id, target["creem_product_id"],
            update_behavior="proration-none")
    except Exception:
        return JSONResponse({"ok": False, "error": "downgrade_failed"}, 502)
    schedule_downgrade(user["id"], target["id"])
    return {"ok": True, "downgrade": "scheduled",
            "from": cur_code, "to": target_code,
            "effective_at": current["renews_at"]}

@router.post("/api/topup/plan-downgrade")
async def plan_downgrade(request: Request):
    """Pro → Plus (next renewal): Creem 官方 product-change endpoint 同样
    承载 downgrade — update_behavior=proration-none (官方语义: no
    proration, change takes effect next billing cycle, no immediate
    charge)。先同步 Creem; provider 成功后才写 pending_downgrades
    (application expectation / local continuity fact, 非 Payment
    authority)。Creem 失败 → ZERO pending。当前周期 Membership/entitlements
    保持 Pro; 周期边界 provider product 必须实际为 Plus 才切换。"""
    user, err = await asyncio.to_thread(_require_auth, request)
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
    return await asyncio.to_thread(_plan_downgrade_impl, user, raw)


MAX_WEBHOOK_BODY = 262144  # 256KB — Creem 事件远小于此; 挡匿名内存放大 (审计🟡9)


def _creem_webhook_impl(event_type: str, event: dict, obj: dict):
    """Sync domain dispatch (thread): every branch below is a blocking
    DB/domain call (subscription state machine, reversal, fulfillment)."""
    from economy import creem
    import logging

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
    # request_id is required for subscription first-checkout binding too
    from repo.orders import order_id_from_request_id as _parse_rid
    try:
        _sub_oid = _parse_rid(obj.get("request_id"))
    except ValueError:
        import logging
        logging.getLogger("flashshare").warning(
            "creem webhook checkout(sub): request_id missing/malformed"
            " (request_id=%r) — zero mutation", obj.get("request_id"))
        return JSONResponse(
            {"ok": False, "error": "checkout_request_id_invalid"}, 500)
    if obj.get("subscription"):
        # 订阅首购: ONE transaction — order paid + sub:<id> + Membership
        # 一起提交 (授信只由 subscription.paid 驱动)。
        from economy import subscription as _sub
        try:
            res = _sub.bind_checkout(obj, order_id=_sub_oid)
        except KeyError as exc:
            import logging
            logging.getLogger("flashshare").warning(
                "creem webhook checkout(sub): %s — retriable", exc)
            return JSONResponse({"ok": False, "error": str(exc.args[0])}, 500)
        return {"ok": True, **res}

    # request_id is THE signed-webhook → local Order locator. It must
    # exist and match the exact 'order:<positive int>' shape we set at
    # checkout creation; provider_order_id / ch: / metadata are NOT
    # identity authorities (no compatibility fallback). request_id is a
    # CORRELATION only — full payment proof validation still runs in the
    # domain handler before any grant/mutation.
    from repo.orders import order_id_from_request_id
    try:
        _oid = order_id_from_request_id(obj.get("request_id"))
    except ValueError:
        import logging
        logging.getLogger("flashshare").warning(
            "creem webhook checkout.completed: request_id missing/malformed"
            " (request_id=%r) — zero mutation", obj.get("request_id"))
        return JSONResponse(
            {"ok": False, "error": "checkout_request_id_invalid"}, 500)
    # metadata correlation (observability fields, but if present they
    # must agree with the signed locator — a mismatch is corruption)
    _meta = obj.get("metadata") or {}
    if isinstance(_meta, dict):
        _m_oid = str(_meta.get("order_id") or "")
        if _m_oid and _m_oid != str(_oid):
            return JSONResponse(
                {"ok": False, "error": "checkout_metadata_mismatch"}, 500)
    order = (obj.get("order") or {})
    try:
        res = creem.fulfill_by_request_id(_oid, order.get("id") or "", obj.get("id") or "",
            int(order.get("amount") or 0),
            product_id=str(order.get("product") or ""),
            currency=str(order.get("currency") or ""))
    except KeyError as exc:
        # 🔴5 修复(the billing lock-order fix): order_not_found 可能是 checkout→provider_order_id
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
    return await asyncio.to_thread(_creem_webhook_impl, event_type, event, obj)
