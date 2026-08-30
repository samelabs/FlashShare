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
import urllib.request

from database import db
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
        "success_url": os.environ.get("BASE_URL", "https://518.plus") + "/account/credits?paid=1",
        "metadata": {"order_id": str(order_id), "user_id": str(user["id"])},
        "customer": {"email": user.get("email") or ""},
    }).encode()
    req = urllib.request.Request(
        _BASE + "/v1/checkouts", data=body, method="POST",
        headers={"x-api-key": CREEM_API_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return {"ok": True, "checkout_url": data["checkout_url"], "checkout_id": data["id"]}


def verify_signature(raw_body: bytes, signature: str) -> bool:
    if not CREEM_WEBHOOK_SECRET:
        return False
    expected = hmac.new(CREEM_WEBHOOK_SECRET.encode(), raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def fulfill(provider_order_id: str, checkout_id: str, amount_cents: int) -> dict:
    """checkout.completed: mark order paid + grant credits (idempotent).
    Returns {granted, credits} — granted=False means replay/no-op."""
    ts = now()
    with db() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE provider_order_id = %s FOR UPDATE",
            (provider_order_id,)).fetchone() if provider_order_id else None
        if row is None:
            # webhook may arrive before checkout callback stores the id —
            # fall back to request_id linkage (order:{id})
            row = None
            if checkout_id:
                row = con.execute(
                    "SELECT * FROM orders WHERE provider_order_id = %s",
                    ("ch:" + checkout_id,)).fetchone()
        if row is None:
            raise KeyError("order_not_found")
        if row["granted"]:
            return {"granted": False, "credits": 0}
        con.execute(
            "UPDATE orders SET status='paid', granted=TRUE, paid_at=%s WHERE id=%s",
            (ts, row["id"]))
    from billing import grant
    pkg = con.execute("SELECT credits FROM packages WHERE id=%s",
                      (row["item_id"],)).fetchone() if row else None
    credits = pkg["credits"] if pkg else 0
    if credits > 0:
        grant(row["user_id"], credits, "grant_package",
              ref_type="order", ref_id=str(row["id"]),
              idem_key=f"order:{row['id']}",
              note=f"topup {row['amount_cents'] / 100:.2f}{row['currency']}")
    return {"granted": True, "credits": credits}
