"""Earnings & withdrawals — user-facing routes (split from usertools 2026-08-26).

The money module owns: summary, per-tool revenue breakdown, withdrawal
requests + history. The tools module owns tool CRUD. They share the
cash ledger (core/economy/earnings.py, usd_cent) as the single source
of truth.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "core"))

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import COOKIE_NAME
from database import db
from economy import earnings
from economy.earnings import _fmt_usd
from utils import now

router = APIRouter()


from app.web_auth import require_session_auth as _require_auth  # 批3a: 应用层公共


@router.get("/api/earnings")
async def my_earnings(request: Request):
    """Earnings panel data: summary + per-tool breakdown + withdrawal list."""
    user, err = _require_auth(request)
    if err:
        return err
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
    s = earnings.earnings_summary(user["id"])
    for k in ("earned", "available", "pending", "withdrawn"):
        s[k + "_usd"] = _fmt_usd(s[k])
    for w in wd_rows:
        w["usd"] = _fmt_usd(w["amount_minor"])
    return {
        "ok": True,
        "summary": s,
        "tools": earnings.per_tool_revenue(user["id"]),  # capped at 100 tools
        "withdrawals": wd_rows,
        "wd_total": earnings.withdrawal_count(user["id"]),
        "wd_page": wd_pg,
        "revenue_lines": rev_rows,
        "rev_total": rev_total,
        "rev_page": rev_pg,
        "min_withdrawal": earnings.MIN_WITHDRAWAL_MINOR,
        "min_withdrawal_usd": _fmt_usd(earnings.MIN_WITHDRAWAL_MINOR),
    }


@router.post("/api/earnings/withdraw")
async def request_withdraw(request: Request):
    """Provider asks to cash out available earnings (request → operator).

    Input contract is unambiguous cash minor units: amount_minor (usd_cent,
    integer). No floating-point USD anywhere in the cash path.
    """
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
        amount_minor = int(raw.get("amount_minor", 0) or 0)
        currency = str(raw.get("currency") or "USD")
        note = str(raw.get("note", ""))[:200]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        w = earnings.request_withdrawal(user["id"], amount_minor, currency, note)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 422)
    return {"ok": True, "id": w["id"], "status": w["status"]}