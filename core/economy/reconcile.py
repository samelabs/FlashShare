"""Membership reconciliation — Creem API pull, login-triggered.

背景: 会员每月赠送积分不能依赖 cron; webhook 是唯一授信
路径但会丢。对账改为**登录时拉取**: 用户登录 → 若名下有活跃订阅且
当期授信缺失 → 主动查 Creem API 补授。

Proof model (payment-forward closure): the ONLY economic fact is a
provider-confirmed successful subscription transaction. The webhook and
this reconciliation channel prove the SAME transaction fact — they are
not two authorities. Reconcile therefore never grants on subscription
status alone: it retrieves the subscription, then retrieves the
transaction named by last_transaction_id and requires it to be a paid
invoice for THIS subscription and THIS period. Anything less → no grant.

防作弊三重锁:
  1. idem_key plan:{uid}:{period_start} — 同期只授一次 (billing 层)
  2. last_grant_period_start 列 — 同 (user,plan) 状态机层防重
  3. period/transaction 字段全部来自 Creem payload — 客户端不可控

补偿 eligibility: scheduled_cancel 只是生命周期状态 — 当期若有
matching paid transaction 仍可补偿; trialing / past_due / canceled /
expired / unknown 不 grant 不改 membership。

失败语义: Creem API 任何异常 → 静默跳过 (登录路径 best-effort,
webhook 仍是主通道); 绝不阻塞登录; 失败时绝不 grant。
"""

import json
import logging
import urllib.request

from config import BASE_URL
from database import db
from repo import orders as orders_repo
from utils import now

log = logging.getLogger("flashshare")


def _creem_base() -> str:
    import os
    test = os.environ.get("CREEM_TEST_MODE", "").strip() in ("1", "true", "yes")
    return "https://test-api.creem.io" if test else "https://api.creem.io"


def _creem_key() -> str:
    import os
    return os.environ.get("CREEM_API_KEY", "").strip()


def fetch_subscription(sub_id: str) -> dict | None:
    """Creem GET /v1/subscriptions?subscription_id= — 原始 payload。
    失败/未配置返回 None (best-effort)。"""
    key = _creem_key()
    if not key or not sub_id:
        return None
    url = f"{_creem_base()}/v1/subscriptions?subscription_id={sub_id}"
    req = urllib.request.Request(url, method="GET",
                                 headers={"x-api-key": key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        sub = data.get("object") == "subscription" and data or (
            data.get("subscription") if isinstance(data.get("subscription"), dict) else None)
        return sub
    except Exception:
        return None


def fetch_transaction(txn_id: str) -> dict | None:
    """Creem GET /v1/transactions?transaction_id= — 原始 payload。
    失败/未配置返回 None (best-effort)。"""
    key = _creem_key()
    if not key or not txn_id:
        return None
    url = f"{_creem_base()}/v1/transactions?transaction_id={txn_id}"
    req = urllib.request.Request(url, method="GET",
                                 headers={"x-api-key": key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        txn = data.get("object") == "transaction" and data or (
            data.get("transaction") if isinstance(data.get("transaction"), dict) else None)
        return txn
    except Exception:
        return None


def transaction_proves_paid_period(txn: dict | None, sub_id: str,
                                   period_start: int, period_end: int) -> bool:
    """The paid-transaction proof predicate (single definition).

    A transaction proves THIS period paid only when ALL hold:
      status == paid; type == invoice (subscription payment);
      transaction.subscription == this subscription id;
      transaction.period_start/end == this subscription's period.
    pending / refunded / partialRefund / chargedBack / wrong subscription /
    wrong period / missing transaction → False. Never grants."""
    if not isinstance(txn, dict):
        return False
    if str(txn.get("status") or "") != "paid":
        return False
    if str(txn.get("type") or "") != "invoice":
        return False
    if str(txn.get("subscription") or "") != sub_id:
        return False
    from economy.subscription import _parse_ts
    tx_ps = _parse_ts(txn.get("period_start"))
    tx_pe = _parse_ts(txn.get("period_end"))
    return tx_ps == period_start and tx_pe == period_end


def reconcile_user(user_id: int) -> dict:
    """登录触发: 检查该用户全部订阅, 当期 (period_start > last_grant)
    且授信缺失 → 拉 Creem API 补授。

    Returns: {checked, granted, credits} — granted=True 时 credits>0。
    """
    ts = now()
    # 补偿通道只跟随 current Membership 的 provider subscription —
    # 不再遍历历史 plan orders 猜当前订阅。
    with db() as con:
        m = orders_repo.membership_row(con, user_id)
    if m is None or not (m.get("provider_subscription_id") or "").startswith("sub:"):
        return {"checked": 0, "granted": 0, "credits": 0}
    checked = granted = 0
    credits_total = 0
    for row in [dict(m)]:
        sub_id = row["provider_subscription_id"].removeprefix("sub:")
        if not sub_id:
            continue
        checked += 1
        # Input = current Membership row (explicit membership fields).
        sub = fetch_subscription(sub_id)
        if not sub:
            continue  # provider failure → best-effort skip
        from economy.subscription import _parse_ts
        period_start = _parse_ts(sub.get("current_period_start_date"))
        period_end = _parse_ts(sub.get("current_period_end_date"))
        if not period_start or not period_end:
            continue
        # Reconcile = compensation for LOST paid webhooks only. Provider
        # state sync stays with the webhook handlers: trialing / past_due /
        # canceled / expired / unknown never grant and never mutate
        # membership through this path. scheduled_cancel is a lifecycle
        # marker only — its current period can still be healed WHEN a
        # matching paid transaction proves it.
        provider_status = (sub.get("status") or "")
        if provider_status not in ("active", "scheduled_cancel"):
            continue
        last = row.get("last_grant_period_start")
        if last is not None and period_start <= int(last):
            continue  # 当期已授, 状态机层防重
        if period_end <= ts:
            continue  # 周期已过, 不补授
        # Paid-transaction proof (never status-alone): the subscription
        # must name last_transaction_id and that transaction must be a
        # paid invoice for THIS subscription and THIS period. Any
        # provider/API failure or mismatch → no grant (best-effort skip).
        txn_id = str(sub.get("last_transaction_id") or "")
        if not txn_id:
            continue
        txn = fetch_transaction(txn_id)
        if not transaction_proves_paid_period(txn, sub_id,
                                               period_start, period_end):
            continue
        # 唯一 paid-period implementation (webhook 与 reconcile 共享):
        # grant(幂等) 先行 → upsert 状态戳; scheduled_cancel snapshot
        # 保留 cancellation marker, 不被补授清除。
        try:
            from economy.subscription import apply_paid_period
            result = apply_paid_period(
                sub_id, sub,
                scheduled_cancel=(provider_status == "scheduled_cancel"))
            credits = int(result.get("credits") or 0)
            if credits > 0:
                credits_total += credits
                granted += 1
                log.info("subscription reconcile: user=%s sub=%s +%d credits",
                         user_id, sub_id, credits)
        except Exception:
            log.exception("subscription reconcile failed user=%s sub=%s",
                          user_id, sub_id)
    return {"checked": checked, "granted": granted, "credits": credits_total}
