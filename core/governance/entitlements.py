"""governance: entitlements — 用户身份 → 当前 Plan → ingress entitlement.

窄职责: 消费 economy.subscription.plan_of() 的公开结果 (member/code),
映射出 24h 上传 files/bytes 配额。不认识 Creem/subscription 状态机,
不写任何状态; Payment / Membership / Credits / Entitlements / Activities
是独立业务域, 本模块只做其中 Entitlement 的单一事实源。

Free 默认配置只有一份事实源 (config 的 FLASHSHARE_DAILY_UPLOADS /
FLASHSHARE_DAILY_MB); Plan override 只在本模块的 _PLAN_INGRESS 定义。
无有效会员、未知 plan code → Free fallback。
"""

from config import DAILY_UPLOADS, DAILY_BYTES
from economy.subscription import plan_of

# plan code → 24h ingress override (files, bytes)。
PLUS_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB
_PLAN_INGRESS = {
    "plus": (500, PLUS_BYTES),
}

FREE = {"plan_code": "free", "ingress_files_24h": DAILY_UPLOADS,
        "ingress_bytes_24h": DAILY_BYTES}


def resolve(user_id: int) -> dict:
    """user_id → effective ingress entitlement。

    返回 {plan_code, ingress_files_24h, ingress_bytes_24h}。
    PLUS 判定只来自 plan_of(): member == True 且 code == 'plus';
    其余一切 (无会员 / 已失效 / 未知 code) → Free fallback。
    """
    try:
        p = plan_of(user_id)
    except Exception:
        p = None
    if p and p.get("member") and p.get("code") in _PLAN_INGRESS:
        files, byts = _PLAN_INGRESS[p["code"]]
        return {"plan_code": p["code"], "ingress_files_24h": files,
                "ingress_bytes_24h": byts}
    return dict(FREE)
