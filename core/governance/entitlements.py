"""governance: entitlements — membership → effective entitlement.

瘦映射层 (Tier 本体化): profile 数值归 plans 行 (plan_catalog.Tier.
benefits), 本模块只保留 Membership → Entitlement 的政策映射与两处
边界语义, 不再内嵌任何档位字典:

- read 面 (projection/API): active 会员的 tier 行缺失/权益未配 →
  EntitlementConfigurationError fail loud — 付费用户绝不悄悄降 Free。
- authorization 面 (resolve): 读取异常 → Free fail-safe (既有语义,
  与 read 面的 fail-loud 是有意共存的两条边界)。

显式声明校验 (声明值 ⊆ platform protocol) 由 plan_catalog.
validate_config() 在启动/运营写入时执行 — 协议扩容 ≠ 自动授权
的语义不变。
"""

from economy.subscription import plan_of
from governance import plan_catalog


class EntitlementConfigurationError(RuntimeError):
    """可售/active plan 缺权益配置 — 配置缺口 fail loud。

    Read model (plan_of/projection 消费方) 传播; authorization
    (resolve) 既有 exception → Free fail-safe 保持。
    """


def entitlement_profile_exists(plan_code: str) -> bool:
    """公开单点: 某 plan code 的行存在且权益已配。"""
    t = plan_catalog.by_code(plan_code)
    return t is not None and t.benefits_configured


def _require_profile(plan_code: str) -> dict:
    t = plan_catalog.by_code(plan_code)
    if t is None or not t.benefits_configured:
        # 用户已付费成为未配档: read 面必须 fail loud — 不悄悄给 Free。
        raise EntitlementConfigurationError(
            f"active plan '{plan_code}' has no entitlement profile")
    return t.benefits.as_dict(t.code)


def resolve_from_membership(membership: dict | None) -> dict:
    """plan_of() public result → effective entitlement (纯函数单点)。

    输入只能是 economy.subscription.plan_of() 的公开结果 (或 None):
    member == True → 该 tier 行的 benefits (未配档 =
    EntitlementConfigurationError); 其余一切 (None / member=false) →
    free 行 benefits。Membership → Entitlement 的政策映射只存在于
    本模块 — 禁止任何消费方复制 member/code 判定。
    """
    if membership and membership.get("member"):
        return _require_profile(membership["code"])
    return plan_catalog.free_tier().benefits.as_dict("free")


def resolve(user_id: int) -> dict:
    """user_id → effective entitlement。

    判定只来自 plan_of() 的公开结果, 经 resolve_from_membership
    单点映射; plan_of 抛错时按既有 Free fallback 行为处理。
    """
    try:
        p = plan_of(user_id)
        return resolve_from_membership(p)
    except Exception:
        # Authorization fail-safe (既有语义): membership fact 读取失败
        # → Free。read 面 (projection/API) 不走本函数, 会 fail loud —
        # 两个边界共存。
        return plan_catalog.free_tier().benefits.as_dict("free")


def check_delivery(user_id: int, delivery: dict) -> dict | None:
    """delivery entitlement 单点检查 (authorization, 非 protocol validation)。

    消费 resolve(user_id), 不自行读 plan_of()。输入 delivery 应为已通过
    platform validate_delivery() 的 canonical 值。有权 → None;
    无权 → 结构化 denial (403 语义, 绝不是 invalid_request)。
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
