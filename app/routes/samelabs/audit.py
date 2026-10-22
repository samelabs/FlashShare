"""/samelabs/audit — append-only operator audit trail."""

from fastapi import APIRouter, Request

from database import db
from repo import operator_views
from utils import page_number

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 50


@router.get("/samelabs/audit")
def audit(request: Request, page: int = 1):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = page_number(page)
    with db() as con:
        rows, total = operator_views.audit_page(
            con, _PAGE, (page - 1) * _PAGE)
    return templates.TemplateResponse(
        request, "samelabs/audit.html",
        ctx(request, user, rows=[dict(r) for r in rows], total=total,
            page=page, pages=max(1, -(-total // _PAGE)),
            fmt_num=fmt_num, page_id="audit"))
