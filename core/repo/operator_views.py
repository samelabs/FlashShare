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

from repo.ledger import CALL_NET_CHARGE_SQL


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
        f"""
        SELECT tc.id, tc.tool_id, tc.status, tc.error,
               tc.started_at, tc.finished_at,
               ({CALL_NET_CHARGE_SQL}) AS net_charge
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


def user_uploads(con, user_id: int, limit: int) -> list:
    """The user's durable share/result files (every row, including
    already-expired ones not yet purged — the operator view shows the
    mapping table, not the consumer live gate). gokapi_id is for the
    caller's live-overlay lookup only and must not reach a template."""
    return con.execute(
        """
        SELECT up.id, up.public_id, up.gokapi_id, up.file_name,
               up.content_type, up.size_bytes, up.allowed_downloads,
               up.expire_at, up.created_at
        FROM uploads up WHERE up.user_id = %s
        ORDER BY up.id DESC LIMIT %s
        """,
        (user_id, limit)).fetchall()


# ── Tool Supply ──

def _like(q: str) -> str:
    """Operator substring search term: LIKE metacharacters matched
    literally (same escaping as _user_search)."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def tools_page(con, q: str | None = None, f_class: str | None = None,
               delisted: bool = False, featured: bool = False,
               limit: int = 50, offset: int = 0) -> tuple[list, int]:
    """One row per platform_tools entry + pricing overlay + call/earn
    aggregates. Classification is derived (owner_id NULL+local →
    Official local; owner_id NULL+http → Operator HTTP; owner_id set →
    Provider Tool) — operator-only view; the user product surface never
    exposes execution class. q searches tool_key / label (any language,
    via the stored label_json text) / owner username; f_class ∈
    {official, community}; delisted/featured keep only flagged rows."""
    where = ["TRUE"]
    params: list = []
    if q:
        like = _like(q)
        where.append("(pt.tool_key ILIKE %s OR pt.label_json ILIKE %s"
                     " OR u.username ILIKE %s)")
        params += [like, like, like]
    if f_class == "official":
        where.append("pt.owner_id IS NULL")
    elif f_class == "community":
        where.append("pt.owner_id IS NOT NULL")
    if delisted:
        where.append("pt.delisted")
    if featured:
        where.append("pt.featured")
    wsql = " AND ".join(where)
    rows = con.execute(
        """
        SELECT pt.tool_key, pt.execution, pt.visibility, pt.enabled,
               pt.delisted,
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
        WHERE """ + wsql + """
        ORDER BY (u.username IS NULL), u.username, pt.sort_order, pt.tool_key
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset)).fetchall()
    total = con.execute(
        """
        SELECT COUNT(*) AS n FROM platform_tools pt
        LEFT JOIN users u ON u.id = pt.owner_id
        WHERE """ + wsql,
        tuple(params)).fetchone()["n"]
    return rows, int(total)


# ── Tool detail (read-only) ──

def tool_row(con, tool_key: str):
    """Full platform row + owner username + pricing overlay for the
    operator detail page. Raw columns only — secret-bearing projection
    (endpoint auth) is the caller's job, never the template's."""
    return con.execute(
        """
        SELECT pt.*, u.username AS owner_name
        FROM platform_tools pt
        LEFT JOIN users u ON u.id = pt.owner_id
        WHERE pt.tool_key = %s
        """,
        (tool_key,)).fetchone()


def tool_calls_window(con, tool_key: str, since_ts: int) -> dict:
    """Call outcome counters inside the window: every finished-or-live
    attempt, ok, failed. insufficient is billed-never and listed
    separately (never 'failed')."""
    return con.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE tc.status = 'ok') AS ok,
               COUNT(*) FILTER (WHERE tc.status = 'failed') AS failed,
               COUNT(*) FILTER (WHERE tc.status = 'insufficient')
                 AS insufficient,
               COUNT(*) FILTER (WHERE tc.status = 'running') AS running
        FROM tool_calls tc
        WHERE tc.tool_id = %s AND tc.started_at >= %s
        """,
        (tool_key, since_ts)).fetchone()


def tool_earn_window(con, tool_key: str, since_ts: int) -> int:
    """Provider earn (USD-millis) attributed to this tool inside the
    window (S2 relation attribution: ledger ref → tool_call)."""
    row = con.execute(
        """
        SELECT COALESCE(SUM(l.delta), 0) AS s FROM ledger l
        JOIN tool_calls tc ON l.ref_type = 'tool_call'
          AND l.ref_id = tc.id::text
        WHERE l.kind = 'provider_earn' AND tc.tool_id = %s
          AND l.created_at >= %s
        """,
        (tool_key, since_ts)).fetchone()
    return int(row["s"])


def tool_recent_calls(con, tool_key: str, limit: int) -> list:
    return con.execute(
        f"""
        SELECT tc.id, tc.user_id, u.username, tc.status,
               tc.error, tc.result_public_id, tc.started_at, tc.finished_at,
               ({CALL_NET_CHARGE_SQL}) AS net_charge
        FROM tool_calls tc LEFT JOIN users u ON u.id = tc.user_id
        WHERE tc.tool_id = %s
        ORDER BY tc.id DESC LIMIT %s
        """,
        (tool_key, limit)).fetchall()


def tool_audit_history(con, tool_key: str, limit: int) -> list:
    """Every audit fact whose target names this tool (tool /
    remote_tool presentations + pricing rows)."""
    return con.execute(
        """
        SELECT a.id, a.actor_user_id, au.username AS actor, a.action,
               a.target_type, a.before_json, a.after_json, a.created_at
        FROM operator_audit a
        LEFT JOIN users au ON au.id = a.actor_user_id
        WHERE a.target_id = %s
          AND a.target_type IN ('tool', 'pricing', 'remote_tool')
        ORDER BY a.id DESC LIMIT %s
        """,
        (tool_key, limit)).fetchall()


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


# ── Content governance (uploads + /r results) ──

def _user_filter(user_q: str | None) -> tuple[str, list]:
    """Operator user filter: exact numeric id OR exact username.
    Returns (WHERE fragment over alias `u`, params)."""
    if not user_q:
        return "TRUE", []
    uid = int(user_q) if user_q.isdigit() and len(user_q) <= 18 else None
    return "(u.id = %s OR u.username = %s)", [uid, user_q]


def content_uploads(con, q_share: str | None, user_q: str | None,
                    tool_key: str | None,
                    limit: int, offset: int) -> tuple[list, int]:
    """Shared/uploaded files (uploads rows) with owner join. q_share
    matches public_id substring; tool_key keeps only files this tool
    PRODUCED (a call's stored result record lists every file item's
    public_id — that record is the only durable tool↔file link since
    tool_calls.upload_id retired)."""
    where = ["TRUE"]
    params: list = []
    if q_share:
        where.append("up.public_id ILIKE %s")
        params.append(_like(q_share))
    uw, up = _user_filter(user_q)
    where.append(uw)
    params += up
    if tool_key:
        where.append(
            "up.public_id IN ("
            " SELECT (item->>'public_id')"
            " FROM tool_calls tc"
            " CROSS JOIN LATERAL jsonb_array_elements("
            "   CASE WHEN tc.result_json IS NOT NULL"
            "        AND tc.result_json LIKE '{%%'"
            "   THEN COALESCE(tc.result_json::jsonb->'items',"
            "                 '[]'::jsonb)"
            "   ELSE '[]'::jsonb END) AS item"
            " WHERE tc.tool_id = %s AND item->>'kind' = 'file')")
        params.append(tool_key)
    wsql = " AND ".join(where)
    rows = con.execute(
        """
        SELECT up.id, up.public_id, up.gokapi_id, up.file_name,
               up.content_type, up.size_bytes, up.user_id,
               u.username AS owner_name,
               up.allowed_downloads, up.expire_at, up.created_at
        FROM uploads up LEFT JOIN users u ON u.id = up.user_id
        WHERE """ + wsql + """
        ORDER BY up.id DESC LIMIT %s OFFSET %s
        """,
        (*params, limit, offset)).fetchall()
    total = con.execute(
        """
        SELECT COUNT(*) AS n FROM uploads up
        LEFT JOIN users u ON u.id = up.user_id
        WHERE """ + wsql,
        tuple(params)).fetchone()["n"]
    return rows, int(total)


def content_results(con, q_result: str | None, user_q: str | None,
                    tool_key: str | None,
                    limit: int, offset: int) -> tuple[list, int]:
    """Tool results (tool_calls rows owning a /r public id). q_result
    matches result_public_id substring."""
    where = ["tc.result_public_id IS NOT NULL"]
    params: list = []
    if q_result:
        where.append("tc.result_public_id ILIKE %s")
        params.append(_like(q_result))
    uw, up = _user_filter(user_q)
    where.append(uw)
    params += up
    if tool_key:
        where.append("tc.tool_id = %s")
        params.append(tool_key)
    wsql = " AND ".join(where)
    rows = con.execute(
        f"""
        SELECT tc.id, tc.result_public_id, tc.tool_id, tc.user_id,
               u.username AS owner_name, tc.status,
               tc.started_at, tc.finished_at,
               ({CALL_NET_CHARGE_SQL}) AS net_charge
        FROM tool_calls tc LEFT JOIN users u ON u.id = tc.user_id
        WHERE """ + wsql + """
        ORDER BY tc.id DESC LIMIT %s OFFSET %s
        """,
        (*params, limit, offset)).fetchall()
    total = con.execute(
        """
        SELECT COUNT(*) AS n FROM tool_calls tc
        LEFT JOIN users u ON u.id = tc.user_id
        WHERE """ + wsql,
        tuple(params)).fetchone()["n"]
    return rows, int(total)


# ── Call log ──

def calls_page(con, tool_q: str | None, user_q: str | None,
               status: str | None, error_q: str | None,
               ts_from: int | None, ts_to: int | None,
               limit: int, offset: int) -> tuple[list, int]:
    """Every tool call with its settlement facts joined from the ledger
    (consume charge / refund of that charge / provider_earn /
    platform_commission / platform_revenue — all keyed ref_type
    'tool_call' + call id, refund keyed to the charge ledger id)."""
    where = ["TRUE"]
    params: list = []
    if tool_q:
        where.append("tc.tool_id ILIKE %s")
        params.append(_like(tool_q))
    uw, up = _user_filter(user_q)
    where.append(uw)
    params += up
    if status:
        where.append("tc.status = %s")
        params.append(status)
    if error_q:
        where.append("tc.error ILIKE %s")
        params.append(_like(error_q))
    if ts_from is not None:
        where.append("tc.started_at >= %s")
        params.append(ts_from)
    if ts_to is not None:
        where.append("tc.started_at < %s")
        params.append(ts_to)
    wsql = " AND ".join(where)
    rows = con.execute(
        f"""
        SELECT tc.id, tc.tool_id, tc.user_id, u.username, tc.status,
               tc.error, tc.started_at, tc.finished_at,
               tc.result_public_id,
               ({CALL_NET_CHARGE_SQL}) AS net_charge,
               ch.delta AS charge_delta,
               rf.delta AS refund_delta,
               pe.delta AS payout_millis,
               pc.delta AS commission_millis,
               pr.delta AS revenue_millis
        FROM tool_calls tc
        LEFT JOIN users u ON u.id = tc.user_id
        LEFT JOIN ledger ch ON ch.kind = 'consume'
             AND ch.ref_type = 'tool_call' AND ch.ref_id = tc.id::text
        LEFT JOIN ledger rf ON rf.kind = 'refund'
             AND rf.ref_type = 'ledger' AND rf.ref_id = ch.id::text
        LEFT JOIN ledger pe ON pe.kind = 'provider_earn'
             AND pe.ref_type = 'tool_call' AND pe.ref_id = tc.id::text
        LEFT JOIN ledger pc ON pc.kind = 'platform_commission'
             AND pc.ref_type = 'tool_call' AND pc.ref_id = tc.id::text
        LEFT JOIN ledger pr ON pr.kind = 'platform_revenue'
             AND pr.ref_type = 'tool_call' AND pr.ref_id = tc.id::text
        WHERE """ + wsql + """
        ORDER BY tc.id DESC LIMIT %s OFFSET %s
        """,
        (*params, limit, offset)).fetchall()
    total = con.execute(
        """
        SELECT COUNT(*) AS n FROM tool_calls tc
        LEFT JOIN users u ON u.id = tc.user_id
        WHERE """ + wsql,
        tuple(params)).fetchone()["n"]
    return rows, int(total)


# ── Overview: daily aggregation (live, no stats table) ──

def overview_daily(con, days: int, ts_to: int) -> list:
    """Per-UTC-day rows (newest first) for the overview window ending at
    ts_to: registrations, call attempts/failed, net credits charged
    (consume − refunds of consumes, same net contract as
    overview_consumer), platform income and provider earn (USD-millis).
    Aggregated live from users/tool_calls/ledger — never a stats table.
    Asset kinds are never merged: credits and the two USD-millis income
    columns stay separate fields."""
    import datetime as _dt
    end = _dt.datetime.fromtimestamp(ts_to, _dt.timezone.utc).date()
    start = end - _dt.timedelta(days=days - 1)

    def _day(ts_col: str) -> str:
        return f"((to_timestamp({ts_col}) AT TIME ZONE 'UTC')::date)"

    day_rows = con.execute(
        f"""
        SELECT d::date AS day,
          (SELECT COUNT(*) FROM users u
             WHERE {_day('u.created_at')} = d) AS registrations,
          (SELECT COUNT(*) FROM tool_calls tc
             WHERE {_day('tc.started_at')} = d) AS calls,
          (SELECT COUNT(*) FROM tool_calls tc
             WHERE {_day('tc.started_at')} = d AND tc.status = 'failed')
            AS calls_failed,
          ((SELECT -COALESCE(SUM(l.delta), 0) FROM ledger l
              WHERE l.kind = 'consume' AND {_day('l.created_at')} = d)
           - (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
              WHERE l.kind = 'refund' AND {_day('l.created_at')} = d))
            AS credits_net,
          ((SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
              WHERE l.kind = 'platform_commission'
                AND {_day('l.created_at')} = d)
           + (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
              WHERE l.kind = 'platform_revenue'
                AND {_day('l.created_at')} = d))
            AS platform_millis,
          (SELECT COALESCE(SUM(l.delta), 0) FROM ledger l
             WHERE l.kind = 'provider_earn'
               AND {_day('l.created_at')} = d) AS provider_millis
        FROM generate_series(%s::date, %s::date, '1 day') d
        ORDER BY d DESC
        """,
        (start, end)).fetchall()
    return [dict(r) for r in day_rows]


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
