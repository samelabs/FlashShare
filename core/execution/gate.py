"""execution/gate — 计量闸门 (批2e, 从 api.py 收编的重构核心).

单一入口 run(): replay/attempt 判定 → 扣费 → 编排回调 → 成功(fact+分账 同事务)
/ 失败(失败记账+退费)。🔴1 修复: 编排回调抛 ExecutionError 时统一走 fail 路径
(计费后的任何失败都不再裸 return)。

调用面: web(api.py) / v1(apiv1) / mcp / selftest 全部走这里。
"""

import json
import logging
import secrets

from database import db
from economy.billing import InsufficientCredits, consume, refund, spendable_balance
from governance.pricing import get_pricing
from repo.ledger import (call_by_client_ref, call_count_by_ref,
                         insert_call_failed, insert_call_ok,
                         insert_provider_earn_if_absent, total_balance)
from utils import now

logger = logging.getLogger("flashshare")

# 分账佣金: 治理参数, 归 governance 域 (批2e 上收)
from governance.commission import commission_rate


def _finish(user, tool, client_ref, cost, started, result, upload) -> dict:
    """成功收口: fact 行 + 分账/平台收入 同一事务 (红线)。

    经济闭合 (0930): cost = 作者 payout (用户工具) 或 平台收入 (系统工具);
    用户工具再拆平台佣金 — 每笔计费调用全额可追溯。"""
    call_id = None
    payout = 0
    try:
        rid = dict(upload).get("id") if upload is not None else None
        owner_id = tool.get("owner_id")
        if cost > 0 and owner_id and owner_id != user["id"]:
            rate = commission_rate()
            payout = cost - (cost * rate // 100)
        with db() as con:
            row = insert_call_ok(
                con, client_ref, user["id"], tool["id"], cost, rid,
                result.get("publicId"), json.dumps(result)[:4000],
                started, now())
            call_id = row["id"]
            if cost > 0:
                from governance.platform import record_platform_income
                if payout > 0 and owner_id is not None:
                    # 路径A: 作者分账 + 平台佣金, 同一事务
                    base = total_balance(con, owner_id)
                    insert_provider_earn_if_absent(
                        con, owner_id, payout, base + payout, call_id,
                        f"payout:{call_id}", f"{tool['id']} payout", now())
                    record_platform_income(
                        con, call_id, "platform_commission", cost - payout,
                        f"{tool['id']} commission", now())
                elif not owner_id:
                    # 路径B: 系统工具直销全额归平台
                    record_platform_income(
                        con, call_id, "platform_revenue", cost,
                        f"{tool['id']} revenue", now())
    except Exception:
        logger.exception("metering record failed (call still returned)")
        call_id = None
    out = {"ok": True, "type": tool["type"], "result": result}
    if call_id:
        out["call_id"] = call_id
    if cost > 0:
        try:
            out["balance"] = spendable_balance(user["id"])
        except Exception:
            logger.exception("balance fetch failed for UI chip")
    return out


def _record_failure(user, tool, client_ref, cost, started, error, status="failed") -> None:
    try:
        with db() as con:
            insert_call_failed(con, client_ref, user["id"], tool["id"],
                               status, cost, str(error)[:500], started, now())
    except Exception:
        logger.exception("failed-call record failed")


def _refund(user, client_ref, tool_id, cost, reason) -> None:
    if cost <= 0:
        return
    try:
        with db() as con:
            n = call_count_by_ref(con, user["id"], client_ref)
        refund(user["id"], cost, ref_type="tool_call", ref_id=client_ref,
               idem_key=f"refund:call:{user['id']}:{client_ref}:{int(n) - 1}",
               note=f"{tool_id}: {reason}"[:200])
    except Exception:
        logger.exception("refund failed for call %s", client_ref)


async def run(user: dict, tool: dict, req, orchestrate):
    """闸门主体。

    Args:
      user: 认证用户 dict
      tool: resolve() 归一后的工具行 (id/type/owner_id/execution/...)
      req: 请求对象 (tool_id/client_ref/public_id[s]/source_token[s]/params)
      orchestrate: async callable(upload_list, params) -> result dict;
                   失败抛 ExecutionError(code) — 闸门负责记账+退费。
    Returns: (payload_dict | None, error_json | None, status_code)
    """
    from . import ExecutionError

    tool_id = req.tool_id
    params = req.params

    price = get_pricing(tool_id)
    if not price["enabled"]:
        return None, {"ok": False, "error": "tool_disabled"}, 403
    client_ref = req.client_ref or secrets.token_hex(16)

    with db() as con:
        prior = call_by_client_ref(con, user["id"], client_ref)
        attempt = call_count_by_ref(con, user["id"], client_ref)

    if prior:
        if prior["status"] == "ok" and prior["result_json"]:
            return ({"ok": True, "type": tool["type"],
                     "result": json.loads(prior["result_json"]),
                     "replay": True, "call_id": prior["id"]},
                    None, None)

    idem_key = f"call:{user['id']}:{client_ref}:{attempt}"
    cost = int(price["cost"])
    started = now()
    if cost > 0:
        try:
            consume(user["id"], cost, "consume", ref_type="tool_call",
                    idem_key=idem_key, note=f"{tool_id}")
        except InsufficientCredits as exc:
            _record_failure(user, tool, client_ref, cost, started,
                            f"balance:{exc.balance}", status="insufficient")
            return (None,
                    {"ok": False, "error": "err_insufficient_credits",
                     "balance": exc.balance, "cost": exc.cost}, 402)

    try:
        result = await orchestrate(params)
    except ExecutionError as exc:
        _record_failure(user, tool, client_ref, cost, started, exc.code)
        _refund(user, client_ref, tool_id, cost, exc.code)
        return None, {"ok": False, "error": exc.code}, 400
    except Exception:
        logger.exception("tool execution failed")
        _record_failure(user, tool, client_ref, cost, started, "tool_failed")
        _refund(user, client_ref, tool_id, cost, "tool_failed")
        return None, {"ok": False, "error": "tool_failed"}, 500

    return _finish(user, tool, client_ref, cost, started, result, None), None, None
