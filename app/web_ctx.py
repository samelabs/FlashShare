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
with open(_STATIC / "518.css", encoding="utf-8") as f:
    _CSS = f.read()
# Version stamp = content hash + mtime: content-converging edits (diverge then
# reconverge) can collide on a pure content hash and serve stale edge caches.
import os as _os
_CSS_VERSION = "v42"  # 硬版本号: v2被CF以旧体缓存污染, 每次大改递增 (footer policy links)
with open(_STATIC / "app.js", encoding="utf-8") as f:
    JS = f.read()
# app.js gets its OWN stamp: it changes independently of 518.css. Sharing
# css_version meant an app.js-only edit kept the old stamp → edge served the
# stale (buggy) app.js forever under the same URL.
_JS_VERSION = hashlib.md5((JS + str(int(_os.path.getmtime(_STATIC / "app.js")))).encode()).hexdigest()[:8]
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
sys.path.insert(0, str(_CORE))

from config import APP_NAME, BASE_URL, SOCIAL_IMAGE_URL, PUBLIC_SUPPORT_CHANNEL
from i18n import LOCALE_TAGS, server_text
from icons import _ICONS, icon_svg, icon_path, tool_icon_svg as _tool_icon_svg
from identity.auth import public_user
from economy.billing import spendable_balance
from governance.access_projection import project
from governance.entitlements import plan_entitlement
from utils import (
    brand_icon_svg, upload_icon_svg, file_size_icon_svg, upload_count_icon_svg,
    bandwidth_icon_svg, expiry_icon_svg, download_count_icon_svg,
    close_icon_svg, history_icon_svg,
)


def fmt_ts(ts) -> str:
    """Unix ts → local time string for templates.

    无 UTC 后缀: 全站单点 (治理4), 用户面时间不带时区术语。"""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(
        int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def ctx_base(request: Request) -> dict:
    """Base context for all pages."""
    lang = request.state.lang
    user = request.state.user
    from economy.billing import spendable_balance as get_balance
    access = project(user["id"]) if user else None
    return {
        "request": request,
        "lang": lang,
        "locale": LOCALE_TAGS.get(lang, "en"),
        "direction": "rtl" if lang == "ar" else "ltr",
        "texts": server_text(lang),
        "user": public_user(user) if user else None,
        "user_json": public_user(user) if user else None,
        "credits": get_balance(user["id"]) if user else None,
        "access": access,
        "my_plan": access["membership"] if access else None,
        "icons_json": json.dumps(_ICONS, ensure_ascii=False),
        "APP_NAME": APP_NAME,
        "support_channel": PUBLIC_SUPPORT_CHANNEL,
        "BASE_URL": BASE_URL,
        "SOCIAL_IMAGE_URL": SOCIAL_IMAGE_URL,
        "css_version": _CSS_VERSION,
        "js_version": _JS_VERSION,
        "pages_version": PAGES_VERSION,
        "app_js_version": APP_JS_VERSION,
        "i18n_version": _I18N_VERSION,
        "fmt_ts": fmt_ts,
        "year": datetime.datetime.now().year,
        "plan_entitlement": plan_entitlement,
        "icon_svg": icon_svg,
        "icon_path": icon_path,
        "icon_tool_svg": _tool_icon_svg,
        "brand_icon": brand_icon_svg(),
        "close_icon": close_icon_svg(),
        "history_icon_svg": history_icon_svg(),
    }
