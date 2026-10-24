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

    # username identity is lower-folded (the same rule as login): an
    # uppercased URL resolves the same author and the page displays the
    # STORED handle, never the URL casing. Sample: 1 page fetch.
    st, html = _page(f"/u/{_u.upper()}")
    check("H /u/<UPPER>: folds to the author, shows the stored handle",
          st == 200 and f"@{_u}" in html and f"@{_u.upper()}" not in html,
          f"{st}")

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
        return int((get_gokapi_file(gid)[1] or {}).get("DownloadCount", -1))

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
          # member chip (fixture plan carries no badge_key → base tier,
          # never the free variant)
          and 'class="tier' in html and 'tier_free' not in html, st)

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


# ════════════════════════════════════════════════════════════════════════
# Auth form single source + account records (filter / paging / overview)
#
#   A*   auth unification: /auth/login + /auth/register render the ONE
#        shared card (auth/_card.html) — same field specs, seg mode
#        switch, recovery + terms on both; base.html embeds the two
#        server-rendered <template>s the modal clones; the partial
#        carries no DOM ids; the modal JS holds no hand-written field
#        markup; the standalone pages keep real POST forms (no-JS).
#   RC*  account records: 20-per-page with real-total page bounds
#        (past the global 10k ceiling via the bound function), range
#        filter (all/7/30/90) with stats window semantics (range=all →
#        overview/trend fixed to last 30 days and labeled), consume /
#        refund / net from ledger kinds only, per-status call counts
#        via the net-charge relation projection, per-user isolation.
#        Fixed timestamps (no sleeps); 21-row samples for 20/1 paging;
#        the 10001st-page reachability uses the bound function with a
#        controlled total, not 200k fixture rows.
# ════════════════════════════════════════╤═══════════════════════════════
section('Auth form single source + account records')

import time as _time_rc

_ts_rc = int(_time_rc.time())
_u_rc = "rc" + _uuid.uuid4().hex[:6]
_RC_USERS = []


def _mkuser_rc(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", _ts_rc, _ts_rc)).fetchone()
    _RC_USERS.append(int(row["id"]))
    return int(row["id"])


def _led_rc(con, uid, kind, delta, bal, at, n, ref_type=None, ref_id=None):
    con.execute(
        "insert into ledger (user_id, delta, balance_after, kind, ref_type,"
        " ref_id, idem_key, note, created_at) values (%s, %s, %s, %s, %s,"
        " %s, %s, NULL, %s)",
        (uid, delta, bal, kind, ref_type, ref_id, f"rc_{_u_rc}_{n}", at))


try:
    from fastapi.testclient import TestClient as _TC_rc
    from app.main import app as _app_rc
    from app.web_auth import create_browser_session as _cbs_rc, COOKIE_NAME as _CK_rc
    client_rc = _TC_rc(_app_rc)

    def _get_rc(path, cookie=None):
        r = client_rc.get(path, cookies={_CK_rc: cookie} if cookie else None,
                          follow_redirects=False)
        return r.status_code, r.text

    # ═══ A. auth unification (structure — login/register behavior is
    # locked in test_identity) ═══
    st, login_html = _get_rc("/auth/login?lang=en")
    st2, reg_html = _get_rc("/auth/register?lang=en")
    check("A1 both standalone pages render the shared card",
          st == 200 and st2 == 200 and 'class="auth-card"' in login_html
          and 'class="auth-card"' in reg_html, (st, st2))
    check("A2 same field specs from the single source (register pattern verbatim on page AND template)",
          login_html.count('pattern="[A-Za-z0-9_\\-]{5,32}"') == 1
          and reg_html.count('pattern="[A-Za-z0-9_\\-]{5,32}"') == 2,
          (login_html.count('pattern="[A-Za-z0-9_\\-]{5,32}"'),
           reg_html.count('pattern="[A-Za-z0-9_\\-]{5,32}"')))
    check("A3 page seg switch links both modes with next preserved",
          'href="/auth/register?next=' in login_html
          and 'href="/auth/login?next=' in reg_html, "")
    check("A4 recovery + terms present on BOTH modes",
          "support_recovery_hint" in login_html
          and "support_recovery_hint" in reg_html
          and 'href="/terms"' in login_html and 'href="/terms"' in reg_html, "")
    check("A5 shared partial carries no DOM ids (page + clone cannot collide)",
          "regUsername" not in reg_html and "registerForm" not in reg_html
          and "pwMismatch" not in reg_html, "")
    check("A6 no-JS: standalone forms are real POSTs to their routes",
          'method="post" action="/auth/login"' in login_html
          and 'method="post" action="/auth/register"' in reg_html, "")
    st, home_html = _get_rc("/?lang=en")
    check("A7 base embeds both server-rendered auth templates site-wide",
          st == 200 and '<template id="authTplLogin">' in home_html
          and '<template id="authTplRegister">' in home_html
          and 'name="password_confirm"' in home_html, st)
    _amjs = open(f"{REPO}/static/pages/auth-modal.js", encoding="utf-8").read()
    check("A8 modal JS holds no hand-written field markup (clones the templates)",
          "<label" not in _amjs and "authTplRegister" in _amjs
          and "bindPwMismatch" in _amjs, "")
    _rjs = open(f"{REPO}/static/pages/register.js", encoding="utf-8").read()
    check("A9 page password-mismatch reuses the shared binder",
          "bindPwMismatch" in _rjs, "")
    # 模板克隆表单也带真实 POST method/action — 无 JS/意外默认提交
    # 永远是 POST 到正确端点, 不会把密码带进 URL 查询串
    check("A10 modal template forms declare the same real POST action",
          'method="post" action="/auth/login"' in home_html
          and 'method="post" action="/auth/register"' in home_html, "")
    # 提交监听挂在稳定容器 (.am-body) 委托到当前表单 — 模式切换换克隆
    # 后提交仍被处理 (旧实现只绑首个克隆, 切换后提交失效)
    check("A11 modal submit is delegated on the stable container",
          'body.addEventListener("submit"' in _amjs, "")
    # 初始/切换后聚焦排除 hidden 字段 (克隆的 next 隐藏域是 DOM 首个 input)
    check("A12 modal first-control focus skips hidden inputs",
          "input:not([type=hidden])" in _amjs, "")
    # 克隆子树按当前语言重写 (站内就地切换语言后浮层不再停留在 SSR 语言)
    check("A13 modal clone re-applies the current language in place",
          "applyLang" in _amjs and "data-i18n-placeholder" in _amjs, "")

    # ═══ RC fixtures ═══
    DAY = 86400
    now_rc = int(_time_rc.time())
    t_today = now_rc
    t_3d = now_rc - 3 * DAY
    t_40d = now_rc - 40 * DAY
    uid_calls = _mkuser_rc(_u_rc + "cl")     # calls page: 21 calls
    uid_cred = _mkuser_rc(_u_rc + "cr")      # credits page: 21 spendable rows
    uid_other = _mkuser_rc(_u_rc + "ot")     # permission isolation
    with db() as con:
        # tool identity for label resolution (one known, others deleted)
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled, files_min,"
            " files_max, outputs_json, created_at, updated_at, accept_json,"
            " format_family) values (%s, NULL, %s, 'local', %s, 'public',"
            " TRUE, 1, 1, '[\"text\"]', %s, %s, '[\".png\"]', 'image')",
            (f"rctool_{_u_rc}", f"rctool_{_u_rc}",
             _json.dumps({"en": "RC Tool"}), _ts_rc, _ts_rc))
        # 21 calls: 18 ok (17 today + 1 at 3d), 2 failed today, 1 insufficient
        # at 40d; ids ascend with time so newest-first = today first
        _call_ids = []
        _specs = ([("failed", t_today, 1), ("failed", t_today, 1),
                   ("insufficient", t_40d, 0)]
                  + [("ok", t_3d, 1)]
                  + [("ok", t_today, 1)] * 17)
        _bal = 0
        _n = 0
        for i, (status, at, charged) in enumerate(_specs):
            row = con.execute(
                "insert into tool_calls (client_ref, user_id, tool_id, status,"
                " cost_snapshot, commission_rate_snapshot, invocation_hash,"
                " started_at, finished_at, result_public_id, result_json)"
                " values (%s, %s, %s, %s, 5, 30, %s, %s, %s, %s, %s)"
                " returning id",
                (f"rc_{_u_rc}_{i}", uid_calls, f"rctool_{_u_rc}", status,
                 f"{i:064x}", at, at if status != "running" else None,
                 f"rcres_{_u_rc}_{i}" if status == "ok" else None,
                 _json.dumps({"expires_at": now_rc + DAY}) if status == "ok" else None)
            ).fetchone()
            _call_ids.append(int(row["id"]))
            if charged:   # ok/failed consume −5 (insufficient never charges)
                _bal -= 5
                _led_rc(con, uid_calls, "consume", -5, _bal, at, _n,
                        ref_type="tool_call", ref_id=str(row["id"]))
                _n += 1
        # refunds +2 on the two most recent ok calls (relation: refund → consume)
        for cid in _call_ids[-2:]:
            _bal += 2
            row = con.execute(
                "select id from ledger where ref_type='tool_call'"
                " and ref_id=%s and user_id=%s order by id limit 1",
                (str(cid), uid_calls)).fetchone()
            _led_rc(con, uid_calls, "refund", 2, _bal, t_today, _n,
                    ref_type="ledger", ref_id=str(row["id"]))
            _n += 1
        # credits fixture: exactly 21 spendable rows for user B
        # (17 consume today, 1 refund today, 1 grant today,
        #  1 refund + 1 topup at 40d)
        _balb = 0
        _n = 0
        for _ in range(17):
            _balb -= 5
            _led_rc(con, uid_cred, "consume", -5, _balb, t_today, _n); _n += 1
        _balb += 2
        _led_rc(con, uid_cred, "refund", 2, _balb, t_today, _n); _n += 1
        _balb += 50
        _led_rc(con, uid_cred, "grant_signup", 50, _balb, t_today, _n); _n += 1
        _balb += 2
        _led_rc(con, uid_cred, "refund", 2, _balb, t_40d, _n); _n += 1
        _balb += 100
        _led_rc(con, uid_cred, "topup", 100, _balb, t_40d, _n); _n += 1
        # other user's rows must never leak (their activity sits OUTSIDE
        # the 7-day window: also the empty-window fixture below)
        _led_rc(con, uid_other, "consume", -5, -5, t_40d, 0)
        con.execute(
            "insert into tool_calls (client_ref, user_id, tool_id, status,"
            " cost_snapshot, commission_rate_snapshot, invocation_hash,"
            " started_at, finished_at) values (%s, %s, %s, 'ok', 5, 30, %s,"
            " %s, %s)",
            (f"rc_{_u_rc}_other", uid_other, f"rctool_{_u_rc}", "0" * 64,
             t_today, t_today))
    ck_cred = _cbs_rc(uid_cred)
    ck_calls = _cbs_rc(uid_calls)

    # ═══ RC1/2: credits 20-per-page with real-total bounds ═══
    st, html = _get_rc("/account/credits?lang=en", ck_cred)
    check("RC1 21 rows → page 1 shows 20 + pager with totals",
          st == 200 and html.count("<tr>") == 21   # 20 body + 1 header
          and "21 records" in html and "1 / 2" in html,
          (st, html.count("<tr>")))
    st, html = _get_rc("/account/credits?page=2&lang=en", ck_cred)
    check("RC2 page 2 shows the single oldest row (not the empty state)",
          st == 200 and html.count("<tr>") == 2
          and "credits_empty" not in html, html.count("<tr>"))
    st, html = _get_rc("/account/credits?page=99&lang=en", ck_cred)
    check("RC2b out-of-range page clamps to the real last page",
          st == 200 and html.count("<tr>") == 2
          and "credits_empty" not in html, html.count("<tr>"))
    st, html = _get_rc("/account/credits?page=abc&lang=en", ck_cred)
    check("RC2c non-integer page falls back to page 1",
          st == 200 and html.count("<tr>") == 21, html.count("<tr>"))

    # ═══ RC3: range filter + stats-window semantics ═══
    st, html = _get_rc("/account/credits?range=7&lang=en", ck_cred)
    check("RC3 range=7 lists only in-window rows (19 of 21)",
          st == 200 and html.count("<tr>") == 20   # 19 body + header
          and "19 records" in html, html.count("<tr>"))
    check("RC3b stats window equals the selected range (labeled)",
          "Stats: last 7 days (UTC)" in html, "")
    st, html = _get_rc("/account/credits?range=30&lang=en", ck_cred)
    check("RC3c range=30 same window as 7 here, still labeled 30",
          "Stats: last 30 days (UTC)" in html and "19 records" in html, "")
    st, html = _get_rc("/account/credits?lang=en", ck_cred)
    check("RC3d range=all lists all 21 but stats stay a labeled 30-day window",
          "21 records" in html and "Stats: last 30 days (UTC)" in html, "")
    st, html = _get_rc("/account/credits?range=bogus&lang=en", ck_cred)
    check("RC3e invalid range behaves as all",
          "21 records" in html, "")

    # ═══ RC4: summary math from ledger kinds only ═══
    # window (30d): consume 17×5=85, refund +2 (today only), net 83;
    # grant/topup and the 40d refund excluded; other user excluded
    st, html = _get_rc("/account/credits?range=30&lang=en", ck_cred)
    check("RC4 overview: Spent 85 / Refunded 2 / Net 83 (grants & topups excluded)",
          ">Spent</span>" in html and "<strong class=\"rec-chip-v mono \">85</strong>" in html
          and "rec-chip-v mono pos\">2<" in html
          and "rec-chip-v mono neg\">83<" in html,
          [s for s in ("85", ">2<", ">83<") if s not in html])
    st_o, html_o = _get_rc("/account/credits?range=all&lang=en", _cbs_rc(uid_other))
    check("RC4b per-user isolation: the other user sees only their own row",
          st_o == 200 and "1 records" in html_o
          and "85" not in html_o.split("rec-toolbar")[-1], "")

    # ═══ RC5: trend strip ═══
    check("RC5 trend present with peak value + UTC window note",
          "rec-trend-bars" in html and "peak 83 ("
          in html, "")
    st, html = _get_rc("/account/credits?range=7&lang=en", _cbs_rc(uid_other))
    check("RC5b empty window shows the trend empty state",
          "rec-trend-empty" in html, "")

    # ═══ RC6: calls page — statuses, net projection, paging, links ═══
    st, html = _get_rc("/account/calls?lang=en", ck_calls)
    check("RC6 calls 21 rows → 20 on page 1 with pager",
          st == 200 and html.count('id="call-') == 20 and "1 / 2" in html
          and "21 records" in html, html.count('id="call-'))
    check("RC6b per-status chips in the 30d window: Calls 20 / Succeeded 18 / Failed 2",
          ">Calls</span><strong class=\"rec-chip-v mono \">20<" in html
          and ">Succeeded</span><strong class=\"rec-chip-v mono \">18<" in html
          and ">Failed</span><strong class=\"rec-chip-v mono \">2<" in html, "")
    # net projection over the 30d window: 20 charged calls ×5 − 2×2 refunds = 96
    # (the insufficient call at 40d and its window exclusion ride the same SQL)
    _expect_net_calls = 20 * 5 - 2 * 2
    check("RC6c calls overview net = consume − refunds via the relation projection",
          f"rec-chip-v mono neg\">{_expect_net_calls}<" in html,
          _expect_net_calls)
    st, html = _get_rc("/account/calls?page=2&lang=en", ck_calls)
    check("RC6d page 2 shows exactly the oldest call (first inserted, failed)",
          st == 200 and html.count('id="call-') == 1
          and "calls-st-failed" in html, html.count('id="call-'))
    st, html = _get_rc("/account/calls?range=7&lang=en", ck_calls)
    check("RC6e range=7 drops only the 40d call (20 records, no insufficient)",
          "calls_empty" not in html and "calls-st-insufficient" not in html
          and "20 records" in html, "")
    st, html = _get_rc("/account/credits?lang=en", ck_calls)
    check("RC6f credits → calls row link rides the call relation",
          'href="/account/calls?page=' in html and "#call-" in html, "")

    # ═══ RC7: page bound function — 10001st page reachable, no wild offsets ═══
    from app.routes.pages import _effective_page as _ep
    check("RC7 effective page: 10001st page reachable past the old 10k ceiling",
          _ep("10001", 200_005, 20) == 10001
          and _ep("10002", 200_005, 20) == 10001
          and _ep("x", 200_005, 20) == 1 and _ep("-1", 200_005, 20) == 1
          and _ep("999999999", 0, 20) == 1,
          [_ep("10001", 200_005, 20), _ep("10002", 200_005, 20)])

    # ═══ RC8: window = 包含今天的最近 N 个 UTC 自然日 (固定请求时钟) ═══
    # 可控时钟: 页面路由在请求时 from utils import now — 打补丁后经
    # TestClient 的请求全部落在同一截点, 覆盖窗口首日/边界前后/UTC 午夜。
    import utils as _utils_mod
    from contextlib import contextmanager as _ctxmanager

    @_ctxmanager
    def _fixed_clock(ts):
        _orig_now = _utils_mod.now
        _utils_mod.now = lambda: ts
        try:
            yield
        finally:
            _utils_mod.now = _orig_now

    _FIXED_NOW = (int(_time_rc.time()) // 86400) * 86400 + 12 * 3600  # UTC 今日正午
    _today_rc = _FIXED_NOW // 86400
    _S7 = (_today_rc - 6) * 86400        # range=7 窗口首日零点 (UTC)
    uid_win = _mkuser_rc(_u_rc + "wn")       # 窗口边界 fixture
    uid_refund = _mkuser_rc(_u_rc + "rf")    # 仅退款窗口 (净额为负)
    uid_free = _mkuser_rc(_u_rc + "fr")      # 免费调用 (净扣费 0)
    uid_cut = _mkuser_rc(_u_rc + "ct")       # 统一查询截止点 fixture
    with db() as con:
        # 窗口首日: 消费 10 + 退款 10 → 有记录净额 0; 首日前 1 秒的消费;
        # UTC 午夜(今日零点)的消费 3; 窗口内 grant (列表可见, 统计排除)
        _balw = 0
        for _i, (_k, _d, _at) in enumerate((
                ("consume", -10, _S7), ("refund", 10, _S7 + 3600),
                ("consume", -5, _S7 - 1),
                ("consume", -3, _today_rc * 86400),
                ("grant_signup", 50, _S7 + 10))):
            _balw += _d
            _led_rc(con, uid_win, _k, _d, _balw, _at, 900 + _i)
        # 仅退款: 窗口内唯一记录 = 退款 +10 → 净额 −10, 有活动
        _led_rc(con, uid_refund, "refund", 10, 10, _FIXED_NOW - 3600, 910)
        # 免费调用: ok 调用, cost 0, 无消费行 → 净扣费 0, 有活动
        con.execute(
            "insert into tool_calls (client_ref, user_id, tool_id, status,"
            " cost_snapshot, commission_rate_snapshot, invocation_hash,"
            " started_at, finished_at) values (%s, %s, %s, 'ok', 0, 30, %s,"
            " %s, %s)",
            (f"rc_{_u_rc}_free", uid_free, f"rctool_{_u_rc}", "1" * 64,
             _FIXED_NOW - 60, _FIXED_NOW - 59))
        # 截止点样本 (uid_cut): 起点前 1 秒 / 起点 / 截止点(含) / 截止点后
        # 1 秒 / 下一 UTC 日 — 统一 since <= ts <= now_ts 的两侧边界
        _balc = 0
        for _i, (_k, _d, _at) in enumerate((
                ("consume", -3, _S7 - 1),
                ("consume", -7, _S7),
                ("refund", 4, _FIXED_NOW - 60),
                ("consume", -9, _FIXED_NOW),
                ("consume", -11, _FIXED_NOW + 1),
                ("refund", 6, _FIXED_NOW + 2),
                ("consume", -13, (_today_rc + 1) * 86400))):
            _balc += _d
            _led_rc(con, uid_cut, _k, _d, _balc, _at, 950 + _i)
        # 调用侧截止点样本: 恰在截止点(含) 与 下一 UTC 日(排)
        for _i, (_cr, _at) in enumerate((
                (f"rc_{_u_rc}_cut", _FIXED_NOW),
                (f"rc_{_u_rc}_nx", (_today_rc + 1) * 86400))):
            con.execute(
                "insert into tool_calls (client_ref, user_id, tool_id,"
                " status, cost_snapshot, commission_rate_snapshot,"
                " invocation_hash, started_at, finished_at) values"
                " (%s, %s, %s, 'ok', 0, 30, %s, %s, %s)",
                (_cr, uid_cut, f"rctool_{_u_rc}", f"{_i + 2}" * 64,
                 _at, _at + 1))
    ck_win = _cbs_rc(uid_win)
    ck_refund = _cbs_rc(uid_refund)
    ck_free = _cbs_rc(uid_free)
    ck_cut = _cbs_rc(uid_cut)

    with _fixed_clock(_FIXED_NOW):
        # 列表边界: 首日零点行在窗内, 首日前 1 秒行在窗外 (滚动 7×86400
        # 窗自正午起算, 会把首日凌晨的两行错杀 — 自然日窗必须全含首日)
        st, html = _get_rc("/account/credits?range=7&lang=en", ck_win)
        check("RC8 range=7 lists the whole first calendar day + midnight rows",
              st == 200 and "4 records" in html, (st, "4 records" in html))
        st, html = _get_rc("/account/credits?lang=en", ck_win)
        check("RC8b range=all still lists every row (5) regardless of the window",
              "5 records" in html, "")
        # 汇总与列表/图表同一自然日边界: Spent 13 / Refunded 10 / Net 3
        st, html = _get_rc("/account/credits?range=7&lang=en", ck_win)
        check("RC8c overview uses the same calendar-day window (13/10/3)",
              'rec-chip-v mono ">13<' in html and 'rec-chip-v mono pos">10<' in html
              and 'rec-chip-v mono neg">3<' in html,
              [s for s in ('">13<', 'pos">10<', 'neg">3<') if s not in html])
        # 趋势: 零净额日 (消费10退款10) 与正净额日并存 → 有图表, 有零刻度
        # 与向上条, 不出现空态; 日桶合计 == 汇总净额
        _days = _re.findall(r'rec-day-v mono">(-?\d+)<', html)
        check("RC8d zero-net day with real records renders a chart, not 'no activity'",
              "rec-trend-empty" not in html and "rec-trend-bars" in html
              and "rec-bar zero" in html and "rec-bar up" in html
              and "rec-bar down" not in html,
              html.count("rec-bar"))
        check("RC8e day buckets sum to the overview net (detail list, 7 days)",
              len(_days) == 7 and sum(int(v) for v in _days) == 3, _days)
        check("RC8f bar heights: today's +3 at full half-scale, non-negative",
              'class="rec-bar up" style="height: 50.0%"' in html
              and not _re.search(r'height: -', html), "")

        # 仅退款窗口: 净额 −10 → 有活动, 负值表达为向下条, 无空态
        st, html = _get_rc("/account/credits?lang=en", ck_refund)
        check("RC8g refund-only window is activity, not 'no activity'",
              st == 200 and "rec-trend-empty" not in html
              and "rec-bar down" in html and "rec-bar up" not in html
              and 'rec-chip-v mono neg">-10<' in html,
              [s for s in ("rec-bar down", "neg\">-10<") if s not in html])
        check("RC8h negative day renders a downward bar at half-scale",
              'class="rec-bar down" style="height: 50.0%"' in html, "")

        # 免费调用: 记录存在, 净扣费 0 → 调用页有图表 (零刻度), 无空态
        st, html = _get_rc("/account/calls?lang=en", ck_free)
        check("RC8i free call (net 0) counts as activity on the calls page",
              st == 200 and "rec-trend-empty" not in html
              and "rec-bar zero" in html and "rec-bar up" not in html
              and "rec-bar down" not in html
              and '>Calls</span><strong class="rec-chip-v mono ">1<' in html,
              "")

        # ═══ RC9: 统一查询截止点 — 有限范围与全部统计查询都用
        # since <= 记录时间 <= 本次请求截点; 晚于截点的数据全部排除 ═══
        # 列表/计数: 起点前 1 秒排除, 起点与截止点(含)计入, 截止点后 1 秒
        # 与下一 UTC 日排除
        st, html = _get_rc("/account/credits?range=7&lang=en", ck_cut)
        check("RC9 finite-range list/count bound by [window start, cutoff]",
              st == 200 and "3 records" in html and html.count("<tr>") == 4,
              (st, html.count("<tr>")))
        # 汇总与日桶同界: Spent 16 / Refunded 4 / Net 12; 日桶合计 = 净额
        check("RC9b overview/daily share the same cutoff (16/4/12)",
              'rec-chip-v mono ">16<' in html
              and 'rec-chip-v mono pos">4<' in html
              and 'rec-chip-v mono neg">12<' in html,
              [s for s in ('">16<', 'pos">4<', 'neg">12<') if s not in html])
        _days = _re.findall(r'rec-day-v mono">(-?\d+)<', html)
        check("RC9c day buckets sum to the same net, cutoff row counted",
              len(_days) == 7 and sum(int(v) for v in _days) == 12, _days)
        # 截止点恰在 now_ts 的行被计入 (<= 而非 <): 日桶今日 = 9 − 4 = 5
        check("RC9d the row exactly AT the cutoff is included (today net 5)",
              _days[-1] == "5", _days[-1] if _days else None)
        # 调用侧: 截止点调用计入, 下一 UTC 日调用排除 (基线无截止时 2 条)
        st, html = _get_rc("/account/calls?range=7&lang=en", ck_cut)
        check("RC9e calls count/paged/status/daily bound by the same cutoff",
              st == 200 and "1 records" in html
              and '>Calls</span><strong class="rec-chip-v mono ">1<' in html
              and html.count('id="call-') == 1,
              (st, html.count('id="call-')))
        # range=all 列表保留全历史 (含下一 UTC 日的未来行), 概览/趋势仍为
        # 标注的最近 30 个 UTC 日期且带同一截点: 30 天窗比 7 天窗宽, 把
        # 7 天窗前 1 秒的行 (30 天内、截点前) 重新计入 → Spent 19 =
        # 3+7+9; 截点后的行 (11/13) 仍被排除 (否则 43)
        st, html = _get_rc("/account/credits?lang=en", ck_cut)
        check("RC9f range=all keeps the full-history list (7 records)",
              st == 200 and "7 records" in html, "")
        check("RC9g range=all stats stay on the labeled 30d window with the cutoff",
              "Stats: last 30 days (UTC)" in html
              and 'rec-chip-v mono ">19<' in html,
              "")
finally:
    with db() as con:
        con.execute("delete from platform_tools where tool_key=%s",
                    (f"rctool_{_u_rc}",))
        con.execute("delete from pricing where tool_id=%s", (f"rctool_{_u_rc}",))
        purge_users(con, _RC_USERS)


finish()
