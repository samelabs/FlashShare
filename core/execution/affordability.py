"""execution/affordability — 只读 Credits affordability preflight 单点.

窄职责两件事:
1. effective_quote(user_id, tool): listed price + self-owned free 规则的
   唯一组合点 — pricing 表是商业合同真相 (user_cost_credits +
   provider_payout_minor + payout_currency), provider 调自己工具
   cost=0, 其他用户 = listed cost。gate 与 preflight 共用, 禁止第二份。
2. check_affordability(user_id, tool): advisory 只读检查 — 余额足 → None;
   不足 → 复用既有 err_insufficient_credits contract。cost=0 不查余额
   不拒绝; pricing disabled 的权威语义仍在 gate (preflight 不建立新的
   disabled 机制)。

preflight = advisory read-only; consume() = authoritative mutation。
本模块绝不 import/调用 consume, 绝不预扣/预留 credits — race-safe 的
最终权威永远是 gate → claim → consume → execute。

quotes 不做任何 credits→cash 换算: payout_minor 是 pricing 行的独立
声明字段, 与 cost 没有数学关系。
"""

from economy.billing import spendable_balance
from governance.pricing import get_pricing


def effective_quote(user_id: int, tool: dict) -> dict:
    """The single combination point of listed price × self-owned free.

    Returns {"enabled": bool, "cost": int, "payout_minor": int} —
    cost is the effective credits charge the authoritative gate applies
    for this (user, tool) pair; payout_minor is the declared provider
    cash payout (usd_cent) settled on success for third-party tools.
    Self-owned calls cost 0 credits and accrue no payout.
    """
    price = get_pricing(tool["tool_key"])
    listed = int(price.get("user_cost_credits") or 0)
    self_owned = tool.get("owner_id") == user_id
    cost = 0 if self_owned else listed
    payout_minor = 0 if self_owned else int(
        price.get("provider_payout_minor") or 0)
    return {"enabled": bool(price["enabled"]), "cost": cost,
            "payout_minor": payout_minor}


def check_affordability(user_id: int, tool: dict) -> dict | None:
    """Advisory read-only affordability check. Never a reservation.

    None → affordable, free, self-owned, or pricing-disabled (availability
    is deferred to the gate's authoritative tool_disabled). Denial reuses
    the existing err_insufficient_credits contract verbatim — no parallel
    error key.
    """
    quote = effective_quote(user_id, tool)
    if not quote["enabled"]:
        # Tool availability is the gate's authority (tool_disabled 403);
        # the preflight never adjudicates disabled-ness and does not
        # touch the balance on this path.
        return None
    cost = quote["cost"]
    if cost <= 0:
        return None  # free (or self-owned priced) — no balance lookup
    balance = spendable_balance(user_id)
    if balance < cost:
        return {"error": "err_insufficient_credits",
                "balance": balance, "cost": cost}
    return None
