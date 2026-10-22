"""/samelabs/calls — tool call log.

Filter by tool / user / status / error code / time range, paginated.
Every row carries the charge (Credits) and the settlement outcome
derived from the ledger facts keyed to the call (consume charge, refund
of that charge, provider_earn / platform_commission / platform_revenue
splits) — links point at the tool detail and user detail pages."""

import datetime

from fastapi import APIRouter, Request

from database import db
from economy.money import millis_to_usd_str
from repo import operator_views
from utils import page_number

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()

_PAGE = 50
_STATUSES = ("running", "ok", "failed", "insufficient")


def _date_to_ts(value: str, end_of_day: bool = False):
    """YYYY-MM-DD (operator local input) → UTC day-boundary unix ts.
    The whole backoffice renders UTC (fmt_ts), so day buckets here are
    UTC too. None for empty/invalid input."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    day = datetime.datetime(d.year, d.month, d.day,
                            tzinfo=datetime.timezone.utc)
    if end_of_day:
        day += datetime.timedelta(days=1)
    return int(day.timestamp())


@router.get("/samelabs/calls")
async def calls(request: Request, tool: str = "", user: str = "",
                status: str = "", error: str = "",
                date_from: str = "", date_to: str = "", page: int = 1):
    user_op, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    page = page_number(page)
    if status not in _STATUSES:
        status = ""
    ts_from = _date_to_ts(date_from)
    ts_to = _date_to_ts(date_to, end_of_day=True)
    with db() as con:
        rows, total = operator_views.calls_page(
            con, tool.strip()[:100] or None, user.strip()[:64] or None,
            status or None, error.strip()[:100] or None,
            ts_from, ts_to, _PAGE, (page - 1) * _PAGE)
    items = []
    for r in rows:
        d = dict(r)
        d["cost"] = int(d["net_charge"])
        settlement = []
        if d["refund_delta"] is not None:
            settlement.append("已退款")
        elif d["status"] == "ok":
            if d["payout_millis"] is not None:
                settlement.append("开发者收益 "
                                  + millis_to_usd_str(int(d["payout_millis"])))
            if d["commission_millis"] is not None:
                settlement.append("抽佣 "
                                  + millis_to_usd_str(
                                      int(d["commission_millis"])))
            if d["revenue_millis"] is not None:
                settlement.append("平台直销 "
                                  + millis_to_usd_str(
                                      int(d["revenue_millis"])))
        elif d["status"] == "failed" and d["cost"] > 0:
            settlement.append("退款待结")
        d["settlement"] = " + ".join(settlement) if settlement else "—"
        items.append(d)
    return templates.TemplateResponse(
        request, "samelabs/calls.html",
        ctx(request, user_op, rows=items, total=total,
            q_tool=tool.strip()[:100], q_user=user.strip()[:64],
            q_status=status, q_error=error.strip()[:100],
            q_from=date_from.strip()[:10], q_to=date_to.strip()[:10],
            statuses=list(_STATUSES),
            page=page, pages=max(1, -(-total // _PAGE)),
            fmt_num=fmt_num, page_id="calls"))
