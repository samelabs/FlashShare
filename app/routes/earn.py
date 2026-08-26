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
    from catalog import _price_of  # per-tool cost view
    breakdown = earnings.per_tool_revenue(user["id"])
    return {
        "ok": True,
        "summary": earnings.earnings_summary(user["id"]),
        "tools": breakdown,
        "withdrawals": earnings.list_withdrawals(user["id"]),
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
