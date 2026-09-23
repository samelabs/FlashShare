"""Earnings & withdrawals — user-facing routes (split from usertools 2026-08-26).

The money module owns: summary, per-tool revenue breakdown, withdrawal
requests + history. The tools module owns tool CRUD. They share the
ledger (core/economy/earnings.py) as the single source of truth.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "core"))

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import COOKIE_NAME
from database import db
from economy import earnings
from repo.sessions import user_by_session_token
from security import token_hash
from utils import now

router = APIRouter()


from app.web_auth import require_session_auth as _require_auth  # 批3a: 应用层公共


@router.get("/api/earnings")
async def my_earnings(request: Request):
    """Earnings panel data: summary + per-tool breakdown + withdrawal list.

    S3 provider-facing money contract — USD strings only, exact values
    from the single money authority (economy.money). No raw milli
    integers, no ambiguous amount aliases:
      summary:  earned_usd / available_usd / pending_usd / withdrawn_usd
      tool:     earned_usd
      rev line: amount_usd
      withdrawal: amount_usd
      minimum:  min_withdrawal_usd ('$10.00', cents contractual)
    """
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
    from economy.money import (millis_to_usd_cents_str,
                               millis_to_usd_str)
    s_raw = earnings.earnings_summary(user["id"])
    summary = {k + "_usd": millis_to_usd_str(s_raw[k])
               for k in ("earned", "available", "pending", "withdrawn")}
    tools_out = [{"tool": t["tool"], "tool_key": t["tool_key"],
                  "earned_usd": millis_to_usd_str(t["earned"]),
                  "payouts": t["payouts"]}
                 for t in earnings.per_tool_revenue(user["id"])]  # ≤100 tools
    rev_rows, rev_total = earnings.revenue_lines(user["id"], limit=50, offset=(rev_pg - 1) * 50)
    lines_out = [{"tool": ln["tool"], "call_id": ln["call_id"],
                  "amount_usd": millis_to_usd_str(ln["delta"]), "ts": ln["ts"]}
                 for ln in rev_rows]
    wd_rows = earnings.list_withdrawals(user["id"], limit=20, offset=(wd_pg - 1) * 20)
    wd_out = [{"id": w["id"], "status": w["status"],
               # lossless: historical sub-cent facts keep exact millis
               "amount_usd": millis_to_usd_str(w["amount"]),
               # request-time freeze from the withdrawal row itself —
               # never the live profile (later edits must not rewrite history)
               "payout_channel_snapshot": w["payout_channel_snapshot"],
               "created_at": w["created_at"]}
              for w in wd_rows]
    # current profile payout channel (what the NEXT request would freeze)
    with db() as con:
        from repo.users import payout_channel_of
        cur_ch = payout_channel_of(con, user["id"])
    return {
        "ok": True,
        "summary": summary,
        "payout_channel": (cur_ch or {}).get("payout_channel") or "",
        "tools": tools_out,
        "withdrawals": wd_out,
        "wd_total": earnings.withdrawal_count(user["id"]),
        "wd_page": wd_pg,
        "revenue_lines": lines_out,
        "rev_total": rev_total,
        "rev_page": rev_pg,
        "min_withdrawal_usd": millis_to_usd_cents_str(earnings.MIN_WITHDRAWAL_MILLIS),
    }


@router.post("/api/earnings/withdraw")
async def request_withdraw(request: Request):
    """Provider asks to cash out available earnings (request → operator).

    S3: the browser sends an exact USD decimal string ('10.25'); this
    route parses it to integer USD-millis via the single money authority
    (economy.money.parse_usd_decimal). No float ever enters the money
    path; sub-cent precision is rejected, never rounded.
    """
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
        usd_text = raw.get("amount_usd")        # strict: JSON string only
        note = str(raw.get("note", ""))[:200]
        if not isinstance(usd_text, str):
            return JSONResponse(
                {"ok": False, "error": "invalid_usd_amount"}, 422)
        from economy.money import parse_usd_decimal
        try:
            amount_millis = parse_usd_decimal(usd_text)
        except ValueError as e:
            # two distinct classes: malformed → invalid_usd_amount;
            # well-formed but sub-cent → sub_cent_amount (never rounded)
            return JSONResponse({"ok": False, "error": str(e)}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        w = earnings.request_withdrawal(user["id"], amount_millis, note)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 422)
    return {"ok": True, "id": w["id"], "status": w["status"]}