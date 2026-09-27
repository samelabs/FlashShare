"""/samelabs/users — user rows with separated asset columns + operator
credits adjustment (command layer: core.operator.credits)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from economy.billing import SPENDABLE_KINDS
from economy.money import millis_to_usd_str
from repo import operator_views
from utils import now

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 30


_MAX_ID = (1 << 63) - 1      # BIGINT identity domain
_RECENT = 20


@router.get("/samelabs/users")
async def users(request: Request, q: str = "", page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = max(1, page)
    q = q.strip()[:100]
    with db() as con:
        rows, total = operator_views.users_page(
            con, q or None, _PAGE, (page - 1) * _PAGE, SPENDABLE_KINDS,
            now())
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


# ledger kind → display unit (explicit sets; never guessed from a name)
_USD_MILLIS_KINDS = ("provider_earn", "withdraw", "platform_commission",
                     "platform_revenue")


@router.get("/samelabs/users/{user_id}")
async def user_detail(user_id: int, request: Request):
    """Read-only operator view of one user: profile, separated balances,
    current membership (effective status), recent orders / tool calls /
    withdrawals / ledger rows."""
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    if not (1 <= user_id <= _MAX_ID):
        return JSONResponse({"ok": False, "error": "user_not_found"}, 404)
    with db() as con:
        prof = operator_views.user_profile(con, user_id, SPENDABLE_KINDS,
                                           now())
        if prof is None:
            return JSONResponse({"ok": False, "error": "user_not_found"},
                                404)
        orders = [dict(r) for r in
                  operator_views.user_recent_orders(con, user_id, _RECENT)]
        calls = [dict(r) for r in
                 operator_views.user_recent_tool_calls(con, user_id,
                                                       _RECENT)]
        wds = [dict(r) for r in
               operator_views.user_recent_withdrawals(con, user_id,
                                                      _RECENT)]
        ledger = [dict(r) for r in
                  operator_views.user_recent_ledger(con, user_id, _RECENT)]
    prof = dict(prof)
    for k in ("provider_earned", "provider_pending", "provider_available"):
        prof[k + "_usd"] = millis_to_usd_str(int(prof[k + "_millis"]))
    for w in wds:
        w["amount_usd"] = millis_to_usd_str(int(w["amount"]))
    for r in ledger:
        if r["kind"] in SPENDABLE_KINDS:
            r["unit"] = "Credits"
            r["amount"] = "%+d" % int(r["delta"])
        elif r["kind"] in _USD_MILLIS_KINDS:
            r["unit"] = "USD"
            d = int(r["delta"])
            r["amount"] = ("-" if d < 0 else "+") + millis_to_usd_str(abs(d))
        else:
            r["unit"] = "?"
            r["amount"] = "%+d" % int(r["delta"])
    from app.routes.samelabs.common import BACKOFFICE_STATUSES
    prof["protected"] = (prof["username"] == "platform"
                         or prof["status"] in BACKOFFICE_STATUSES)
    return templates.TemplateResponse(
        request, "samelabs/user_detail.html",
        ctx(request, user, u=prof, orders=orders, calls=calls,
            withdrawals=wds, ledger=ledger, fmt_num=fmt_num,
            page_title=f"User #{user_id}", page_id="users"))


@router.post("/samelabs/users/{user_id}/reset-password")
async def reset_password(user_id: int, request: Request):
    """Operator password reset (B2b-2): one-time temporary password
    shown to the operator ONCE; every browser session is revoked, API
    keys survive. Platform row and backoffice accounts are protected.
    The credential itself never reaches storage or the audit."""
    user, resp = gate(request)
    if resp:
        return resp
    from fastapi.responses import JSONResponse
    from backoffice import users as users_cmd
    try:
        uid, temp = users_cmd.reset_user_password(
            int(user["id"]), user_id)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "user_id": uid, "temp_password": temp}


@router.post("/samelabs/users/{user_id}/suspend")
async def suspend_user(user_id: int, request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    from fastapi.responses import JSONResponse
    from backoffice import users as users_cmd
    try:
        row = users_cmd.set_user_suspended(
            int(user["id"]), user_id, suspended=True)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}


@router.post("/samelabs/users/{user_id}/unsuspend")
async def unsuspend_user(user_id: int, request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    from fastapi.responses import JSONResponse
    from backoffice import users as users_cmd
    try:
        row = users_cmd.set_user_suspended(
            int(user["id"]), user_id, suspended=False)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}


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
    from backoffice import credits as credits_cmd
    if not (1 <= user_id <= _MAX_ID):
        return JSONResponse({"ok": False, "error": "user_not_found"}, 404)
    try:
        out = credits_cmd.adjust_credits(int(user["id"]), user_id, amount,
                                         action_id, note)
    except KeyError:
        return JSONResponse({"ok": False, "error": "user_not_found"}, 404)
    except ValueError as exc:
        token = str(exc)
        if token in ("insufficient", "action_id_conflict"):
            return JSONResponse({"ok": False, "error": token}, 409)
        return JSONResponse({"ok": False, "error": token}, 422)
    return {"ok": True, "balance_after": out["balance_after"],
            "replayed": out["replayed"]}
