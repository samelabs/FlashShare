"""repo: orders/plans/packages/settings 数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional


# ── orders (充值/订阅单) ──

def insert_order(con, user_id: int, kind: str, item_id: int, provider: str,
                 amount_cents: int, currency: str, ts: int,
                 credits_snapshot: int | None = None) -> dict:
    """One-time purchase intent. Purchase identity = Order.id; the
    provider request identity is request_id=order:<id>. A pending order
    (same user, same item / same user for plans) is retried as the SAME
    logical purchase — never a second row. credits_snapshot freezes the
    purchased quantity at checkout time (package orders) — fulfillment
    reads ONLY the snapshot, never the live catalog row."""
    return con.execute(
        """
        INSERT INTO orders (user_id, kind, item_id, provider, status,
           amount_cents, currency, credits_snapshot, created_at)
           VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, %s)
           RETURNING *
        """,
        (user_id, kind, item_id, provider, amount_cents, currency,
         credits_snapshot, ts)).fetchone()


def open_package_order(con, user_id: int, item_id: int) -> Optional[dict]:
    """The single pending package checkout intent for (user, package).
    paid/failed/refunded/disputed never block a fresh purchase."""
    return con.execute(
        """
        SELECT * FROM orders
        WHERE user_id=%s AND kind='package' AND item_id=%s
          AND status='pending'
        ORDER BY id ASC LIMIT 1
        """,
        (user_id, item_id)).fetchone()


def order_by_id_for_update(con, order_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()


def has_pending_plan_order(con, plan_id: int) -> bool:
    """True when a pending plan checkout froze this plan's live price."""
    return con.execute(
        "SELECT 1 FROM orders WHERE kind='plan' AND item_id=%s"
        " AND status='pending' LIMIT 1", (plan_id,)).fetchone() is not None


def order_by_provider_id(con, provider_order_id: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM orders WHERE provider_order_id = %s",
        (provider_order_id,)).fetchone()


def mark_failed(con, order_id: int) -> None:
    """Provider-terminal failure/expiry proof only. paid/refunded/
    disputed outrank failure evidence — a later stale failure never
    downgrades a terminal order."""
    con.execute(
        "UPDATE orders SET status='failed'"
        " WHERE id=%s AND status = 'pending'", (order_id,))


def mark_refunded(con, order_id: int, ts: int) -> None:
    """package full-refund terminal state (economic reversal applied).
    disputed outranks refund — the marker never downgrades.
    refunded_at is the FIRST economic reversal projection time: replays
    and refunded→disputed flips keep the original timestamp."""
    con.execute(
        "UPDATE orders SET status='refunded',"
        " refunded_at=COALESCE(refunded_at, %s)"
        " WHERE id=%s AND status IN ('paid','refunded')",
        (ts, order_id))


def mark_disputed(con, order_id: int, ts: int) -> None:
    """dispute (chargeback) terminal state — top of the evidence
    precedence; once disputed, no event rewrites the marker.
    refunded_at keeps the FIRST reversal time across the flip."""
    con.execute(
        "UPDATE orders SET status='disputed',"
        " refunded_at=COALESCE(refunded_at, %s)"
        " WHERE id=%s AND status IN ('paid','refunded','disputed')",
        (ts, order_id))


def order_id_from_request_id(value) -> int:
    """Signed-webhook order locator: the ONLY accepted request_id shape
    is 'order:<positive integer>'. Returns the local order id
    candidate; callers must still validate the full payment contract
    (kind/product/amount/currency/provider identities) — request_id
    locates, it never authorizes."""
    if not isinstance(value, str) or not value.startswith("order:"):
        raise ValueError("checkout_request_id_invalid")
    raw = value[6:]
    if not raw.isdigit() or raw[0] == "0" and len(raw) > 1:
        raise ValueError("checkout_request_id_invalid")
    oid = int(raw)
    if oid <= 0:
        raise ValueError("checkout_request_id_invalid")
    return oid


def bind_checkout_id(con, order_id: int, checkout_id: str) -> dict | None:
    """The checkout-create response writer. Allowed ONLY when the order
    is still pending with no provider identity, or is an idempotent
    replay of the SAME ch:<id>. A late response must NEVER overwrite a
    canonical identity the webhook already advanced (ord:*/sub:*); a
    DIFFERENT checkout id on the same pending order is a hard conflict.
    Returns the current row (dict) or raises."""
    prefix = "ch:" + str(checkout_id)
    row = con.execute(
        "SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,)).fetchone()
    if row is None:
        raise KeyError("order_not_found")
    cur = row["provider_order_id"]
    if cur is None:
        con.execute(
            "UPDATE orders SET provider_order_id=%s"
            " WHERE id=%s AND status='pending' AND provider_order_id IS NULL",
            (prefix, order_id))
        return dict(con.execute(
            "SELECT * FROM orders WHERE id=%s", (order_id,)).fetchone())
    if cur == prefix:
        return dict(row)  # replay — same checkout entity
    if cur.startswith("ch:"):
        raise KeyError("checkout_identity_conflict")
    # canonical identity (ord:*/sub:*) already bound by the webhook —
    # the late create response must not rewind it
    return dict(row)


def complete_package_order(con, order_id: int, provider_order_id: str,
                           ts: int) -> dict | None:
    """package terminal-paid writer: pending/failed → paid with the
    CANONICAL provider order id (ord:*), granted=TRUE, paid_at
    COALESCE. Validates the current provider identity first: NULL,
    ch:<this checkout>, or the same canonical id; anything else fails
    loud. Returns the row, or None when the guarded transition did not
    apply (already terminal — reversal markers are NOT downgraded)."""
    row = con.execute(
        "SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,)).fetchone()
    if row is None:
        raise KeyError("order_not_found")
    # The paid final identity may never be the local provisional
    # namespace (ch:* is pending/failed-only), enforced by the WRITER
    # itself — callers cannot bypass it.
    if str(provider_order_id).startswith("ch:"):
        raise KeyError("provider_order_id_invalid_namespace")
    cur = row["provider_order_id"]
    if cur is not None and cur != provider_order_id \
            and not cur.startswith("ch:"):
        # canonical A → canonical B is a hard conflict on EVERY state —
        # checked BEFORE the terminal no-op branch so replays under a
        # different canonical id can never silently pass.
        raise KeyError("provider_identity_conflict")
    if row["status"] not in ("pending", "failed"):
        return dict(row)  # paid replay / refunded / disputed: no rewrite
    out = con.execute(
        """
        UPDATE orders
        SET status='paid', granted=TRUE,
            provider_order_id=%s,
            paid_at=COALESCE(paid_at, %s)
        WHERE id=%s AND status IN ('pending','failed')
        RETURNING *
        """,
        (provider_order_id, ts, order_id)).fetchone()
    return dict(out) if out else None


def complete_plan_checkout(con, order_id: int, sub_provider_id: str,
                           ts: int) -> dict | None:
    """plan checkout-completed writer: pending/failed → paid with
    provider_order_id='sub:<id>', paid_at COALESCE, granted stays
    FALSE (cycle credits are the paid-period path's authority).
    Subscription identity monotonicity is enforced by the WRITER, not
    the caller: pending/failed + sub:A → paid; paid + sub:A → replay
    (returned unchanged); paid + sub:B → hard conflict."""
    row = con.execute(
        "SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,)).fetchone()
    if row is None:
        raise KeyError("order_not_found")
    cur = row["provider_order_id"] or ""
    if cur and cur != sub_provider_id and not cur.startswith("ch:"):
        # sub:A → sub:B is a conflict on EVERY state (including paid
        # replay); a provisional ch:* binding converges UP to its own
        # canonical sub id (the caller's proof validated the match).
        raise KeyError("subscription_identity_conflict")
    out = con.execute(
        """
        UPDATE orders
        SET status='paid', provider_order_id=%s,
            paid_at=COALESCE(paid_at, %s)
        WHERE id=%s AND status IN ('pending','failed')
        RETURNING *
        """,
        (sub_provider_id, ts, order_id)).fetchone()
    return dict(out) if out else None


# ── plans / packages ──

def active_packages(con) -> list:
    """production projection: shared Credits Product 模型下 Package 无
    per-provider binding — 只暴露 id/code/name/credits/price_cents/
    currency。"""
    return con.execute(
        "SELECT id, code, name, credits, price_cents, currency "
        "FROM packages WHERE active ORDER BY sort_order, id").fetchall()


def package_by_id_active(con, package_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM packages WHERE id=%s AND active", (package_id,)).fetchone()


def all_plans(con) -> list:
    return con.execute(
        "SELECT * FROM plans ORDER BY sort_order, id").fetchall()


def insert_plan(con, code: str, name: str, monthly_credits: int,
                price_cents: int, ts: int, *, active: bool,
                period_days: int = 30,
                sort_order: int = 0, creem_product_id: str | None = None,
                ingress_files_24h: int = 0, ingress_bytes_24h: int = 0,
                expiry_days=(), download_limits=()) -> dict:
    """operator 创建新 paid tier 行。active 由调用方显式声明 (运营可
    直接建为不可售草稿)。权益未配 (空数组) = 诚实不可售, 由
    benefits_configured 门挡在 sellable 外, 直到运营补齐。"""
    return con.execute(
        """
        INSERT INTO plans (code, name, monthly_credits, period_days, period,
                           price_cents, creem_product_id, sort_order, active,
                           ingress_files_24h, ingress_bytes_24h,
                           expiry_days, download_limits,
                           created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'monthly', %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (code, name, monthly_credits, period_days, price_cents,
         creem_product_id, sort_order, active, ingress_files_24h,
         ingress_bytes_24h,
         list(expiry_days), list(download_limits), ts, ts)).fetchone()


def package_by_id_for_update(con, package_id: int) -> Optional[dict]:
    """行锁读 packages (update audit 的 before 必须取自锁后状态)。"""
    return con.execute(
        "SELECT * FROM packages WHERE id = %s FOR UPDATE",
        (package_id,)).fetchone()


def update_package(con, package_id: int, fields: dict, ts: int) -> Optional[dict]:
    """operator 部分更新 packages 行 (键白名单: name/credits/price/active)。"""
    sets, params = [], []
    for k in ("name", "credits", "price_cents", "active"):
        if k in fields:
            sets.append(f"{k}=%s")
            params.append(fields[k])
    if not sets:
        return package_by_id(con, package_id)
    sets.append("updated_at=%s")
    params.extend([ts, package_id])
    return con.execute(
        f"UPDATE packages SET {', '.join(sets)} WHERE id=%s RETURNING *",
        tuple(params)).fetchone()


def package_by_id(con, package_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM packages WHERE id = %s", (package_id,)).fetchone()


_PLAN_EDITABLE = ("name", "monthly_credits", "period_days", "price_cents",
                  "creem_product_id", "active", "sort_order",
                  "ingress_files_24h", "ingress_bytes_24h",
                  "expiry_days", "download_limits")


def update_plan(con, plan_id: int, fields: dict, ts: int) -> Optional[dict]:
    """operator 部分更新 plans 行 (fields 键 = _PLAN_EDITABLE 白名单,
    值域校验在 route 层 fail-closed)。code 不可改 — 行身份。
    list 字段 (expiry_days/download_limits) 传 tuple/list。"""
    sets, params = [], []
    for k in _PLAN_EDITABLE:
        if k in fields:
            sets.append(f"{k}=%s")
            params.append(fields[k])
    if not sets:
        return plan_by_id(con, plan_id)
    sets.append("updated_at=%s")
    params.extend([ts, plan_id])
    return con.execute(
        f"UPDATE plans SET {', '.join(sets)} WHERE id=%s RETURNING *",
        tuple(params)).fetchone()


def all_packages(con) -> list:
    return con.execute("SELECT * FROM packages ORDER BY credits").fetchall()


def insert_package(con, code: str, name: str, credits: int,
                   price_cents: int, ts: int, *, active: bool) -> dict:
    return con.execute(
        """
        INSERT INTO packages (code, name, credits, price_cents,
                              active, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (code, name, credits, price_cents, active, ts, ts)).fetchone()


# ── plans (subscription tiers) ──

def plan_by_id_active(con, plan_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM plans WHERE id = %s AND active", (plan_id,)).fetchone()



def membership_row(con, user_id: int) -> Optional[dict]:
    """用户唯一 current Membership (memberships.user_id PK 单行权威)。

    plan_of() 的唯一数据源; 数据库不变量保证一个 user 至多一行。
    """
    return con.execute(
        """
        SELECT m.plan_id, m.provider_subscription_id, m.status, m.started_at,
               m.renews_at, m.cancel_scheduled_at, m.last_grant_period_start,
               p.code, p.name
        FROM memberships m JOIN plans p ON p.id = m.plan_id
        WHERE m.user_id = %s
        """, (user_id,)).fetchone()


def membership_effective_rows(con, user_ids: list) -> list:
    """Batch membership facts for the effective predicate (one round trip).

    plan_of() 的批量读侧: 每 user 至多一行 (user_id PK), 只取谓词字段。
    谓词应用留在 economy.subscription — 本函数只做 SQL。
    """
    if not user_ids:
        return []
    return con.execute(
        """
        SELECT user_id, status, renews_at
        FROM memberships
        WHERE user_id = ANY(%s)
        """, (list(user_ids),)).fetchall()


def membership_by_sub(con, sub_id: str, lock: bool = False) -> Optional[dict]:
    """provider subscription → current Membership 定位 (事件/reconcile 绑定)。
    lock=True 时行读带 FOR UPDATE OF m (只锁 memberships 行, 不波及
    join 的 plans 行) — 事件处理器读到与本事务内后续 upsert 同一快照
    (迟到旧周期事件与并发新周期事件串行化)。"""
    return con.execute(
        """
        SELECT m.*, p.code, p.name FROM memberships m
        JOIN plans p ON p.id = m.plan_id
        WHERE m.provider_subscription_id = %s
        """ + (" FOR UPDATE OF m" if lock else ""),
        (f"sub:{sub_id}",)).fetchone()


class MembershipBindingError(RuntimeError):
    """不同 provider subscription 试图替换 live/pending Membership —
    普通 state update 禁止换绑; 重新首购须走显式 rebind (旧 Membership
    已 definitively non-live 后由 bind_membership 完成)。"""


def bind_membership(con, user_id: int, plan_id: int, sub_id: str,
                    status: str, started_at: int, renews_at: int | None = None,
                    period_start: int | None = None,
                    cancel_scheduled_at: int | None = None) -> dict:
    """首购/重新首购建档 (建立 provider subscription 绑定)。

    仅当 user 无 Membership 或既有 Membership definitively non-live
    (status ∉ active/canceled/past_due 且 renews_at ≤ now) 时允许;
    live/pending Membership 存在时换绑 → MembershipBindingError fail loud,
    原 Membership 不变。"""
    from economy.subscription import membership_is_occupied  # 单一语义 (禁第二套)
    from utils import now as _now
    sub_ref = f"sub:{sub_id}"
    cur = con.execute(
        "SELECT status, renews_at, provider_subscription_id FROM memberships"
        " WHERE user_id = %s FOR UPDATE", (user_id,)).fetchone()
    if cur is not None:
        if membership_is_occupied(cur["status"], cur["renews_at"], _now()):
            raise MembershipBindingError(
                f"binding conflict: user={user_id} current sub"
                f" {cur['provider_subscription_id']} is occupied (live or"
                " bound-but-unpaid)")
        # definitively non-live (expired 或 renews_at ≤ now) → 显式 rebind 允许
    return con.execute(
        """
        INSERT INTO memberships (user_id, plan_id, provider_subscription_id,
                                 status, started_at, renews_at,
                                 last_grant_period_start, cancel_scheduled_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
          plan_id = EXCLUDED.plan_id,
          provider_subscription_id = EXCLUDED.provider_subscription_id,
          status = EXCLUDED.status,
          renews_at = EXCLUDED.renews_at,
          last_grant_period_start = COALESCE(EXCLUDED.last_grant_period_start,
                                             memberships.last_grant_period_start),
          cancel_scheduled_at = EXCLUDED.cancel_scheduled_at,
          updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (user_id, plan_id, sub_ref, status, started_at, renews_at,
         period_start, cancel_scheduled_at, started_at)).fetchone()


def upsert_membership_state(con, sub_id: str, status: str,
                            renews_at: int | None,
                            plan_id: int | None = None,
                            period_start: int | None = None,
                            cancel_scheduled_at: int | None = None) -> dict:
    """同一 provider subscription 的状态/plan/period 幂等更新。
    updated_at = 写入时刻。

    绝不换绑: 目标行必须已绑定 sub:{sub_id}; 否则 MembershipBindingError。
    plan change (subscription.update) 走本函数的 plan_id 参数 — 同 sub。"""
    from utils import now as _now
    cur = con.execute(
        "SELECT user_id, plan_id FROM memberships"
        " WHERE provider_subscription_id = %s FOR UPDATE", (f"sub:{sub_id}",)).fetchone()
    if cur is None:
        raise MembershipBindingError(
            f"no membership bound to sub:{sub_id} — state update cannot bind")
    return con.execute(
        """
        UPDATE memberships SET
          plan_id = COALESCE(%s, plan_id),
          status = %s,
          renews_at = %s,
          last_grant_period_start = COALESCE(%s, last_grant_period_start),
          cancel_scheduled_at = %s,
          updated_at = %s
        WHERE provider_subscription_id = %s
        RETURNING *
        """,
        (plan_id, status, renews_at, period_start, cancel_scheduled_at,
         _now(), f"sub:{sub_id}")).fetchone()


def lock_user_for_checkout(con, user_id: int) -> None:
    """serialize concurrent subscribe intents for the same user."""
    con.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))


def open_plan_order(con, user_id: int) -> Optional[dict]:
    """该 user 最新的 open (pending) plan order — checkout intent 单点
    (orders 即 purchase intent, 无第二套 intent 表)。"""
    return con.execute(
        "SELECT * FROM orders WHERE user_id = %s AND kind = 'plan'"
        " AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (user_id,)).fetchone()


def plan_by_code(con, code: str) -> Optional[dict]:
    return con.execute("SELECT * FROM plans WHERE code = %s", (code,)).fetchone()


def period_plan_credits(con, user_id: int, period_start: int) -> int:
    """该 billing period 已获得的 plan-derived credits (grant_plan +
    grant_plan_upgrade), 从 ledger 聚合推导 — 无第二余额字段。
    subscription payment reversal (kind=revoke, idem
    payment-reversal:{txn}) 计入净额: 该周期支付被逆转后, 聚合不再
    把已撤销的 grant 当作有效 (spec §9)。package reversal 不进本聚合。"""
    prefix = f"plan:{user_id}:{period_start}"
    upg_prefix = f"plan-upgrade:{user_id}:{period_start}:"
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM ledger"
        " WHERE user_id = %s AND kind IN ('grant_plan', 'grant_plan_upgrade')"
        " AND (idem_key = %s OR idem_key LIKE %s)",
        (user_id, prefix, upg_prefix + "%")).fetchone()
    grants = int(row["total"] or 0)
    # reversals of grants whose ref transaction funded THIS period:
    # revoke rows linked by creem_transaction ref to the same period's
    # grant rows (matched via ref_id of this period's grant rows).
    rev = con.execute(
        """
        SELECT COALESCE(SUM(l.delta), 0) AS total FROM ledger l
        WHERE l.user_id = %s AND l.kind = 'revoke'
          AND l.ref_type = 'creem_transaction'
          AND l.ref_id IN (
            SELECT g.ref_id FROM ledger g
            WHERE g.user_id = %s
              AND g.kind IN ('grant_plan', 'grant_plan_upgrade')
              AND g.ref_type = 'creem_transaction'
              AND (g.idem_key = %s OR g.idem_key LIKE %s)
          )
        """,
        (user_id, user_id, prefix, upg_prefix + "%")).fetchone()
    return grants + int(rev["total"] or 0)  # revoke delta < 0


def record_period_base_payment(con, sub_id: str, period_start: int,
                               user_id: int, base_txn_id: str,
                               created_at: int) -> bool:
    """durable per-period base-payment fact (transaction record,
    NOT a ledger-grant inference). First writer for (sub_id, period_start)
    wins; later same-period charges (upgrades) and replays never overwrite.
    Returns True when this call created the record."""
    row = con.execute(
        """
        INSERT INTO subscription_period_payments
          (sub_id, period_start, user_id, base_txn_id, created_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (sub_id, period_start) DO NOTHING
        RETURNING sub_id
        """,
        (sub_id, int(period_start), int(user_id), base_txn_id,
         int(created_at))).fetchone()
    return row is not None


def period_base_payment(con, sub_id: str,
                        period_start: int) -> Optional[dict]:
    """The recorded base-cycle payment transaction for one period, or
    None when the period was never applied through apply_paid_period."""
    return con.execute(
        "SELECT * FROM subscription_period_payments"
        " WHERE sub_id = %s AND period_start = %s",
        (sub_id, int(period_start))).fetchone()


def period_has_payment_reversal(con, user_id: int, period_start: int) -> bool:
    """该 billing period 的基础支付 (base grant, idem plan:{uid}:{ps}) 是否
    已被经济反转 (对应 revoke 行 payment-reversal:{base txn} 存在) —
    反转使该周期的 paid 有效性失效: 旧 subscription.paid 重放授零、
    过期 Membership 保持过期。
    升级补差 (grant_plan_upgrade) 的退款不封周期: 基础支付仍然有效
    (refund 流程自身也绝不断开 base-paid 周期), 同周期一笔新的升级
    交易照常入账 — 其幂等键含交易号, 不会重放旧 grant。
    reversal scope = period: 只有被反转支付所属的周期被封, 新周期的
    matching paid transaction 仍可正常授予。"""
    row = con.execute(
        """
        SELECT 1 FROM ledger g
        JOIN ledger r
          ON r.user_id = g.user_id
         AND r.kind = 'revoke'
         AND r.ref_type = 'creem_transaction'
         AND r.ref_id = g.ref_id
         AND r.idem_key = 'payment-reversal:' || g.ref_id
        WHERE g.user_id = %s
          AND g.kind = 'grant_plan'
          AND g.ref_type = 'creem_transaction'
          AND g.idem_key = %s
        LIMIT 1
        """,
        (user_id, f"plan:{user_id}:{period_start}")).fetchone()
    return row is not None


def pending_downgrade(con, user_id: int) -> Optional[dict]:
    """scheduled downgrade fact (next renewal)。"""
    return con.execute(
        "SELECT target_plan_id, effective_at FROM pending_downgrades"
        " WHERE user_id = %s", (user_id,)).fetchone()


def set_pending_downgrade(con, user_id: int, target_plan_id: int,
                          effective_at: int) -> None:
    con.execute(
        """INSERT INTO pending_downgrades (user_id, target_plan_id, effective_at)
           VALUES (%s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE SET
             target_plan_id = EXCLUDED.target_plan_id,
             effective_at = EXCLUDED.effective_at""",
        (user_id, target_plan_id, effective_at))


def clear_pending_downgrade(con, user_id: int) -> None:
    con.execute("DELETE FROM pending_downgrades WHERE user_id = %s", (user_id,))


def plan_by_product_id(con, product_id: str) -> Optional[dict]:
    """subscription.update: provider product → 本地唯一 plan。

    只映射 plan_catalog 的 tier 行 codes —
    effective product states = Free/Plus/Pro only。不要求 active=TRUE:
    已停售 canonical plan 的历史 subscription lifecycle 仍可解析。
    historical third-tier / unknown product → None (caller fail loud,
    绝不把 current Membership 写成第三档)。"""
    from governance import plan_catalog
    codes = tuple(t.code for t in plan_catalog.catalog()
                  if not t.is_free)
    placeholders = ",".join(["%s"] * len(codes))
    rows = con.execute(
        f"SELECT * FROM plans WHERE creem_product_id = %s"
        f" AND code IN ({placeholders}) ORDER BY id",
        (product_id, *codes)).fetchall()
    return rows[0] if len(rows) == 1 else None


def plan_order_by_sub_id(con, sub_id: str) -> Optional[dict]:
    """事件→订阅行映射: provider_order_id='sub:{id}' 最新一条。"""
    return con.execute(
        """
        SELECT * FROM orders WHERE provider_order_id = %s
        ORDER BY id DESC LIMIT 1
        """, (f"sub:{sub_id}",)).fetchone()


def plan_by_id_for_update(con, plan_id: int) -> Optional[dict]:
    """行锁读 (operator audit 的 in-tx before snapshot 用 — 不用事务外
    stale row 作 before/after authority)。"""
    return con.execute(
        "SELECT * FROM plans WHERE id = %s FOR UPDATE",
        (plan_id,)).fetchone()


def plan_by_id(con, plan_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM plans WHERE id = %s", (plan_id,)).fetchone()



def plan_credits(con, plan_id: int) -> int:
    row = con.execute(
        "SELECT monthly_credits FROM plans WHERE id=%s", (plan_id,)).fetchone()
    return int(row["monthly_credits"] or 0) if row else 0



def membership_counts_by_plan(con, ts: int) -> dict:
    """effective membership 数 per plan_id — 与 canonical
    membership_is_effective predicate 同构 (named query, 无 domain import):
    status active/past_due/canceled 且 renews_at 非空且 > ts。"""
    return {r["plan_id"]: int(r["n"]) for r in con.execute(
        "SELECT plan_id, COUNT(*) n FROM memberships"
        " WHERE status IN ('active','past_due','canceled')"
        " AND renews_at IS NOT NULL AND renews_at > %s"
        " GROUP BY plan_id", (ts,)).fetchall()}


def pending_downgrade_counts_by_plan(con) -> dict:
    """pending downgrade 数 per target_plan_id。"""
    return {r["target_plan_id"]: int(r["n"]) for r in con.execute(
        "SELECT target_plan_id, COUNT(*) n FROM pending_downgrades"
        " GROUP BY target_plan_id").fetchall()}


# ── subscriptions (reconcile: user → orders rows with sub: binding) ──


def setting(con, key: str) -> Optional[dict]:
    return con.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
