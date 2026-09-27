#!/usr/bin/env python3
"""C1 closure canonical — MCP envelope, delivery errors, storage meta
tools, hosted-path removal, daily-credit restore, UI consistency.

  A*  every structuredContent tools/call response (execution tool,
      create_upload, catalog_search, list_uploads) carries a content
      text block whose JSON parses to exactly the structuredContent.
  B*  structurally invalid delivery → isError (never -32602) listing
      the legal values FROM the delivery authority constants.
  C*  list_uploads/delete_upload meta tools ride the web single points:
      stage → PUT → list shows the file with the full projection →
      delete_upload removes it → /f/{id} 404s (Gokapi bytes + mapping
      together).
  D*  hosted execution path is gone: /tool-endpoints/* 404, modules
      absent, no capability mint/verify anywhere.
  E*  daily check-in credit restored (owner decision after the C1
      removal): route present, module present, kind spendable, widget
      i18n keys present, amount sourced from settings (0/unset = off).
  F*  UI consistency: tool page crumbs + unified file-count source +
      localized outputs + one MCP entry; /r and /f share the action-bar
      component inside the card; agents page has no TOKEN placeholders
      and shows the endpoint once.
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:120]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import base64
import http.client as _hc
import json
import time
import urllib.request as _ur
ts = int(time.time())
_u = "c1" + _os.urandom(4).hex()

from database import db

HOST = ("127.0.0.1", int(_TEST_PORT))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req(method, path, body=None, headers=None, raw=False):
    conn = _hc.HTTPConnection(*HOST, timeout=60)
    payload = None
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        if isinstance(payload, str):
            payload = payload.encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    conn.close()
    if raw:
        return r.status, data
    try:
        return r.status, json.loads(data) if data else None
    except Exception:
        return r.status, data.decode(errors="replace")


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    st, _b = _req("POST", "/auth/register", b, {"Content-Type": ct})
    if st not in (200, 302, 303):
        raise SystemExit(f"register failed: {st}")
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


def _mcp(body, token):
    return _req("POST", "/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})


def _call(name, arguments, token, mid=1):
    return _mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token)


def _sc_text_blocks(result):
    """text blocks of a tool result content."""
    return [b.get("text") for b in (result.get("content") or [])
            if b.get("type") == "text"]


from identity.apikeys import create_key
from economy.billing import grant as _grant

uid, CK = _register(_u + "a")
_grant(uid, 100000, "manual_topup", idem_key=f"mp:{_u}", note="c1")
TOKEN = create_key(uid, "c1")["key"]

# ══════════ A. text block on every structuredContent response ══════════
st, d = _call("compress", {"files": [{"filename": "a.png", "b64":
                                      base64.b64encode(PNG).decode()}],
                           "client_ref": f"a1-{_u}"}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
texts = _sc_text_blocks(res)
parsed = None
for t in texts:
    try:
        cand = json.loads(t)
    except Exception:
        continue
    if isinstance(cand, dict) and "items" in cand:
        parsed = cand
        break
check("A1 execution tool: content carries the structuredContent JSON text block",
      res.get("isError") is False and parsed == sc and bool(sc.get("items")),
      f"{str(d)[:110]}")

st, d = _call("create_upload", {"filename": "big.png", "size": len(PNG),
                                "client_ref": f"c1up-{_u}"}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
texts = _sc_text_blocks(res)
check("A2 create_upload: text block == structuredContent",
      res.get("isError") is False and texts and
      json.loads(texts[0]) == sc and "upload_url" in sc, str(d)[:110])

st, d = _call("catalog_search", {"q": "compress"}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
texts = _sc_text_blocks(res)
check("A3 catalog_search: text block == structuredContent",
      res.get("isError") is False and texts and
      json.loads(texts[0]) == sc and sc.get("tools"), str(d)[:110])

# one builder: all four envelope sites share the _meta_result/_mcp_success
# construction (source-level single point)
_mcp_src = open(_os.path.join(_REPO, "app", "routes", "mcp.py"),
                encoding="utf-8").read()
check("A4 single envelope builder in source",
      "def _meta_result(" in _mcp_src
      and _mcp_src.count('"structuredContent"') <= 3,
      str(_mcp_src.count('"structuredContent"')))

# ══════════ B. invalid delivery → isError + legal values ══════════
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS
for label, dv in (("bad expiry", {"expiry_days": 5}),
                  ("bad downloads", {"allowed_downloads": 7}),
                  ("non-dict", "x"),
                  ("unknown key", {"expiry_days": 1, "extra": 2})):
    args = {"files": [{"filename": "a.png", "b64":
                       base64.b64encode(PNG).decode()}],
            "delivery": dv, "client_ref": f"b-{label[:3]}-{_u}"}
    st, d = _call("compress", args, TOKEN)
    res = (d or {}).get("result") or {}
    err = None
    try:
        err = json.loads(_sc_text_blocks(res)[0])
    except Exception:
        pass
    ok = (res.get("isError") is True
          and (d.get("error") is None)
          and err and err.get("error") == "invalid_delivery"
          and err.get("legal_values", {}).get("expiry_days") == list(EXPIRY_DAYS)
          and err.get("legal_values", {}).get("allowed_downloads") == list(DOWNLOAD_LIMITS))
    check(f"B1 {label} → isError with authority legal values", ok,
          f"{str(d)[:110]}")

# ══════════ C. list_uploads / delete_upload meta tools ══════════
# a real tool call produces a durable uploads row owned by the caller
# (staged-only files are NOT shares — they enter the list when a call
# consumes them, exactly like the web surface)
st, d = _call("share", {"files": [{"filename": "store.png", "b64":
                                   base64.b64encode(PNG).decode()}],
                        "client_ref": f"c1share-{_u}"}, TOKEN)
_sc = (((d or {}).get("result") or {}).get("structuredContent") or {})
_items = _sc.get("items") or []
_res_url = _sc.get("result_url") or ""
_f = next((i for i in _items if i.get("kind") == "file"), None)
check("C1 fixture: share call produced a file item", _f is not None,
      str(d)[:110])
pid = ((_f or {}).get("page_url") or "/f/").rsplit("/f/", 1)[-1]

st, d = _call("list_uploads", {}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
entry = next((e for e in sc.get("uploads", [])
              if e.get("public_id") == pid), None)
check("C2 list_uploads shows the staged file (full projection)",
      res.get("isError") is False and entry is not None
      and entry.get("filename") == "store.png"
      and entry.get("size") == len(PNG)
      and entry.get("content_type") == "image/png"
      and isinstance(entry.get("expires_at"), int)
      and entry.get("page_url", "").endswith(f"/f/{pid}")
      and "download=1" in entry.get("download_url", ""),
      str(entry)[:120])

st, d = _call("delete_upload", {"public_id": pid}, TOKEN)
res = (d or {}).get("result") or {}
sc = res.get("structuredContent") or {}
st_f, _b = _req("GET", f"/f/{pid}", raw=False)
check("C3 delete_upload removes it → /f 404",
      res.get("isError") is False and sc.get("deleted") is True
      and st_f == 404, f"{str(d)[:100]} f={st_f}")
# gokapi physical object gone too (web single point deleted both)
_gk = _os.environ["FLASHSHARE_GOKAPI_URL"].rstrip("/")
_rq = _ur.Request(_gk + "/api/files/list",
                  headers={"apikey": _os.environ["FLASHSHARE_GOKAPI_KEY"]})
with _ur.urlopen(_rq, timeout=5) as r:
    _gids = {f["Id"] for f in json.loads(r.read())}
with db() as con:
    _row = con.execute("select gokapi_id from uploads where public_id=%s",
                       (pid,)).fetchone()
check("C4 mapping row gone", _row is None, str(_row))
# result page keeps rendering the deleted item (link 404s, counters gone)
_res_id = _res_url.rsplit("/r/", 1)[-1]
st_r, _rb = _req("GET", f"/r/{_res_id}")
_okr = st_r == 200
check("C4b result page still renders after item delete (link 404s)",
      _okr, str(st_r))

# someone else's file → one shape, no existence oracle
uid2, _ck2 = _register(_u + "b")
TOKEN2 = create_key(uid2, "c1b")["key"]
st, d = _call("delete_upload", {"public_id": pid}, TOKEN2)
res = (d or {}).get("result") or {}
_err_json = json.loads(((res.get("content") or [{}])[0].get("text") or "{}"))
check("C5 foreign/unknown delete → isError not_found (no oracle)",
      res.get("isError") is True
      and _err_json.get("error") == "not_found"
      and isinstance(_err_json.get("message"), str),
      str(d)[:100])

# specs present in tools/list alongside create_upload
st, d = _mcp({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, TOKEN)
names = set()
for t in ((d or {}).get("result") or {}).get("tools", []):
    names.add(t.get("name"))
check("C6 meta tools listed", {"create_upload", "catalog_search",
                               "list_uploads", "delete_upload"} <= names,
      str(sorted(names))[:120])

# llms.txt documents the new sections + delivery legal values
_llms = _req("GET", "/llms.txt", raw=True)[1].decode()
check("C7 llms.txt: storage meta tools + delivery legal values",
      "list_uploads" in _llms and "delete_upload" in _llms
      and f"{list(EXPIRY_DAYS)}" in _llms
      and f"{list(DOWNLOAD_LIMITS)}" in _llms, "")

# ══════════ D. hosted execution path deleted ══════════
st, _b = _req("POST", "/tool-endpoints/rounded-corners", {"x": 1},
              {"Content-Type": "application/json"})
check("D1 /tool-endpoints/* → 404", st == 404, str(st))
check("D2 modules gone",
      not _os.path.exists(_os.path.join(_REPO, "app", "routes",
                                        "hosted_tools.py"))
      and not _os.path.exists(_os.path.join(_REPO, "core",
                                            "tools_corners.py")))
_sec = open(_os.path.join(_REPO, "core", "security.py"),
            encoding="utf-8").read()
_orc = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"),
            encoding="utf-8").read()
check("D3 capability mint/verify gone",
      "sign_hosted_exec_token" not in _sec
      and "verify_hosted_exec_token" not in _sec
      and "X-Hosted-Execution" not in _orc
      and "sign_fetch_token" in _sec, "")  # fetch tokens REMAIN (remote)

# ══════════ E. daily check-in credit (owner-restored, amount from settings) ══════════
# c59ab16 removed the retired mechanism; a later owner decision restored it
# as the first cold-start activity. Closure now pins the restored shape:
# route present, module present, kind spendable, widget i18n keys present,
# amount sourced from settings.daily_login_amount (0/unset = off).
st, _b = _req("POST", "/account/credits/claim", {},
              {"Cookie": CK, "Accept": "text/html"})
check("E1 claim route restored (not 404/405)", st not in (404, 405), str(st))
check("E2 economy.daily module restored",
      _os.path.exists(_os.path.join(_REPO, "core", "economy",
                                    "daily.py")))
from economy.billing import SPENDABLE_KINDS
_i18n_src = open(_os.path.join(_REPO, "core", "i18n.py"),
                 encoding="utf-8").read()
check("E3 grant_daily in SPENDABLE_KINDS + widget i18n keys present",
      "grant_daily" in SPENDABLE_KINDS
      and '"daily_claim"' in _i18n_src
      and '"ledger_grant_daily"' in _i18n_src)
_auth_src = open(_os.path.join(_REPO, "app", "routes", "auth_route.py"),
                 encoding="utf-8").read()
check("E4 login-time daily grant hook present", "_grant_daily" in _auth_src)
_daily_src = open(_os.path.join(_REPO, "core", "economy", "daily.py"),
                  encoding="utf-8").read()
check("E5 amount sourced from settings key (no module constant)",
      "daily_login_amount" in _daily_src
      and "DAILY_AMOUNT =" not in _daily_src)
_deliv_doc = open(_os.path.join(_REPO, "core", "governance", "delivery.py"),
                  encoding="utf-8").read()
check("E6 delivery docstring: coerce_share_delivery mention removed",
      "coerce_share_delivery" not in _deliv_doc)

# ══════════ F. UI consistency ══════════
_tpl = open(_os.path.join(_REPO, "app", "templates", "tool_page.html"),
            encoding="utf-8").read()
check("F1 tool page: crumbs + unified count + localized outputs + one MCP entry",
      'class="crumbs"' in _tpl
      and "tool_files_window" not in _tpl
      and "files_window_short" in _tpl and "n_files_one" in _tpl
      and "out_kind_" in _tpl and "{{ o }}" not in _tpl
      and _tpl.count("toolMcpName") == 1
      and 'copyfield toolpage-mcp' in _tpl.split("toolpage-run", 1)[1],
      "")
_res = open(_os.path.join(_REPO, "app", "templates", "result.html"),
            encoding="utf-8").read()
_det = open(_os.path.join(_REPO, "app", "templates", "detail.html"),
            encoding="utf-8").read()
check("F2 /r + /f share the action bar inside the card",
      'class="row row-wrap r-actions"' in _res
      and 'class="row row-wrap r-actions file-actions"' in _det
      and _det.index("r-actions file-actions")
          < _det.index("file-hero-row")
      and _res.index("r-actions") > _res.index("page-head-meta")
      and "rPrintBtn" not in _res
      and "rShareBtn" in _res and "shareImgBtn" in _det, "")
check("F3 /f stray empty divider gone (owner tools only render for owners)",
      '{% if is_owner %}\n      <div class="workspace-tools"' in _det)
_ag = open(_os.path.join(_REPO, "app", "templates", "agents.html"),
           encoding="utf-8").read()
check("F4 agents page: no TOKEN placeholders, endpoint once",
      "TOKEN" not in _ag and "token" not in _ag
      and _ag.count('id="mcpUrl"') == 1
      and "MCP endpoint:" not in _ag and "<dt>URL</dt>" not in _ag
      and "fsk_" in _ag)

# cleanup
with db() as con:
    purge_users(con, [uid, uid2])

print()
print("FAIL:", FAIL if FAIL else "无 — 全绿")
_sys.exit(1 if FAIL else 0)
