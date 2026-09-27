#!/usr/bin/env python3
"""Governance & discovery closure canonical.

  A*  publish IS listing: a new community tool lands visibility=public
      immediately (no self-test gate); the owner can still choose
      unlisted/private at submission or later.
  B*  operator delist vs developer enabled are independent switches:
      delist hides the tool from home/market/search/author page/MCP
      list+search and makes resolve() refuse; the developer's enable
      PATCH is refused while delisted (typed) and the owner view shows
      the takedown; developer enable/disable writes an audit row.
  C*  suspended provider leaves the market page too (the one surface
      the earlier suspended-owner sweep missed).
  D*  search = one ordering with the lists (featured → sort_order → id)
      and matches tool_key + label + desc (all languages).
  E*  author visibility: community cards carry a /u/{handle} link on
      home and market; MCP summaries and the llms.txt catalog note
      "by @handle"; official rows keep the official badge and no
      fabricated author.
"""
import os as _os
import sys as _sys

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

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0
def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:130]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import json
import http.client as _hc
import time
ts = int(time.time())
_u = "c2" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(_TEST_PORT))


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body).encode()
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, json.loads(data) if data else None
    except Exception:
        return r.status, hdrs, data.decode(errors="replace")


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    conn = _hc.HTTPConnection(*HOST, timeout=30)
    b, ct = _form({"login": name, "password": "Test12345"})
    conn.request("POST", "/auth/login", b, {"Content-Type": ct})
    r = conn.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    conn.close()
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck


def _publish(ck, tool_id, label, extra=None):
    body = {"tool_id": tool_id, "label": label,
            "url": "https://93.184.216.34/x", "timeout_s": 30,
            "files_min": 1, "files_max": 1,
            "format_families": "image", "accept": [".png"],
            "outputs": ["file"], "cost": 0}
    body.update(extra or {})
    return _req("POST", "/api/usertools", body,
                {"Content-Type": "application/json", "Cookie": ck})


dev_ck_user = _u + "dev"
uid, CK = _register(dev_ck_user)

# ═══ A. publish IS listing ═══
tk = f"{dev_ck_user}/aaa"
st, _h, d = _publish(CK, "aaa", "Alpha Tool")
check("A1 create accepted", st == 200 and d.get("ok"), f"{st} {d}")
with db() as con:
    row = con.execute("select visibility, enabled, delisted from"
                      " platform_tools where tool_key=%s", (tk,)).fetchone()
check("A2 lands public + enabled + not delisted",
      dict(row) == {"visibility": "public", "enabled": True,
                    "delisted": False}, str(row and dict(row)))
st, _h, home = _req("GET", "/")
st, _h, market = _req("GET", "/tools")
check("A3 visible on home + market immediately",
      tk in (home or "") and tk in (market or ""))
# self-test gate gone: switching unlisted→public needs no prior call
st, _h, d = _req("PATCH", "/api/usertools/aaa", {"visibility": "unlisted"},
                 {"Content-Type": "application/json", "Cookie": CK})
st, _h, d = _req("PATCH", "/api/usertools/aaa", {"visibility": "public"},
                 {"Content-Type": "application/json", "Cookie": CK})
check("A4 visibility round-trips without self-test", st == 200 and d.get("ok"),
      f"{st} {d}")
# explicit private still honored
st, _h, d = _publish(CK, "bbb", "Beta", {"visibility": "private"})
with db() as con:
    row = con.execute("select visibility from platform_tools where"
                      " tool_key=%s", (f"{dev_ck_user}/bbb",)).fetchone()
check("A5 explicit private honored", row["visibility"] == "private",
      str(row and dict(row)))

# ═══ B. delist vs enabled ═══
op, op_ck = _register(_u + "op")
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (op,))
from backoffice.tools import set_tool_delisted
set_tool_delisted(op, tk, True)
st, _h, home = _req("GET", "/")
st, _h, market = _req("GET", "/tools")
st, _h, author = _req("GET", f"/u/{dev_ck_user}")
check("B1 delisted: gone from home + market (author page row gone)",
      tk not in (home or "") and tk not in (market or "")
      and "aaa" not in (author or ""),
      f"home={tk in (home or '')} market={tk in (market or '')}")
st, _h, d = _req("GET", f"/api/tools/search?q=alpha")
check("B2 delisted: search misses", st == 200 and
      not any(t["id"] == tk for t in (d.get("tools") or [])), str(d)[:100])
from governance.catalog import resolve
t, err = resolve(tk, None)
t2, err2 = resolve(tk, uid)
check("B3 resolve: public refuses, owner still resolves",
      t is None and err == "not_found" and t2 is not None,
      f"{err} {err2}")
st, _h, d = _req("PATCH", "/api/usertools/aaa", {"enabled": False},
                 {"Content-Type": "application/json", "Cookie": CK})
check("B4 dev enable PATCH refused while delisted (typed)",
      st == 403 and d.get("error") == "err_tool_delisted", f"{st} {d}")
with db() as con:
    au = con.execute(
        "select action, target_id from operator_audit where"
        " action='tool.delisted.update' and target_id=%s"
        " order by id desc limit 1", (tk,)).fetchone()
check("B5 delist audited", au is not None, str(au))
set_tool_delisted(op, tk, False)
st, _h, home = _req("GET", "/")
check("B6 restore revives listing", tk in (home or ""))
# dev enable writes audit
st, _h, d = _req("PATCH", "/api/usertools/aaa", {"enabled": False},
                 {"Content-Type": "application/json", "Cookie": CK})
with db() as con:
    au = con.execute(
        "select after_json from operator_audit where"
        " action='tool.enabled.update' and target_id=%s"
        " order by id desc limit 1", (tk,)).fetchone()
check("B7 developer enable/disable audited (actor=owner)",
      st == 200 and au is not None
      and au["after_json"]["enabled"] is False,
      f"{st} {au}")

# ═══ C. suspended provider leaves the market ═══
tk3 = f"{dev_ck_user}/ccc"
_publish(CK, "ccc", "Gamma ZZZ")
with db() as con:
    con.execute("update users set status='suspended' where id=%s", (uid,))
st, _h, home = _req("GET", "/")
st, _h, market = _req("GET", "/tools")
check("C1 suspended provider: home AND market both hide",
      tk3 not in (home or "") and tk3 not in (market or ""),
      f"home={tk3 in (home or '')} market={tk3 in (market or '')}")
with db() as con:
    con.execute("update users set status='active' where id=%s", (uid,))

# ═══ D. search: one ordering + desc match ═══
# make the tool featured and give it a desc, then search by desc word
with db() as con:
    con.execute("update platform_tools set featured=true, sort_order=1,"
                " desc_json='{\"en\":\"unusualxy multiplier\"}'"
                " where tool_key=%s", (tk3,))
st, _h, d = _req("GET", "/api/tools/search?q=unusualxy")
hits = [t["id"] for t in (d.get("tools") or [])]
check("D1 search matches desc (all languages)", tk3 in hits, str(hits)[:100])
st, _h, d2 = _req("GET", "/api/tools/search?q=gamma")
hits2 = [t["id"] for t in (d2.get("tools") or [])]
check("D2 label still matches", tk3 in hits2, str(hits2)[:100])
# ordering parity: two label matches, the featured one ranks first
st, _h, d = _req("PATCH", "/api/usertools/aaa", {"enabled": True},
                 {"Content-Type": "application/json", "Cookie": CK})
_publish(CK, "ddd", "Delta Tool")
with db() as con:
    con.execute("update platform_tools set featured=true, sort_order=99"
                " where tool_key=%s", (tk,))
st, _h, d3 = _req("GET", "/api/tools/search?q=tool")
ids3 = [t["id"] for t in (d3.get("tools") or [])]
check("D3 featured tool ranks first (list ordering parity)",
      ids3 and ids3[0] == tk and f"{dev_ck_user}/ddd" in ids3,
      str(ids3[:5]))
with db() as con:
    con.execute("update platform_tools set featured=false, desc_json=null"
                " where tool_key=%s", (tk3,))

# ═══ E. author visibility ═══
st, _h, home = _req("GET", "/")
check("E1 community card links the author page",
      f'href="/u/{dev_ck_user}"' in (home or ""))
st, _h, market = _req("GET", "/tools")
check("E2 market card links the author page",
      f'href="/u/{dev_ck_user}"' in (market or ""))
# MCP summaries
from identity.apikeys import create_key
from economy.billing import grant as _grant
_grant(uid, 1000, "manual_topup", idem_key=f"c2a:{_u}", note="c2a")
TOK = create_key(uid, "c2a")["key"]
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream",
     "Authorization": f"Bearer {TOK}"}
st, _h, d = _req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
                                   "method": "tools/list"}, H)
summaries = (d.get("result") or {}).get("tools") or []
mine = [t for t in summaries if "ccc" in t["name"] or "aaa" in t["name"]]
check("E3 MCP summary notes the author",
      mine and all(f"by @{dev_ck_user}" in t["description"] for t in mine),
      str([t["description"][:60] for t in mine])[:120])
st, _h, d = _req("POST", "/mcp",
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": "gamma"}}}, H)
tools = ((d.get("result") or {}).get("structuredContent") or {}).get("tools") or []
check("E4 catalog_search summary notes the author",
      tools and all(f"by @{dev_ck_user}" in t["description"] for t in tools),
      str([t["description"][:60] for t in tools])[:120])
st, _h, llms = _req("GET", "/llms.txt")
check("E5 llms.txt catalog notes the author",
      f"by @{dev_ck_user}" in (llms or ""))
# official tools: no fabricated author
st, _h, d = _req("POST", "/mcp",
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "catalog_search",
                             "arguments": {"q": "compress"}}}, H)
tools = ((d.get("result") or {}).get("structuredContent") or {}).get("tools") or []
check("E6 official tools carry no author tag",
      tools and all("by @" not in t["description"] for t in tools),
      str([t["description"][:60] for t in tools])[:120])

# ═══ F. i18n + t fallback (source-level facts) ═══
_i18n_src = open(_os.path.join(_REPO, "core", "i18n.py"),
                 encoding="utf-8").read()
check("F1 err_insufficient_credits present in all locales",
      _i18n_src.count('"err_insufficient_credits":') >= 10,
      str(_i18n_src.count('"err_insufficient_credits":')))
_engine = open(_os.path.join(_REPO, "static", "pages", "base-engine.js"),
               encoding="utf-8").read()
check("F2 window.t falls back to en before the key name",
      "E[k]||k" in _engine.replace(" ", "") or "E[k] || k" in _engine,
      "")
_cfg = open(_os.path.join(_REPO, "core", "config.py"),
            encoding="utf-8").read()
check("F3 no wrong-port defaults; required envs fail loud",
      "53843" not in _cfg and "53842" not in _cfg
      and "_required_env" in _cfg)

with db() as con:
    purge_users(con, [uid, op])

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
_sys.exit(1 if FAIL else 0)

