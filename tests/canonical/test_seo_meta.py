#!/usr/bin/env python3
"""SEO / share-card / PWA canonical.

  M*  single point: every page renders og:title/description/url/image,
      twitter:* and canonical from base.html — og/twitter reuse the SAME
      values (no per-page hand-written og tags left anywhere); twitter:card
      is summary_large_image site-wide.
  H*  hreflang: indexable routes (home /tools /tool/* /tool/*/mcp /agents
      /pricing /developers /terms /privacy /u/*) carry 10 languages +
      x-default (11 links, ?lang=xx on the canonical path); noindex routes
      (auth/account//r) carry none. Query strings (tracking params, ?lang)
      never enter a canonical/og:url.
  C*  share cards: /r keeps noindex but has og:title = tool + result count
      and og:description = expiry + remaining downloads (same mechanism as
      the /f page: og_* context overrides, base renders the tags).
  L*  JSON-LD: home carries WebSite (+ SearchAction → /tools?q=) and
      Organization, parseable by json.loads; /developers adds none.
  S*  sitemap.xml: every indexable static surface + every public tool page
      + its MCP page + author pages behind public supply, with real
      lastmod (tool declaration updated_at); private/unlisted tools and
      account/auth//r never appear.
  R*  robots.txt excludes account/samelabs/auth/r/api/mcp (+ legacy /dh/)
      and points at the sitemap.
  P*  PWA manifest: required fields, description injected from the i18n
      single source (en seo_desc), start_url /?source=pwa, no orientation
      lock, split any/maskable icons whose files exist with the declared
      dimensions, maskable artwork inside the 80% safe circle.

Fail-closed env guard; per-run unique names; fixtures cleaned at end.
"""
import base64
import http.client as _hc
import json as _json
import os as _os
import re as _re
import sys as _sys
import time as _time
import urllib.parse as _up
import uuid as _uuid

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

_REPO = _os.path.abspath(_os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
from config import BASE_URL  # noqa: E402
from i18n import SERVER_I18N, LOCALE_TAGS  # noqa: E402
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


_HOST = ("127.0.0.1", int(_TEST_PORT))
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
        "tool_id": tid, "label": "SEO fixture", "url": "https://93.184.216.34/x",
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
    indexable = ["/", "/tools", "/tool/compress", "/tool/compress/mcp",
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
    pub_id = "seop" + _uuid.uuid4().hex[:5]
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
                " created_at, updated_at, accept_json)"
                " VALUES (%s, %s, %s, 'public', TRUE, '{\"en\": \"SEO twin\"}',"
                " 0, 1, '[\"text\"]', %s, %s, '[\".png\"]')",
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
    check("S sitemap: public tool pages + mcp twins with lastmod",
          f"{BASE_URL}/tool/compress" in locs and f"{BASE_URL}/tool/compress/mcp" in locs
          and bool(_re.search(r"<loc>%s/tool/compress</loc><lastmod>\d{4}-\d{2}-\d{2}</lastmod>"
                              % _re.escape(BASE_URL), xml)))
    check("S sitemap: community public tool + author page",
          f"{BASE_URL}/tool/{pub_id}" in locs and f"{BASE_URL}/u/{_u}" in locs)
    check("S sitemap: unlisted fixture tool excluded",
          f"/tool/{tid}</loc>" not in xml and f"/tool/{tid}/mcp" not in xml
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
        path = _os.path.join(_REPO, "brand", "web",
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
        path = _os.path.join(_REPO, "brand", "web",
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

    print(f"\n{COUNT - len(FAIL)}/{COUNT} checks passed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
        _sys.exit(1)
    print("ALL GREEN")


main()
