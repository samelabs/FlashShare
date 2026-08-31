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


# ── settings (KV) ──

def setting(con, key: str) -> Optional[dict]:
    return con.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
