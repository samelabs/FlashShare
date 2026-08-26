"""User-submitted tools (C2C supply side, phase 3).

Self-service manifest submission: a user registers their OWN http
endpoint as a namespaced tool (`{username}/{tool_id}`). No human gate —
visibility is an owner-set field. The platform thesis: new capabilities
enter as DATA rows.

Safety model (no review queue):
  - SSRF pre-check at submission: https + resolved public IPs required
    (same guard the driver applies per-call; fail early, fail typed)
  - Landing identity: output_filename required with whitelisted ext
  - Per-user caps: max 20 tools, price 0..100000
  - Zero-trust output: sniff whitelist at every landing, no exemptions
  - Economics: provider earns (price - commission) per successful call;
    rate lives in settings (commission_rate, default 30%)

Ownership boundaries:
  - tool_key namespace = owner username at creation time (renames do not
    re-key; the row's owner_id is the authority, tool_key is addressing)
  - pricing rows are written with updated_by = owner; only the owner (or
    /samelabs) may change them — set_pricing stays the single writer.
"""

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import db
from utils import now

router = APIRouter()

MAX_TOOLS_PER_USER = 20
MAX_PRICE = 100000
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
ACCEPT_MIME_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")
DEFAULT_COMMISSION_RATE = 30


def _require_auth(request: Request):
    user = request.state.user
    if not user:
        return None, JSONResponse({"ok": False, "error": "auth_required"}, 401)
    return user, None


def commission_rate() -> int:
    """Platform commission percent (0-100) from settings; default 30."""
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key = 'commission_rate'"
        ).fetchone()
    try:
        return max(0, min(100, int(row["value"]))) if row else DEFAULT_COMMISSION_RATE
    except (ValueError, TypeError):
        return DEFAULT_COMMISSION_RATE


def _owner_rows(user_id: int) -> list:
    with db() as con:
        rows = con.execute(
            """SELECT pt.*, u.username AS owner_name, p.cost, p.enabled AS priced
               FROM platform_tools pt
               JOIN users u ON u.id = pt.owner_id
               LEFT JOIN pricing p ON p.tool_id = pt.tool_key
               WHERE pt.owner_id = %s ORDER BY pt.id DESC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _public_item(row: dict) -> dict:
    """Owner-surface view of a user tool (no endpoint secrets)."""
    decl = {}
    try:
        decl = json.loads(row.get("endpoint_json") or "{}")
    except Exception:
        pass
    return {
        "tool_key": row["tool_key"],
        "tool_id": row["tool_id"],
        "execution": row["execution"],
        "label": json.loads(row["label_json"]).get("en")
                 or json.loads(row["label_json"]).get("zh")
                 or row["tool_id"],
        "visibility": row["visibility"],
        "enabled": row["enabled"],
        "trusted": bool(row["trusted"]),
        "cost": int(row.get("cost") or 0),
        "priced_enabled": bool(row.get("priced")),
        "url": decl.get("url", ""),
        "timeout_s": decl.get("timeout_s", 60),
        "has_auth": bool(decl.get("auth_token")),
        "calls": _call_stats(row["tool_key"], row.get("owner_id")),
    }


def _call_stats(tool_key: str, owner_id: int | None = None) -> dict:
    """Per-tool call stats + provider earnings (accurate join via payout
    ledger rows referencing the call fact)."""
    with db() as con:
        row = con.execute(
            """SELECT COUNT(*) FILTER (WHERE status = 'ok') AS ok,
                      COUNT(*) AS total
               FROM tool_calls WHERE tool_id = %s""",
            (tool_key,),
        ).fetchone()
        earned = 0
        if owner_id is not None:
            erow = con.execute(
                """SELECT COALESCE(SUM(l.delta), 0) AS earned
                   FROM ledger l
                   JOIN tool_calls tc ON l.ref_id = tc.id::text
                   WHERE l.user_id = %s AND l.kind = 'provider_earn'
                     AND tc.tool_id = %s""",
                (owner_id, tool_key),
            ).fetchone()
            earned = int(erow["earned"]) if erow else 0
    return {"ok": row["ok"], "total": row["total"], "earned": earned}


def _validate_submission(raw: dict, partial: bool = False, trusted_ok: bool = False) -> tuple:
    """Shared validation for create (full) and patch (partial).
    Returns (fields, error_code). fields keys only for provided values."""
    fields = {}

    if "tool_id" in raw and not partial:
        tid = str(raw.get("tool_id") or "").strip()
        if not TOOL_ID_RE.match(tid):
            return None, "bad_tool_id"

    if "label" in raw or not partial:
        label = str(raw.get("label") or "").strip()
        if not label or len(label) > 40:
            return None, "bad_label"
        fields["label"] = label

    if "url" in raw or not partial:
        url = str(raw.get("url") or "").strip()
        # https+public-IP is the user-row contract; trusted (operator-adopted)
        # rows are exempt at runtime and on edit — flag set by the caller.
        if not trusted_ok and not url.startswith("https://"):
            return None, "https_required"
        if not url.startswith(("http://", "https://")):
            return None, "bad_url"
        fields["url"] = url

    if "timeout_s" in raw or not partial:
        try:
            timeout_s = int(raw.get("timeout_s") or 60)
        except (ValueError, TypeError):
            return None, "bad_timeout"
        if not (5 <= timeout_s <= 300):
            return None, "bad_timeout"
        fields["timeout_s"] = timeout_s

    if raw.get("auth_header") and raw.get("auth_token"):
        fields["auth_header"] = str(raw["auth_header"]).strip()[:64]
        fields["auth_token"] = str(raw["auth_token"]).strip()[:512]

    if "accept" in raw:
        accept_raw = str(raw.get("accept") or "").strip()
        accept = None
        if accept_raw:
            parts = [x.strip() for x in accept_raw.split(",") if x.strip()]
            if not parts or any(not ACCEPT_MIME_RE.match(x) for x in parts):
                return None, "bad_accept"
            accept = sorted(set(parts))
        fields["accept"] = accept

    if "output_filename" in raw or not partial:
        name = str(raw.get("output_filename") or "").strip()
        if not name:
            return None, "output_filename_required"
        import os as _os
        ext = _os.path.splitext(name)[1].lower()
        from config import ALLOWED_EXT
        if ext not in ALLOWED_EXT:
            return None, "bad_output_filename"
        fields["output_filename"] = name

    if "output_content_type" in raw:
        ct = str(raw.get("output_content_type") or "").strip()
        if ct and not ACCEPT_MIME_RE.match(ct):
            return None, "bad_output_content_type"
        fields["output_content_type"] = ct

    if "visibility" in raw or not partial:
        vis = str(raw.get("visibility") or "private").strip()
        if vis not in ("private", "unlisted", "public"):
            return None, "bad_visibility"
        fields["visibility"] = vis

    if "cost" in raw or not partial:
        try:
            cost = int(raw.get("cost") or 0)
        except (ValueError, TypeError):
            return None, "bad_cost"
        if not (0 <= cost <= MAX_PRICE):
            return None, "bad_cost"
        fields["cost"] = cost

    return fields, None


def _endpoint_decl(fields: dict, execution: str, existing: dict | None = None) -> dict:
    """Compose endpoint_json from validated fields (merge for patch)."""
    decl = dict(existing or {})
    if "url" in fields:
        decl["url"] = fields["url"]
    if "timeout_s" in fields:
        decl["timeout_s"] = fields["timeout_s"]
    if "auth_header" in fields:
        decl["auth_header"] = fields["auth_header"]
    if "auth_token" in fields:
        decl["auth_token"] = fields["auth_token"]
    if "output_filename" in fields:
        decl["output_filename"] = fields["output_filename"]
    if "output_content_type" in fields:
        decl["output_content_type"] = fields["output_content_type"]
    return decl


@router.get("/api/tools/search")
async def search_tools(request: Request, q: str = ""):
    """Home tool search — one list, self-operated first, community next.
    Anonymous: public rows only. Signed-in: own private/unlisted included."""
    from catalog import search_catalog
    user = request.state.user
    viewer = user["id"] if user else None
    return {"ok": True, "tools": search_catalog(q, viewer)}  # noqa: E501 (search_catalog caps at 30)


@router.get("/api/usertools")
async def list_user_tools(request: Request):
    """Owner's own tools with live stats."""
    user, err = _require_auth(request)
    if err:
        return err
    return {"ok": True, "tools": [_public_item(r) for r in _owner_rows(user["id"])]}


@router.post("/api/usertools")
async def create_user_tool(request: Request):
    """Submit a tool manifest. New tools land as visibility=private by
    default — the owner flips to unlisted/public after self-testing.
    SSRF pre-check runs here so misconfiguration fails at submission,
    not at first customer call."""
    user, err = _require_auth(request)
    if err:
        return err
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    tool_id = str(raw.get("tool_id") or "").strip()
    if not TOOL_ID_RE.match(tool_id):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)
    # Supply-side contract: ONE execution class — plain HTTP API.
    # (mcp forwarding removed 2026-08-26: agent-native MCP servers don't
    # need a broker; 518 IS the MCP surface northbound.)
    execution = "http"

    fields, ferr = _validate_submission(raw)
    if ferr:
        return JSONResponse({"ok": False, "error": ferr}, 422)

    # SSRF pre-check (typed failure at submission time)
    from remote import guard_endpoint, RemoteError
    try:
        guard_endpoint(fields["url"], trusted=False)
    except RemoteError as exc:
        code = "https_required" if exc.code == "ssrf_blocked" else "bad_url"
        return JSONResponse({"ok": False, "error": code}, 422)

    # New tools ALWAYS land private (self-test before exposure)
    fields["visibility"] = "private"

    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        clash = con.execute(
            "SELECT 1 FROM platform_tools WHERE tool_key = %s", (tool_key,)
        ).fetchone()
        if clash:
            return JSONResponse({"ok": False, "error": "duplicate_tool"}, 409)
        n = con.execute(
            "SELECT COUNT(*) AS n FROM platform_tools WHERE owner_id = %s",
            (user["id"],)).fetchone()["n"]
        if n >= MAX_TOOLS_PER_USER:
            return JSONResponse({"ok": False, "error": "too_many_tools"}, 429)

        ts = now()
        label = fields["label"]
        con.execute(
            # 11 bound columns + 4 literals (tool_type/presets/multi/enabled... trusted default FALSE)
            """INSERT INTO platform_tools
               (tool_key, owner_id, tool_id, execution,
                label_json, accept_json,
                visibility, endpoint_json, enabled, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)""",
            (tool_key, user["id"], tool_id, execution,
             json.dumps({"en": label, "zh": label}, ensure_ascii=False),
             json.dumps(fields.get("accept")) if fields.get("accept") else None,
             fields["visibility"],
             json.dumps(_endpoint_decl(fields, execution)),
             ts, ts),
        )
    # pricing row: owner-priced from day one
    from pricing import set_pricing
    set_pricing(tool_key, fields["cost"], True, admin_user_id=user["id"])
    return {"ok": True, "tool_key": tool_key}


@router.get("/api/usertools/{tool_id}/calls")
async def tool_call_log(tool_id: str, request: Request):
    """B3: provider-visible call log for THEIR tool (last 20).
    Owner-scoped; no caller identity beyond status/latency — privacy."""
    user, err = _require_auth(request)
    if err:
        return err
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        row = con.execute(
            "SELECT id FROM platform_tools WHERE tool_key = %s AND owner_id = %s",
            (tool_key, user["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        calls = [dict(r) for r in con.execute(
            """SELECT status, error, started_at, finished_at, cost_snapshot
               FROM tool_calls WHERE tool_id = %s AND user_id != %s
               ORDER BY id DESC LIMIT 20""",
            (tool_key, user["id"])).fetchall()]
    for cl in calls:
        f, s = cl.pop("finished_at", None), cl.pop("started_at", None)
        cl["ms"] = (f - s) if (f is not None and s is not None) else None
    return {"ok": True, "calls": calls}


@router.post("/api/usertools/{tool_id}/selftest")
async def selftest_tool(tool_id: str, request: Request):
    """T3: run ONE platform-mediated test call so the self-test gate is a
    button, not a riddle. Requires a source the owner uploads (same gate
    as a real call — metering, refunds, sniff); response carries ok/err +
    ms so the owner can diagnose. No free side-channel."""
    user, err = _require_auth(request)
    if err:
        return err
    tool_key = f"{user['username']}/{tool_id}"
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    public_id = str(raw.get("public_id") or "").strip()
    source_token = str(raw.get("source_token") or "").strip()
    if not public_id:
        return JSONResponse({"ok": False, "error": "invalid_id"}, 400)
    import time as _t
    t0 = _t.time()
    # Same single gate as any call (metering, refunds, sniff).
    request.state._v1_raw = {
        "tool_id": tool_key, "params": {},
        "public_id": public_id, "source_token": source_token,
        "client_ref": f"selftest:{tool_key}",
    }
    from app.routes.api import execute_tool
    resp = await execute_tool(request)
    ms = int((_t.time() - t0) * 1000)
    if isinstance(resp, JSONResponse):
        try:
            body = json.loads(resp.body)
        except Exception:
            body = {"ok": False, "error": "internal_error"}
        body["ms"] = ms
        return JSONResponse(body, status_code=resp.status_code)
    body = resp if isinstance(resp, dict) else {"ok": False, "error": "internal_error"}
    body["ms"] = ms
    return body


@router.patch("/api/usertools/{tool_id}")
async def update_user_tool(tool_id: str, request: Request):
    """Partial update: label/url/timeout/auth/accept/output identity/
    visibility/enabled/cost. Endpoint changes re-run the SSRF pre-check."""
    user, err = _require_auth(request)
    if err:
        return err
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        row = con.execute(
            "SELECT * FROM platform_tools WHERE tool_key = %s AND owner_id = %s",
            (tool_key, user["id"]),
        ).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, 404)
    if not TOOL_ID_RE.match(tool_id or ""):
        return JSONResponse({"ok": False, "error": "bad_tool_id"}, 422)

    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, 422)

    fields, ferr = _validate_submission(raw, partial=True,
                                        trusted_ok=bool(row["trusted"]))
    if ferr:
        return JSONResponse({"ok": False, "error": ferr}, 422)

    existing_decl = {}
    try:
        existing_decl = json.loads(row["endpoint_json"] or "{}")
    except Exception:
        pass

    # visibility cannot jump private→public directly (self-test gate:
    # at least one successful self-call recorded first)
    new_vis = fields.get("visibility")
    if new_vis == "public" and row["visibility"] != "public":
        stats = _call_stats(tool_key, row["owner_id"])
        if stats["ok"] < 1:
            return JSONResponse({"ok": False, "error": "self_test_required"}, 422)

    # endpoint change → SSRF re-check on the MERGED url.
    # Trusted (operator-adopted) rows keep their exemption, same as runtime.
    if "url" in fields and not row["trusted"]:
        from remote import guard_endpoint, RemoteError
        try:
            guard_endpoint(fields["url"], trusted=False)
        except RemoteError:
            return JSONResponse({"ok": False, "error": "https_required"}, 422)

    sets = {}
    if "label" in fields:
        sets["label_json"] = json.dumps(
            {"en": fields["label"], "zh": fields["label"]}, ensure_ascii=False)
    if "visibility" in fields:
        sets["visibility"] = fields["visibility"]
    if "accept" in fields:
        sets["accept_json"] = json.dumps(fields["accept"]) if fields["accept"] else None
    if "enabled" in raw:
        sets["enabled"] = bool(raw.get("enabled"))
    if sets or fields:
        decl = _endpoint_decl(fields, row["execution"], existing_decl)
        sets["endpoint_json"] = json.dumps(decl)
        sets["updated_at"] = now()
        assign = ", ".join(f"{k} = %s" for k in sets)
        with db() as con:
            con.execute(
                f"UPDATE platform_tools SET {assign} WHERE tool_key = %s",
                (*sets.values(), tool_key),
            )
    if "cost" in fields:
        from pricing import set_pricing
        set_pricing(tool_key, fields["cost"], True, admin_user_id=user["id"])
    return {"ok": True}


@router.delete("/api/usertools/{tool_id}")
async def delete_user_tool(tool_id: str, request: Request):
    """Remove a tool row. pricing flips disabled (history interpretable)."""
    user, err = _require_auth(request)
    if err:
        return err
    tool_key = f"{user['username']}/{tool_id}"
    with db() as con:
        row = con.execute(
            "SELECT id FROM platform_tools WHERE tool_key = %s AND owner_id = %s",
            (tool_key, user["id"]),
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, 404)
        con.execute("DELETE FROM platform_tools WHERE id = %s", (row["id"],))
    from pricing import set_pricing
    set_pricing(tool_key, 0, False, admin_user_id=user["id"])
    return {"ok": True}
