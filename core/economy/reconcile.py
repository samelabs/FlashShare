"""Membership reconciliation — Creem API pull, login-triggered.

背景: 会员每月赠送积分不能依赖 cron; webhook 是唯一授信
路径但会丢。对账改为**登录时拉取**: 用户登录 → 若名下有活跃订阅且
当期授信缺失 → 主动查 Creem API 补授。

授信权威不变: subscription.paid webhook (实时, 毫秒级)。
本模块是**补偿通道**: webhook 丢失后, 用户下次登录时补齐 — 账目最终
实时, 无 cron 进程/循环任务。

防作弊三重锁:
  1. idem_key plan:{uid}:{period_start} — 同期只授一次 (billing 层)
  2. last_grant_period_start 列 — 同 (user,plan) 状态机层防重
  3. period_start 必须来自 Creem payload — 客户端不可控字段, 无伪造面

补偿 eligibility: 仅 active / scheduled_cancel snapshot 进入 paid-period
补偿; trialing / past_due / canceled / expired / unknown 不 grant 不改
membership (provider 状态同步属 webhook handlers, 不在本模块)。

失败语义: Creem API 任何异常 → 静默跳过 (登录路径 best-effort,
webhook 仍是主通道); 绝不阻塞登录。
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


def reconcile_user(user_id: int) -> dict:
    """登录触发: 检查该用户全部订阅, 当期 (period_start > last_grant)
    且授信缺失 → 拉 Creem API 补授。

    Returns: {checked, granted, credits} — granted=True 时 credits>0。
    """
    ts = now()
    with db() as con:
        rows = orders_repo.user_subscriptions(con, user_id)
    checked = granted = 0
    credits_total = 0
    for row in rows:
        sub_id = (row.get("provider_order_id") or "").removeprefix("sub:")
        if not sub_id or not row.get("plan_id"):
            continue
        checked += 1
        # Read-contract guard: explicit LEFT JOIN aliases only — never the
        # ambiguous bare row["status"]/["created_at"]/["item_id"].
        sub = fetch_subscription(sub_id)
        if not sub:
            continue  # provider failure → best-effort skip
        from economy.subscription import _parse_ts
        period_start = _parse_ts(sub.get("current_period_start_date"))
        period_end = _parse_ts(sub.get("current_period_end_date"))
        if not period_start or not period_end:
            continue
        # Reconcile = compensation for LOST paid webhooks only. Provider
        # state sync stays with the webhook handlers: only snapshots that
        # read as "in a paid period" may enter compensation. trialing /
        # past_due / canceled / expired / unknown never grant and never
        # mutate membership through this path.
        provider_status = (sub.get("status") or "")
        if provider_status not in ("active", "scheduled_cancel"):
            continue
        last = row.get("last_grant_period_start")
        if last is not None and period_start <= int(last):
            continue  # 当期已授, 状态机层防重
        if period_end <= ts:
            continue  # 周期已过, 不补授
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
