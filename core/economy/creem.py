"""
Creem top-up: checkout session + webhook (checkout.completed → grant).

Env (all optional — absent key keeps the button in 'coming soon' state):
  CREEM_API_KEY        prod key (test key → CREEM_TEST_MODE=1 uses test-api.creem.io)
  CREEM_WEBHOOK_SECRET HMAC secret for creem-signature verification

Flow: POST /api/topup/checkout {package_id} → orders row (pending) →
Creem checkout session → redirect URL. Webhook is the ONLY credit-granting
path (idempotent on order:{id}); success_url is UX only.
"""
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request

from config import BASE_URL
from database import db
from repo import orders as orders_repo
from utils import now

CREEM_API_KEY = os.environ.get("CREEM_API_KEY", "").strip()
CREEM_WEBHOOK_SECRET = os.environ.get("CREEM_WEBHOOK_SECRET", "").strip()
CREEM_TEST = os.environ.get("CREEM_TEST_MODE", "").strip() in ("1", "true", "yes")
# Credits one-time Product: 所有 package 共用同一 Creem Product,
# custom_price 由本地 order.amount_cents 决定 (客户端零 authoritative 输入)
CREEM_CREDITS_PRODUCT_ID = os.environ.get("CREEM_CREDITS_PRODUCT_ID", "").strip()
# shared Credits Product 的固定 currency (Creem Product 自带; 本地 Package
# 必须一致才可售)
CREEM_CREDITS_CURRENCY = os.environ.get("CREEM_CREDITS_CURRENCY", "").strip().upper()
_BASE = "https://test-api.creem.io" if CREEM_TEST else "https://api.creem.io"


def creem_enabled() -> bool:
    return bool(CREEM_API_KEY)


def _post(path: str, payload: dict) -> dict:
    """shared private HTTP helper — raises RuntimeError on API failure."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _BASE + path, data=body, method="POST",
        headers={"x-api-key": CREEM_API_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _checkout_session(order_id: int, user: dict, product_payload: dict) -> dict:
    payload = {
        "request_id": f"order:{order_id}",
        "success_url": BASE_URL + "/account/credits?paid=1",
        "metadata": {"order_id": str(order_id), "user_id": str(user["id"])},
        "customer": {"email": user.get("email") or ""},
    }
    payload.update(product_payload)
    data = _post("/v1/checkouts", payload)
    return {"ok": True, "checkout_url": data["checkout_url"], "checkout_id": data["id"]}


def create_credits_checkout(order_id: int, amount_cents: int, user: dict) -> dict:
    """Credits package checkout: 所有 package 共用 CREEM_CREDITS_PRODUCT_ID
    (one-time Product) + custom_price = 本地 order.amount_cents。"""
    if not credits_ready():
        raise RuntimeError("credits_product_not_configured")
    if not custom_price_in_range(amount_cents):
        raise ValueError("custom_price_out_of_range")
    return _checkout_session(order_id, user, {
        "product_id": CREEM_CREDITS_PRODUCT_ID,
        "custom_price": int(amount_cents),
    })


def create_subscription_checkout(order_id: int, plan: dict, user: dict) -> dict:
    """Subscription checkout: plan.creem_product_id, ZERO custom_price
    (价格由 Creem Product 权威)。"""
    if not plan.get("creem_product_id"):
        raise RuntimeError("plan_product_not_configured")
    return _checkout_session(order_id, user, {
        "product_id": plan["creem_product_id"],
    })


def upgrade_subscription(subscription_id: str, product_id: str,
                         update_behavior: str) -> dict:
    """POST /v1/subscriptions/{id}/upgrade — product change。behavior 由
    caller 域语义决定 (升级=proration-charge: 补差入 next invoice, 原
    billing cycle 不重启; 降级=proration-none: next billing cycle 生效)。
    Membership 收敛由 subscription.update webhook 完成, 本调用不做任何
    本地 Membership 伪造。"""
    if update_behavior not in ("proration-charge", "proration-none"):
        raise ValueError("invalid_update_behavior")
    return _post(f"/v1/subscriptions/{subscription_id}/upgrade", {
        "product_id": product_id,
        "update_behavior": update_behavior,
    })


def credits_product_id() -> str:
    return CREEM_CREDITS_PRODUCT_ID


def credits_ready() -> bool:
    """Credits 售卖 readiness: API key + shared Product + 固定 currency。"""
    return bool(CREEM_API_KEY and CREEM_CREDITS_PRODUCT_ID and CREEM_CREDITS_CURRENCY)


# Creem official custom_price bounds (cents)
CUSTOM_PRICE_MIN = 100
CUSTOM_PRICE_MAX = 99999999


def custom_price_in_range(amount_cents: int) -> bool:
    return CUSTOM_PRICE_MIN <= int(amount_cents) <= CUSTOM_PRICE_MAX


def retrieve_checkout(checkout_id: str) -> dict:
    """GET /v1/checkouts?checkout_id={id} (官方当前合同) —
    status / checkout_url / subscription。raises on API failure
    (caller fail loud, 不猜 TTL)。"""
    req = urllib.request.Request(
        _BASE + "/v1/checkouts?" + urllib.parse.urlencode(
            {"checkout_id": checkout_id}),
        method="GET", headers={"x-api-key": CREEM_API_KEY})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return data


def verify_signature(raw_body: bytes, signature: str) -> bool:
    if not CREEM_WEBHOOK_SECRET:
        return False
    expected = hmac.new(CREEM_WEBHOOK_SECRET.encode(), raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def fulfill(provider_order_id: str, checkout_id: str, amount_cents: int,
            product_id: str = "", currency: str = "") -> dict:
    """checkout.completed: mark order paid + grant credits (idempotent).
    Returns {granted, credits} — granted=False means replay/no-op.

    订单锁定+标记+套餐读取在同一事务内 (批1c 顺带修复: 原实现块外用 con
    的连接逃逸); 授信仍走 billing.grant 的独立幂等事务 (order:{id} 键)。"""
    from repo import orders as _orders_repo
    ts = now()
    with db() as con:
        row = None
        if provider_order_id:
            row = orders_repo.order_by_provider_id_for_update(con, provider_order_id)
        if row is None and checkout_id:
            # webhook may arrive before checkout callback stores the id —
            # fall back to checkout-id linkage (ch:{id})
            row = orders_repo.order_by_provider_id(con, "ch:" + checkout_id)
        if row is None:
            raise KeyError("order_not_found")
        if row["kind"] != "package":
            raise KeyError("wrong_order_kind")
        # fail-closed payment contract: shared Credits Product + amount +
        # currency 全部必须显式匹配, 缺失即拒 (zero grant)。
        if not CREEM_CREDITS_PRODUCT_ID or not product_id:
            raise KeyError("product_missing")
        if product_id != CREEM_CREDITS_PRODUCT_ID:
            import logging
            logging.getLogger("flashshare").warning(
                "creem fulfill product mismatch: order=%s expected=%s got=%s",
                row["id"], CREEM_CREDITS_PRODUCT_ID, product_id)
            raise KeyError("product_mismatch")
        if not amount_cents or int(amount_cents) <= 0:
            raise KeyError("amount_missing")
        if int(amount_cents) != int(row["amount_cents"]):
            import logging
            logging.getLogger("flashshare").warning(
                "creem fulfill amount mismatch: order=%s expected=%s got=%s",
                row["id"], row["amount_cents"], amount_cents)
            raise KeyError("amount_mismatch")
        if not currency:
            raise KeyError("currency_missing")
        cur_local = (row["currency"] or "").upper()
        if currency.upper() != cur_local or cur_local != CREEM_CREDITS_CURRENCY:
            import logging
            logging.getLogger("flashshare").warning(
                "creem fulfill currency mismatch: order=%s local=%s webhook=%s configured=%s",
                row["id"], cur_local, currency, CREEM_CREDITS_CURRENCY)
            raise KeyError("currency_mismatch")
        order_id = row["id"]
        amount_note = f"topup {row['amount_cents'] / 100:.2f}{row['currency']}"
        pkg = _orders_repo.package_credits(con, row["item_id"])
        credits = pkg["credits"] if pkg else 0
        user_id = row["user_id"]
    # 授信先行 (billing 幂等事务, idem order:{id}); mark_paid 是终态戳 —
    # granted=TRUE 蕴含 ledger 行已存在, 留痕永不撒谎 (崩溃一致性)。
    # 崩溃自愈: 授信后崩溃 → 重试时 grant 幂等重放 + mark_paid 补戳。
    if credits > 0:
        from economy.billing import grant
        grant(user_id, credits, "grant_package",
              ref_type="order", ref_id=str(order_id),
              idem_key=f"order:{order_id}", note=amount_note)
    with db() as con:
        orders_repo.mark_paid(con, order_id, ts)
    return {"granted": True, "credits": credits}
