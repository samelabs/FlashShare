"""Top-up endpoints: Creem checkout + webhook."""
import json

from fastapi import APIRouter

router = APIRouter()

from fastapi import Request
from fastapi.responses import JSONResponse

from app.routes.earn import _require_auth


@router.post("/api/topup/checkout")
async def topup_checkout(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    import creem
    if not creem.creem_enabled():
        return JSONResponse({"ok": False, "error": "topup_unavailable"}, 503)
    try:
        raw = await request.json()
        package_id = int(raw.get("package_id", 0))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from database import db
    from utils import now
    with db() as con:
        pkg = con.execute(
            "SELECT * FROM packages WHERE id=%s AND active", (package_id,)).fetchone()
    if not pkg or not pkg["creem_price_id"]:
        return JSONResponse({"ok": False, "error": "package_not_ready"}, 422)
    ts = now()
    with db() as con:
        order = con.execute(
            """INSERT INTO orders (user_id, kind, item_id, provider, status,
               amount_cents, currency, created_at)
               VALUES (%s, 'package', %s, 'creem', 'pending', %s, %s, %s)
               RETURNING *""",
            (user["id"], pkg["id"], pkg["price_cents"], pkg["currency"], ts)).fetchone()
    try:
        out = creem.create_checkout(order["id"], dict(pkg), user)
    except Exception:
        with db() as con:
            con.execute("UPDATE orders SET status='failed' WHERE id=%s", (order["id"],))
        return JSONResponse({"ok": False, "error": "checkout_failed"}, 502)
    with db() as con:
        con.execute("UPDATE orders SET provider_order_id=%s WHERE id=%s",
                    ("ch:" + out["checkout_id"], order["id"]))
    return out


@router.post("/webhooks/creem")
async def creem_webhook(request: Request):
    import creem
    raw = await request.body()
    sig = request.headers.get("creem-signature", "")
    if not creem.verify_signature(raw, sig):
        return JSONResponse({"ok": False, "error": "bad_signature"}, 401)
    try:
        event = json.loads(raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, 400)
    if event.get("eventType") != "checkout.completed":
        return {"ok": True, "ignored": True}
    obj = event.get("object") or {}
    order = (obj.get("order") or {})
    try:
        res = creem.fulfill(order.get("id") or "", obj.get("id") or "",
                            int(order.get("amount") or 0))
    except KeyError:
        # unknown order: acknowledge to avoid retry storms, log via status
        return {"ok": True, "granted": False, "error": "order_not_found"}
    return {"ok": True, **res}
