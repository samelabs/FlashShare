#!/usr/bin/env python3
"""Public web surface — SEO/share cards/PWA, agent-facing pages
(robots, 404, llms.txt), and the /pricing shop.
"""
from _harness import REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# SEO meta, share cards, sitemap, PWA
#
# SEO / share-card / PWA canonical.
#
#   M*  single point: every page renders og:title/description/url/image,
#       twitter:* and canonical from base.html — og/twitter reuse the SAME
#       values (no per-page hand-written og tags left anywhere); twitter:card
#       is summary_large_image site-wide.
#   H*  hreflang: indexable routes (home /tools /tool/* /agents
#       /pricing /developers /terms /privacy /u/*) carry 10 languages +
#       x-default (11 links, ?lang=xx on the canonical path); noindex routes
#       (auth/account//r) carry none. Query strings (tracking params, ?lang)
#       never enter a canonical/og:url.
#   C*  share cards: /r keeps noindex but has og:title = tool + result count
#       and og:description = expiry + remaining downloads (same mechanism as
#       the /f page: og_* context overrides, base renders the tags).
#   L*  JSON-LD: home carries WebSite (+ SearchAction → /tools?q=) and
#       Organization, parseable by json.loads; /developers adds none.
#   S*  sitemap.xml: every indexable static surface + every public tool page
#       + its MCP page + author pages behind public supply, with real
#       lastmod (tool declaration updated_at); private/unlisted tools and
#       account/auth//r never appear.
#   R*  robots.txt excludes account/samelabs/auth/r/api/mcp (+ legacy /dh/)
#       and points at the sitemap.
#   P*  PWA manifest: required fields, description injected from the i18n
#       single source (en seo_desc), start_url /?source=pwa, no orientation
#       lock, split any/maskable icons whose files exist with the declared
#       dimensions, maskable artwork inside the 80% safe circle.
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('SEO meta, share cards, sitemap, PWA')

import base64
import http.client as _hc
import json as _json
import os as _os
import re as _re
import sys as _sys
import time as _time
import urllib.parse as _up
import uuid as _uuid


from config import BASE_URL  # noqa: E402
from i18n import SERVER_I18N, LOCALE_TAGS  # noqa: E402


_HOST = ("127.0.0.1", int(TEST_PORT))
_u = "seo" + _uuid.uuid4().hex[:6]


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
    payload = None
    if body is not None:
        # dict → JSON; str/bytes (urlencoded forms) pass through as-is
        payload = body if isinstance(body, (bytes, str)) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = c_resp = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return r.status, data, hdrs, c_resp


def _meta(html, name):
    m = _re.search(
        r'<meta[^>]*(?:property|name)="%s"[^>]*content="([^"]*)"' % _re.escape(name), html)
    return m.group(1) if m else None


def _tag(html, pattern):
    m = _re.search(pattern, html)
    return m.group(1) if m else None


def _page(path, cookie=None):
    st, data, hdrs, _ = _req("GET", path, headers={"Cookie": cookie} if cookie else None)
    return st, data.decode("utf-8", "replace")


# ── fixtures: one user (session + API key), one executed call → /r ──
_COOKIES = ""
_API_KEY = ""
_RESULT_ID = ""
_UPLOAD_F = ""


def _setup_fixtures():
    global _COOKIES, _API_KEY, _RESULT_ID
    st, data, hdrs, resp = _req("POST", "/auth/register", {
        "username": _u, "password": "Test12345", "email": f"{_u}@t.co",
        "password_confirm": "Test12345"}, {"Content-Type": "application/json"})
    if st != 302:
        st, data, hdrs, resp = _req("POST", "/auth/register", _up.urlencode({
            "username": _u, "password": "Test12345", "email": f"{_u}@t.co",
            "password_confirm": "Test12345"}),
            {"Content-Type": "application/x-www-form-urlencoded"})
    setc = hdrs.get("set-cookie", "")
    _COOKIES = setc.split(";")[0] if setc else ""
    st, data, _, _ = _req("POST", "/api/keys", {"name": "seo"},
                          {"Content-Type": "application/json", "Cookie": _COOKIES})
    _API_KEY = _json.loads(data)["key"]["key"]

    img = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD9"
        "1JpzAAAAFklEQVR4nGOI6rE5oaHBxMDAMClFBAAa3ANHnVmc8QAAAABJRU5ErkJggg==")
    st, data, _, _ = _req("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "compress", "arguments": {
            "files": [{"filename": "t.png", "b64": base64.b64encode(img).decode()}],
            "params": {}, "delivery": {"expiry_days": 1, "allowed_downloads": 50},
            "client_ref": "seo-" + _uuid.uuid4().hex[:8]}}},
        {"Content-Type": "application/json", "Authorization": "Bearer " + _API_KEY})
    sc = (_json.loads(data).get("result") or {}).get("structuredContent") or {}
    _RESULT_ID = (sc.get("result_url") or "").rsplit("/", 1)[-1]


def _community_tool_fixture():
    """One community tool: published unlisted via the real API, then one
    twin row flipped to public visibility directly (the publish gate —
    author run — is another test's concern; here only the sitemap's
    visibility predicate is under test)."""
    tid = "seot" + _uuid.uuid4().hex[:5]
    st, data, _, _ = _req("POST", "/api/usertools", {
        "label": "SEO fixture", "url": "https://93.184.216.34/x",
        "timeout_s": 30, "files_min": 0, "files_max": 1,
        "format_families": "image", "accept": [".png"],
        "outputs": ["text"], "cost": 0, "params": []},
        {"Content-Type": "application/json", "Cookie": _COOKIES})
    ok = st == 200 and _json.loads(data).get("ok") is True
    return tid, ok


def main():
    _setup_fixtures()
    check("fixture: user + API key + executed call", bool(_COOKIES) and bool(_API_KEY) and bool(_RESULT_ID))

    # ── M*/H*: indexable pages ─────────────────────────────────────────
    indexable = ["/", "/tools", "/tool/compress",
                 "/agents", "/pricing", "/developers", "/terms", "/privacy"]
    for p in indexable:
        st, html = _page(p)
        title = _tag(html, r"<title>(.*?)</title>")
        desc = _meta(html, "description")
        og_t, og_d = _meta(html, "og:title"), _meta(html, "og:description")
        tw_t, tw_d = _meta(html, "twitter:title"), _meta(html, "twitter:description")
        canon = _tag(html, r'<link rel="canonical" href="([^"]*)"')
        check(f"M {p}: 200 + title + description", st == 200 and bool(title) and bool(desc), f"{st} {title!r} {desc!r}")
        # /developers overrides og_* with its own share-card values (the
        # shareable title/description, no site suffix) — like /f and /r;
        # every other page derives og/twitter straight from title/desc.
        if p == "/developers":
            check(f"M {p}: og/twitter reuse the same share values",
                  og_t == tw_t and og_d == tw_d
                  and title == f"{og_t} - 518.PLUS" and og_d == desc,
                  f"og:{og_t!r} tw:{tw_t!r} t:{title!r} ogd:{og_d!r} d:{desc!r}")
        else:
            check(f"M {p}: og/twitter reuse the same values",
                  og_t == tw_t == title and og_d == tw_d == desc,
                  f"og:{og_t!r} tw:{tw_t!r} t:{title!r} ogd:{og_d!r} d:{desc!r}")
        check(f"M {p}: canonical + og:url = BASE_URL + path",
              canon == BASE_URL + p and _meta(html, "og:url") == canon, f"{canon!r}")
        check(f"M {p}: og:image + twitter:card large",
              _meta(html, "og:image") == f"{BASE_URL}/brand/web/og-image.png"
              and _meta(html, "og:image:width") == "1200"
              and _meta(html, "og:image:height") == "630"
              and _meta(html, "og:image:alt") == og_t
              and _meta(html, "twitter:card") == "summary_large_image")
        hl = _re.findall(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)"', html)
        tags = [t for t, _ in hl]
        langs = {u for t, u in hl if t != "x-default"}
        check(f"H {p}: robots index + 11 hreflang (10 langs + x-default)",
              _meta(html, "robots") == "index, follow"
              and len(hl) == 11 and "x-default" in tags
              and all(t != "x-default" for t in tags[:-1]),
              f"robots={_meta(html, 'robots')} n={len(hl)}")
        check(f"H {p}: hreflang = canonical path ?lang=xx, x-default = canonical",
              langs == {f"{BASE_URL + p}?lang={l}" for l in LOCALE_TAGS}
              and (dict(hl).get("x-default") == BASE_URL + p),
              str(sorted(langs))[:200])

    # author page (public supply comes from the community fixture below)
    tid, created = _community_tool_fixture()
    check("fixture: community tool created", created)
    pub_id = f"{_u}_" + "seop" + _uuid.uuid4().hex[:5]  # key = username_toolid
    if created:
        from database import db
        from utils import now
        with db() as con:
            row = con.execute(
                "SELECT id FROM users WHERE username = %s", (_u,)).fetchone()
            uid = row["id"]
            con.execute(
                "INSERT INTO platform_tools (tool_key, tool_id, owner_id, visibility,"
                " enabled, label_json, files_min, files_max, outputs_json,"
                " created_at, updated_at, accept_json, format_family)"
                " VALUES (%s, %s, %s, 'public', TRUE, '{\"en\": \"SEO twin\"}',"
                " 0, 1, '[\"text\"]', %s, %s, '[\".png\"]', 'image')",
                (pub_id, pub_id, uid, now(), now()))
            con.execute(
                "INSERT INTO pricing (tool_id, cost, enabled, updated_at)"
                " VALUES (%s, 0, TRUE, %s)", (pub_id, now()))
    st, html = _page(f"/u/{_u}")
    check("H /u/*: indexable author page with hreflang",
          st == 200 and _meta(html, "robots") == "index, follow"
          and html.count('rel="alternate" hreflang=') == 11,
          f"{st} robots={_meta(html, 'robots')}")

    # ── explicit ?lang page is its own canonical (hreflang alternates) ──
    st, html = _page("/pricing?lang=de&utm_source=x")
    canon = _tag(html, r'<link rel="canonical" href="([^"]*)"')
    check("H /pricing?lang=de: canonical keeps ?lang, drops tracking params",
          canon == f"{BASE_URL}/pricing?lang=de" and _meta(html, "og:url") == canon, f"{canon!r}")

    # ── alternates on an explicit ?lang page: each of the 10 carries
    # exactly ONE lang param; x-default carries none (no double query) ──
    _alts = _re.findall(
        r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)"', html)
    _xd = [h for t, h in _alts if t == "x-default"]
    _lang_alts = [(t, h) for t, h in _alts if t != "x-default"]
    _alt_codes = [h.rsplit("lang=", 1)[-1] for _t, h in _lang_alts]
    check("H /pricing?lang=de: every alternate carries exactly one ?lang=xx",
          len(_lang_alts) == 10
          and all(h.count("lang=") == 1
                  and h.startswith(f"{BASE_URL}/pricing?lang=")
                  for _t, h in _lang_alts)
          and len(set(_alt_codes)) == 10 and all(_alt_codes),
          str(_lang_alts[:3]))
    check("H /pricing?lang=de: x-default has no query string",
          len(_xd) == 1 and _xd[0] == f"{BASE_URL}/pricing", str(_xd))

    # ── noindex pages: canonical/og present, hreflang absent ───────────
    for p in ("/auth/login", "/auth/register", "/account/keys",
              "/account/tools/publish", f"/r/{_RESULT_ID}"):
        st, html = _page(p, cookie=_COOKIES if p.startswith("/account") or p.startswith("/r") else None)
        check(f"H {p}: noindex, zero hreflang, still canonical + og",
              st == 200 and _meta(html, "robots") == "noindex"
              and 'hreflang=' not in html
              and _tag(html, r'<link rel="canonical" href="([^"]*)"') == BASE_URL + p
              and bool(_meta(html, "og:title")),
              f"{st} robots={_meta(html, 'robots')}")

    # ── H: query strings never enter canonical/og:url ──────────────────
    st, html = _page("/?utm_source=x&fbclid=abc&gclid=1")
    check("H tracking params stripped from canonical/og:url",
          _tag(html, r'<link rel="canonical" href="([^"]*)"') == BASE_URL + "/"
          and _meta(html, "og:url") == BASE_URL + "/")
    st, html = _page("/tools?q=compress&page=3")
    check("H search params stripped from canonical",
          _tag(html, r'<link rel="canonical" href="([^"]*)"') == BASE_URL + "/tools")
    st, html = _page("/?lang=zh")
    check("H ?lang renders that locale and is its own canonical (hreflang alternate)",
          _meta(html, "og:locale") == "zh-CN"
          and _tag(html, r'<link rel="canonical" href="([^"]*)"') == BASE_URL + "/?lang=zh")

    # ── C*: share cards for /r (and /f parity via the same override) ──
    st, html = _page(f"/r/{_RESULT_ID}")
    og_t = _meta(html, "og:title")
    check("C /r: og:title = result content · tool", st == 200
          and og_t and og_t.endswith(" · Compress") and len(og_t) > len(" · Compress"), og_t)
    check("C /r: og:description = expiry + remaining downloads",
          "Available until" in (_meta(html, "og:description") or "")
          and "Downloads left" in (_meta(html, "og:description") or ""),
          _meta(html, "og:description"))
    check("C /r: twitter mirrors og", _meta(html, "twitter:title") == og_t)

    # ── L*: JSON-LD ────────────────────────────────────────────────────
    st, html = _page("/")
    ld = _re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, _re.S)
    parsed = None
    try:
        parsed = _json.loads(ld[0]) if ld else None
    except ValueError:
        parsed = None
    graph = {n.get("@type"): n for n in (parsed or {}).get("@graph", [])} if parsed else {}
    check("L home: ld+json parses, WebSite + Organization",
          parsed is not None and "WebSite" in graph and "Organization" in graph)
    site = graph.get("WebSite") or {}
    pa = site.get("potentialAction") or {}
    check("L home: SearchAction targets /tools?q=",
          pa.get("@type") == "SearchAction"
          and (pa.get("target") or {}).get("urlTemplate", "").endswith("/tools?q={search_term_string}")
          and "search_term_string" in (pa.get("query-input") or ""))
    st, html = _page("/developers")
    check("L /developers: no ld+json", "application/ld+json" not in html)

    # ── S*: sitemap ────────────────────────────────────────────────────
    st, data, hdrs, _ = _req("GET", "/sitemap.xml")
    xml = data.decode()
    locs = _re.findall(r"<loc>([^<]+)</loc>", xml)
    check("S sitemap: 200 xml + static surfaces",
          st == 200 and "xml" in hdrs.get("content-type", "")
          and all(BASE_URL + p in locs for p in
                  ("/", "/tools", "/agents", "/pricing", "/developers", "/terms", "/privacy")),
          f"{st} {len(locs)}")
    check("S sitemap: public tool pages with lastmod (mcp twins retired)",
          f"{BASE_URL}/tool/compress" in locs
          and f"{BASE_URL}/tool/compress/mcp" not in locs
          and bool(_re.search(r"<loc>%s/tool/compress</loc><lastmod>\d{4}-\d{2}-\d{2}</lastmod>"
                              % _re.escape(BASE_URL), xml)))
    check("S sitemap: community public tool + author page",
          f"{BASE_URL}/tool/{pub_id}" in locs and f"{BASE_URL}/u/{_u}" in locs)
    check("S sitemap: unlisted fixture tool excluded",
          f"/tool/{tid}</loc>" not in xml
          and all(not l.startswith((BASE_URL + "/account", BASE_URL + "/auth",
                                   BASE_URL + "/r/", BASE_URL + "/samelabs")) for l in locs),
          str([l for l in locs if "/account" in l or "/auth" in l])[:120])

    # ── R*: robots.txt ─────────────────────────────────────────────────
    st, data, _, _ = _req("GET", "/robots.txt")
    body = data.decode()
    for dis in ("/account", "/samelabs", "/auth", "/api/", "/mcp", "/dh/"):
        check(f"R robots: Disallow {dis}", f"Disallow: {dis}" in body)
    check("R robots: share pages /f and /r stay fetchable (preview bots, agents)",
          not any(ln.strip() in ("Disallow: /r", "Disallow: /r/", "Disallow: /f", "Disallow: /f/")
                  for ln in body.splitlines()))
    check("R robots: sitemap pointer", f"Sitemap: {BASE_URL}/sitemap.xml" in body)

    # ── P*: PWA manifest ───────────────────────────────────────────────
    st, data, hdrs, _ = _req("GET", "/site.webmanifest")
    mf = _json.loads(data)
    for f in ("name", "short_name", "id", "start_url", "scope", "display",
              "background_color", "theme_color", "icons", "shortcuts"):
        check(f"P manifest: field {f}", f in mf)
    check("P manifest: description = en seo_desc (i18n single source)",
          mf.get("description") == SERVER_I18N["en"]["seo_desc"], mf.get("description"))
    check("P manifest: start_url /?source=pwa + no orientation lock",
          mf.get("start_url") == "/?source=pwa" and "orientation" not in mf)
    check("P manifest: theme/background from design tokens",
          mf.get("theme_color") == "#fbfbfc" and mf.get("background_color") == "#fbfbfc")
    icons = mf.get("icons") or []
    mask512 = [i for i in icons if i.get("purpose") == "maskable" and i["sizes"] == "512x512"]
    check("P manifest: any + maskable split, one maskable 512", bool(mask512)
          and all(i.get("purpose") in ("any", "maskable") for i in icons))
    # declared dimensions match the actual files; maskable art inside the
    # 80% safe circle (white glyph max radius <= 40% of the canvas)
    from PIL import Image
    import math
    for i in icons:
        path = _os.path.join(REPO, "brand", "web",
                             i["src"].split("?")[0].rsplit("/", 1)[-1])
        exists = _os.path.isfile(path)
        w = h = 0
        if exists:
            with Image.open(path) as im:
                w, h = im.size
        sw, sh = (int(x) for x in i["sizes"].split("x"))
        check(f"P manifest: icon {i['src'].split('/')[-1]} exists, {i['sizes']} real",
              exists and (w, h) == (sw, sh), f"{(w, h)} vs {i['sizes']}")
    for i in mask512:
        path = _os.path.join(REPO, "brand", "web",
                             i["src"].split("?")[0].rsplit("/", 1)[-1])
        with Image.open(path) as im:
            px = im.convert("RGBA").load()
            n = im.size[0]
            cx = cy = (n - 1) / 2
            maxr = 0.0
            for y in range(0, n, 2):
                for x in range(0, n, 2):
                    r, g, b, a = px[x, y]
                    if r > 235 and g > 235 and b > 235:
                        maxr = max(maxr, math.hypot(x - cx, y - cy))
        check("P manifest: maskable 512 glyph inside the safe circle",
              maxr <= 0.4 * n, f"max r {maxr:.0f} > {0.4 * n:.0f}")
    sc_urls = [s.get("url", "") for s in mf.get("shortcuts") or []]
    check("P manifest: shortcuts = share / tools / AI entries",
          any(u.startswith("/") for u in sc_urls) and len(sc_urls) == 3
          and {u.split("?")[0] for u in sc_urls} == {"/", "/tools", "/agents"},
          sc_urls)

    # base.html chrome parity
    st, html = _page("/")
    check("P base: theme-color light/dark match the manifest",
          '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#fbfbfc">' in html
          and '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0f1419">' in html)
    check("P base: apple web-app metas",
          'name="apple-mobile-web-app-capable"' in html
          and 'name="apple-mobile-web-app-title" content="518.PLUS"' in html
          and 'name="apple-mobile-web-app-status-bar-style"' in html)

    # cleanup
    try:
        from database import db
        with db() as con:
            row = con.execute("SELECT id FROM users WHERE username = %s", (_u,)).fetchone()
            if row:
                purge_users(con, [row["id"]])
    except Exception as exc:  # noqa: BLE001
        print("cleanup warning:", exc)


main()


# ════════════════════════════════════════════════════════════════════════
# Agent-web surface
#
# Agent-web surface (B7): robots policy, real 404, /llms.txt generated
# guide, discovery links, and share-page agent affordances.
#
#   R*  robots.txt: /f/ fetchable; /api/ /auth/ /dh/ /mcp Disallowed
#       (the SEO/PWA batch narrowed the crawl boundary: private/ephemeral
#       namespaces — account/samelabs/auth//r — and the POST-only machine
#       surface /mcp stay out of crawlers; share links stay fetchable);
#       stale /d/ rule gone
#   X*  X-Robots-Tag: noindex, nofollow on every /f/ and /mcp response
#   N*  unknown page → real 404 + rendered page with home/tools/MCP/
#       llms.txt links; /api/* stays JSON 404; missing share stays 404
#   L*  /llms.txt: text/plain utf-8, live protocol versions from mcp.py,
#       endpoint, curl examples, catalog parity with tools/list (names,
#       one-line descriptions, prices)
#   D*  discovery: <link rel=alternate llms.txt> + <link rel=service /mcp>
#       on every page head; home and /agents carry SSR-readable MCP
#       endpoint + llms.txt text
#   S*  share page: Link enclosure header (+type), visible plain <a> to
#       ?download=1, <link rel=enclosure>; HEAD page = 200 same headers
#       empty body; HEAD download = 302+Location, never touching a
#       counting Gokapi path; GET behavior unchanged
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('Agent-web surface')

import base64
import hashlib
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _time
import urllib.request as _ur
import uuid as _uuid


_HOST_agw = ("127.0.0.1", int(TEST_PORT))
_BASE = f"http://127.0.0.1:{TEST_PORT}"
_u_agw = "aw" + _uuid.uuid4().hex[:6]
ts = int(_time.time())

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req_agw(method, path, body=None, headers=None, raw=False):
    conn = _hc.HTTPConnection(*_HOST_agw, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    if raw:
        return r.status, hdrs, data, setc
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


from database import db

uid = None
share_pid = None
try:
    # ═══ R. robots.txt ═══
    st, h, body, _sc = _req_agw("GET", "/robots.txt")
    check("R1 robots.txt text/plain",
          st == 200 and "text/plain" in h.get("content-type", ""), st)
    check("R2 /f/ is fetchable (not Disallowed)",
          "Disallow: /f/" not in body and "/f/" not in body.split("Sitemap")[0], body)
    check("R3 /api/ /auth/ /dh/ stay Disallowed",
          all(f"Disallow: {p}" in body for p in ("/api/", "/auth\n", "/dh/")), body)
    check("R4 stale /d/ rule removed; /mcp not crawlable",
          "Disallow: /d/\n" not in body and "Disallow: /mcp" in body, body)

    # ═══ X. per-response noindex ═══
    # (a live /f/ row is created below; first prove the /mcp half)
    st, h, _b, _sc = _req_agw("POST", "/mcp",
                          {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          {"Content-Type": "application/json"})
    check("X1 /mcp carries X-Robots-Tag: noindex, nofollow",
          h.get("x-robots-tag") == "noindex, nofollow", h.get("x-robots-tag"))

    # ═══ N. real 404 ═══
    st, h, body, _sc = _req_agw("GET", "/definitely-not-a-page-" + _u_agw)
    check("N1 unknown page → HTTP 404 (no home redirect)",
          st == 404, st)
    check("N2 404 page lists home / tools / MCP / llms.txt",
          st == 404 and 'href="/tools"' in body and 'href="/agents"' in body
          and 'href="/llms.txt"' in body and 'href="/"' in body, None)
    st, h, d, _sc = _req_agw("GET", "/api/definitely-not-" + _u_agw)
    check("N3 /api/* stays JSON 404",
          st == 404 and h.get("content-type", "").startswith("application/json")
          and (d or {}).get("error") == "not_found", (st, d))
    st, h, body, _sc = _req_agw("GET", "/f/nonexistent" + _u_agw)
    check("N4 missing share stays 404 (rendered page)",
          st == 404 and b"<html" in (body.encode() if isinstance(body, str) else body or b""), st)
    check("N4b missing share carries X-Robots-Tag",
          h.get("x-robots-tag") == "noindex, nofollow", h.get("x-robots-tag"))

    # ═══ L. /llms.txt ═══
    st, h, body, _sc = _req_agw("GET", "/llms.txt")
    ct = h.get("content-type", "")
    check("L1 /llms.txt 200 text/plain; charset=utf-8",
          st == 200 and ct.startswith("text/plain") and "utf-8" in ct, ct)
    from app.routes.mcp import SUPPORTED_VERSIONS
    check("L2 live protocol versions present",
          all(v in body for v in SUPPORTED_VERSIONS), SUPPORTED_VERSIONS)
    check("L3 endpoint + auth + client_ref documented",
          "/mcp" in body and "fsk_" in body and "client_ref" in body, None)
    check("L4 file input forms + two-stage upload documented",
          "b64" in body and '"url"' in body and "public_id" in body
          and "create_upload" in body and "-X PUT" in body, None)
    check("L5 share link semantics + billing documented",
          "?download=1" in body and "1000 credits = 1 USD" in body, None)
    # catalog parity: llms.txt lists the recommended (featured) tools
    from governance.catalog import search_catalog
    from tools import load_all as _load_all
    _load_all()
    from governance.llms_txt import LLMS_TOOLS_MAX
    from governance.tool_params import resolve_label
    _pub = search_catalog("", limit=LLMS_TOOLS_MAX)
    parity = True
    for t in _pub:
        nm = t["id"]  # MCP 名 = key 原文
        line = f"- {nm}: "
        if line not in body:
            parity = False
            break
    check("L6 catalog lines match the recommended tools",
          parity and "- share:" in body, None)
    # register + token → tools/list parity over the wire
    b, ct2 = _form({"username": _u_agw + "a", "password": "Test12345",
                    "password_confirm": "Test12345",
                    "email": f"{_u_agw}@t.co"})
    _req_agw("POST", "/auth/register", b, {"Content-Type": ct2})
    b, ct2 = _form({"login": _u_agw + "a", "password": "Test12345"})
    st_l, _h, _d, sc = _req_agw("POST", "/auth/login", b, {"Content-Type": ct2})
    ck = sc.split(";")[0]
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (_u_agw + "a",)).fetchone()["id"])
    st, _h, d_t, _sc = _req_agw("POST", "/api/keys", {"name": "aw"},
                            {"Content-Type": "application/json", "Cookie": ck})
    tok = d_t["key"]["key"]
    st, _h, d, _sc = _req_agw("POST", "/mcp",
                          {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          {"Content-Type": "application/json",
                           "Authorization": f"Bearer {tok}"})
    listed = {t["name"] for t in d["result"]["tools"]}
    catalog_block = body.split("## Recommended tools")[1].split("(server:")[0]
    llms_names = {ln[2:].split(":")[0] for ln in catalog_block.splitlines()
                  if ln.startswith("- ")}
    expect_names = {t["id"] for t in _pub}  # MCP 名 = key 原文
    first_party = {n for n in expect_names if "_" not in n}
    check("L7 llms.txt tool names == recommended catalog names",
          llms_names == expect_names
          and first_party <= listed,
          (sorted(llms_names), sorted(expect_names)))

    # ═══ D. discovery links ═══
    for path, label in (("/", "home"), ("/tools", "market"),
                        ("/agents", "agents"), ("/auth/login", "login")):
        st, _h, body, _sc = _req_agw("GET", path)
        ok = ('rel="alternate" type="text/plain" href="/llms.txt"' in body
              and 'rel="service"' in body and 'href="/mcp"' in body)
        check(f"D1 {label}: llms.txt alternate + MCP service links in head",
              st == 200 and ok, st)
    st, _h, body, _sc = _req_agw("GET", "/")
    check("D2 home SSR text names the MCP endpoint and links /llms.txt",
          "/mcp" in body and 'href="/llms.txt"' in body, None)
    st, _h, body, _sc = _req_agw("GET", "/agents")
    check("D3 /agents SSR text names endpoint + llms.txt (fsk_ hint)",
          "/mcp" in body and "/llms.txt" in body and "fsk_" in body, None)

    # ═══ S. share-page agent affordances ═══
    # sharing runs through the share tool — stage the source,
    # then execute; the settled file item carries the public share id.
    b = "----aw"
    up_body = (f'--{b}\r\nContent-Disposition: form-data; name="client_ref"'
               f'\r\n\r\naw-{_u_agw}\r\n'
               f'--{b}\r\nContent-Disposition: form-data; name="tool"'
               f'\r\n\r\nshare\r\n'
               f'--{b}\r\nContent-Disposition: form-data; name="file";'
               f' filename="s.png"\r\nContent-Type: image/png\r\n\r\n'
               ).encode() + PNG + f"\r\n--{b}--\r\n".encode()
    st, _h, d_up, _sc = _req_agw("POST", "/api/uploads", up_body,
                             {"Content-Type":
                              f"multipart/form-data; boundary={b}",
                              "Cookie": ck})
    _staged = (d_up or {}).get("file") or {}
    gid = _staged.get("stagingId")
    _stok = _staged.get("sourceToken") or ""
    st_x, _h2, d_x, _sc2 = _req_agw("POST", "/api/tools/execute",
        {"tool_id": "share",
         "source": {"files": [{"public_id": gid, "source_token": _stok}]},
         "params": {},
         "delivery": {"expiry_days": 1, "allowed_downloads": 50},
         "client_ref": f"awx-{_u_agw}"},
        {"Content-Type": "application/json", "Cookie": ck})
    items = ((d_x or {}).get("result") or {}).get("items") or []
    share_pid = next((it.get("public_id") for it in items
                      if it.get("kind") == "file"), None)
    check("S0 share fixture (upload+execute) settled",
          st == 200 and st_x == 200 and bool(share_pid), (st, st_x, str(d_x)[:80]))

    st, h, body, _sc = _req_agw("GET", f"/f/{share_pid}")
    link = h.get("link", "")
    check("S1 page Link header = enclosure + type",
          'rel="enclosure"' in link and f"/f/{share_pid}?download=1" in link
          and 'type="image/png"' in link, link)
    check("S1b X-Robots-Tag on the share page", h.get("x-robots-tag") ==
          "noindex, nofollow", h.get("x-robots-tag"))
    check("S2 visible plain <a href> to ?download=1 (SSR, not JS)",
          f'href="/f/{share_pid}?download=1"' in body, None)
    check("S3 <link rel=enclosure> in head",
          'rel="enclosure"' in body.split("</head>")[0], None)

    def _dl_count():
        from gokapi import get_gokapi_file
        with db() as con:
            gid = con.execute("select gokapi_id from uploads where"
                              " public_id=%s", (share_pid,)).fetchone()["gokapi_id"]
        return int((get_gokapi_file(gid) or {}).get("DownloadCount", -1))

    c0 = _dl_count()
    st, h, body_raw, _sc = _req_agw("HEAD", f"/f/{share_pid}", raw=True)
    check("S4 HEAD page → 200, same Link/X-Robots headers, empty body",
          st == 200 and 'rel="enclosure"' in h.get("link", "")
          and h.get("x-robots-tag") == "noindex, nofollow"
          and len(body_raw) == 0, (st, h.get("link"), len(body_raw)))
    st, h, body_raw, _sc = _req_agw("HEAD", f"/f/{share_pid}?download=1", raw=True)
    loc = h.get("location", "")
    check("S5 HEAD download → 302 + Location (no follow, no body)",
          st == 302 and loc and len(body_raw) == 0, (st, loc))
    c1 = _dl_count()
    check("S6 HEAD never consumes a download", c1 == c0, (c0, c1))
    st, h, _b, _sc = _req_agw("GET", f"/f/{share_pid}?download=1")
    check("S7 GET download still 302 to the file",
          st == 302 and h.get("location"), (st, h.get("location")))
    st, h, _b, _sc = _req_agw("GET", f"/f/{share_pid}")
    check("S8 GET page unchanged (200 render)",
          st == 200 and "text/html" in h.get("content-type", ""), st)
finally:
    with db() as con:
        if uid is not None:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from upload_usage where user_id=%s", (uid,))
            con.execute("delete from ingress_requests where user_id=%s", (uid,))
            con.execute("delete from uploads where user_id=%s", (uid,))
            purge_users(con, [uid])
    print("cleanup done")


# ════════════════════════════════════════════════════════════════════════
# Public /pricing shop
#
# Public /pricing shop.
#
# Signed-out visitors see the free plan, every sellable membership plan and
# credit pack, and the refund policy; buying opens the login modal. The
# account credits page keeps balance, membership status and history and
# links to /pricing for purchases. The refund policy is part of the terms
# in every language.
# ════════════════════════════════════════════════════════════════════════
section('Public /pricing shop')

import os as _os
import sys as _sys


import json as _json
import time as _time

ts_prc = int(_time.time())
_u_prc = "pr" + _os.urandom(4).hex()

from database import db

_USERS, _PLANS, _PKGS = [], [], []


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_prc, ts_prc)).fetchone()
    _USERS.append(int(row["id"]))
    return int(row["id"])


def _mkplan(code, price, monthly, so, active=True):
    with db() as con:
        row = con.execute(
            "insert into plans (code, name, monthly_credits, price_cents,"
            " currency, creem_product_id, sort_order, active,"
            " ingress_files_24h, ingress_bytes_24h, expiry_days,"
            " download_limits, created_at, updated_at) values"
            " (%s, %s, %s, %s, 'USD', %s, %s, %s, 500, 524288000,"
            " ARRAY[1,7,30], ARRAY[50,200], %s, %s) returning id",
            (code, code.upper(), monthly, price, f"prod_{code}", so, active,
             ts_prc, ts_prc)).fetchone()
    _PLANS.append(int(row["id"]))
    return int(row["id"])


def _cleanup():
    with db() as con:
        for uid in _USERS:
            con.execute("delete from pending_downgrades where user_id=%s", (uid,))
            con.execute("delete from memberships where user_id=%s", (uid,))
            purge_users(con, [uid])
        for pid in _PLANS:
            con.execute("delete from plans where id=%s", (pid,))
        for kid in _PKGS:
            con.execute("delete from packages where id=%s", (kid,))
    from governance import plan_catalog
    plan_catalog.catalog(refresh=True)


try:
    from economy import creem as _creem
    from fastapi.testclient import TestClient
    from app.main import app as _app
    from app.web_auth import create_browser_session, COOKIE_NAME
    from governance import plan_catalog
    client = TestClient(_app)

    def _get(path, cookie=None):
        r = client.get(path, cookies={COOKIE_NAME: cookie} if cookie else None,
                       follow_redirects=False)
        return r.status_code, r.text

    # ═══ P1: public page, no payment configured ═══
    _creem.creem_enabled = lambda: False
    _creem.credits_ready = lambda: False
    st, html = _get("/pricing?lang=en")
    check("P1 anonymous /pricing → 200", st == 200, st)
    check("P1b header nav links /pricing", 'href="/pricing"' in html)
    check("P1c free plan card + refund section + Creem named",
          "pr-card" in html and 'id="refund"' in html and "Creem" in html)
    check("P1d nothing sellable → no buy buttons, 'not open yet' note",
          "sub-buy" not in html and "topup-buy" not in html
          and "pricing_none" in html, html[:0])
    st, home = _get("/?lang=en")
    check("P2 home header links /pricing for signed-out visitors",
          st == 200 and 'href="/pricing"' in home)

    # ═══ P3: payment configured → sellable plans + packs listed ═══
    _creem.creem_enabled = lambda: True
    _creem.credits_ready = lambda: True
    with db() as con:
        _so = int(con.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans").fetchone()["s"])
        k = con.execute(
            "insert into packages (code, name, credits, price_cents, sort_order,"
            " active, created_at, updated_at) values (%s, 'Pack', 1234, 499, 0,"
            " TRUE, %s, %s) returning id", (f"pk_{_u_prc}", ts_prc, ts_prc)).fetchone()
        _PKGS.append(int(k["id"]))
    plus_id = _mkplan(f"plus_{_u_prc}", 999, 100, _so)
    pro_id = _mkplan(f"pro_{_u_prc}", 2999, 500, _so + 1)
    off_id = _mkplan(f"off_{_u_prc}", 1999, 300, _so + 2, active=False)
    plan_catalog.catalog(refresh=True)
    st, html = _get("/pricing?lang=en")
    check("P3 anonymous sees subscribe buttons for sellable plans",
          f'data-plan="{plus_id}"' in html and f'data-plan="{pro_id}"' in html,
          st)
    check("P3b inactive plan not listed", f'data-plan="{off_id}"' not in html)
    check("P3c credit pack listed with buy button",
          f'data-pkg="{_PKGS[0]}"' in html and "$4.99" in html)
    check("P3d plan price + perks rendered (max expiry / downloads)",
          "$9.99" in html and "30 days" in html and "200 downloads" in html)
    check("P3e page loads the checkout script",
          "/static/pages/pricing.js" in html)

    # ═══ P4: signed-in PLUS member → current / upgrade actions ═══
    uid = _mkuser(f"{_u_prc}m")
    with db() as con:
        con.execute(
            "insert into memberships (user_id, plan_id, provider_subscription_id,"
            " status, started_at, renews_at, updated_at) values"
            " (%s, %s, %s, 'active', %s, %s, %s)",
            (uid, plus_id, f"sub_{_u_prc}", ts_prc, ts_prc + 30 * 86400, ts_prc))
    ck = create_browser_session(uid)
    st, html = _get("/pricing?lang=en", ck)
    check("P4 member: own plan marked current, higher plan offers upgrade",
          st == 200 and "pr-card on" in html
          and f'class="btn btn-p sub-upgrade" data-plan="pro_{_u_prc}"' in html
          and f'data-plan="{plus_id}"' not in html, st)

    # ═══ P5: credits page shows balance/status, buys live on /pricing ═══
    st, html = _get("/account/credits?lang=en", ck)
    check("P5 /account/credits → 200 with link to /pricing, no buy buttons",
          st == 200 and 'href="/pricing"' in html
          and "sub-buy" not in html and "topup-buy" not in html
          and "sub-badge on" in html, st)

    # ═══ P6: refund policy in the terms, every language ═══
    for lang in ("en", "zh", "ja", "ko", "fr", "de", "es", "pt", "ru", "ar"):
        st, html = _get(f"/terms?lang={lang}")
        check(f"P6 /terms?lang={lang} has the refund section",
              st == 200 and 'id="refund"' in html and "14" in html
              and "Creem" in html, st)
        check(f"P6b /terms?lang={lang} links /pricing + support email",
              st == 200 and 'href="/pricing"' in html
              and "ad@live.it" in html, st)
finally:
    _cleanup()


finish()
