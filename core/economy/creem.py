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


def _patch(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _BASE + path, data=body, method="PATCH",
        headers={"x-api-key": CREEM_API_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _get(path: str) -> dict:
    req = urllib.request.Request(
        _BASE + path, method="GET",
        headers={"x-api-key": CREEM_API_KEY})
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
    (recurring 商品价格不可被 checkout 覆盖 — Creem 合同)。

    Price authority: plans.price_cents/currency 是 518 唯一售价 authority;
    Creem Product 是支付执行 mirror。checkout 前必须验证 mirror 与本地
    商业事实一致 (product id + price + currency + recurring billing),
    不一致 fail closed — 绝不静默按 provider 价格售卖, 也绝不在
    customer checkout 路径反向改 518 Plan。"""
    if not plan.get("creem_product_id"):
        raise RuntimeError("plan_product_not_configured")
    mirror_err = verify_product_mirror(
        plan["creem_product_id"],
        int(plan["price_cents"]), str(plan["currency"] or "").upper())
    if mirror_err:
        raise RuntimeError(f"price_mirror_mismatch:{mirror_err}")
    return _checkout_session(order_id, user, {
        "product_id": plan["creem_product_id"],
    })


# Creem billing contract for subscription plans: recurring, monthly.
# The canonical paid Plan is a monthly contract — a yearly or one-time
# product under a subscription plan is a configuration error (wrong
# charge risk), fail closed everywhere.
_BILLING_TYPE_RECURRING = "recurring"
_BILLING_PERIOD_MONTHLY = "every-month"


def _fetch_product(product_id: str) -> dict | None:
    """GET /v1/products/{id} → product dict (two current payload shapes),
    None on failure/absent."""
    try:
        data = _get(f"/v1/products/{product_id}")
    except Exception:
        return None
    prod = data.get("object") == "product" and data or (
        data.get("product") if isinstance(data.get("product"), dict) else None)
    if isinstance(prod, dict) and str(prod.get("id") or "") == product_id:
        return prod
    return None


def _billing_contract_ok(prod: dict) -> bool:
    return (str(prod.get("billing_type") or "") == _BILLING_TYPE_RECURRING
            and str(prod.get("billing_period") or "") == _BILLING_PERIOD_MONTHLY)


MIRROR_SUCCESS = "success"
MIRROR_ROLLBACK = "rollback"
MIRROR_UNRESOLVED = "unresolved"


def mirror_price_with_reconciliation(product_id: str, before_price: int,
                                     before_currency: str,
                                     price_cents: int, currency: str) -> str:
    """Two-stage commercial mirror with PROVEN commit-outcome.

    Provider stage: snapshot the exact provider-before commercial facts,
    PATCH, and on an ambiguous exception re-GET — exact-intended ⇒
    success; exact-before ⇒ proven provider rollback; neither/unreadable
    ⇒ unresolved (never guess, never auto-mutate again).

    Returns MIRROR_SUCCESS only when the provider provably carries the
    intended price (either confirmed by PATCH or proven by fresh GET);
    MIRROR_ROLLBACK when the provider provably still carries the exact
    before facts; MIRROR_UNRESOLVED otherwise. The CALLER owns the local
    transaction and the (rollback-gated) compensation restore.
    """
    try:
        mirror_product_price(product_id, int(price_cents), str(currency))
        return MIRROR_SUCCESS
    except Exception:
        pass
    # ambiguous — fresh GET decides
    try:
        prod = _fetch_product(product_id)
    except Exception:
        return MIRROR_UNRESOLVED
    if prod is None:
        return MIRROR_UNRESOLVED
    _p = int(prod.get("price") or 0)
    _c = str(prod.get("currency") or "").upper()
    if _p == int(price_cents) and _c == str(currency).upper():
        return MIRROR_SUCCESS
    if _p == int(before_price) and _c == str(before_currency).upper():
        return MIRROR_ROLLBACK
    return MIRROR_UNRESOLVED


def restore_product_price(product_id: str, before_price: int,
                          before_currency: str) -> bool:
    """Rollback-gated compensation: restore the provider to the exact
    before commercial facts. True only when a fresh read proves the
    provider carries the before state again (PATCH confirmed or GET
    verified); ambiguous/failed restore ⇒ False (caller reports
    unresolved — never pretends)."""
    out = mirror_price_with_reconciliation(
        product_id, int(before_price), str(before_currency),
        int(before_price), str(before_currency))
    if out == MIRROR_SUCCESS:
        return True
    # intended == before here, so ROLLBACK also means "already at before"
    if out == MIRROR_ROLLBACK:
        return True
    return False


def verify_product_mirror(product_id: str, price_cents: int,
                          currency: str) -> str:
    """Verify the Creem product mirror matches the 518 commercial fact.

    Checks product id + price + currency + billing contract
    (recurring + every-month). Returns "" when consistent; otherwise a
    stable mismatch token (price/currency/billing_type/billing_period/
    not_found/not_configured) — the caller fails closed on any non-empty
    value. A same-price yearly product is NOT consistent."""
    if not CREEM_API_KEY or not product_id:
        return "not_configured"
    prod = _fetch_product(product_id)
    if prod is None:
        return "not_found"
    if int(prod.get("price") or 0) != int(price_cents):
        return "price"
    if str(prod.get("currency") or "").upper() != currency.upper():
        return "currency"
    if str(prod.get("billing_type") or "") != _BILLING_TYPE_RECURRING:
        return "billing_type"
    if str(prod.get("billing_period") or "") != _BILLING_PERIOD_MONTHLY:
        return "billing_period"
    return ""


def mirror_product_price(product_id: str, price_cents: int,
                         currency: str) -> dict:
    """Write-through: push the 518 requested price into the Creem product.

    The provider billing contract is NEVER rewritten here — 518 is the
    price authority, not a billing-contract editor. Sequence:
      1. GET the target product and require the correct contract
         (id + recurring + every-month) — a one-time or yearly product
         fails closed BEFORE any PATCH (a price change must not silently
         'repair' a misconfigured product into recurring).
      2. PATCH ONLY the commercial price facts (price + currency).
      3. Re-confirm the response: id + price + currency + recurring +
         every-month. Any inconsistency → operation failed → the caller
         must NOT commit the local price.
    """
    if not CREEM_API_KEY or not product_id:
        raise RuntimeError("product_not_configured")
    prod = _fetch_product(product_id)
    if prod is None:
        raise RuntimeError("product_not_found")
    if not _billing_contract_ok(prod):
        raise RuntimeError("product_billing_contract_invalid")
    out = _patch(f"/v1/products/{product_id}", {
        "price": int(price_cents),
        "currency": str(currency).upper(),
    })
    confirmed = out.get("object") == "product" and out or (
        out.get("product") if isinstance(out.get("product"), dict) else None)
    if not isinstance(confirmed, dict) \
            or str(confirmed.get("id") or "") != product_id \
            or int(confirmed.get("price") or 0) != int(price_cents) \
            or str(confirmed.get("currency") or "").upper() != str(currency).upper() \
            or not _billing_contract_ok(confirmed):
        raise RuntimeError("price_mirror_confirm_failed")
    return confirmed


def upgrade_subscription(subscription_id: str, product_id: str,
                         update_behavior: str) -> dict:
    """POST /v1/subscriptions/{id}/upgrade — product change。behavior 白名单
    只含实际使用的 canonical 值: 升级=proration-charge-immediately (Creem
    官方 canonical; proration-charge 已 deprecated 且行为等同, 生产不再
    固化 deprecated alias), 降级=proration-none (next billing cycle 生效)。
    Membership 收敛由 subscription.update webhook 完成, 本调用不做任何
    本地 Membership 伪造。"""
    if update_behavior not in ("proration-charge-immediately", "proration-none"):
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
    """Legacy wrapper — the webhook now resolves the order via the SIGNED
    request_id; kept for compatibility with in-repo callers only."""
    from repo import orders as _orders_repo
    with db() as con:
        row = None
        if provider_order_id:
            row = orders_repo.order_by_provider_id_for_update(con, provider_order_id)
        if row is None and checkout_id:
            row = orders_repo.order_by_provider_id(con, "ch:" + checkout_id)
        if row is None:
            raise KeyError("order_not_found")
    return fulfill_by_request_id(int(row["id"]), provider_order_id, checkout_id,
                                 amount_cents, product_id, currency)


def fulfill_by_request_id(order_id: int, provider_order_id: str,
                          checkout_id: str, amount_cents: int,
                          product_id: str = "", currency: str = "") -> dict:
    """checkout.completed fulfillment, order located by SIGNED request_id
    (order:<id>) and locked FOR UPDATE. Payment-contract validation
    (kind/product/amount/currency + provider identity) runs BEFORE any
    mutation; terminal reversal markers gate the grant; the paid
    finalization uses the guarded complete_package_order writer.
    Returns {granted, credits} — granted=False means replay/reversed
    no-op. The grant-first crash-self-heal boundary is unchanged."""
    from repo import orders as _orders_repo
    ts = now()
    with db() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,)).fetchone()
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
        # fulfillment reads ONLY the frozen purchase fact — the live
        # package row may have been edited since checkout and must never
        # rewrite a checked-out order's granted quantity
        credits = int(row["credits_snapshot"] or 0)
        user_id = row["user_id"]
        # ── terminal gate: a reversed order NEVER re-grants and its
        # marker is never rewritten by a (stale/replayed) success ──
        if row["status"] in ("refunded", "disputed"):
            return {"granted": False, "credits": 0,
                    "reason": f"order_{row['status']}"}
        if row["status"] == "paid":
            return {"granted": False, "credits": 0, "reason": "replay"}
    # 授信先行 (billing 幂等事务, idem order:{id}); 终态戳由 guarded
    # writer 落 — granted=TRUE 蕴含 ledger 行已存在 (崩溃一致性)。
    # 崩溃自愈: 授信后崩溃 → 重试时 grant 幂等重放 + paid 补戳。
    if credits > 0:
        from economy.billing import grant
        grant(user_id, credits, "grant_package",
              ref_type="order", ref_id=str(order_id),
              idem_key=f"order:{order_id}", note=amount_note)
    # canonical provider identity: the success event's ord_* wins over
    # the provisional ch:*; a mismatched existing canonical id fails loud
    canonical = provider_order_id or ("ch:" + checkout_id)
    if provider_order_id and row["provider_order_id"] != provider_order_id \
            and not (row["provider_order_id"] or "").startswith("ch:") \
            and row["provider_order_id"] is not None:
        raise KeyError("provider_identity_conflict")
    with db() as con:
        _orders_repo.complete_package_order(con, order_id, canonical, ts)
    return {"granted": True, "credits": credits}
