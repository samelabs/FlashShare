"""repo: operator_views — back-office READ projections only.

Parameterized SQL: select / join / pagination / aggregate. NO mutation,
NO business fallback. Asset-kind classification (spendable vs provider
vs platform) is decided by the caller passing EXPLICIT kind sets —
this repo never guesses an asset class from a kind string.

Replaces the retired mixed-unit repo.reports authority
(overview_stats / users_overview / ledger_page) for the new control
plane. Ledger rows are NEVER summed across kinds into one number.
"""

from typing import Sequence


# Effective membership predicate — isomorphic to
# economy.subscription.membership_is_effective and
# repo.orders.membership_counts_by_plan (named SQL, no domain import):
# status active/past_due/canceled AND renews_at NOT NULL AND > ts.
# Takes ONE %s parameter (ts). Alias `m` = memberships.
_M_EFFECTIVE = ("(m.status IN ('active','past_due','canceled')"
                " AND m.renews_at IS NOT NULL AND m.renews_at > %s)")

# Display status that never presents a lapsed row as current: effective
# rows keep their stored status; live-state rows past renews_at read
# 'expired'; live-state rows never paid (renews_at NULL) read 'not_started'.
# Takes TWO %s parameters (ts, ts).
_M_EFFECTIVE_STATUS = (
    "CASE WHEN m.user_id IS NULL THEN NULL"
    " WHEN " + _M_EFFECTIVE + " THEN m.status"
    " WHEN m.status IN ('active','past_due','canceled')"
    "   AND m.renews_at IS NOT NULL AND m.renews_at <= %s THEN 'expired'"
    " WHEN m.status IN ('active','past_due','canceled') THEN 'not_started'"
    " ELSE m.status END")


# ── Overview ──

def overview_product(con) -> dict:
    """Product facts: users, tool call outcomes, live priced tools."""
    return con.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM users) AS users,
          -- historical fact, independent of the CURRENT catalog: a
          -- retired/deleted tool's past successes still happened
          (SELECT COUNT(*) FROM tool_calls tc
             WHERE tc.status = 'ok') AS calls_ok,
          -- the label says Failed: only status='failed'. running and
          -- insufficient are in-flight / non-billed, never "failed"
          (SELECT COUNT(*) FROM tool_calls tc
             WHERE tc.status = 'failed') AS calls_failed,
          (SELECT COUNT(*) FROM platform_tools pt
             WHERE pt.enabled
               AND EXISTS (SELECT 1 FROM pricing p
                           WHERE p.tool_id = pt.tool_key
                             AND p.enabled)) AS tools_live_priced
        """).fetchone()


def overview_consumer(con, spendable_kinds: Sequence[str],
                      ts_now: int) -> dict:
    """Consumer Credits facts — SPENDABLE_KINDS only (grants/consumes),
    never provider/platform cash kinds. Tool charges are NET of the
    refunds that reverse them (refund rows reference the exact consume
    row: ref_type='ledger', ref_id=consume id). Membership count =
    effective memberships only."""
    return con.execute(
        """
        SELECT
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.kind = ANY(%s)) AS credits_outstanding,
          ((SELECT COALESCE(-SUM(l.delta), 0) FROM ledger l
              WHERE l.kind = 'consume')
           - (SELECT COALESCE(SUM(r.delta), 0) FROM ledger r
              JOIN ledger c ON r.ref_type = 'ledger'
                AND r.ref_id = c.id::text AND c.kind = 'consume'
              WHERE r.kind = 'refund')) AS credits_charged_net,
          (SELECT COUNT(*) FROM memberships m
             WHERE """ + _M_EFFECTIVE + """) AS memberships_effective
        """,
        (list(spendable_kinds), ts_now)).fetchone()


def overview_provider(con) -> dict:
    """Provider economy facts — all USD-millis (S3/S4)."""
    return con.execute(
        """
        SELECT
          (SELECT COALESCE(SUM(amount), 0) FROM withdrawals
             WHERE status = 'pending') AS wd_pending_millis,
          -- approved = debited from the earnings pool, NOT yet paid out;
          -- paid = external payout recorded. Never merged into one label.
          (SELECT COALESCE(SUM(amount), 0) FROM withdrawals
             WHERE status = 'approved') AS wd_approved_unpaid_millis,
          (SELECT COALESCE(SUM(amount), 0) FROM withdrawals
             WHERE status = 'paid') AS wd_paid_millis,
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.kind = 'provider_earn') AS provider_earn_millis
        """).fetchone()


def overview_platform(con) -> dict:
    """Platform income — commission + revenue, USD-millis, separated."""
    return con.execute(
        """
        SELECT
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.kind = 'platform_commission')
            AS commission_millis,
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.kind = 'platform_revenue')
            AS revenue_millis
        """).fetchone()


def recent_tool_calls(con, limit: int) -> list:
    return con.execute(
        """
        SELECT tc.id, tc.tool_id, tc.status, tc.started_at, u.username
        FROM tool_calls tc LEFT JOIN users u ON u.id = tc.user_id
        ORDER BY tc.id DESC LIMIT %s
        """,
        (limit,)).fetchall()


# ── Users ──

def _user_search(q: str | None) -> tuple[str, tuple]:
    """Operator user search: username / display name / email substring
    (case-insensitive, LIKE metacharacters matched literally) or exact
    numeric id. Returns (WHERE fragment, params)."""
    if not q:
        return "TRUE", ()
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    uid = int(q) if q.isdigit() and len(q) <= 18 else None
    return ("(u.username ILIKE %s OR u.display_name ILIKE %s"
            " OR u.email ILIKE %s OR u.id = %s)",
            (like, like, like, uid))


def users_page(con, q: str | None, limit: int, offset: int,
               spendable_kinds: Sequence[str],
               ts_now: int) -> tuple[list, int]:
    """User rows with SEPARATED asset columns. Spendable = SUM over
    SPENDABLE_KINDS only. Provider available = provider_earn −
    approved/paid withdrawals − pending reserved (canonical earnings
    authority). Never SUM(all ledger). Plan column carries the
    membership's effective status — a lapsed row is never current."""
    where, wparams = _user_search(q)
    rows = con.execute(
        """
        SELECT u.id, u.username, u.display_name, u.email, u.status,
          u.created_at, u.last_login_at,
          u.payout_channel IS NOT NULL AND btrim(u.payout_channel) <> ''
            AS has_payout_channel,
          p.code AS plan_code,
          """ + _M_EFFECTIVE_STATUS + """ AS membership_status,
          COALESCE(""" + _M_EFFECTIVE + """, FALSE) AS membership_effective,
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.user_id = u.id AND l.kind = ANY(%s))
            AS spendable_credits,
          ((SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
              WHERE l.user_id = u.id AND l.kind = 'provider_earn')
           - (SELECT COALESCE(SUM(w.amount), 0) FROM withdrawals w
              WHERE w.user_id = u.id AND w.status IN ('approved','paid'))
           - (SELECT COALESCE(SUM(w.amount), 0) FROM withdrawals w
              WHERE w.user_id = u.id AND w.status = 'pending'))
            AS provider_available_millis,
          (SELECT COUNT(*) FROM tool_calls tc
             WHERE tc.user_id = u.id AND tc.status = 'ok')
            AS calls_ok,
          (SELECT COUNT(*) FROM platform_tools pt
             WHERE pt.owner_id = u.id) AS provider_tools
        FROM users u
        LEFT JOIN memberships m ON m.user_id = u.id
        LEFT JOIN plans p ON p.id = m.plan_id
        WHERE """ + where + """
        ORDER BY u.id DESC LIMIT %s OFFSET %s
        """,
        (ts_now, ts_now, ts_now, list(spendable_kinds), *wparams,
         limit, offset)).fetchall()
    total = con.execute(
        "SELECT COUNT(*) AS n FROM users u WHERE " + where,
        wparams).fetchone()["n"]
    return rows, int(total)


# ── User detail (read-only) ──

def user_profile(con, user_id: int, spendable_kinds: Sequence[str],
                 ts_now: int):
    """Profile basics + separated balances + current membership row
    (with effective status). None when the user does not exist."""
    return con.execute(
        """
        SELECT u.id, u.username, u.display_name, u.email, u.status,
          u.x_username, u.payout_channel, u.created_at, u.last_login_at,
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.user_id = u.id AND l.kind = ANY(%s))
            AS spendable_credits,
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.user_id = u.id AND l.kind = 'provider_earn')
            AS provider_earned_millis,
          (SELECT COALESCE(SUM(w.amount), 0) FROM withdrawals w
             WHERE w.user_id = u.id AND w.status = 'pending')
            AS provider_pending_millis,
          ((SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
              WHERE l.user_id = u.id AND l.kind = 'provider_earn')
           - (SELECT COALESCE(SUM(w.amount), 0) FROM withdrawals w
              WHERE w.user_id = u.id AND w.status IN ('approved','paid'))
           - (SELECT COALESCE(SUM(w.amount), 0) FROM withdrawals w
              WHERE w.user_id = u.id AND w.status = 'pending'))
            AS provider_available_millis,
          p.code AS plan_code, p.name AS plan_name,
          m.status AS membership_stored_status,
          """ + _M_EFFECTIVE_STATUS + """ AS membership_status,
          COALESCE(""" + _M_EFFECTIVE + """, FALSE) AS membership_effective,
          m.provider_subscription_id, m.started_at AS membership_started_at,
          m.renews_at AS membership_renews_at,
          m.cancel_scheduled_at AS membership_cancel_scheduled_at
        FROM users u
        LEFT JOIN memberships m ON m.user_id = u.id
        LEFT JOIN plans p ON p.id = m.plan_id
        WHERE u.id = %s
        """,
        (list(spendable_kinds), ts_now, ts_now, ts_now,
         user_id)).fetchone()


def user_recent_orders(con, user_id: int, limit: int) -> list:
    return con.execute(
        """
        SELECT o.id, o.kind, o.item_id, o.provider, o.provider_order_id,
               o.amount_cents, o.currency, o.credits_snapshot, o.status,
               o.granted, o.created_at, o.paid_at, o.refunded_at
        FROM orders o WHERE o.user_id = %s
        ORDER BY o.id DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


def user_recent_tool_calls(con, user_id: int, limit: int) -> list:
    return con.execute(
        """
        SELECT tc.id, tc.tool_id, tc.status, tc.cost_snapshot, tc.error,
               tc.started_at, tc.finished_at
        FROM tool_calls tc WHERE tc.user_id = %s
        ORDER BY tc.id DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


def user_recent_withdrawals(con, user_id: int, limit: int) -> list:
    return con.execute(
        """
        SELECT w.id, w.amount, w.status, w.payout_channel_snapshot,
               w.payout_ref, w.created_at, w.reviewed_at, w.paid_at
        FROM withdrawals w WHERE w.user_id = %s
        ORDER BY w.id DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


def user_recent_ledger(con, user_id: int, limit: int) -> list:
    """Raw per-row facts (every kind). Rows are listed, never summed
    across kinds; the caller labels each row's unit from explicit kind
    sets."""
    return con.execute(
        """
        SELECT l.id, l.kind, l.delta, l.balance_after, l.ref_type,
               l.ref_id, l.note, l.created_at
        FROM ledger l WHERE l.user_id = %s
        ORDER BY l.id DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


# ── Tool Supply ──

def tools_page(con) -> list:
    """One row per platform_tools entry + pricing overlay + call/earn
    aggregates. Classification is derived (owner_id NULL+local →
    Official local; owner_id NULL+http → Operator HTTP; owner_id set →
    Provider Tool) — operator-only view; the user product surface never
    exposes execution class."""
    return con.execute(
        """
        SELECT pt.tool_key, pt.execution, pt.visibility, pt.enabled,
               pt.featured, pt.sort_order, pt.owner_id,
               u.username AS owner_name,
               -- runtime-effective trust exactly as resolve computes it
               -- (first-party ⇒ trusted; provider rows ⇒ stored flag)
               (pt.owner_id IS NULL OR pt.trusted) AS trusted,
               p.cost, p.enabled AS priced,
               (SELECT COUNT(*) FROM tool_calls tc
                  WHERE tc.tool_id = pt.tool_key AND tc.status = 'ok')
                 AS calls_ok,
               (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
                  JOIN tool_calls tc ON l.ref_type = 'tool_call'
                    AND l.ref_id = tc.id::text
                  WHERE l.kind = 'provider_earn' AND tc.tool_id = pt.tool_key)
                 AS provider_earn_millis
        FROM platform_tools pt
        LEFT JOIN users u ON u.id = pt.owner_id
        LEFT JOIN pricing p ON p.tool_id = pt.tool_key
        ORDER BY (u.username IS NULL), u.username, pt.sort_order, pt.tool_key
        """).fetchall()


# ── Commerce: Orders ──

def orders_page(con, limit: int, offset: int) -> tuple[list, int]:
    rows = con.execute(
        """
        SELECT o.id, o.user_id, u.username, o.kind, o.item_id,
               o.provider, o.provider_order_id, o.amount_cents,
               o.currency, o.credits_snapshot, o.status, o.granted,
               o.created_at, o.paid_at, o.refunded_at
        FROM orders o LEFT JOIN users u ON u.id = o.user_id
        ORDER BY o.id DESC LIMIT %s OFFSET %s
        """,
        (limit, offset)).fetchall()
    total = con.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    return rows, int(total)


# ── Commerce: Memberships ──

def memberships_page(con, limit: int, offset: int,
                     ts_now: int) -> tuple[list, int]:
    """Membership authority = memberships.user_id PK row. Never guessed
    from orders."""
    return _membership_rows(con, limit, offset, ts_now)


def _membership_rows(con, limit, offset, ts_now) -> tuple[list, int]:
    rows = con.execute(
        """
        SELECT m.user_id, u.username, m.plan_id, p.code AS plan_code,
               p.name AS plan_name, m.provider_subscription_id,
               m.status, m.started_at, m.renews_at, m.cancel_scheduled_at,
               m.updated_at,
               """ + _M_EFFECTIVE_STATUS + """ AS effective_status,
               """ + _M_EFFECTIVE + """ AS effective
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        LEFT JOIN plans p ON p.id = m.plan_id
        ORDER BY m.user_id DESC LIMIT %s OFFSET %s
        """,
        (ts_now, ts_now, ts_now, limit, offset)).fetchall()
    total = con.execute("SELECT COUNT(*) AS n FROM memberships"
                        ).fetchone()["n"]
    return rows, int(total)


# ── Economy: Credits (SPENDABLE_KINDS only) ──

def credits_page(con, user_id: int | None, kind: str | None,
                 spendable_kinds: Sequence[str],
                 limit: int, offset: int) -> tuple[list, int]:
    """Generic mixed-asset ledger stream is RETIRED. Only spendable
    Credits kinds, filtered by explicit kind sets from the caller."""
    if kind is not None and kind not in spendable_kinds:
        return [], 0
    rows = con.execute(
        """
        SELECT l.id, l.user_id, u.username, l.kind, l.delta,
               l.balance_after, l.note, l.created_at
        FROM ledger l LEFT JOIN users u ON u.id = l.user_id
        WHERE l.kind = ANY(%s)
          AND (%s IS NULL OR l.user_id = %s)
          AND (%s IS NULL OR l.kind = %s)
        ORDER BY l.id DESC LIMIT %s OFFSET %s
        """,
        (list(spendable_kinds), user_id, user_id, kind, kind,
         limit, offset)).fetchall()
    params: list = [list(spendable_kinds)]
    if user_id is not None:
        params.append(user_id)
    if kind is not None:
        params.append(kind)
    total = con.execute(
        "SELECT COUNT(*) AS n FROM ledger WHERE kind = ANY(%s)"
        + (" AND user_id = %s" if user_id is not None else "")
        + (" AND kind = %s" if kind is not None else ""),
        tuple(params)).fetchone()["n"]
    return rows, int(total)


# ── Economy: Provider Earnings (USD-millis; S2 relation attribution) ──

def provider_earnings_page(con, limit: int, offset: int) -> tuple[list, int]:
    """provider_earn rows only; Tool identity via
    ref_type='tool_call' → tool_calls.id → tool_id (S2). No note
    identity."""
    rows = con.execute(
        """
        SELECT l.id, l.user_id, u.username, l.delta, l.ref_type, l.ref_id,
               tc.tool_id AS attributed_tool,
               l.note, l.created_at
        FROM ledger l
        LEFT JOIN users u ON u.id = l.user_id
        LEFT JOIN tool_calls tc ON l.ref_type = 'tool_call'
               AND l.ref_id = tc.id::text
        WHERE l.kind = 'provider_earn'
        ORDER BY l.id DESC LIMIT %s OFFSET %s
        """,
        (limit, offset)).fetchall()
    total = con.execute(
        "SELECT COUNT(*) AS n FROM ledger WHERE kind = 'provider_earn'"
    ).fetchone()["n"]
    return rows, int(total)


# ── Economy: Platform Income (USD-millis) ──

def platform_income_page(con, limit: int, offset: int) -> tuple[list, int]:
    """platform_commission + platform_revenue only — never merged with
    Credits kinds."""
    rows = con.execute(
        """
        SELECT l.id, l.user_id, u.username, l.kind, l.delta, l.note,
               l.created_at
        FROM ledger l LEFT JOIN users u ON u.id = l.user_id
        WHERE l.kind IN ('platform_commission','platform_revenue')
        ORDER BY l.id DESC LIMIT %s OFFSET %s
        """,
        (limit, offset)).fetchall()
    total = con.execute(
        "SELECT COUNT(*) AS n FROM ledger "
        "WHERE kind IN ('platform_commission','platform_revenue')"
    ).fetchone()["n"]
    return rows, int(total)


# ── Audit ──

def audit_page(con, limit: int, offset: int) -> tuple[list, int]:
    rows = con.execute(
        """
        SELECT a.id, a.actor_user_id, au.username AS actor, a.action,
               a.target_type, a.target_id, a.before_json, a.after_json,
               a.created_at
        FROM operator_audit a
        LEFT JOIN users au ON au.id = a.actor_user_id
        ORDER BY a.id DESC LIMIT %s OFFSET %s
        """,
        (limit, offset)).fetchall()
    total = con.execute("SELECT COUNT(*) AS n FROM operator_audit"
                        ).fetchone()["n"]
    return rows, int(total)
