"""governance/access_projection — 只读 current access projection.

Read model, 不是新业务域: 读取三个独立事实 (Membership public result /
Entitlements / rolling-24h ingress usage) 并组合成一个 JSON-friendly
呈现。无授权权威、无状态写入、无跨域事务/cache/materialized state —
数据在返回后允许立即变化。

只消费 public capabilities:
- economy.subscription.plan_of()            (membership fact, 原样透出)
- governance.entitlements.resolve_from_membership()  (域边界单点)
- sharing.uploads.usage_24h()               (usage facts)

禁止在本模块出现: "plus" 业务判定 / config 或 _PLANS 直读 / pricing /
Credits(billing) / Creem / Activities / 直接 SQL / 授权逻辑。Credits
后续 surface 继续独立读取 spendable_balance()。
"""

from economy.subscription import plan_of
from governance.entitlements import resolve_from_membership
from sharing.uploads import usage_24h


def project(user_id: int) -> dict:
    """user_id → current access projection (canonical read model).

    Free 用户 membership 为 None — Free 是 entitlement fallback, 不是
    伪造的会员记录。expired membership 保留真实 plan_of 结果, 而
    entitlements.plan_code 落到 free: Membership fact ≠ effective
    entitlement, 域边界必须可见。expiry/download 值输出 JSON-friendly
    list (内部 tuple 不外泄)。
    """
    try:
        membership = plan_of(user_id)
    except Exception:
        membership = None
    ent = resolve_from_membership(membership)
    return {
        "membership": membership,
        "entitlements": {
            "plan_code": ent["plan_code"],
            "ingress_files_24h": ent["ingress_files_24h"],
            "ingress_bytes_24h": ent["ingress_bytes_24h"],
            "expiry_days": list(ent["expiry_days"]),
            "download_limits": list(ent["download_limits"]),
        },
        "usage": usage_24h(user_id),
    }
