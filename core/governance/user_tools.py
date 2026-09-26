"""governance/user_tools — 用户工具治理面 (批3a 从 usertools 路由上收).

owner 视图投影 + 调用统计。规则归属治理域; 路由只管 HTTP。
"""

import json

from database import db
from repo.catalog import owner_rows_joined
from repo.ledger import call_stats_by_tool, earn_sum_joined_calls

MAX_TOOLS_PER_USER = 20


def _owner_rows(user_id: int) -> list:
    from repo.catalog import owner_rows_joined
    with db() as con:
        rows = owner_rows_joined(con, user_id)
    return [dict(r) for r in rows]




def _public_item(row: dict) -> dict:
    """Owner-surface view of a user tool (no endpoint secrets)."""
    decl = {}
    try:
        decl = json.loads(row.get("endpoint_json") or "{}")
    except Exception:
        pass
    label_full = json.loads(row["label_json"] or "{}")
    desc_full = None
    try:
        desc_full = json.loads(row["desc_json"]) if row.get("desc_json") else None
    except Exception:
        desc_full = None
    return {
        "tool_key": row["tool_key"],
        "tool_id": row["tool_id"],
        "execution": row["execution"],
        # 完整结构 (spec v3 语言模型): 编辑回填需要全部语言; 呈现层自行回落
        "label": label_full or row["tool_id"],
        "desc": desc_full,
        "visibility": row["visibility"],
        "enabled": row["enabled"],
        "trusted": bool(row["trusted"]),
        "cost": int(row.get("cost") or 0),
        "priced_enabled": bool(row.get("priced")),
        "url": decl.get("url", ""),
        "timeout_s": decl.get("timeout_s", 60),
        "has_auth": bool(decl.get("auth_token")),
        "accept": json.loads(row["accept_json"]) if row.get("accept_json") else None,
        # stored window verbatim — files_min 0 (text-only tools) is legal
        "files_min": int(row["files_min"]),
        "files_max": int(row["files_max"]),
        "outputs": json.loads(row["outputs_json"]),
        "params": json.loads(row["params_json"]) if row.get("params_json") else [],
        "calls": _call_stats(row["tool_key"], row.get("owner_id")),
    }




def _call_stats(tool_key: str, owner_id: int | None = None) -> dict:
    """Per-tool call stats + provider earnings (accurate join via payout
    ledger rows referencing the call fact)."""
    from repo.ledger import call_stats_by_tool, earn_sum_joined_calls
    with db() as con:
        row = call_stats_by_tool(con, tool_key)
        earned = 0
        if owner_id is not None:
            earned = earn_sum_joined_calls(con, owner_id, tool_key)
    return {"ok": row["ok"], "total": row["total"], "earned": earned}


