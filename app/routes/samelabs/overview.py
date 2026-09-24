"""/samelabs — Overview: truthful asset-separated dashboard."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from database import db
from economy.billing import SPENDABLE_KINDS
from economy.money import millis_to_usd_str
from repo import operator_views
from utils import now

from app.routes.samelabs.common import gate, ctx, fmt_num

router = APIRouter()


@router.get("/samelabs")
async def overview(request: Request):
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    with db() as con:
        product = dict(operator_views.overview_product(con))
        consumer = dict(operator_views.overview_consumer(
            con, SPENDABLE_KINDS, now()))
        provider = dict(operator_views.overview_provider(con))
        platform = dict(operator_views.overview_platform(con))
        recent = [dict(r) for r in
                  operator_views.recent_tool_calls(con, 15)]
    # USD-millis → USD display via the single money authority; Credits
    # stay Credits. No cross-asset SUM anywhere.
    provider_usd = {
        "earned": millis_to_usd_str(int(provider["provider_earn_millis"])),
        "wd_pending": millis_to_usd_str(int(provider["wd_pending_millis"])),
        "wd_approved_unpaid": millis_to_usd_str(
            int(provider["wd_approved_unpaid_millis"])),
        "wd_paid": millis_to_usd_str(int(provider["wd_paid_millis"])),
    }
    platform_usd = {
        "commission": millis_to_usd_str(int(platform["commission_millis"])),
        "revenue": millis_to_usd_str(int(platform["revenue_millis"])),
    }
    return templates.TemplateResponse(
        request, "samelabs/overview.html",
        ctx(request, user, product=product, consumer=consumer,
            provider_usd=provider_usd, platform_usd=platform_usd,
            recent=recent, fmt_num=fmt_num, page="overview"))
