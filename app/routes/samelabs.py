"""/samelabs back-office: access control + shared helpers (batch C4).

Gate model: users.status in ('admin','samelabs') grants back-office
access (status 'admin' kept as a legacy alias; the path is /samelabs
only). Sessions AND Bearer both work — the gate is the user row, not
the channel.

Pages (server-rendered, no SPA):
    /samelabs            overview  — users count, calls, credits flow
    /samelabs/users      user list + search + credit balance
    /samelabs/tools      tool pricing + enable switch
    /samelabs/plans      subscription tiers
    /samelabs/packages   credit packs
    /samelabs/ledger     global ledger stream
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

from database import db
from utils import now

router = APIRouter()

BACKOFFICE_STATUSES = ("admin", "samelabs")

# ── samelabs.css cache-busting (same pattern as pages.py _CSS_VERSION) ──
import hashlib as _hashlib
from pathlib import Path as _Path

_SL_CSS_VERSION = _hashlib.md5(
    (_Path(__file__).resolve().parent.parent.parent / "static" / "samelabs.css").read_bytes()
).hexdigest()[:8]


def is_operator(user: dict | None) -> bool:
    return bool(user) and user.get("status") in BACKOFFICE_STATUSES


def _gate(request: Request):
    """Return (user, None) for operators, else (None, response)."""
    user = getattr(request.state, "user", None)
    if not is_operator(user):
        return None, RedirectResponse("/auth/login?next=/samelabs", status_code=302)
    return user, None


def _ctx(request: Request, user: dict, **extra) -> dict:
    """Full page context: shared vars + samelabs.css version + operator row."""
    from app.routes.pages import _ctx_base
    ctx = _ctx_base(request)
    ctx["op"] = user
    ctx["fmt_ts"] = _fmt_ts
    ctx["sl_css_version"] = _SL_CSS_VERSION
    ctx.update(extra)
    return ctx


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def _fmt_num(v) -> str:
    """Thousands separators for stat/table numbers ('' → 0)."""
    try:
        return f"{int(v or 0):,}"
    except (TypeError, ValueError):
        return str(v)


@router.get("/samelabs")
async def samelabs_overview(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    with db() as con:
        stats = {
            "users": con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            "uploads": con.execute("SELECT COUNT(*) n FROM uploads").fetchone()["n"],
            "calls_ok": con.execute(
                "SELECT COUNT(*) n FROM tool_calls WHERE status='ok'").fetchone()["n"],
            "calls_failed": con.execute(
                "SELECT COUNT(*) n FROM tool_calls WHERE status<>'ok'").fetchone()["n"],
            "credits_granted": con.execute(
                "SELECT COALESCE(SUM(delta),0) n FROM ledger WHERE delta>0").fetchone()["n"],
            "credits_consumed": con.execute(
                "SELECT COALESCE(-SUM(delta),0) n FROM ledger WHERE delta<0").fetchone()["n"],
        }
        recent = con.execute(
            """SELECT tc.*, u.username FROM tool_calls tc
               LEFT JOIN users u ON u.id = tc.user_id
               ORDER BY tc.id DESC LIMIT 15""").fetchall()
        top_tools = con.execute(
            """SELECT tool_id, COUNT(*) n FROM tool_calls
               GROUP BY tool_id ORDER BY n DESC LIMIT 10""").fetchall()
    ctx = _ctx(request, user, stats=stats,
               recent=[dict(r) for r in recent],
               top_tools=[dict(r) for r in top_tools],
               fmt_num=_fmt_num, page="overview")
    return templates.TemplateResponse("samelabs/overview.html", ctx)


@router.get("/samelabs/users")
async def samelabs_users(request: Request, q: str = ""):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    with db() as con:
        if q:
            rows = con.execute(
                """SELECT u.id, u.username, u.display_name, u.status, u.created_at,
                          u.last_login_at, u.avatar_url,
                          COALESCE((SELECT SUM(delta) FROM ledger l WHERE l.user_id=u.id), 0) balance,
                          (SELECT COUNT(*) FROM tool_calls tc WHERE tc.user_id=u.id
                             AND tc.status='ok') calls_ok
                   FROM users u WHERE u.username ILIKE %s OR u.display_name ILIKE %s
                   ORDER BY u.id DESC LIMIT 100""",
                (f"%{q}%", f"%{q}%")).fetchall()
        else:
            rows = con.execute(
                """SELECT u.id, u.username, u.display_name, u.status, u.created_at,
                          u.last_login_at, u.avatar_url,
                          COALESCE((SELECT SUM(delta) FROM ledger l WHERE l.user_id=u.id), 0) balance,
                          (SELECT COUNT(*) FROM tool_calls tc WHERE tc.user_id=u.id
                             AND tc.status='ok') calls_ok
                   FROM users u ORDER BY u.id DESC LIMIT 100""").fetchall()
    ctx = _ctx(request, user, users=[dict(r) for r in rows],
               q=q, fmt_num=_fmt_num, page="users")
    return templates.TemplateResponse("samelabs/users.html", ctx)


@router.get("/samelabs/tools")
async def samelabs_tools(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from pricing import list_pricing
    from catalog import list_catalog
    # Registry truth is the data-state catalog (platform_tools + pricing
    # overlay). 'registered' = present in the live catalog; retired rows
    # show as unregistered so history stays interpretable.
    reg = {t["id"]: t for t in list_catalog(None)}
    rows = []
    for p in list_pricing():
        t = reg.get(p["tool_id"])
        rows.append({
            **p,
            "registered": t is not None,
            "label": (t["label"].get("zh") or t["label"].get("en")) if t else "",
            "icon": t["icon"] if t else "",
        })
    ctx = _ctx(request, user, tools=rows, fmt_num=_fmt_num, page="tools")
    return templates.TemplateResponse("samelabs/tools.html", ctx)


@router.post("/samelabs/tools/{tool_id}/pricing")
async def samelabs_set_pricing(tool_id: str, request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        cost = int(raw.get("cost", 0))
        enabled = bool(raw.get("enabled", True))
        if cost < 0 or cost > 1000000:
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from pricing import set_pricing
    row = set_pricing(tool_id, cost, enabled, admin_user_id=user["id"])
    return {"ok": True, "pricing": {k: row[k] for k in ("tool_id", "cost", "enabled")}}


@router.get("/samelabs/plans")
async def samelabs_plans(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    with db() as con:
        rows = con.execute("SELECT * FROM plans ORDER BY price_cents").fetchall()
    ctx = _ctx(request, user, plans=[dict(r) for r in rows], fmt_num=_fmt_num, page="plans")
    return templates.TemplateResponse("samelabs/plans.html", ctx)


@router.post("/samelabs/plans")
async def samelabs_create_plan(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        code = str(raw.get("code", "")).strip().lower()[:32]
        name = str(raw.get("name", "")).strip()[:64]
        monthly_price = int(raw.get("monthly_price", 0))
        monthly_credits = int(raw.get("monthly_credits", 0))
        if not code or not name or monthly_price < 0 or monthly_credits < 0:
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import psycopg2
    try:
        with db() as con:
            row = con.execute(
                """INSERT INTO plans (code, name, monthly_credits, price_cents,
                                     active, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING *""",
                (code, name, monthly_credits, monthly_price, now(), now())).fetchone()
    except psycopg2.IntegrityError:
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    return {"ok": True, "plan": dict(row)}


@router.get("/samelabs/packages")
async def samelabs_packages(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    with db() as con:
        rows = con.execute("SELECT * FROM packages ORDER BY credits").fetchall()
    ctx = _ctx(request, user, packages=[dict(r) for r in rows], fmt_num=_fmt_num, page="packages")
    return templates.TemplateResponse("samelabs/packages.html", ctx)


@router.post("/samelabs/packages")
async def samelabs_create_package(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        code = str(raw.get("code", "")).strip().lower()[:32]
        name = str(raw.get("name", "")).strip()[:64]
        credits = int(raw.get("credits", 0))
        price = int(raw.get("price", 0))
        if not code or not name or credits <= 0 or price < 0:
            raise ValueError
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import psycopg2
    try:
        with db() as con:
            row = con.execute(
                """INSERT INTO packages (code, name, credits, price_cents,
                                        active, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, TRUE, %s, %s) RETURNING *""",
                (code, name, credits, price, now(), now())).fetchone()
    except psycopg2.IntegrityError:
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    return {"ok": True, "package": dict(row)}


@router.get("/samelabs/ledger")
async def samelabs_ledger(request: Request, page: int = 1):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    page = max(1, min(page, 10000))
    size = 50
    with db() as con:
        total = con.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
        rows = con.execute(
            """SELECT l.*, u.username FROM ledger l
               LEFT JOIN users u ON u.id = l.user_id
               ORDER BY l.id DESC LIMIT %s OFFSET %s""",
            (size, (page - 1) * size)).fetchall()
    ctx = _ctx(request, user, entries=[dict(r) for r in rows],
               total=total, pg=page, pages=(total + size - 1) // size,
               fmt_num=_fmt_num, page="ledger")
    return templates.TemplateResponse("samelabs/ledger.html", ctx)


@router.post("/samelabs/users/{user_id}/credits")
async def samelabs_adjust_credits(user_id: int, request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        amount = int(raw.get("amount", 0))
        note = str(raw.get("note", ""))[:200]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from billing import grant, consume, InsufficientCredits
    # Each manual adjustment is a distinct event — idem must never collide
    # (two ops in the same second would share a now()-second key).
    import secrets as _secrets
    nonce = f"{now()}:{_secrets.token_hex(8)}"
    try:
        if amount > 0:
            row = grant(user_id, amount, "adjust",
                        note=note or "samelabs adjust",
                        idem_key=f"adjust:{user['id']}:{nonce}")
        elif amount < 0:
            row = consume(user_id, -amount, "adjust",
                          note=note or "samelabs adjust",
                          idem_key=f"adjust:{user['id']}:{nonce}")
        else:
            return JSONResponse({"ok": False, "error": "zero_amount"}, 422)
    except InsufficientCredits as exc:
        return JSONResponse({"ok": False, "error": "insufficient",
                             "balance": exc.balance}, 409)
    return {"ok": True, "balance_after": row["balance_after"]}


# ── Activities (credit campaigns: signup / daily_login / future) ──

@router.get("/samelabs/activities")
async def samelabs_activities(request: Request):
    """Campaign management: amount / active / window per activity row."""
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from activities import list_activities, activity_label
    acts = []
    for a in list_activities():
        acts.append({
            **a,
            "label": activity_label(a),
            "starts_fmt": _fmt_ts(a["starts_at"]) if a["starts_at"] else "",
            "ends_fmt": _fmt_ts(a["ends_at"]) if a["ends_at"] else "",
        })
    ctx = _ctx(request, user, activities=acts, page="activities")
    return templates.TemplateResponse("samelabs/activities.html", ctx)


@router.post("/samelabs/activities/{code}/config")
async def samelabs_set_activity(code: str, request: Request):
    """Partial update: amount / active / window bounds. 0 clears a bound."""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        amount = raw.get("amount")
        active = raw.get("active")
        starts_at = raw.get("starts_at")
        ends_at = raw.get("ends_at")
        if amount is not None:
            amount = int(amount)
        if starts_at is not None:
            starts_at = int(starts_at)  # epoch seconds; 0 = clear
        if ends_at is not None:
            ends_at = int(ends_at)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from activities import set_activity
    try:
        row = set_activity(code, amount, active, starts_at, ends_at,
                           admin_user_id=user["id"])
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "activity": {
        "code": row["code"], "amount": row["amount"], "active": row["active"],
        "starts_at": row["starts_at"], "ends_at": row["ends_at"]}}


# ── Remote tools (http execution — operator-managed data rows) ──

@router.get("/samelabs/withdrawals")
async def samelabs_withdrawals(request: Request):
    """Withdrawal queue: pending first, operator approve/reject."""
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from earnings import list_all_pending
    rows = list_all_pending()
    for r in rows:
        r["created_fmt"] = _fmt_ts(r["created_at"])
    ctx = _ctx(request, user, withdrawals=rows, page="withdrawals")
    return templates.TemplateResponse("samelabs/withdrawals.html", ctx)


@router.post("/samelabs/withdrawals/{wid}/process")
async def samelabs_process_withdrawal(wid: int, request: Request):
    """approve=1 → ledger consume + status approved (one tx); else reject."""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    approve = bool(raw.get("approve"))
    txn_ref = str(raw.get("txn_ref") or "").strip()[:120] or None
    from earnings import process_withdrawal
    try:
        row = process_withdrawal(wid, approve, admin_user_id=user["id"],
                                 txn_ref=txn_ref)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    return {"ok": True, "status": row["status"]}


@router.get("/samelabs/remote")
async def samelabs_remote(request: Request):
    """Remote tool registry: trusted endpoints (owner_id NULL)."""
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from catalog import list_catalog
    # remote rows only, all visibilities (operator view)
    from database import db as _db
    with _db() as con:
        rows = con.execute(
            """SELECT pt.*, u.username AS owner_name FROM platform_tools pt
               LEFT JOIN users u ON u.id = pt.owner_id
               WHERE pt.execution = 'http' ORDER BY pt.id""").fetchall()
    items = []
    for r in rows:
        decl = {}
        label = r["tool_id"]
        try:
            import json as _json
            decl = _json.loads(r["endpoint_json"] or "{}")
            lab = _json.loads(r["label_json"] or "{}")
            label = lab.get("zh") or lab.get("en") or r["tool_id"]
        except Exception:
            pass
        items.append({
            "tool_key": r["tool_key"], "execution": r["execution"],
            "label": label,
            "icon": r["icon"], "visibility": r["visibility"],
            "enabled": r["enabled"], "owner": r["owner_name"] or "平台",
            "url": decl.get("url", ""), "timeout_s": decl.get("timeout_s", 60),
            "has_auth": bool(decl.get("auth_token")),
        })
    ctx = _ctx(request, user, remote_tools=items, page="remote")
    return templates.TemplateResponse("samelabs/remote.html", ctx)


@router.post("/samelabs/remote/create")
async def samelabs_remote_create(request: Request):
    """Create a trusted remote tool row. tool_key must be a bare id
    (operator namespace = first-party)."""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        tool_id = str(raw.get("tool_id", "")).strip()
        execution = str(raw.get("execution", "http")).strip()
        label = str(raw.get("label", "")).strip() or tool_id
        url = str(raw.get("url", "")).strip()
        timeout_s = int(raw.get("timeout_s", 60))
        auth_header = str(raw.get("auth_header", "")).strip()
        auth_token = str(raw.get("auth_token", "")).strip()
        accept_raw = str(raw.get("accept", "")).strip()
        cost = int(raw.get("cost", 0))
        output_filename = str(raw.get("output_filename", "")).strip()
        output_content_type = str(raw.get("output_content_type", "")).strip()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_]{1,40}$", tool_id):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)
    # Supply contract: single execution class (http). mcp forwarding
    # removed 2026-08-26 — operator rows follow the same API contract.
    execution = "http"
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "bad_url"}, 422)
    if not (5 <= timeout_s <= 300):
        return JSONResponse({"ok": False, "error": "bad_timeout"}, 422)
    # Raw-bytes delivery needs a whitelisted landing identity: sniff
    # validates the filename extension, so output_filename is REQUIRED
    # with a whitelisted extension when the endpoint may return raw bytes.
    if output_filename:
        import os as _os
        ext = _os.path.splitext(output_filename)[1].lower()
        from config import ALLOWED_EXT
        if ext not in ALLOWED_EXT:
            return JSONResponse({"ok": False, "error": "bad_output_filename"}, 422)
    import json as _json
    endpoint = {"url": url, "timeout_s": timeout_s}
    if auth_header and auth_token:
        endpoint["auth_header"] = auth_header
        endpoint["auth_token"] = auth_token
    if output_filename:
        endpoint["output_filename"] = output_filename
    if output_content_type:
        endpoint["output_content_type"] = output_content_type
    accept = None
    if accept_raw:
        accept = sorted({x.strip() for x in accept_raw.split(",") if x.strip()})

    from database import db as _db
    from utils import now as _now
    ts = _now()
    try:
        with _db() as con:
            # typed duplicate check (unique index would 500 otherwise)
            clash = con.execute(
                "SELECT 1 FROM platform_tools WHERE tool_key = %s", (tool_id,)
            ).fetchone()
            if clash:
                return JSONResponse({"ok": False, "error": "duplicate_tool"}, 409)
            con.execute(
                """INSERT INTO platform_tools
                   (tool_key, owner_id, tool_id, execution, tool_type,
                    label_json, accept_json, presets_json, multi,
                    visibility, endpoint_json, enabled, created_at, updated_at)
                   VALUES (%s, NULL, %s, %s, 'produce', %s, %s, NULL, FALSE,
                           'public', %s, TRUE, %s, %s)""",
                (tool_id, tool_id, execution,
                 _json.dumps({"en": label, "zh": label}, ensure_ascii=False),
                 _json.dumps(accept) if accept else None,
                 _json.dumps(endpoint), ts, ts),
            )
    except Exception:
        return JSONResponse({"ok": False, "error": "duplicate_or_db"}, 409)
    # seed pricing row so the metering gate can charge it
    from pricing import seed_pricing
    seed_pricing([tool_id])
    if cost > 0:
        from pricing import set_pricing
        set_pricing(tool_id, cost, True, admin_user_id=user["id"])
    return {"ok": True, "tool_key": tool_id}


@router.post("/samelabs/remote/{tool_key}/delete")
async def samelabs_remote_delete(tool_key: str, request: Request):
    """Remove a remote tool row (platform-owned rows only)."""
    user, resp = _gate(request)
    if resp:
        return resp
    from database import db as _db
    with _db() as con:
        row = con.execute(
            "SELECT id FROM platform_tools WHERE tool_key = %s AND owner_id IS NULL",
            (tool_key,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        con.execute("DELETE FROM platform_tools WHERE id = %s", (row["id"],))
    from pricing import set_pricing
    set_pricing(tool_key, 0, False, admin_user_id=user["id"])
    return {"ok": True}
