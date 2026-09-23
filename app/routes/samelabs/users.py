"""/samelabs/users — user rows with separated asset columns + operator
credits adjustment (command layer: core.operator.credits)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from economy.billing import SPENDABLE_KINDS
from economy.money import millis_to_usd_str
from repo import operator_views

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 30


@router.get("/samelabs/users")
async def users(request: Request, q: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = max(1, page)
    with db() as con:
        rows, total = operator_views.users_page(
            con, q or None, _PAGE, (page - 1) * _PAGE, SPENDABLE_KINDS)
    users_ = []
    for r in rows:
        d = dict(r)
        d["provider_available_usd"] = millis_to_usd_str(
            int(d["provider_available_millis"]))
        users_.append(d)
    return templates.TemplateResponse(
        request, "samelabs/users.html",
        ctx(request, user, users=users_, q=q, total=total,
            page=page, pages=max(1, -(-total // _PAGE)),
            fmt_num=fmt_num, page_id="users"))


@router.post("/samelabs/users/{user_id}/credits")
async def adjust_credits(user_id: int, request: Request):
    """Stable-action-id credits adjustment — behavior identical to the
    retired route (billing grant/consume + ledger idem + same-tx
    audit); orchestration now lives in core.operator.credits."""
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        from backoffice.strict import strict_int
        try:
            amount = strict_int(raw.get("amount", 0), allow_zero=False)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
        note = str(raw.get("note", ""))[:200]
        action_id = str(raw.get("action_id", "")).strip()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from economy.billing import InsufficientCredits
    from backoffice import credits as credits_cmd
    try:
        out = credits_cmd.adjust_credits(int(user["id"]), user_id, amount,
                                         action_id, note)
    except ValueError as exc:
        token = str(exc)
        if token == "insufficient":
            return JSONResponse({"ok": False, "error": "insufficient"}, 409)
        return JSONResponse({"ok": False, "error": token}, 422)
    return {"ok": True, "balance_after": out["balance_after"]}
