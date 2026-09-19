"""/samelabs back-office: access control + shared helpers (batch C4).

Gate model: /samelabs is the manual operator control plane. Access =
live browser session + users.status in BACKOFFICE_STATUSES. Capability
tokens (Bearer) authenticate the API/MCP surfaces only — they never
enter the back-office. The user row stays the operator authority;
the credential channel is session-only.

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

from app.web_ctx import fmt_ts as _fmt_ts  # 单点 (治理4)

router = APIRouter()

BACKOFFICE_STATUSES = ("samelabs",)


class _PlanMirrorError(Exception):
    """Operator plan write aborted at the provider-mirror stage.

    Raised INSIDE the plan create/update transaction so the with-block
    rolls the uncommitted local row back — the provider may or may not
    have been reached, but the local plan never half-commits. Maps to
    a 502 with a stable error token."""


# ── samelabs.css cache-busting (same pattern as pages.py _CSS_VERSION) ──
import hashlib as _hashlib
import re as _re
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS
from pathlib import Path as _Path

_SL_CSS_VERSION = _hashlib.md5(
    (_Path(__file__).resolve().parent.parent.parent / "static" / "samelabs.css").read_bytes()
).hexdigest()[:8]


def is_operator(user: dict | None) -> bool:
    return bool(user) and user.get("status") in BACKOFFICE_STATUSES


def _gate(request: Request):
    """Return (user, None) for operator browser sessions, else (None, response).

    Session-only: the request must carry a live browser session whose user
    row has back-office status. Bearer capability tokens are rejected here
    even when the underlying account is an operator — the control plane is
    not a token surface.
    """
    if getattr(request.state, "auth_via", "") != "session":
        return None, RedirectResponse("/auth/login?next=/samelabs", status_code=302)
    user = getattr(request.state, "user", None)
    if not is_operator(user):
        return None, RedirectResponse("/auth/login?next=/samelabs", status_code=302)
    return user, None


def _ctx(request: Request, user: dict, **extra) -> dict:
    """Full page context: shared vars + samelabs.css version + operator row."""
    from app.web_ctx import ctx_base as _ctx_base
    ctx = _ctx_base(request)
    ctx["op"] = user
    ctx["fmt_ts"] = _fmt_ts
    ctx["sl_css_version"] = _SL_CSS_VERSION
    ctx.update(extra)
    return ctx




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
    from repo.reports import overview_stats, recent_calls, top_tools
    with db() as con:
        stats = overview_stats(con)
        recent = recent_calls(con, 15)
        top_tools = top_tools(con, 10)
    ctx = _ctx(request, user, stats=stats,
               recent=[dict(r) for r in recent],
               top_tools=[dict(r) for r in top_tools],
               fmt_num=_fmt_num, page="overview")
    return templates.TemplateResponse(request, "samelabs/overview.html", ctx)


@router.get("/samelabs/users")
async def samelabs_users(request: Request, q: str = ""):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.reports import users_overview
    with db() as con:
        rows = users_overview(con, q or None)
    ctx = _ctx(request, user, users=[dict(r) for r in rows],
               q=q, fmt_num=_fmt_num, page="users")
    return templates.TemplateResponse(request, "samelabs/users.html", ctx)


@router.get("/samelabs/tools")
async def samelabs_tools(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from governance.pricing import list_pricing
    from governance.catalog import list_catalog
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
            "featured": bool(t["featured"]) if t else False,
            "sort_order": int(t["sort_order"]) if t else 0,
        })
    ctx = _ctx(request, user, tools=rows, fmt_num=_fmt_num, page="tools")
    return templates.TemplateResponse(request, "samelabs/tools.html", ctx)


# ── operator audit: safe snapshot projections (allowlist ONLY) ──
# 快照纪律: 只按 target_type 投影白名单字段; 绝不 blacklist 删秘密。
from repo import operator_audit as _audit_repo


def _proven_provider_price(prod: dict):
    """The provider-before price must be an EXPLICIT canonical integer
    fact (Creem contract: integer cents). Anything that would need an
    int() COERCION is not a proven fact and fails closed — including
    floats (150.7 would silently truncate to 150), strings, and bool
    (Python bool is an int subclass but not a price). An explicit
    integer 0 is a legitimate fact."""
    v = prod["price"]
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _classify_db_commit_outcome(plan_id, intended, before, actor_id,
                                tx_started, action):
    """DB-commit-outcome closure for the plan commercial mirror — a
    strict three-way verdict against EXACT snapshots.

    The provider provably carries the intended price, but the local
    COMMIT raised / its outcome is unknown. An INDEPENDENT fresh
    connection reads the exact plan row and THIS operation's audit fact
    (correlation boundary = tx_started + action + before→intended pair;
    created_at is frozen when the transaction opens — a provider call
    crossing a second boundary never loses the match, and a historical
    same-price audit with a different before never matches).

      update:  row == intended + matching audit → DURABLE_SUCCESS
               row == exact BEFORE + no matching audit → PROVEN_ROLLBACK
               any other row/audit combination → UNRESOLVED
      create:  row == intended + matching audit → DURABLE_SUCCESS
               row absent + no matching audit → PROVEN_ROLLBACK
               any other → UNRESOLVED
    Never a second speculative mutation inside this classification.
    """
    from database import db as _fdb
    try:
        with _fdb() as con:
            row = con.execute(
                "SELECT * FROM plans WHERE id=%s", (plan_id,)).fetchone()
            audit = con.execute(
                "SELECT 1 FROM operator_audit"
                " WHERE action=%s AND target_type='plan' AND target_id=%s"
                "   AND actor_user_id=%s AND created_at>=%s"
                "   AND after_json->>'price_cents'=%s"
                " LIMIT 1",
                (action, str(plan_id), int(actor_id), int(tx_started),
                 str(intended["price_cents"]))).fetchone()
    except Exception:
        return "unresolved", None
    if row is None:
        # create: row absent — only "no THIS-operation audit" proves rollback
        return ("rollback", None) if audit is None else ("unresolved", None)
    cur_price = int(row["price_cents"] or 0)
    cur_cur = str(row["currency"] or "")
    cur_prod = str(row["creem_product_id"] or "")
    _is_intended = (cur_price == int(intended["price_cents"])
                    and cur_cur == str(intended.get("currency") or "")
                    and cur_prod == str(intended.get("creem_product_id") or ""))
    if _is_intended:
        return ("success", dict(row)) if audit is not None \
            else ("unresolved", None)
    # NOT intended: PROVEN_ROLLBACK requires the row to be the EXACT
    # before snapshot AND no matching THIS-operation audit — anything
    # else (third-state row, or a matching audit over a foreign row)
    # is unprovable.
    if before is None:
        return "unresolved", None   # update must pass its exact before
    _is_before = (cur_price == int(before["price_cents"])
                  and cur_cur == str(before.get("currency") or "")
                  and cur_prod == str(before.get("creem_product_id") or ""))
    if _is_before and audit is None:
        return "rollback", dict(row)
    return "unresolved", None


def _snap_plan(r) -> dict:
    if r is None:
        return {}
    return {k: r[k] for k in (
        "id", "code", "name", "monthly_credits", "period_days", "period",
        "price_cents", "currency", "creem_product_id", "sort_order",
        "active", "ingress_files_24h", "ingress_bytes_24h",
        "expiry_days", "download_limits") if k in r.keys()}


def _snap_package(r) -> dict:
    if r is None:
        return {}
    return {k: r[k] for k in ("id", "code", "name", "credits",
                              "price_cents", "currency", "sort_order",
                              "active") if k in r.keys()}


def _snap_pricing(r) -> dict | None:
    if r is None:
        return None  # first-time pricing: before = NULL (create semantics)
    return {"tool_id": r["tool_id"], "cost": int(r["cost"]),
            "enabled": bool(r["enabled"])}


def _snap_presentation(r) -> dict:
    if r is None:
        return {}
    return {"tool_key": r["tool_key"], "featured": bool(r["featured"]),
            "sort_order": int(r["sort_order"])}


def _safe_remote_projection(con, tool_key: str) -> dict | None:
    """tool + pricing 的 safe 字段投影 (allowlist; URL/endpoint_json/
    auth/Vault 永不进入)。"""
    row = con.execute(
        """
        SELECT t.tool_key, t.tool_id, t.execution, t.tool_type,
               t.visibility, t.enabled, t.featured, t.sort_order, t.multi,
               t.accept_json, p.cost
        FROM platform_tools t LEFT JOIN pricing p ON p.tool_id = t.tool_id
        WHERE t.tool_key = %s
        """, (tool_key,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("accept_json"):
        import json as _json
        try:
            d["accept"] = _json.loads(d["accept_json"])
        except Exception:
            d["accept"] = None
    d.pop("accept_json", None)
    return d


def _snap_remote(r) -> dict:
    # remote allowlist ONLY — URL/endpoint_json/auth/Vault never enter audit
    if r is None:
        return {}
    return {k: r[k] for k in ("tool_key", "tool_id", "execution",
                              "tool_type", "visibility", "enabled",
                              "featured", "sort_order", "multi", "accept",
                              "cost") if k in r.keys()}


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
    from governance.pricing import set_pricing
    from repo.catalog import (tool_row_for_update, pricing_row,
                             pricing_row_for_update)
    # one transaction: serialize on the platform_tools row (the SAME
    # boundary presentation update and remote delete use), read the
    # before snapshot AFTER the lock, mutate, audit. Retired tools
    # (platform row gone, pricing row alive) fall back to a pricing-row
    # lock — existing retired semantics unchanged.
    with db() as con:
        tool = tool_row_for_update(con, tool_id)
        if tool is not None:
            # registered tool: existing pricing or missing-pricing repair
            # both allowed — the tool row is the serialization boundary
            before = pricing_row(con, tool_id)
        else:
            # retired tool (platform row gone): pricing row must exist and
            # is the serialization boundary — absent both rows means the
            # target does not exist: 404, zero pricing row, zero audit,
            # never an orphan pricing row
            before = pricing_row_for_update(con, tool_id)
            if before is None:
                return JSONResponse({"ok": False, "error": "not_found"}, 404)
        row = set_pricing(tool_id, cost, enabled,
                          admin_user_id=user["id"], con=con)
        _audit_repo.record(con, user["id"], "pricing.update", "pricing",
                           str(tool_id), _snap_pricing(before),
                           _snap_pricing(row), now())
    return {"ok": True, "pricing": {k: row[k] for k in ("tool_id", "cost", "enabled")}}


@router.get("/samelabs/plans")
async def samelabs_plans(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.orders import all_plans
    with db() as con:
        rows = [r for r in all_plans(con) if r["code"] != "free"]  # 管理面: 全部 paid 行
        # 订阅计数: current memberships per plan (含 pending 排期目标) —
        # 下架/改配前的事实依据
        counts = {r["plan_id"]: int(r["n"]) for r in con.execute(
            "SELECT plan_id, COUNT(*) n FROM memberships"
            " WHERE status IN ('active','past_due','canceled') GROUP BY plan_id")}
        pend = {r["target_plan_id"]: int(r["n"]) for r in con.execute(
            "SELECT target_plan_id, COUNT(*) n FROM pending_downgrades"
            " GROUP BY target_plan_id")}
    plans = []
    for r in rows:
        d = dict(r)
        d["member_count"] = int(counts.get(r["id"], 0))
        d["pending_count"] = int(pend.get(r["id"], 0))
        plans.append(d)
    ctx = _ctx(request, user, plans=plans, fmt_num=_fmt_num, page="plans")
    return templates.TemplateResponse(request, "samelabs/plans.html", ctx)


_PLAN_INT_FIELDS = ("monthly_credits", "period_days", "price_cents",
                    "sort_order", "ingress_files_24h", "ingress_bytes_24h")


def _parse_plan_fields(raw: dict, *, creating: bool = False) -> dict | None:
    """operator plan 字段解析 + 值域 fail-closed。返回 None = 请求非法。
    权益枚举必须 ⊆ 平台协议 (delivery 单源); 越界值 422 拒绝, 绝不裁剪。"""
    fields: dict = {}
    if creating:
        code = str(raw.get("code", "")).strip().lower()[:32]
        if not code or not _re.fullmatch(r"[a-z][a-z0-9_]*", code) or code == "free":
            return None
        fields["code"] = code
        for k in ("name",):
            v = str(raw.get(k, "")).strip()[:64]
            if not v:
                return None
            fields[k] = v
        for k in _PLAN_INT_FIELDS:
            # period_days 值域 >0 (create 未提供时默认 30 — 由 raw.get 的
            # create default 提供, 不把显式非法 0 修成 30)
            default = 30 if k == "period_days" else 0
            v = raw.get(k, default)
            if not isinstance(v, int) or v < 0 or (k == "period_days" and v <= 0):
                return None
            fields[k] = v
    else:
        if "name" in raw:
            v = str(raw["name"]).strip()[:64]
            if not v:
                return None
            fields["name"] = v
        for k in _PLAN_INT_FIELDS:
            if k in raw:
                v = raw[k]
                if not isinstance(v, int) or v < 0 \
                        or (k == "period_days" and v <= 0):
                    return None
                fields[k] = v
    if "creem_product_id" in raw or creating:
        fields["creem_product_id"] = str(raw.get("creem_product_id", "")).strip()[:64] or None
    if "active" in raw or creating:
        if not isinstance(raw.get("active", True), bool):
            return None
        fields["active"] = bool(raw.get("active", True))
    for k, allowed in (("expiry_days", EXPIRY_DAYS),
                       ("download_limits", DOWNLOAD_LIMITS)):
        if k in raw or (creating and raw.get(k) is not None):
            v = raw.get(k)
            if not isinstance(v, list) or not all(isinstance(x, int) for x in v):
                return None
            if any(x not in allowed for x in v):
                return None  # 越界值 fail-closed, 不静默裁剪
            fields[k] = v
    return fields


@router.post("/samelabs/plans")
async def samelabs_create_plan(request: Request):
    """创建新 paid tier 行 (plans 行 = 商品事实源; 新档不硬编码)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        fields = _parse_plan_fields(raw, creating=True)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if fields is None:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import insert_plan
    from database import db as _db
    import psycopg2
    # Operator control-plane narrow transaction (low-frequency admin
    # write; NEVER spread to customer checkout/runtime payment paths):
    #   open tx → INSERT uncommitted → DB unique constraints pass FIRST
    #   → (if product-bound) mirror Creem → provider success → COMMIT.
    # Local constraint failure → ZERO provider mutation. Provider
    # failure → DB rollback — a failed local write can never pollute
    # the provider mirror.
    try:
        with _db() as con:
            row = insert_plan(
                con, fields["code"], fields["name"], fields["monthly_credits"],
                fields["price_cents"], now(),
                period_days=fields["period_days"],
                sort_order=fields["sort_order"],
                creem_product_id=fields["creem_product_id"],
                ingress_files_24h=fields["ingress_files_24h"],
                ingress_bytes_24h=fields["ingress_bytes_24h"],
                expiry_days=fields.get("expiry_days") or (),
                download_limits=fields.get("download_limits") or ())
            # audit lands INSIDE the tx, BEFORE any Creem HTTP side
            # effect: audit failure → rollback → ZERO provider call;
            # Creem failure → rollback → zero committed audit row.
            _audit_repo.record(con, user["id"], "plan.create", "plan",
                               str(row["id"]), None, _snap_plan(row), now())
            _mirror_before = None
            if fields.get("creem_product_id"):
                from economy import creem as _creem
                if not _creem.creem_enabled():
                    raise _PlanMirrorError("price_mirror_unavailable")
                # compensation basis = the PROVEN provider-before facts.
                # An unprovable GET (failure/absent → None) can NEVER be
                # degraded to a fabricated (0, "") before: the whole
                # write — PATCH and any later compensation — would run
                # on a guessed state. Fail closed BEFORE any provider
                # mutation.
                try:
                    _pb = _creem._fetch_product(fields["creem_product_id"])
                except Exception:
                    _pb = None
                # provider-before price must be an EXPLICIT, provable
                # provider fact: missing / None / unparseable fails
                # closed BEFORE any provider mutation. An explicit
                # legitimate numeric 0 is a real fact, never treated as
                # missing ("key absent" ≠ "value 0").
                if not isinstance(_pb, dict) \
                        or not str(_pb.get("currency") or "").strip() \
                        or "price" not in _pb or _pb["price"] is None:
                    raise _PlanMirrorError("price_mirror_unavailable")
                _pb_price = _proven_provider_price(_pb)
                if _pb_price is None:
                    raise _PlanMirrorError("price_mirror_unavailable")
                _mirror_before = (_pb_price,
                                  str(_pb.get("currency") or "").upper())
                _mout = _creem.mirror_price_with_reconciliation(
                    fields["creem_product_id"], _mirror_before[0],
                    _mirror_before[1],
                    int(fields["price_cents"]),
                    str(fields.get("currency") or "USD"))
                if _mout != _creem.MIRROR_SUCCESS:
                    raise _PlanMirrorError("price_mirror_update_failed")
    except _PlanMirrorError as exc:
        # provider failure: the with-block raised → tx rolled back
        return JSONResponse({"ok": False, "error": str(exc)}, 502)
    except psycopg2.IntegrityError as exc:
        # 唯一索引兜底: code 冲突 / paid rank 冲突 / product 已绑它档
        # (constraints fired BEFORE any provider call — zero side effect)
        msg = str(getattr(exc, "diag", None) and exc.diag.message_primary or exc)
        if "plans_paid_sort_order_uk" in msg or "sort_order" in msg:
            return JSONResponse({"ok": False, "error": "sort_order_taken"}, 409)
        if "plans_product_binding_uk" in msg or "creem_product_id" in msg:
            return JSONResponse({"ok": False, "error": "product_already_bound"}, 409)
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    except psycopg2.Error:
        # COMMIT failed / outcome unknown AFTER the provider provably
        # carries the intended facts — classify from a fresh connection
        if locals().get("_mirror_before") and fields.get("creem_product_id"):
            from economy import creem as _ccreem
            _prod = fields["creem_product_id"]
            _bprice, _bcur = _mirror_before
            try:
                outcome, _row2 = _classify_db_commit_outcome(
                    int(row["id"]),
                    {"price_cents": int(fields["price_cents"]),
                     "currency": str(fields.get("currency") or "USD"),
                     "creem_product_id": _prod},
                    None, int(user["id"]), int(row["created_at"]),
                    "plan.create")
            except Exception:
                outcome, _row2 = "unresolved", None
            if outcome == "rollback":
                if not _ccreem.restore_product_price(_prod, _bprice, _bcur):
                    return JSONResponse(
                        {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
                return JSONResponse(
                    {"ok": False, "error": "price_mirror_local_rollback"}, 502)
            if outcome == "success":
                from governance.plan_catalog import validate_config
                validate_config()
                return {"ok": True, "plan": dict(row)}
            return JSONResponse(
                {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
        raise
    # ── DB COMMIT-OUTCOME CLOSURE (provider already proven intended) ──
    # The block above exited cleanly, but the COMMIT itself can still
    # fail (connection drop at commit) with the row's fate unknown.
    # Classify via an INDEPENDENT fresh DB read; only a PROVEN local
    # rollback may restore the provider to the exact before facts.
    if _mirror_before is not None and fields.get("creem_product_id"):
        from economy import creem as _ccreem
        _prod = fields["creem_product_id"]
        _bprice, _bcur = _mirror_before
        try:
            outcome, _row2 = _classify_db_commit_outcome(
                int(row["id"]),
                {"price_cents": int(fields["price_cents"]),
                 "currency": str(fields.get("currency") or "USD"),
                 "creem_product_id": _prod},
                None, int(user["id"]), int(row["created_at"]),
                "plan.create")
        except Exception:
            outcome, _row2 = "unresolved", None
        if outcome == "rollback":
            if not _ccreem.restore_product_price(_prod, _bprice, _bcur):
                import logging
                logging.getLogger("flashshare").critical(
                    "plan create mirror UNRESOLVED: product=%s before=%s/%s"
                    " intended=%s restore-unprovable",
                    _prod, _bprice, _bcur, fields["price_cents"])
                return JSONResponse(
                    {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
        elif outcome == "unresolved":
            import logging
            logging.getLogger("flashshare").critical(
                "plan create mirror UNRESOLVED: product=%s before=%s/%s"
                " intended=%s db-outcome-unprovable",
                _prod, _bprice, _bcur, fields["price_cents"])
            return JSONResponse(
                {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
    from governance.plan_catalog import validate_config
    validate_config()  # refresh + fail-closed 校验
    return {"ok": True, "plan": dict(row)}


@router.patch("/samelabs/plans/{plan_id}")
async def samelabs_update_plan(plan_id: int, request: Request):
    """配置 paid tier 行: 名称/积分/周期/价格/product/权益/上下架/排序
    (code 不可改; free 行是基础档非商品, 不可经此修改)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        fields = _parse_plan_fields(raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if fields is None or not fields:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import update_plan, plan_by_id, plan_by_id_for_update
    from governance.plan_catalog import is_paid_tier
    with db() as con:
        row = plan_by_id(con, plan_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not is_paid_tier(row["code"]):
        return JSONResponse({"ok": False, "error": "not_editable_tier"}, 422)
    # Operator control-plane narrow transaction (low-frequency admin
    # write; NEVER spread to customer checkout/runtime payment paths).
    # Commercial mirror write-through: ANY commercial fact change — the
    # price OR the product binding itself — closes at the operator write.
    # Order inside ONE transaction:
    #   open tx + row UPDATE uncommitted → DB unique constraints pass
    #   FIRST → (commercial change only) mirror Creem → provider success
    #   → COMMIT. Local constraint failure (product/sort conflict) →
    #   rollback with ZERO provider mutation — a failed rebind can never
    #   reprice another plan's product. Provider failure → rollback, the
    #   local plan is untouched. Non-commercial edits never call Creem.
    #
    # The pre-transaction read above serves ONLY not_found / paid-tier
    # eligibility. Mirror values are derived AFTER the in-transaction
    # UPDATE, from the RETURNING row — never from the stale outer read:
    # a concurrent writer may commit between that read and the row lock,
    # and the mirror must reflect the row this transaction is committing
    # (the UPDATE row lock is the serialization boundary).
    commercial_change = ("price_cents" in fields) or ("creem_product_id" in fields)
    import psycopg2
    _tx_ts = now()   # THIS operation's correlation boundary (frozen)
    _db_before = None
    try:
        with db() as con:
            # in-tx locked read → before snapshot (NOT the stale outer row)
            locked = plan_by_id_for_update(con, plan_id)
            before = _snap_plan(locked)
            _db_before = {"price_cents": int(locked["price_cents"] or 0),
                          "currency": str(locked["currency"] or ""),
                          "creem_product_id":
                              str(locked["creem_product_id"] or "")}
            # PRODUCT BINDING IMMUTABILITY: creem_product_id is a
            # stable provider binding identity. NULL→X binds; X→X is a
            # no-op; X→Y and X→NULL are FORBIDDEN — a rebind (or clear)
            # would sever every existing subscription/payment lifecycle
            # keyed off the product (period proof, reversal location,
            # upgrade paths), bound or not. Product migration is a
            # separate mechanism, not a PATCH.
            if "creem_product_id" in fields:
                _cur_p = locked["creem_product_id"] or None
                _new_p = fields.get("creem_product_id") or None
                if _cur_p is not None and _new_p != _cur_p:
                    return JSONResponse(
                        {"ok": False, "error": "product_binding_immutable"}, 409)
            # PENDING-CHECKOUT PRICE FREEZE: a pending plan order froze
            # this plan's amount at checkout; retry must submit the SAME
            # purchase intent, so the live price may not move under it.
            if "price_cents" in fields:
                _pend = con.execute(
                    "SELECT 1 FROM orders WHERE kind='plan' AND item_id=%s"
                    " AND status='pending' LIMIT 1", (plan_id,)).fetchone()
                if _pend is not None:
                    return JSONResponse(
                        {"ok": False, "error": "plan_checkout_pending"}, 409)
            row = update_plan(con, plan_id, fields, now())
            # audit BEFORE any Creem side effect, same transaction
            _audit_repo.record(con, user["id"], "plan.update", "plan",
                               str(plan_id), before, _snap_plan(row), now())
            if commercial_change:
                target_price = int(row["price_cents"])
                target_product = row["creem_product_id"] or ""
                target_currency = row["currency"] or "USD"
                if target_product:
                    from economy import creem as _creem
                    if not _creem.creem_enabled():
                        raise _PlanMirrorError("price_mirror_unavailable")
                    # exact provider-before snapshot (for ambiguity
                    # proof). Unprovable GET (None) fails closed BEFORE
                    # any provider mutation — never a fabricated (0,"").
                    try:
                        _pb = _creem._fetch_product(target_product)
                    except Exception:
                        _pb = None
                    # provider-before price must be an EXPLICIT,
                    # provable provider fact (missing / None /
                    # unparseable → fail closed before any mutation);
                    # an explicit legitimate 0 is a real fact.
                    if not isinstance(_pb, dict) \
                            or not str(_pb.get("currency") or "").strip() \
                            or "price" not in _pb or _pb["price"] is None:
                        raise _PlanMirrorError("price_mirror_unavailable")
                    _before_price = _proven_provider_price(_pb)
                    if _before_price is None:
                        raise _PlanMirrorError("price_mirror_unavailable")
                    _before_cur = str(_pb.get("currency") or "").upper()
                    _mirror_before = (_before_price, _before_cur)
                    try:
                        _mout = _creem.mirror_price_with_reconciliation(
                            target_product, _before_price, _before_cur,
                            target_price, str(target_currency))
                    except RuntimeError as exc:
                        err = "price_mirror_update_failed"
                        if "billing_contract" in str(exc) or "not_found" in str(exc):
                            err = "product_billing_contract_invalid"
                        raise _PlanMirrorError(err)
                    if _mout == _creem.MIRROR_ROLLBACK:
                        # proven provider no-mutation → local rollback
                        raise _PlanMirrorError("price_mirror_update_failed")
                    if _mout == _creem.MIRROR_UNRESOLVED:
                        raise _PlanMirrorError("price_mirror_commit_unresolved")
    except _PlanMirrorError as exc:
        # provider failure: with-block raised → tx rolled back
        return JSONResponse({"ok": False, "error": str(exc)}, 502)
    except psycopg2.IntegrityError as exc:
        # 唯一索引兜底 (并发/撞既有行): rank 冲突 / product 已绑它档
        # (constraints fired BEFORE any provider call — zero side effect)
        msg = str(getattr(exc, "diag", None) and exc.diag.message_primary or exc)
        if "plans_paid_sort_order_uk" in msg or "sort_order" in msg:
            return JSONResponse({"ok": False, "error": "sort_order_taken"}, 409)
        if "plans_product_binding_uk" in msg or "creem_product_id" in msg:
            return JSONResponse({"ok": False, "error": "product_already_bound"}, 409)
        raise
    except psycopg2.Error:
        # COMMIT failed / outcome unknown AFTER the provider provably
        # carries the intended facts — classify from a fresh connection
        if locals().get("_mirror_before") and commercial_change \
                and row["creem_product_id"]:
            from economy import creem as _ccreem
            _prod = row["creem_product_id"]
            _bprice, _bcur = _mirror_before
            try:
                outcome, _row2 = _classify_db_commit_outcome(
                    int(plan_id),
                    {"price_cents": int(row["price_cents"]),
                     "currency": str(row["currency"] or "USD"),
                     "creem_product_id": _prod},
                    _db_before, int(user["id"]), int(_tx_ts),
                    "plan.update")
            except Exception:
                outcome, _row2 = "unresolved", None
            if outcome == "rollback":
                if not _ccreem.restore_product_price(_prod, _bprice, _bcur):
                    return JSONResponse(
                        {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
                return JSONResponse(
                    {"ok": False, "error": "price_mirror_local_rollback"}, 502)
            if outcome == "success":
                from governance.plan_catalog import validate_config
                validate_config()
                return {"ok": True, "plan": dict(row)}
            return JSONResponse(
                {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
        raise
    # ── DB COMMIT-OUTCOME CLOSURE (provider already proven intended) ──
    # A clean block exit does not prove the COMMIT landed (connection
    # drop at commit leaves the row's fate unknown). Classify via an
    # INDEPENDENT fresh DB read; only a PROVEN rollback may restore.
    if commercial_change and row["creem_product_id"] \
            and locals().get("_mirror_before"):
        from economy import creem as _ccreem3
        _prod3 = row["creem_product_id"]
        _bp3, _bc3 = _mirror_before
        try:
            outcome3, _row3 = _classify_db_commit_outcome(
                int(plan_id),
                {"price_cents": int(row["price_cents"]),
                 "currency": str(row["currency"] or "USD"),
                 "creem_product_id": _prod3},
                _db_before, int(user["id"]), int(_tx_ts),
                "plan.update")
        except Exception:
            outcome3, _row3 = "unresolved", None
        if outcome3 == "rollback":
            if not _ccreem3.restore_product_price(_prod3, _bp3, _bc3):
                import logging
                logging.getLogger("flashshare").critical(
                    "plan update mirror UNRESOLVED: product=%s before=%s/%s"
                    " intended=%s restore-unprovable",
                    _prod3, _bp3, _bc3, row["price_cents"])
                return JSONResponse(
                    {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
        elif outcome3 == "unresolved":
            import logging
            logging.getLogger("flashshare").critical(
                "plan update mirror UNRESOLVED: product=%s before=%s/%s"
                " intended=%s db-outcome-unprovable",
                _prod3, _bp3, _bc3, row["price_cents"])
            return JSONResponse(
                {"ok": False, "error": "price_mirror_commit_unresolved"}, 502)
    # POST-COMMIT READ-BACK: the transaction above has committed; re-GET
    # the product and confirm the provider now carries the committed
    # price/currency. This closes the residual crash window (PATCH
    # applied provider-side, local commit outcome unprovable): any
    # divergence surfaces IMMEDIATELY as an explicit error instead of
    # drifting — and re-running the same update converges idempotently
    # (GET-first mirror + in-tx row lock).
    if commercial_change and row["creem_product_id"]:
        from economy import creem as _creem2
        try:
            _merr = _creem2.verify_product_mirror(
                row["creem_product_id"], int(row["price_cents"]),
                str(row["currency"] or "USD"))
        except Exception:
            _merr = "verify_unavailable"
        if _merr:
            return JSONResponse(
                {"ok": False, "error": "price_mirror_divergence",
                 "detail": str(_merr)}, 502)
    # plans 行是 Tier 唯一事实源, 进程内目录缓存必须随运营写入失效
    from governance.plan_catalog import validate_config
    validate_config()  # refresh=True + fail-closed 校验新配置
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    return {"ok": True, "plan": dict(row)}


@router.patch("/samelabs/packages/{package_id}")
async def samelabs_update_package(package_id: int, request: Request):
    """配置积分包行: 名称/积分/价格/上下架 (code 不可改)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        fields = {}
        if "name" in raw:
            v = str(raw["name"]).strip()[:64]
            if not v:
                raise ValueError
            fields["name"] = v
        for k in ("credits", "price_cents"):
            if k in raw:
                v = raw[k]
                # credits 值域 >0 (与 create 一致); price_cents >=0
                if not isinstance(v, int) or v < 0 \
                        or (k == "credits" and v <= 0):
                    raise ValueError
                fields[k] = v
        if "active" in raw:
            if not isinstance(raw["active"], bool):
                raise ValueError
            fields["active"] = raw["active"]
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if not fields:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.orders import update_package, package_by_id_for_update
    with db() as con:
        cur = package_by_id_for_update(con, package_id)
        if cur is None:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        before = _snap_package(cur)
        row = update_package(con, package_id, fields, now())
        if row is None:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        _audit_repo.record(con, user["id"], "package.update", "package",
                           str(package_id), before, _snap_package(row), now())
    return {"ok": True, "package": dict(row)}


@router.patch("/samelabs/tools/{tool_key}/presentation")
async def samelabs_set_tool_presentation(tool_key: str, request: Request):
    """管理后台遴选: 推荐标记 + 排序 (首页/市场呈现序的权威)。"""
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        featured = bool(raw.get("featured", False))
        sort_order = int(raw.get("sort_order", 0))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from repo.catalog import set_tool_presentation, tool_row_for_update
    with db() as con:
        cur = tool_row_for_update(con, tool_key)
        if cur is None:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        before = _snap_presentation(cur)
        row = set_tool_presentation(con, tool_key, featured, sort_order, now())
        _audit_repo.record(con, user["id"], "tool.presentation.update",
                           "tool", str(tool_key), before,
                           _snap_presentation(row), now())
    return {"ok": True, "tool": dict(row)}


@router.get("/samelabs/packages")
async def samelabs_packages(request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.orders import all_packages
    with db() as con:
        rows = all_packages(con)
    ctx = _ctx(request, user, packages=[dict(r) for r in rows], fmt_num=_fmt_num, page="packages")
    return templates.TemplateResponse(request, "samelabs/packages.html", ctx)


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
    from repo.orders import insert_package
    try:
        with db() as con:
            row = insert_package(con, code, name, credits, price, now())
            _audit_repo.record(con, user["id"], "package.create", "package",
                               str(row["id"]), None, _snap_package(row), now())
    except psycopg2.IntegrityError:
        return JSONResponse({"ok": False, "error": "code_taken"}, 409)
    return {"ok": True, "package": dict(row)}


@router.get("/samelabs/ledger")
async def samelabs_ledger(request: Request, page: int = 1):
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from repo.reports import ledger_page
    page = max(1, min(page, 10000))
    size = 50
    with db() as con:
        rows, total = ledger_page(con, size, (page - 1) * size)
    ctx = _ctx(request, user, entries=[dict(r) for r in rows],
               total=total, pg=page, pages=(total + size - 1) // size,
               fmt_num=_fmt_num, page="ledger")
    return templates.TemplateResponse(request, "samelabs/ledger.html", ctx)


@router.post("/samelabs/users/{user_id}/credits")
async def samelabs_adjust_credits(user_id: int, request: Request):
    user, resp = _gate(request)
    if resp:
        return resp
    try:
        raw = await request.json()
        amount = int(raw.get("amount", 0))
        note = str(raw.get("note", ""))[:200]
        # Stable action identity: the operator UI mints ONE id per edited
        # action and resends it verbatim on network retry — a retried HTTP
        # request replays the same ledger idem key instead of double
        # mutating. A different action sends a different action_id.
        action_id = str(raw.get("action_id", "")).strip()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    if not action_id or len(action_id) > 64:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)
    from economy.billing import grant, consume, InsufficientCredits
    idem = f"adjust:{user['id']}:{action_id}"
    # ledger movement + audit fact in ONE transaction; same action_id
    # replay → billing idem returns the ORIGINAL row → zero new audit.
    try:
        with db() as con:
            if amount > 0:
                row = grant(user_id, amount, "adjust",
                            note=note or "samelabs adjust",
                            idem_key=idem, con=con)
            elif amount < 0:
                row = consume(user_id, -amount, "adjust",
                              note=note or "samelabs adjust",
                              idem_key=idem, con=con)
            else:
                return JSONResponse({"ok": False, "error": "zero_amount"}, 422)
            # one ledger movement ↔ one audit fact: a replayed action
            # resolves to the SAME ledger row id → audit already exists
            _already = _audit_repo.exists_for_ledger(con, int(row["id"]))
            if not _already:
                delta = int(row["delta"])
                bal_after = int(row["balance_after"])
                _audit_repo.record(
                    con, user["id"], "credits.adjust", "user_credits",
                    str(user_id),
                    {"user_id": user_id, "balance_before": bal_after - delta,
                     "balance_after": bal_after},
                    {"user_id": user_id, "delta": delta,
                     "balance_before": bal_after - delta,
                     "balance_after": bal_after,
                     "ledger_id": int(row["id"])}, now())
    except InsufficientCredits as exc:
        return JSONResponse({"ok": False, "error": "insufficient",
                             "balance": exc.balance}, 409)
    return {"ok": True, "balance_after": row["balance_after"]}


# ── Withdrawals (operator payout queue) ──

@router.get("/samelabs/withdrawals")
async def samelabs_withdrawals(request: Request):
    """Withdrawal queue: pending first, operator approve/reject."""
    user, resp = _gate(request)
    if resp:
        return resp
    from app.main import templates
    from economy.earnings import list_all_pending
    rows = list_all_pending()
    for r in rows:
        r["created_fmt"] = _fmt_ts(r["created_at"])
    ctx = _ctx(request, user, withdrawals=rows, page="withdrawals")
    return templates.TemplateResponse(request, "samelabs/withdrawals.html", ctx)


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
    from economy.earnings import process_withdrawal
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
    from governance.catalog import list_catalog
    # remote rows only, all visibilities (operator view)
    from repo.catalog import all_remote_joined
    with db() as con:
        rows = all_remote_joined(con)
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
    return templates.TemplateResponse(request, "samelabs/remote.html", ctx)


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
        # 4C-2-close: multi 与 user ToolDeclaration 同语义 —
        # 缺失 → false; 显式值必须是真 JSON boolean, 非 bool → bad_multi
        multi = raw.get("multi", False)
        if not isinstance(multi, bool):
            return JSONResponse({"ok": False, "error": "bad_multi"}, 422)
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
    # 4C-2: 逗号分隔输入只收 canonical ext (.png,.pdf); 统一过
    # extensions_for_accept, 存库只写 canonical ext。
    # operator 受信供给允许跨 family; unknown token → bad_accept。
    accept = None
    if accept_raw:
        tokens = [x.strip() for x in accept_raw.split(",") if x.strip()]
        from governance.formats import extensions_for_accept
        try:
            canon = extensions_for_accept(tokens)
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "bad_accept"}, 422)
        accept = sorted(canon) if canon else None

    # 阶段 B: operator policy 之上构造 canonical declaration →
    # tool_supply 单点 (与 user 同一 create_tool / binding shape)。
    # HTTP binding composition + auth seal 唯一入口 = build_http_binding
    # (旧 endpoint 组装段已删除, 不再存在第二份 seal)。
    from repo.catalog import tool_key_exists
    from governance.tool_supply import create_tool, build_http_binding, DeclarationError
    # Credential boundary BEFORE any persistence — and before the broad
    # except below, so VaultUnavailable can never masquerade as 409.
    if auth_token:
        from secrets_vault import vault_enabled
        if not vault_enabled():
            return JSONResponse(
                {"ok": False, "error": "credential_vault_unavailable"}, 503)
    try:
        with db() as con:
            # typed duplicate check (unique index would 500 otherwise)
            if tool_key_exists(con, tool_id):
                return JSONResponse({"ok": False, "error": "duplicate_tool"}, 409)
        declaration = {
            "tool_key": tool_id,
            "tool_id": tool_id,
            "owner_id": None,
            "execution": "http",
            "tool_type": "produce",
            "label": {"en": label, "zh": label},
            "desc": None,
            "icon": "wand",
            "accept": accept,
            "multi": multi,
            "params": None,
            "pass_through": False,
            "visibility": "public",
            "enabled": True,
            "binding": build_http_binding({
                "url": url, "timeout_s": timeout_s,
                **({"auth_header": auth_header} if auth_header else {}),
                **({"auth_token": auth_token} if auth_token else {}),
                **({"output_filename": output_filename} if output_filename else {}),
                **({"output_content_type": output_content_type} if output_content_type else {}),
            }),
        }
        try:
            # atomic: declaration + commercial contract + audit fact in
            # ONE transaction — audit failure rolls the tool+pricing
            # creation back with it
            from governance.pricing import create_tool_with_pricing
            with db() as con:
                tool_key = create_tool_with_pricing(declaration, cost,
                                                    admin_user_id=user["id"],
                                                    con=con)
                after = _snap_remote(_safe_remote_projection(con, tool_key))
                _audit_repo.record(con, user["id"], "remote.create",
                                   "remote_tool", str(tool_key), None,
                                   after, now())
        except DeclarationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 422)
        except ValueError:
            return JSONResponse({"ok": False, "error": "bad_cost"}, 422)
    except Exception:
        return JSONResponse({"ok": False, "error": "duplicate_or_db"}, 409)
    return {"ok": True, "tool_key": tool_id}


@router.post("/samelabs/remote/{tool_key}/delete")
async def samelabs_remote_delete(tool_key: str, request: Request):
    """Remove a remote tool row (platform-owned rows only)."""
    user, resp = _gate(request)
    if resp:
        return resp
    # one local transaction: tool delete + pricing disable + audit —
    # all three commit or roll back together (audit failure → tool row
    # and pricing both unchanged)
    from repo.catalog import platform_row_for_update, delete_tool_row
    from governance.pricing import set_pricing
    with db() as con:
        # lock FIRST: concurrent deletes serialize here; the loser
        # re-evaluates after the winner commits → not_found, zero
        # pricing mutation, zero second audit.
        row = platform_row_for_update(con, tool_key, owner_id_is_null=True)
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        before = _snap_remote(_safe_remote_projection(con, tool_key))
        delete_tool_row(con, row["id"])
        set_pricing(tool_key, 0, False,
                    admin_user_id=user["id"], con=con)
        _audit_repo.record(con, user["id"], "remote.delete",
                           "remote_tool", str(tool_key), before, None, now())
    return {"ok": True}
