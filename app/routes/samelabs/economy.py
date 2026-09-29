"""/samelabs/economy — asset-separated ledgers + withdrawal queue.

Credits = SPENDABLE_KINDS only (Credits). Provider Earnings =
provider_earn (USD-millis, S2 relation attribution). Platform Income =
platform_commission + platform_revenue (USD). Withdrawals = S4
lifecycle queue."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from economy.billing import SPENDABLE_KINDS
from economy.money import millis_to_usd_str
from repo import operator_views
from utils import page_number

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
    page = page_number(page)
    with db() as con:
        rows, total = operator_views.credits_page(
            con, user_id, kind or None, SPENDABLE_KINDS, _PAGE,
            (page - 1) * _PAGE)
    return templates.TemplateResponse(
        request, "samelabs/economy_credits.html",
        ctx(request, user, rows=[dict(r) for r in rows], total=total,
            page=page, pages=max(1, -(-total // _PAGE)),
            q_user=user_id, q_kind=kind, kinds=list(SPENDABLE_KINDS),
            fmt_num=fmt_num, page_id="economy_credits"))


@router.get("/samelabs/economy/earnings")
async def provider_earnings(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = page_number(page)
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
            page_id="economy_earnings"))


@router.get("/samelabs/economy/platform")
async def platform_income(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    from governance.commission import commission_rate
    from economy.daily import daily_amount
    page = page_number(page)
    with db() as con:
        rows, total = operator_views.platform_income_page(
            con, _PAGE, (page - 1) * _PAGE)
        rate = commission_rate(con)
        amount = daily_amount(con)
    items = []
    for r in rows:
        d = dict(r)
        d["amount_usd"] = millis_to_usd_str(int(d["delta"]))
        items.append(d)
    return templates.TemplateResponse(
        request, "samelabs/economy_platform.html",
        ctx(request, user, rows=items, total=total, page=page,
            pages=max(1, -(-total // _PAGE)), fmt_num=fmt_num,
            commission_rate=rate, daily_amount=amount,
            page_id="economy_platform"))


@router.post("/samelabs/economy/commission")
async def set_commission(request: Request):
    """Operator write: platform commission 0..100 integer .
    Audited old → new inside the settings transaction; anything but a
    whole 0..100 int is refused with the current value untouched."""
    user, resp = gate(request)
    if resp:
        return resp
    from fastapi.responses import JSONResponse
    from repo import operator_audit
    from governance.commission import commission_rate, set_commission_rate
    from utils import now
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    rate = raw.get("rate") if isinstance(raw, dict) else None
    if not isinstance(rate, int) or isinstance(rate, bool) \
            or not 0 <= rate <= 100:
        return JSONResponse(
            {"ok": False, "error": "invalid_commission_rate"}, 422)
    ts = now()
    try:
        with db() as con:
            before = commission_rate(con)
            set_commission_rate(con, rate, ts, int(user["id"]))
            operator_audit.record(
                con, int(user["id"]), "commission.update",
                "setting", "commission_rate",
                {"rate": before}, {"rate": rate}, ts)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "invalid_commission_rate"}, 422)
    return {"ok": True, "rate": rate}


@router.post("/samelabs/economy/daily-amount")
async def set_daily_amount(request: Request):
    """Operator write: daily login credit amount 0..1000 integer.
    Audited old → new inside the settings transaction; anything but a
    whole 0..1000 int is refused with the current value untouched.
    0 disables the activity (no grant, widget hidden)."""
    user, resp = gate(request)
    if resp:
        return resp
    from fastapi.responses import JSONResponse
    from repo import operator_audit
    from economy.daily import (daily_amount as current_amount,
                               set_daily_amount as write_amount)
    from utils import now
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    amount = raw.get("amount") if isinstance(raw, dict) else None
    if not isinstance(amount, int) or isinstance(amount, bool) \
            or not 0 <= amount <= 1000:
        return JSONResponse(
            {"ok": False, "error": "invalid_daily_amount"}, 422)
    ts = now()
    try:
        with db() as con:
            before = current_amount(con)
            write_amount(con, amount, ts, int(user["id"]))
            operator_audit.record(
                con, int(user["id"]), "daily_amount.update",
                "setting", "daily_login_amount",
                {"amount": before}, {"amount": amount}, ts)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "invalid_daily_amount"}, 422)
    return {"ok": True, "amount": amount}


# ── Withdrawal queue (withdrawal lifecycle) ──

_WD_STATUSES = ("pending", "approved", "rejected", "paid",
                "payout_failed")


@router.get("/samelabs/economy/withdrawals")
async def withdrawals(request: Request, status: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.economy import withdrawals_queue
    page = page_number(page)
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
            page_id="economy_withdrawals"))


# ── Payment review queue (partial refunds) ──

@router.get("/samelabs/economy/reviews")
async def payment_reviews(request: Request, status: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo import payment_reviews as reviews_repo
    page = page_number(page)
    q_status = status if status in ("open", "resolved") else ""
    with db() as con:
        rows, total = reviews_repo.reviews_page(
            con, _PAGE, (page - 1) * _PAGE, q_status or None)
    return templates.TemplateResponse(
        request, "samelabs/economy_reviews.html",
        ctx(request, user, reviews=[dict(r) for r in rows], total=total,
            page=page, pages=max(1, -(-total // _PAGE)),
            q_status=q_status, statuses=["open", "resolved"],
            fmt_num=fmt_num, page_id="economy_reviews"))


@router.post("/samelabs/economy/reviews/{rid}/resolve")
async def resolve_payment_review(rid: int, request: Request):
    """Operator marks one review RESOLVED. Status flip only — this queue
    NEVER moves balances (economic rules for partial refunds stay
    zero-mutation); the decision evidence lives in operator_audit."""
    user, resp = gate(request)
    if resp:
        return resp
    from fastapi.responses import JSONResponse
    from repo import operator_audit, payment_reviews as reviews_repo
    from utils import now
    ts = now()
    with db() as con:
        row = reviews_repo.review_for_update(con, rid)
        if row is None:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        if row["status"] == "resolved":
            return {"ok": True, "status": "resolved"}   # replay: no-op
        before = dict(row)
        out = reviews_repo.mark_review_resolved(con, rid, ts, int(user["id"]))
        operator_audit.record(
            con, int(user["id"]), "payment_review.resolve",
            "payment_review", str(rid),
            {"status": before["status"], "txn_id": before["txn_id"],
             "event_id": before["event_id"]},
            {"status": "resolved", "resolved_by": int(user["id"])}, ts)
    return {"ok": True, "status": out["status"]}


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


@router.post("/samelabs/economy/withdrawals/{wid}/payout-failed")
async def payout_failed(wid: int, request: Request):
    """approved → payout_failed : earn-pool return in the same
    transaction; reason required (audit evidence)."""
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            return JSONResponse(
                {"ok": False, "error": "invalid_payout_reason_type"}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import withdrawals as wd_cmd
    try:
        row = wd_cmd.mark_withdrawal_payout_failed(
            wid, reason, admin_user_id=int(user["id"]))
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
