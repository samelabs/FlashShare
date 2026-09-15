"""repo: orders/plans/packages/settings 数据访问 (批1b).

规则: 函数=(con,...)->rows, 参数化, 无事务/无业务判断。
"""

from typing import Optional


# ── orders (充值/订阅单) ──

def insert_order(con, user_id: int, kind: str, item_id: int, provider: str,
                 amount_cents: int, currency: str, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO orders (user_id, kind, item_id, provider, status,
           amount_cents, currency, created_at)
           VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)
           RETURNING *
        """,
        (user_id, kind, item_id, provider, amount_cents, currency, ts)).fetchone()


def order_by_id_for_update(con, order_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()


def order_by_provider_id_for_update(con, provider_order_id: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM orders WHERE provider_order_id = %s FOR UPDATE",
        (provider_order_id,)).fetchone()


def order_by_provider_id(con, provider_order_id: str) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM orders WHERE provider_order_id = %s",
        (provider_order_id,)).fetchone()


def mark_paid(con, order_id: int, ts: int) -> None:
    """package/top-up fulfillment 终态戳: grant 已由 billing 落账,
    granted=TRUE 与 paid 同点写入。"""
    con.execute(
        "UPDATE orders SET status='paid', granted=TRUE, paid_at=%s WHERE id=%s",
        (ts, order_id))


def mark_subscription_checkout_completed(con, order_id: int, ts: int) -> None:
    """subscription checkout.completed 窄 writer: 只推进 payment/order
    状态 (paid + paid_at), 不写 granted — cycle credits 由后续
    subscription.paid 的 paid-period path 授予, 两事实分离。"""
    con.execute(
        "UPDATE orders SET status='paid', paid_at=%s WHERE id=%s",
        (ts, order_id))


def mark_failed(con, order_id: int) -> None:
    con.execute(
        "UPDATE orders SET status='failed' WHERE id=%s", (order_id,))


def set_provider_order_id(con, order_id: int, provider_order_id: str) -> None:
    con.execute(
        "UPDATE orders SET provider_order_id=%s WHERE id=%s",
        (provider_order_id, order_id))


# ── plans / packages ──

def active_packages(con) -> list:
    """production projection: shared Credits Product 模型下 Package 无
    per-provider binding — 只暴露 id/code/name/credits/price_cents/
    currency。历史 per-package Creem 值仅存 DB legacy 列, 不进此投影。"""
    return con.execute(
        "SELECT id, code, name, credits, price_cents, currency "
        "FROM packages WHERE active ORDER BY sort_order, id").fetchall()


def package_by_id_active(con, package_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM packages WHERE id=%s AND active", (package_id,)).fetchone()


def package_credits(con, package_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT credits FROM packages WHERE id=%s", (package_id,)).fetchone()


def all_plans(con) -> list:
    return con.execute(
        "SELECT * FROM plans ORDER BY sort_order, id").fetchall()


def insert_plan(con, code: str, name: str, monthly_credits: int,
                price_cents: int, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO plans (code, name, monthly_credits, price_cents,
                           active, created_at, updated_at)
        VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING *
        """,
        (code, name, monthly_credits, price_cents, ts, ts)).fetchone()


def update_plan_pricing(con, plan_id: int, price_cents: int,
                        creem_product_id: str | None, ts: int) -> Optional[dict]:
    """operator 只可改 price / Creem Product ID (code 与 canonical
    monthly credits 不可经此修改 — 见 samelabs route 的 canonical 校验)。"""
    return con.execute(
        """
        UPDATE plans SET price_cents=%s, creem_product_id=%s, updated_at=%s
        WHERE id=%s RETURNING *
        """, (price_cents, creem_product_id, ts, plan_id)).fetchone()


def all_packages(con) -> list:
    return con.execute("SELECT * FROM packages ORDER BY credits").fetchall()


def insert_package(con, code: str, name: str, credits: int,
                   price_cents: int, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO packages (code, name, credits, price_cents,
                              active, created_at, updated_at)
        VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING *
        """,
        (code, name, credits, price_cents, ts, ts)).fetchone()


# ── plans (subscription tiers) ──

def active_plans(con) -> list:
    return con.execute(
        "SELECT * FROM plans WHERE active ORDER BY sort_order, price_cents"
    ).fetchall()


def canonical_plans(con) -> list:
    """sellable membership catalog: plan_catalog.sellable_catalog 的行集
    (plans 行是唯一产品层事实源; 参数化, 不在 repo 复制业务 tier)。
    DB 中历史其它 plan rows 保留但不进 sellable/current Membership。"""
    from governance import plan_catalog
    codes = tuple(t.code for t in plan_catalog.sellable_catalog())
    if not codes:
        return []
    placeholders = ",".join(["%s"] * len(codes))
    return con.execute(
        f"SELECT * FROM plans WHERE active AND code IN ({placeholders})"
        " ORDER BY sort_order, price_cents", tuple(codes)).fetchall()


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


def membership_by_sub(con, sub_id: str) -> Optional[dict]:
    """provider subscription → current Membership 定位 (事件/reconcile 绑定)。"""
    return con.execute(
        """
        SELECT m.*, p.code, p.name FROM memberships m
        JOIN plans p ON p.id = m.plan_id
        WHERE m.provider_subscription_id = %s
        """, (f"sub:{sub_id}",)).fetchone()


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
                            started_at: int, renews_at: int | None,
                            plan_id: int | None = None,
                            period_start: int | None = None,
                            cancel_scheduled_at: int | None = None) -> dict:
    """同一 provider subscription 的状态/plan/period 幂等更新。

    绝不换绑: 目标行必须已绑定 sub:{sub_id}; 否则 MembershipBindingError。
    plan change (subscription.update) 走本函数的 plan_id 参数 — 同 sub。"""
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
         started_at, f"sub:{sub_id}")).fetchone()


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
    grant_plan_upgrade), 从 ledger 聚合推导 — 无第二余额字段。"""
    prefix = f"plan:{user_id}:{period_start}"
    upg_prefix = f"plan-upgrade:{user_id}:{period_start}:"
    row = con.execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM ledger"
        " WHERE user_id = %s AND kind IN ('grant_plan', 'grant_plan_upgrade')"
        " AND (idem_key = %s OR idem_key LIKE %s)",
        (user_id, prefix, upg_prefix + "%")).fetchone()
    return int(row["total"] or 0)


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

    只映射 governance.tiers 的 canonical tier codes (plus/pro) —
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


def plan_by_id(con, plan_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM plans WHERE id = %s", (plan_id,)).fetchone()



def plan_credits(con, plan_id: int) -> int:
    row = con.execute(
        "SELECT monthly_credits FROM plans WHERE id=%s", (plan_id,)).fetchone()
    return int(row["monthly_credits"] or 0) if row else 0



def sweep_expired_plans(con, ts: int) -> int:
    """过期 membership → expired。状态转移 only, 授信永不在此发生。"""
    cur = con.execute(
        """
        UPDATE memberships SET status='expired', updated_at=%s
        WHERE status IN ('active','past_due') AND renews_at IS NOT NULL
          AND renews_at < %s
        RETURNING user_id
        """, (ts, ts))
    return len(cur.fetchall())


# ── subscriptions (reconcile: user → orders rows with sub: binding) ──


def setting(con, key: str) -> Optional[dict]:
    return con.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
