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
async def overview(request: Request, range: str = "30"):
    """Daily window toggle: only 14 / 30 are vocabulary values; any
    other input (including non-numeric) falls back to 30."""
    user, resp = gate(request)
    if resp:
        return resp
    from app.main import templates
    days = int(range) if range.isdigit() else 30
    if days not in (14, 30):
        days = 30
    ts = now()
    with db() as con:
        product = dict(operator_views.overview_product(con))
        consumer = dict(operator_views.overview_consumer(
            con, SPENDABLE_KINDS, ts))
        provider = dict(operator_views.overview_provider(con))
        platform = dict(operator_views.overview_platform(con))
        recent = [dict(r) for r in
                  operator_views.recent_tool_calls(con, 15)]
        daily = operator_views.overview_daily(con, days, ts)
    for d in daily:
        d["platform_usd"] = millis_to_usd_str(int(d["platform_millis"]))
        d["provider_usd"] = millis_to_usd_str(int(d["provider_millis"]))
        calls = int(d["calls"] or 0)
        d["fail_rate"] = (f"{int(d['calls_failed']) * 100 // calls}%"
                          if calls else "—")
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
    from config import BASE_URL, X_CLIENT_ID, X_CLIENT_SECRET
    integrations = {
        "x_login": bool(X_CLIENT_ID and X_CLIENT_SECRET),
        "x_callback": f"{BASE_URL}/auth/x/callback",
    }
    return templates.TemplateResponse(
        request, "samelabs/overview.html",
        ctx(request, user, product=product, consumer=consumer,
            integrations=integrations,
            provider_usd=provider_usd, platform_usd=platform_usd,
            recent=recent, daily=daily, days=days, fmt_num=fmt_num,
            page="overview"))
