"""governance: entitlements — 用户身份 → 当前 Plan → effective entitlement.

窄职责: 消费 economy.subscription.plan_of() 的公开结果 (member/code),
映射出用户的 effective entitlement (24h ingress 配额 + delivery 值域)。
不认识 Creem/subscription 状态机, 不写任何状态; Payment / Membership /
Credits / Entitlements / Activities 是独立业务域, 本模块只做其中
Entitlement 的单一事实源。

分层契约 (与 governance.delivery 分工, 禁止合并):
- delivery.validate_delivery() 只回答"值是否属于平台 canonical protocol";
- 本模块只回答"该用户当前是否有权使用该值"。

Free 默认配置只有一份事实源 (config 的 FLASHSHARE_DAILY_UPLOADS /
FLASHSHARE_DAILY_MB; delivery Free 值域为平台枚举去掉会员档); Plan
override 只在本模块的 _PLANS 定义一份。无有效会员、未知 plan code →
Free fallback。
"""

from config import DAILY_UPLOADS, DAILY_BYTES
from economy.subscription import plan_of
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS

PLUS_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB

# 唯一 plan override 定义: plan code → entitlement 增量。
# Free 档 delivery = 平台枚举去掉会员专属值 (30 天 / 1000 次)。
FREE = {
    "plan_code": "free",
    "ingress_files_24h": DAILY_UPLOADS,
    "ingress_bytes_24h": DAILY_BYTES,
    "expiry_days": tuple(d for d in EXPIRY_DAYS if d != 30),
    "download_limits": tuple(d for d in DOWNLOAD_LIMITS if d != 1000),
}
_PLANS = {
    "plus": {
        "plan_code": "plus",
        "ingress_files_24h": 500,
        "ingress_bytes_24h": PLUS_BYTES,
        "expiry_days": EXPIRY_DAYS,
        "download_limits": DOWNLOAD_LIMITS,
    },
}


def resolve(user_id: int) -> dict:
    """user_id → effective entitlement。

    返回 {plan_code, ingress_files_24h, ingress_bytes_24h,
    expiry_days, download_limits}。PLUS 判定只来自 plan_of():
    member == True 且 code 在 _PLANS; 其余一切 (无会员 / 已失效 /
    未知 code) → Free fallback。
    """
    try:
        p = plan_of(user_id)
    except Exception:
        p = None
    if p and p.get("member") and p.get("code") in _PLANS:
        return dict(_PLANS[p["code"]])
    return dict(FREE)


def check_delivery(user_id: int, delivery: dict) -> dict | None:
    """delivery entitlement 单点检查 (authorization, 非 protocol validation)。

    消费 resolve(user_id), 不自行读 plan_of()。输入 delivery 应为已通过
    platform validate_delivery() 的 canonical 值。有权 → None;
    无权 → 结构化 denial:

    {"error": "err_expiry_not_entitled", "field": "expiry_days",
     "requested": 30, "allowed": [1, 3, 7], "plan_code": "free"}

    entitlement denial 是 403 语义, 绝不是 invalid_request。
    """
    ent = resolve(user_id)
    ed = delivery.get("expiry_days")
    if ed not in ent["expiry_days"]:
        return {"error": "err_expiry_not_entitled", "field": "expiry_days",
                "requested": ed, "allowed": list(ent["expiry_days"]),
                "plan_code": ent["plan_code"]}
    dl = delivery.get("allowed_downloads")
    if dl not in ent["download_limits"]:
        return {"error": "err_download_limit_not_entitled",
                "field": "allowed_downloads", "requested": dl,
                "allowed": list(ent["download_limits"]),
                "plan_code": ent["plan_code"]}
    return None
