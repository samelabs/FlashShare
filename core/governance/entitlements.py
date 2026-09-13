"""governance: entitlements — 用户身份 → 当前 Plan → effective entitlement.

窄职责: 消费 economy.subscription.plan_of() 的公开结果 (member/code),
映射出用户的 effective entitlement (24h ingress 配额 + delivery 值域)。
不认识 Creem/subscription 状态机, 不写任何状态; Payment / Membership /
Credits / Entitlements / Activities 是独立业务域, 本模块只做其中
Entitlement 的单一事实源。

分层契约 (与 governance.delivery 分工, 禁止合并):
- delivery.validate_delivery() 只回答"值是否属于平台 canonical protocol";
- 本模块只回答"该用户当前是否有权使用该值"。

Entitlement 值必须显式声明 — 禁止从 platform enum 自动推导 (去差集/
直接引用)。平台未来新增 protocol 值不等于任何 plan 自动获得授权;
两个 contract 数值允许重叠, 但各自独立定义。模块初始化时 fail-closed
校验: 所有 entitlement 值必须是 platform-supported 值, 非法即
ConfigurationError (不静默剔除)。

Free 默认配置只有一份事实源 (config 的 FLASHSHARE_DAILY_UPLOADS /
FLASHSHARE_DAILY_MB)。无有效会员、未知 plan code → Free fallback。
"""

from config import DAILY_UPLOADS, DAILY_BYTES
from economy.subscription import plan_of
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS


class ConfigurationError(RuntimeError):
    """Entitlement 声明引用了平台 protocol 不支持的值 — fail closed."""


# 唯一 plan entitlement 定义: 每一档显式声明被授予的值。
# 不从 EXPIRY_DAYS / DOWNLOAD_LIMITS 推导 — protocol 扩容 ≠ 自动授权。
PLUS_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB

FREE = {
    "plan_code": "free",
    "ingress_files_24h": DAILY_UPLOADS,
    "ingress_bytes_24h": DAILY_BYTES,
    "expiry_days": (1, 3, 7),
    "download_limits": (1, 10, 50, 100, 200),
}
_PLANS = {
    "plus": {
        "plan_code": "plus",
        "ingress_files_24h": 500,
        "ingress_bytes_24h": PLUS_BYTES,
        "expiry_days": (1, 3, 7, 30),
        "download_limits": (1, 10, 50, 100, 200, 1000),
    },
    "pro": {
        "plan_code": "pro",
        "ingress_files_24h": 2000,
        "ingress_bytes_24h": 20 * 1024 ** 3,
        "expiry_days": (1, 3, 7, 30),
        "download_limits": (1, 10, 50, 100, 200, 1000),
    },
}


def _validate_against_protocol(spec: dict, name: str) -> None:
    """fail-closed: entitlement 声明值 ⊆ platform protocol 值。

    依赖方向是"显式声明 → 检查被 protocol 支持", 不是从 protocol
    生成 entitlement。任何越界值直接 raise, 不剔除不回落。
    """
    bad_ed = [d for d in spec["expiry_days"] if d not in EXPIRY_DAYS]
    bad_dl = [d for d in spec["download_limits"] if d not in DOWNLOAD_LIMITS]
    if bad_ed or bad_dl:
        raise ConfigurationError(
            f"entitlement '{name}' declares values unsupported by the "
            f"platform protocol: expiry={bad_ed} downloads={bad_dl}")


_validate_against_protocol(FREE, "free")
for _code, _spec in _PLANS.items():
    _validate_against_protocol(_spec, _code)


class EntitlementConfigurationError(RuntimeError):
    """可售/active plan 缺 entitlement profile — 配置缺口 fail loud。

    Read model (plan_of/projection 消费方) 传播; authorization
    (resolve) 既有 exception → Free fail-safe 保持。
    """


def entitlement_profile_exists(plan_code: str) -> bool:
    """公开单点: 某 plan code 是否有显式 entitlement profile。"""
    return plan_code in _PLANS


def plan_entitlement(plan_code: str) -> dict:
    """Public read-only entitlement projection for membership product UI."""
    return dict(_require_profile(plan_code))


def _require_profile(plan_code: str) -> dict:
    spec = _PLANS.get(plan_code)
    if spec is None:
        raise EntitlementConfigurationError(
            f"active plan '{plan_code}' has no entitlement profile")
    return spec


def resolve_from_membership(membership: dict | None) -> dict:
    """plan_of() public result → effective entitlement (纯函数单点)。"""
    if membership and membership.get("member"):
        return dict(_require_profile(membership["code"]))
    return dict(FREE)


def resolve(user_id: int) -> dict:
    """user_id → effective entitlement。"""
    try:
        p = plan_of(user_id)
        return resolve_from_membership(p)
    except Exception:
        return dict(FREE)


def check_delivery(user_id: int, delivery: dict) -> dict | None:
    """delivery entitlement 单点检查 (authorization, 非 protocol validation)。"""
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
