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
    if not pkg or not pkg["creem_price_id"]:
        return JSONResponse({"ok": False, "error": "package_not_ready"}, 422)
    from repo.orders import insert_order, mark_failed, set_provider_order_id
    ts = now()
    with db() as con:
        order = insert_order(con, user["id"], "package", pkg["id"], "creem",
                             pkg["price_cents"], pkg["currency"], ts)
    try:
        out = creem.create_checkout(order["id"], dict(pkg), user)
    except Exception:
        with db() as con:
            mark_failed(con, order["id"])
        return JSONResponse({"ok": False, "error": "checkout_failed"}, 502)
    with db() as con:
        set_provider_order_id(con, order["id"], "ch:" + out["checkout_id"])
    return out


@router.post("/subscribe")
async def subscribe_checkout(request: Request):
    """订阅 checkout (v3 spec): plan 必须绑 creem_price_id 才可售。"""
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
    from repo.orders import plan_by_id_active, insert_order, set_provider_order_id
    with db() as con:
        plan = plan_by_id_active(con, plan_id)
    if not plan or not plan["creem_price_id"]:
        return JSONResponse({"ok": False, "error": "plan_not_ready"}, 422)
    from utils import now
    with db() as con:
        order = insert_order(con, user["id"], "plan", plan["id"], "creem",
                             plan["price_cents"], plan["currency"], now())
    try:
        out = creem.create_checkout(order["id"], dict(plan), user)
    except Exception:
        with db() as con:
            from repo.orders import mark_failed
            mark_failed(con, order["id"])
        return JSONResponse({"ok": False, "error": "checkout_failed"}, 502)
    with db() as con:
        set_provider_order_id(con, order["id"], "ch:" + out["checkout_id"])
    return out


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
        import logging
        logging.getLogger("flashshare").warning(
            "creem %s: obj=%s — operator review required", event_type, obj.get("id"))
        return {"ok": True, "logged": True}

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
                            int(order.get("amount") or 0))
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
