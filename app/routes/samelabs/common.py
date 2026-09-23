"""Operator session gate + common page context + presentation helpers.

NO business mutation lives here (batch A rule)."""

import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.web_ctx import fmt_ts as _fmt_ts, ctx_base as _ctx_base

BACKOFFICE_STATUSES = ("samelabs",)


def is_operator(user: dict | None) -> bool:
    return bool(user) and user.get("status") in BACKOFFICE_STATUSES


def gate(request: Request):
    """Return (user, None) for operator browser sessions, else
    (None, response). Session-only: Bearer capability tokens are
    rejected even when the underlying account is an operator — the
    control plane is not a token surface."""
    if getattr(request.state, "auth_via", "") != "session":
        return None, RedirectResponse("/auth/login?next=/samelabs",
                                      status_code=302)
    user = getattr(request.state, "user", None)
    if not is_operator(user):
        return None, RedirectResponse("/auth/login?next=/samelabs",
                                      status_code=302)
    return user, None


_SL_CSS_VERSION = hashlib.md5(
    (Path(__file__).resolve().parents[3] / "static" / "samelabs.css"
     ).read_bytes()).hexdigest()[:8]


def ctx(request: Request, user: dict, **extra) -> dict:
    """Full page context: shared vars + samelabs.css version + operator."""
    c = _ctx_base(request)
    c["op"] = user
    c["fmt_ts"] = _fmt_ts
    c["sl_css_version"] = _SL_CSS_VERSION
    c.update(extra)
    return c


# ── presentation helpers (display only) ──

def fmt_num(v) -> str:
    """Thousands separators for stat/table numbers ('' → 0)."""
    try:
        return f"{int(v or 0):,}"
    except (TypeError, ValueError):
        return str(v)


def tool_class(row) -> str:
    """Supply classification for the operator Tool Supply page ONLY —
    the user product surface never exposes execution class.
      owner_id NULL + local → Official local
      owner_id NULL + http  → Operator HTTP
      owner_id set          → Provider Tool"""
    if row["owner_id"] is not None:
        return "Provider Tool"
    return "Operator HTTP" if row["execution"] == "http" else \
        "Official local"
