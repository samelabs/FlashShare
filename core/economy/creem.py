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
_BASE = "https://test-api.creem.io" if CREEM_TEST else "https://api.creem.io"


def creem_enabled() -> bool:
    return bool(CREEM_API_KEY)


def create_checkout(order_id: int, package: dict, user: dict) -> dict:
    """Create a Creem checkout session for a pending order. Returns
    {ok, checkout_url} — raises RuntimeError on API failure."""
    body = json.dumps({
        "product_id": package["creem_price_id"],
        "request_id": f"order:{order_id}",
        "success_url": BASE_URL + "/account/credits?paid=1",
        "metadata": {"order_id": str(order_id), "user_id": str(user["id"])},
        "customer": {"email": user.get("email") or ""},
    }).encode()
    req = urllib.request.Request(
        _BASE + "/v1/checkouts", data=body, method="POST",
        headers={"x-api-key": CREEM_API_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return {"ok": True, "checkout_url": data["checkout_url"], "checkout_id": data["id"]}


def retrieve_checkout(checkout_id: str) -> dict:
    """GET /v1/checkouts/{id} — status / checkout_url / subscription。
    raises RuntimeError on API failure (caller fail loud, 不猜 TTL)。"""
    req = urllib.request.Request(
        _BASE + "/v1/checkouts/" + urllib.parse.quote(checkout_id),
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


def fulfill(provider_order_id: str, checkout_id: str, amount_cents: int) -> dict:
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
        # 🟡(订阅S2 随批): 金额一致性 — Creem 侧金额与订单不符时拒绝授信
        # (改价/异常事件), 运营对账后人工处理。amount_cents<=0 视为未知, 放行。
        if amount_cents and row["amount_cents"] and int(amount_cents) != int(row["amount_cents"]):
            import logging
            logging.getLogger("flashshare").warning(
                "creem fulfill amount mismatch: order=%s expected=%s got=%s",
                row["id"], row["amount_cents"], amount_cents)
            raise KeyError("amount_mismatch")
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
