"""Membership reconciliation — Creem API pull, login-triggered (1004).

背景 (用户裁定): 会员每月赠送积分不能依赖 cron; webhook 是唯一授信
路径但会丢。对账改为**登录时拉取**: 用户登录 → 若名下有活跃订阅且
当期授信缺失 → 主动查 Creem API 补授。

授信权威不变: subscription.paid webhook (实时, 毫秒级)。
本模块是**补偿通道**: webhook 丢失后, 用户下次登录时补齐 — 账目最终
实时, 无 cron 进程/循环任务。

防作弊三重锁:
  1. idem_key plan:{uid}:{period_start} — 同期只授一次 (billing 层)
  2. last_grant_period_start 列 — 同 (user,plan) 状态机层防重
  3. period_start 必须来自 Creem payload — 客户端不可控字段, 无伪造面

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
        if not sub_id or not row.get("item_id"):
            continue
        checked += 1
        if row.get("status") == "expired":
            continue
        sub = fetch_subscription(sub_id)
        if not sub:
            continue
        from economy.subscription import _parse_ts
        period_start = _parse_ts(sub.get("current_period_start_date"))
        period_end = _parse_ts(sub.get("current_period_end_date"))
        if not period_start or not period_end:
            continue
        if (sub.get("status") or "") not in (
                "active", "scheduled_cancel", "past_due", "trialing"):
            continue
        last = row.get("last_grant_period_start")
        if last is not None and period_start <= int(last):
            continue  # 当期已授, 状态机层防重
        if period_end <= ts:
            continue  # 周期已过, 不补授
        # 授信先行 (1005 崩溃一致性, 与 _ev_paid 同序): grant 幂等落账后
        # 才写 last_grant_period_start — 判据列永远不超前于账。
        try:
            credits = 0
            with db() as con:
                plan = orders_repo.plan_by_id(con, row["item_id"])
                if plan is None:
                    continue
                credits = int(plan["monthly_credits"] or 0)
            if credits > 0:
                from economy.billing import grant
                grant(user_id, credits, "grant_plan",
                      ref_type="subscription_reconcile", ref_id=sub_id,
                      idem_key=f"plan:{user_id}:{period_start}",
                      note=f"plus cycle {period_start}-{period_end} (reconcile)")
                credits_total += credits
                granted += 1
                log.info("subscription reconcile: user=%s sub=%s +%d credits",
                         user_id, sub_id, credits)
            with db() as con:
                orders_repo.upsert_user_plan(
                    con, user_id, row["item_id"], "active",
                    started_at=row.get("created_at") or ts,
                    renews_at=period_end, period_start=period_start)
        except Exception:
            log.exception("subscription reconcile failed user=%s sub=%s",
                          user_id, sub_id)
    return {"checked": checked, "granted": granted, "credits": credits_total}
