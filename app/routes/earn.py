"""Earnings & withdrawals — user-facing routes (split from usertools 2026-08-26).

The money module owns: summary, per-tool revenue breakdown, withdrawal
requests + history. The tools module owns tool CRUD. They share the
ledger (core/earnings.py) as the single source of truth.
"""

import sys

sys.path.insert(0, "/opt/flashshare/core")

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import COOKIE_NAME
from security import token_hash
from database import db
from utils import now

router = APIRouter()


def _require_auth(request: Request):
    """Session-only (money surface is human-facing)."""
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    with db() as con:
        row = con.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = %s AND s.expires_at > %s""",
            (token_hash(tok), now()),
        ).fetchone()
    if not row:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    return dict(row), None


@router.get("/api/earnings")
async def my_earnings(request: Request):
    """Earnings panel data: summary + per-tool breakdown + withdrawal list."""
    user, err = _require_auth(request)
    if err:
        return err
    import earnings
    # growth contract: rev page paginates (50/page); withdrawals 20/page
    try:
        rev_pg = max(1, int(request.query_params.get("rev_page", "1") or 1))
    except ValueError:
        rev_pg = 1
    try:
        wd_pg = max(1, int(request.query_params.get("wd_page", "1") or 1))
    except ValueError:
        wd_pg = 1
    rev_rows, rev_total = earnings.revenue_lines(user["id"], limit=50, offset=(rev_pg - 1) * 50)
    wd_rows = earnings.list_withdrawals(user["id"], limit=20, offset=(wd_pg - 1) * 20)
    return {
        "ok": True,
        "summary": earnings.earnings_summary(user["id"]),
        "tools": earnings.per_tool_revenue(user["id"]),  # capped at 100 tools
        "withdrawals": wd_rows,
        "wd_total": earnings.withdrawal_count(user["id"]),
        "wd_page": wd_pg,
        "revenue_lines": rev_rows,
        "rev_total": rev_total,
        "rev_page": rev_pg,
        "min_withdrawal": earnings.MIN_WITHDRAWAL,
    }


@router.post("/api/earnings/withdraw")
async def request_withdraw(request: Request):
    """Provider asks to cash out available credits (request → operator)."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
        amount = int(raw.get("amount", 0))
        note = str(raw.get("note", ""))[:200]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import earnings
    try:
        w = earnings.request_withdrawal(user["id"], amount, note)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 422)
    return {"ok": True, "id": w["id"], "status": w["status"]}
