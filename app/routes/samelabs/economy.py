"""/samelabs/economy — asset-separated ledgers + withdrawal queue.

Credits = SPENDABLE_KINDS only (Credits). Provider Earnings =
provider_earn (USD-millis, S2 relation attribution). Platform Income =
platform_commission + platform_revenue (USD). Withdrawals = S4
lifecycle queue. The generic mixed "积分流水" page is retired."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from economy.billing import SPENDABLE_KINDS
from economy.money import millis_to_usd_str
from repo import operator_views

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 50


@router.get("/samelabs/economy/credits")
async def credits(request: Request, user_id: int | None = None,
                  kind: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = max(1, page)
    with db() as con:
        rows, total = operator_views.credits_page(
            con, user_id, kind or None, SPENDABLE_KINDS, _PAGE,
            (page - 1) * _PAGE)
    return templates.TemplateResponse(
        request, "samelabs/economy_credits.html",
        ctx(request, user, rows=[dict(r) for r in rows], total=total,
            page=page, pages=max(1, -(-total // _PAGE)),
            q_user=user_id, q_kind=kind, kinds=list(SPENDABLE_KINDS),
            fmt_num=fmt_num, page_id="economy"))


@router.get("/samelabs/economy/earnings")
async def provider_earnings(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = max(1, page)
    with db() as con:
        rows, total = operator_views.provider_earnings_page(
            con, _PAGE, (page - 1) * _PAGE)
    items = []
    for r in rows:
        d = dict(r)
        d["amount_usd"] = millis_to_usd_str(int(d["delta"]))
        d["tool"] = (d["attributed_tool"]
                     if d["attributed_tool"] is not None else "?")
        items.append(d)
    return templates.TemplateResponse(
        request, "samelabs/economy_earnings.html",
        ctx(request, user, rows=items, total=total, page=page,
            pages=max(1, -(-total // _PAGE)), fmt_num=fmt_num,
            page_id="economy"))


@router.get("/samelabs/economy/platform")
async def platform_income(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = max(1, page)
    with db() as con:
        rows, total = operator_views.platform_income_page(
            con, _PAGE, (page - 1) * _PAGE)
    items = []
    for r in rows:
        d = dict(r)
        d["amount_usd"] = millis_to_usd_str(int(d["delta"]))
        items.append(d)
    return templates.TemplateResponse(
        request, "samelabs/economy_platform.html",
        ctx(request, user, rows=items, total=total, page=page,
            pages=max(1, -(-total // _PAGE)), fmt_num=fmt_num,
            page_id="economy"))


# ── Withdrawal queue (S4 lifecycle) ──

_WD_STATUSES = ("pending", "approved", "rejected", "paid")


@router.get("/samelabs/economy/withdrawals")
async def withdrawals(request: Request, status: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.economy import withdrawals_queue
    page = max(1, page)
    # unknown filter value → unfiltered view (never passed into SQL)
    q_status = status if status in _WD_STATUSES else ""
    with db() as con:
        rows, total = withdrawals_queue(con, _PAGE, (page - 1) * _PAGE,
                                        q_status or None)
    items = []
    for r in rows:
        d = dict(r)
        d["amount_usd"] = millis_to_usd_str(int(d["amount"]))
        # payout destination = REQUEST-TIME snapshot only (never live
        # users.payout_channel)
        d["payout"] = d["payout_channel_snapshot"]
        items.append(d)
    return templates.TemplateResponse(
        request, "samelabs/economy_withdrawals.html",
        ctx(request, user, withdrawals=items, total=total, page=page,
            pages=max(1, -(-total // _PAGE)), q_status=q_status,
            statuses=list(_WD_STATUSES), fmt_num=fmt_num,
            page_id="economy"))


@router.post("/samelabs/economy/withdrawals/{wid}/approve")
async def approve_withdrawal(wid: int, request: Request):
    """pending → approved (exactly one withdraw ledger row)."""
    user, resp = gate(request)
    if resp:
        return resp
    from backoffice import withdrawals as wd_cmd
    try:
        row = wd_cmd.approve_withdrawal(wid, admin_user_id=int(user["id"]))
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}


@router.post("/samelabs/economy/withdrawals/{wid}/reject")
async def reject_withdrawal(wid: int, request: Request):
    """pending → rejected (zero ledger mutation)."""
    user, resp = gate(request)
    if resp:
        return resp
    from backoffice import withdrawals as wd_cmd
    try:
        row = wd_cmd.reject_withdrawal(wid, admin_user_id=int(user["id"]))
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}


@router.post("/samelabs/economy/withdrawals/{wid}/paid")
async def mark_paid(wid: int, request: Request):
    """approved → paid with external payout_ref (zero ledger mutation)."""
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        payout_ref = raw.get("payout_ref", "")
        if not isinstance(payout_ref, str):
            return JSONResponse(
                {"ok": False, "error": "invalid_payout_ref_type"}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import withdrawals as wd_cmd
    try:
        row = wd_cmd.mark_withdrawal_paid(
            wid, payout_ref, admin_user_id=int(user["id"]))
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}
