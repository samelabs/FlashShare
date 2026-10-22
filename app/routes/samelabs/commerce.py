"""/samelabs/commerce — Orders (read-only facts) / Memberships
(memberships-row authority) / Products (plans + packages merged;
mutation orchestration in core.operator.products)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import psycopg2

from database import db
from repo import operator_views, orders as _orders_repo
from utils import now, page_number

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 30


# ── Orders: read-only real facts ──

@router.get("/samelabs/commerce/orders")
def orders(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = page_number(page)
    with db() as con:
        rows, total = operator_views.orders_page(
            con, _PAGE, (page - 1) * _PAGE)
    return templates.TemplateResponse(
        request, "samelabs/commerce_orders.html",
        ctx(request, user, orders=[dict(r) for r in rows], total=total,
            page=page, pages=max(1, -(-total // _PAGE)),
            fmt_num=fmt_num, page_id="commerce"))


# ── Memberships: single-row authority ──

@router.get("/samelabs/commerce/memberships")
def memberships(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = page_number(page)
    with db() as con:
        rows, total = operator_views.memberships_page(
            con, _PAGE, (page - 1) * _PAGE, now())
    return templates.TemplateResponse(
        request, "samelabs/commerce_memberships.html",
        ctx(request, user, memberships=[dict(r) for r in rows],
            total=total, page=page, pages=max(1, -(-total // _PAGE)),
            fmt_num=fmt_num, page_id="commerce"))


# ── Products: plans + packages merged ──

@router.get("/samelabs/commerce/products")
def products(request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    _ts = now()
    with db() as con:
        plan_rows = [r for r in _orders_repo.all_plans(con)
                     if r["code"] != "free"]
        counts = _orders_repo.membership_counts_by_plan(con, _ts)
        pend = _orders_repo.pending_downgrade_counts_by_plan(con)
        package_rows = _orders_repo.all_packages(con)
    plans = []
    for r in plan_rows:
        d = dict(r)
        d["member_count"] = int(counts.get(r["id"], 0))
        d["pending_count"] = int(pend.get(r["id"], 0))
        plans.append(d)
    packages = [dict(r) for r in package_rows]
    return templates.TemplateResponse(
        request, "samelabs/commerce_products.html",
        ctx(request, user, plans=plans, packages=packages,
            fmt_num=fmt_num, page_id="commerce"))


@router.post("/samelabs/commerce/products/plans")
async def create_plan(request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import products as products_cmd
    fields = products_cmd.parse_plan_fields(raw, creating=True)
    if fields is None:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        out = products_cmd.create_plan(int(user["id"]), fields)
    except products_cmd.PlanMirrorError:
        return JSONResponse({"ok": False,
                             "error": "price_mirror_update_failed"}, 502)
    except ValueError as exc:
        token = str(exc)
        return JSONResponse({"ok": False, "error": token},
                            409 if token in ("sort_order_taken",
                                             "product_already_bound",
                                             "code_taken",
                                             "product_binding_immutable",
                                             "plan_checkout_pending")
                            else 422)
    except products_cmd.ConflictError as exc:
        return JSONResponse({"ok": False, "error": exc.token}, 409)
    except psycopg2.IntegrityError:
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    return {"ok": True, "plan": out["plan"]}


@router.patch("/samelabs/commerce/products/plans/{plan_id}")
async def update_plan(plan_id: int, request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import products as products_cmd
    fields = products_cmd.parse_plan_fields(raw, creating=False)
    if fields is None or not fields:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        out = products_cmd.update_plan(int(user["id"]), plan_id, fields)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except products_cmd.PlanMirrorError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 502)
    except ValueError as exc:
        token = str(exc)
        return JSONResponse({"ok": False, "error": token},
                            409 if token in ("sort_order_taken",
                                             "product_already_bound",
                                             "product_binding_immutable",
                                             "plan_checkout_pending")
                            else 422)
    except products_cmd.ConflictError as exc:
        return JSONResponse({"ok": False, "error": exc.token}, 409)
    except psycopg2.IntegrityError as exc:
        from backoffice.products import _integrity_error_token
        return JSONResponse(
            {"ok": False, "error": _integrity_error_token(exc)}, 409)
    return {"ok": True, "plan": out["plan"]}


@router.post("/samelabs/commerce/products/packages")
async def create_package(request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import products as products_cmd
    fields = products_cmd.parse_package_fields(raw, creating=True)
    if fields is None:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        out = products_cmd.create_package(int(user["id"]), fields)
    except ValueError as exc:
        token = str(exc)
        return JSONResponse({"ok": False, "error": token},
                            409 if token == "code_taken" else 422)
    return {"ok": True, "package": out["package"]}


@router.patch("/samelabs/commerce/products/packages/{package_id}")
async def update_package(package_id: int, request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from backoffice import products as products_cmd
    fields = products_cmd.parse_package_fields(raw, creating=False)
    if fields is None:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    try:
        out = products_cmd.update_package(int(user["id"]), package_id,
                                          fields)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "package": out["package"]}
