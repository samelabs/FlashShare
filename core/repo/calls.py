"""Read projections for the user's tool-call records (account surface).

Facts stay in tool_calls/ledger. Net charge rides the same
CALL_NET_CHARGE_SQL relation projection the page has always used
(refunds via refund → consume → call) — note text is never parsed.
Every query is per-user and DB-aggregated; no per-row round trips."""

from repo.ledger import CALL_NET_CHARGE_SQL


def paged(con, user_id: int, size: int, offset: int,
          since_ts: int | None = None) -> list:
    """One page of the user's calls, newest first, optionally
    time-boxed (started_at >= since_ts, epoch seconds)."""
    sql = ("SELECT tc.id, tc.tool_id, tc.status, tc.started_at,"
           " tc.finished_at, tc.result_public_id, tc.result_json,"
           " tc.error, pt.label_json,"
           f" ({CALL_NET_CHARGE_SQL}) AS net_charge,"
           " COALESCE(("
           "  SELECT SUM(r.delta) FROM ledger r"
           "  JOIN ledger c ON r.kind = 'refund' AND r.ref_type = 'ledger'"
           "   AND r.ref_id = c.id::text"
           "  WHERE c.kind = 'consume' AND c.ref_type = 'tool_call'"
           "    AND c.ref_id = tc.id::text), 0) AS refunded"
           " FROM tool_calls tc"
           " LEFT JOIN platform_tools pt ON pt.tool_key = tc.tool_id"
           " WHERE tc.user_id = %s")
    params: list = [user_id]
    if since_ts is not None:
        sql += " AND tc.started_at >= %s"
        params.append(since_ts)
    sql += " ORDER BY tc.id DESC LIMIT %s OFFSET %s"
    params += [size, offset]
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def count(con, user_id: int, since_ts: int | None = None) -> int:
    """The user's REAL filtered call count — the page-bounding fact (no
    global page ceiling: history past page 10,000 stays reachable while
    offsets stay bounded by the true total)."""
    sql = "SELECT COUNT(*) AS n FROM tool_calls tc WHERE tc.user_id = %s"
    params: list = [user_id]
    if since_ts is not None:
        sql += " AND tc.started_at >= %s"
        params.append(since_ts)
    return int(con.execute(sql, params).fetchone()["n"])


def status_summary(con, user_id: int, since_ts: int | None = None) -> dict:
    """Per-status call counts + the same net-charge projection, summed
    by the database. Statuses are the stored call states — no success
    rate is redefined here."""
    sql = ("SELECT tc.status, COUNT(*) AS n,"
           f" SUM({CALL_NET_CHARGE_SQL}) AS net"
           " FROM tool_calls tc WHERE tc.user_id = %s")
    params: list = [user_id]
    if since_ts is not None:
        sql += " AND tc.started_at >= %s"
        params.append(since_ts)
    sql += " GROUP BY tc.status"
    return {r["status"]: {"n": int(r["n"]), "net": int(r["net"] or 0)}
            for r in con.execute(sql, params).fetchall()}


def daily_net(con, user_id: int, since_ts: int) -> dict:
    """Daily net-charge buckets (UTC day = epoch // 86400), sparse and
    DB-aggregated: {day: net}."""
    rows = con.execute(
        f"SELECT (tc.started_at / 86400) AS d,"
        f" SUM({CALL_NET_CHARGE_SQL}) AS net"
        " FROM tool_calls tc WHERE tc.user_id = %s AND tc.started_at >= %s"
        " GROUP BY 1",
        (user_id, since_ts)).fetchall()
    return {int(r["d"]): int(r["net"] or 0) for r in rows}
