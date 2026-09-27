"""repo: operator_audit — append-only governance fact (single writer).

规则: record() 必须使用 caller transaction (con 必传) — audit 与业务
本地 mutation 同一 DB transaction, 不自开第二连接。生产代码只有
INSERT + 只读查询; 本表永不 UPDATE / DELETE, 永不成为任何价格/余额/
Plan/Tool/Withdrawal/权限的 Source of Truth。

快照纪律: 调用方必须传 allowlist 投影后的 safe dict (见
core/operator 命令层投影函数), 本 repo 不做二次过滤 — 但保留
_secret_scan 断言作为最后防线: 快照值里出现明显凭据形状即拒绝写入。
"""
import json
from typing import Optional

# 按 target_type 的 allowlist — record() 强制: unknown target_type
# fail loud; snapshot 任一键不属于该 target 的 allowlist 即 fail loud
# (caller transaction rollback)。不做 value-redaction/blacklist。
TARGET_SAFE_KEYS = {
    "pricing": {"tool_id", "cost", "enabled"},
    "plan": {"id", "code", "name", "monthly_credits", "period_days",
             "period", "price_cents", "currency", "creem_product_id",
             "sort_order", "active", "ingress_files_24h",
             "ingress_bytes_24h", "expiry_days", "download_limits"},
    "package": {"id", "code", "name", "credits", "price_cents",
                "currency", "sort_order", "active"},
    "tool": {"tool_key", "featured", "sort_order", "enabled", "delisted"},
    "user_credits": {"user_id", "delta", "balance_before",
                     "balance_after", "ledger_id"},
    "withdrawal": {"id", "user_id", "amount", "status", "reviewed_at",
                   "reviewed_by", "paid_at", "paid_by", "reason",
                   "resolved_by"},
    "remote_tool": {"tool_key", "tool_id", "execution", "tool_type",
                    "visibility", "enabled", "featured", "sort_order",
                    "files_min", "files_max", "outputs", "accept", "cost"},
    "setting": {"rate"},
    "payment_review": {"status", "txn_id", "event_id", "resolved_by"},
    "user": {"status", "pw_present", "pw_reset", "logins_revoked"},
}

_SECRET_MARKERS = ("password", "token", "secret", "auth_header",
                   "auth_token", "endpoint_json", "vault", "apikey",
                   "api_key", "credential", "session")


def _project(snap: Optional[dict], target_type: str) -> Optional[str]:
    """target-specific allowlist 投影 + 凭据形状防线; None 透传。
    非法 snapshot 直接 raise → caller transaction rollback (不扫描、
    不篡改、不静默裁剪)。"""
    if snap is None:
        return None
    if not isinstance(snap, dict):
        raise ValueError("audit snapshot must be dict or None")
    allowed = TARGET_SAFE_KEYS.get(target_type)
    if allowed is None:
        raise ValueError(f"audit_target_type_unknown:{target_type}")
    out = {}
    for k, v in snap.items():
        if k not in allowed:
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
    # target_type must be validated UNCONDITIONALLY, before any INSERT or
    # snapshot projection — a bogus target with NULL snapshots must still
    # fail with zero insert.
    target_type = str(target_type)
    if target_type not in TARGET_SAFE_KEYS:
        raise ValueError(f"audit_target_type_unknown:{target_type}")
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
         _project(before, str(target_type)), _project(after, str(target_type)),
         int(ts))).fetchone()


def recent_pair_rows(con, action: str, target_type: str, target_id: str,
                     actor_user_id: int, created_at_gte: int,
                     limit: int = 20) -> list:
    """newest-first 只读: 满足 (action, target_type, target_id,
    actor_user_id, created_at >= created_at_gte) 的 before/after 快照对
    rows。纯 persistence query — 快照相等/分类判定属于调用方。"""
    return con.execute(
        """SELECT before_json, after_json FROM operator_audit
        WHERE action=%s AND target_type=%s AND target_id=%s
          AND actor_user_id=%s AND created_at >= %s
        ORDER BY id DESC LIMIT %s""",
        (str(action), str(target_type), str(target_id),
         int(actor_user_id), int(created_at_gte), int(limit))).fetchall()


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
