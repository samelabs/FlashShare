"""Earnings & withdrawals — user-facing routes.

Credits never enter this API. All numeric earnings/withdrawal amounts are
integer USD cents; display strings are derived with to_usd().
"""

import sys

sys.path.insert(0, "/opt/flashshare/core")

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from economy import earnings

router = APIRouter()

from app.web_auth import require_session_auth as _require_auth


@router.get("/api/earnings")
async def my_earnings(request: Request):
    user, err = _require_auth(request)
    if err:
        return err
    try:
        rev_pg = max(1, int(request.query_params.get("rev_page", "1") or 1))
    except ValueError:
        rev_pg = 1
    try:
        wd_pg = max(1, int(request.query_params.get("wd_page", "1") or 1))
    except ValueError:
        wd_pg = 1
    rev_rows, rev_total = earnings.revenue_lines(
        user["id"], limit=50, offset=(rev_pg - 1) * 50)
    wd_rows = earnings.list_withdrawals(
        user["id"], limit=20, offset=(wd_pg - 1) * 20)
    s = earnings.earnings_summary(user["id"])
    for k in ("earned", "available", "pending", "withdrawn"):
        s[k + "_usd"] = earnings.to_usd(s[k])
    for w in wd_rows:
        w["usd"] = earnings.to_usd(w["amount"])
    return {
        "ok": True,
        "summary": s,
        "tools": earnings.per_tool_revenue(user["id"]),
        "withdrawals": wd_rows,
        "wd_total": earnings.withdrawal_count(user["id"]),
        "wd_page": wd_pg,
        "revenue_lines": rev_rows,
        "rev_total": rev_total,
        "rev_page": rev_pg,
        "min_withdrawal": earnings.MIN_WITHDRAWAL,
        "min_withdrawal_usd": earnings.to_usd(earnings.MIN_WITHDRAWAL),
    }


@router.post("/api/earnings/withdraw")
async def request_withdraw(request: Request):
    """Provider requests a USD withdrawal; amount is converted to cents."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
        usd = float(raw.get("usd", 0) or 0)
        amount = int(round(usd * 100))
        note = str(raw.get("note", ""))[:200]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        w = earnings.request_withdrawal(user["id"], amount, note)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 422)
    return {"ok": True, "id": w["id"], "status": w["status"]}
