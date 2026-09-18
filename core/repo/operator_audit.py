"""repo: operator_audit — append-only governance fact (single writer).

规则: record() 必须使用 caller transaction (con 必传) — audit 与业务
本地 mutation 同一 DB transaction, 不自开第二连接。生产代码只有
INSERT + 只读查询; 本表永不 UPDATE / DELETE, 永不成为任何价格/余额/
Plan/Tool/Withdrawal/权限的 Source of Truth。

快照纪律: 调用方必须传 allowlist 投影后的 safe dict (见
routes.samelabs._audit_* 投影函数), 本 repo 不做二次过滤 — 但保留
_secret_scan 断言作为最后防线: 快照值里出现明显凭据形状即拒绝写入。
"""
import json
from typing import Optional

# 允许出现在 audit JSON 里的键 (按 target_type 投影后再进来;
# 这里是全库 allowlist 上限 — 不在名单内的键直接 fail loud)。
SAFE_KEYS = {
    # pricing
    "tool_id", "cost", "enabled", "updated_by",
    # plan
    "id", "code", "name", "monthly_credits", "period_days", "period",
    "price_cents", "currency", "creem_product_id", "sort_order", "active",
    "ingress_files_24h", "ingress_bytes_24h", "expiry_days",
    "download_limits",
    # package (subset of plan keys + credits)
    "credits",
    # tool presentation
    "tool_key", "featured",
    # remote tool (safe projection ONLY — never URL/endpoint/auth)
    "execution", "tool_type", "visibility", "multi", "accept",
    # user_credits
    "user_id", "delta", "balance_before", "balance_after", "ledger_id",
    # withdrawal
    "amount", "status", "processed_by",
}

_SECRET_MARKERS = ("password", "token", "secret", "auth_header",
                   "auth_token", "endpoint_json", "vault", "apikey",
                   "api_key", "credential", "session")


def _project(snap: Optional[dict]) -> Optional[str]:
    """allowlist 投影 + 凭据形状防线; None 透传 (before 可为 NULL)。"""
    if snap is None:
        return None
    if not isinstance(snap, dict):
        raise ValueError("audit snapshot must be dict or None")
    out = {}
    for k, v in snap.items():
        if k not in SAFE_KEYS:
            raise ValueError(f"audit_snapshot_key_not_allowed:{k}")
        lk = k.lower()
        if any(m in lk for m in _SECRET_MARKERS):
            raise ValueError(f"audit_snapshot_secret_key:{k}")
        out[k] = v
    return json.dumps(out, ensure_ascii=False)


def record(con, actor_user_id: int, action: str, target_type: str,
           target_id: str, before: Optional[dict], after: Optional[dict],
           ts: int) -> dict:
    """Append one audit fact INSIDE the caller's transaction.

    con 必传 — 本函数绝不自开事务; audit 写失败随业务 transaction
    一起 rollback。
    """
    if con is None:
        raise ValueError("operator_audit.record requires a caller transaction")
    return con.execute(
        """
        INSERT INTO operator_audit (actor_user_id, action, target_type,
                                    target_id, before_json, after_json,
                                    created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id, actor_user_id, action, target_type, target_id,
                  before_json, after_json, created_at
        """,
        (int(actor_user_id), str(action), str(target_type), str(target_id),
         _project(before), _project(after), int(ts))).fetchone()


def list_recent(con, limit: int = 50, offset: int = 0) -> list:
    """newest-first 只读分页 (canonical tests / future surface)."""
    return con.execute(
        """
        SELECT id, actor_user_id, action, target_type, target_id,
               before_json, after_json, created_at
        FROM operator_audit
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """, (int(limit), int(offset))).fetchall()


def exists_for_ledger(con, ledger_id: int) -> bool:
    """credits.adjust 幂等伴生: 该 ledger 行是否已有对应 audit 事实
    (one ledger movement ↔ one audit fact)。"""
    row = con.execute(
        "SELECT 1 FROM operator_audit WHERE action = 'credits.adjust'"
        " AND after_json->>'ledger_id' = %s LIMIT 1",
        (str(int(ledger_id)),)).fetchone()
    return row is not None


def count(con) -> int:
    row = con.execute("SELECT COUNT(*) AS c FROM operator_audit").fetchone()
    return int(row["c"])
