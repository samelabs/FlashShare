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
    con.execute(
        "UPDATE orders SET status='paid', granted=TRUE, paid_at=%s WHERE id=%s",
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
    return con.execute(
        "SELECT id, code, name, credits, price_cents, currency, creem_price_id "
        "FROM packages WHERE active ORDER BY sort_order, id").fetchall()


def package_by_id_active(con, package_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM packages WHERE id=%s AND active", (package_id,)).fetchone()


def package_credits(con, package_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT credits FROM packages WHERE id=%s", (package_id,)).fetchone()


def all_plans(con) -> list:
    return con.execute("SELECT * FROM plans ORDER BY price_cents").fetchall()


def insert_plan(con, code: str, name: str, monthly_credits: int,
                price_cents: int, ts: int) -> dict:
    return con.execute(
        """
        INSERT INTO plans (code, name, monthly_credits, price_cents,
                           active, created_at, updated_at)
        VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING *
        """,
        (code, name, monthly_credits, price_cents, ts, ts)).fetchone()


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


def plan_by_id_active(con, plan_id: int) -> Optional[dict]:
    return con.execute(
        "SELECT * FROM plans WHERE id = %s AND active", (plan_id,)).fetchone()


def upsert_user_plan(con, user_id: int, plan_id: int, status: str,
                     started_at: int, renews_at: int | None,
                     period_start: int | None = None,
                     cancel_scheduled_at: int | None = None) -> dict:
    """状态机落库: 同 (user,plan) 复购=UPDATE。所有列幂等写 (SET 同值无副作用)。"""
    return con.execute(
        """
        INSERT INTO user_plans (user_id, plan_id, status, started_at,
                                renews_at, last_grant_period_start,
                                cancel_scheduled_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, plan_id) DO UPDATE SET
          status = EXCLUDED.status,
          renews_at = EXCLUDED.renews_at,
          last_grant_period_start = EXCLUDED.last_grant_period_start,
          cancel_scheduled_at = EXCLUDED.cancel_scheduled_at,
          updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (user_id, plan_id, status, started_at, renews_at,
         period_start, cancel_scheduled_at, started_at)).fetchone()


def plan_of(con, user_id: int) -> Optional[dict]:
    """身份单点读取: 用户当前订阅行 (含 plan code)。"""
    return con.execute(
        """
        SELECT up.status, up.started_at, up.renews_at, up.cancel_scheduled_at,
               p.code, p.name
        FROM user_plans up JOIN plans p ON p.id = up.plan_id
        WHERE up.user_id = %s
        """, (user_id,)).fetchone()


def plan_order_by_sub_id(con, sub_id: str) -> Optional[dict]:
    """事件→订阅行映射: provider_order_id='sub:{id}' 最新一条。"""
    return con.execute(
        """
        SELECT * FROM orders WHERE provider_order_id = %s
        ORDER BY id DESC LIMIT 1
        """, (f"sub:{sub_id}",)).fetchone()


def latest_plan_order_for_user(con, user_id: int) -> Optional[dict]:
    return con.execute(
        """
        SELECT * FROM orders WHERE user_id = %s AND kind = 'plan'
        ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()


# ── settings (KV) ──

def setting(con, key: str) -> Optional[dict]:
    return con.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
