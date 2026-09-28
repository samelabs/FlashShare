"""app/web_ctx — 应用层公共页面上下文 (批3a, 消灭 routes 互 import).

_ctx_base 原实现从 pages.py 上收; pages 保留同名薄壳 (模板侧零改动)。
"""

import datetime
import hashlib
import json
import os as _os
import sys
from pathlib import Path

from fastapi import Request

_CORE = Path(__file__).resolve().parent.parent / "core"

_BASE = Path(__file__).resolve().parent.parent
# Load static assets once
_STATIC = _BASE / "static"
# CSS version stamp = hard prefix + content digest of the FULL CSS set
# (tokens + components + the 518.css page file): any file change bumps
# every stylesheet URL.
import os as _os
_CSS_VERSION = "v66"  # 硬版本号: v2被CF以旧体缓存污染, 每次大改递增 (FX-2 戳纳入 518.css)
for _cssf in ("518.css", "css/tokens.css", "css/components.css"):
    with open(_STATIC / _cssf, encoding="utf-8") as f:
        _CSS_VERSION += hashlib.md5(f.read().encode()).hexdigest()[:4]
# Separate cache-bust version for i18n.js — it changes independently of 518.css
with open(_STATIC / "i18n.js", encoding="utf-8") as f:
    _I18N_JS = f.read()
_I18N_VERSION = hashlib.md5((_I18N_JS + str(int(_os.path.getmtime(_STATIC / "i18n.js")))).encode()).hexdigest()[:8]

# pages/*.js 独立戳: 目录聚合 md5 (任一文件改动即变)。此前全部借用 js_version
# (app.js 的 hash) — pages 文件改动戳不变, 边缘永发旧文件 (skill 记录过的同款缺陷)。
_PAGES_DIR = _STATIC / "pages"
_pages_digest = hashlib.md5()
for _f in sorted(_PAGES_DIR.glob("*.js")):
    _pages_digest.update(_f.name.encode())
    _pages_digest.update(_f.read_bytes())
PAGES_VERSION = _pages_digest.hexdigest()[:8]

# app.js 独立内容戳 (审计: app.js 借 pages_version — 改动戳不变, CF 永发旧文件)
_app_digest = hashlib.md5()
_app_src = _STATIC / "app.js"
_app_digest.update(_app_src.read_bytes())
APP_JS_VERSION = _app_digest.hexdigest()[:8]

# zip-preview.js 独立内容戳 — 同款缺陷类: 它在 static 根目录, pages 目录
# 聚合摘要不覆盖它, detail.js 若借用 pages_version 则该文件改动戳不变。
_zip_digest = hashlib.md5()
_zip_src = _STATIC / "zip-preview.js"
_zip_digest.update(_zip_src.read_bytes())
ZIP_PREVIEW_VERSION = _zip_digest.hexdigest()[:8]
sys.path.insert(0, str(_CORE))

from config import (APP_NAME, BASE_URL, SOCIAL_IMAGE_URL,
                    PUBLIC_SUPPORT_CHANNEL, X_CLIENT_ID, X_CLIENT_SECRET)
from i18n import LOCALE_TAGS, server_text
from icons import _ICONS, icon_svg, icon_path, tool_icon_svg as _tool_icon_svg
from identity.auth import public_user
from economy.billing import spendable_balance
from governance.access_projection import project
def fmt_ts(ts) -> str:
    """Unix ts → local time string for templates.

    无 UTC 后缀: 全站单点 (治理4), 用户面时间不带时区术语。"""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(
        int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def error_text(request: Request, code: str) -> str:
    """Domain error code → localized user text. Codes without a text in
    SERVER_I18N render the generic localized message; a raw err_* key or
    exception text never reaches the page. Client-parametrized values
    ({mb}/{formats}/…) render bare here — the server has no channel for
    the values on this path, so placeholders and their empty brackets
    are stripped instead of leaking."""
    import re as _re
    texts = server_text(getattr(request.state, "lang", "en"))
    text = texts.get(code) or texts["err_unexpected"]
    text = _re.sub(r"\{[a-z_]+\}", "", text)
    return _re.sub(r"\s*[（(]\s*[）)]\s*$", "", text).rstrip()


def ctx_base(request: Request, page_id: str = "") -> dict:
    """Base context for all pages. page_id names the surface for the
    header nav highlight (empty string = no highlight)."""
    lang = request.state.lang
    user = request.state.user
    from economy.billing import spendable_balance as get_balance
    # Membership fact via the access projection (read model):
    # access.membership is the single membership shape every template
    # reads — no alias, no second name. Anonymous → access None.
    # Projection fact-read failures propagate (never masked as Free).
    # Credits stay an independent balance, outside the projection.
    access = project(user["id"]) if user else None
    return {
        "request": request,
        "lang": lang,
        "locale": LOCALE_TAGS.get(lang, "en"),
        "direction": "rtl" if lang == "ar" else "ltr",
        "texts": server_text(lang),
        "user": public_user(user) if user else None,
        # dict (not pre-dumped JSON) so base.html can apply |tojson,
        # which escapes < > & — required inside a <script> block.
        "user_json": public_user(user) if user else None,
        # Header credit chip (None for anonymous)
        "credits": get_balance(user["id"]) if user else None,
        "access": access,
        "icons_json": json.dumps(_ICONS, ensure_ascii=False),
        "APP_NAME": APP_NAME,
        "support_channel": PUBLIC_SUPPORT_CHANNEL,
        "page_id": page_id,
        # X one-click login availability (pages + modal gate)
        "x_login_enabled": bool(X_CLIENT_ID and X_CLIENT_SECRET),
        "BASE_URL": BASE_URL,
        "SOCIAL_IMAGE_URL": SOCIAL_IMAGE_URL,
        "css_version": _CSS_VERSION,
        "pages_version": PAGES_VERSION,
        "app_js_version": APP_JS_VERSION,
        "zip_preview_version": ZIP_PREVIEW_VERSION,
        "i18n_version": _I18N_VERSION,
        "fmt_ts": fmt_ts,
        "year": datetime.datetime.now().year,
        # icon helpers
        "icon_svg": icon_svg,
        "icon_path": icon_path,
        # Tool identity single renderer (fallback 单源在 core/icons.py)
        "icon_tool_svg": _tool_icon_svg,
        "brand_icon": "",
        "close_icon": icon_svg("close"),
        "history_icon_svg": icon_svg("history"),
    }


