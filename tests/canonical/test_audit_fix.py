#!/usr/bin/env python3
"""Audit-fix canonical — batch/audit-fix behavior locks (items 1-14).

  G1  housekeeping reconciliation order: a share landing between the
      local-row read and the Gokapi snapshot is NOT mistaken for stale.
  G2  subscription upgrade idem includes the txn ref: same-period
      upgrade(txn A) → refund → upgrade(txn B) grants B for real; the
      reversed-period gate still blocks replay after a BASE reversal.
  G3  worker contract codes are ToolError: oversize local output → typed
      4xx err_output_too_large + exact refund (not 500 tool_failed);
      "Unknown tool" stays a plain ValueError.
  G4  supply validator rejects control characters in label/desc (any
      locale); every first-party seed declaration still passes.
  G6  zip-preview.js carries its own content stamp, independent of the
      pages digest.
  G7  reciprocal provider calls (A↔B) settle concurrently 20×2 with
      zero deadlock and full value conservation.
  G8  takedown audit rides INSIDE the delete transaction (failure rolls
      both back; concurrent takedowns → one audit, one delete).
  G9  owner price PATCH preserves the operator's pricing switch.
  G10 PATCH enabled with a non-bool → 422 bad_enabled (not 500).
  G11 reserved usernames + folded handle uniqueness (Platform rejected,
      alice01 vs Alice01, X auto-name falls back to x_{id}).
  G12 no-declaration tools: params serialization capped at
      TEXT_PARAM_MAX_CHARS, rejected before claim/charge.
  G13 MCP third-party annotation on community tools (tools/list +
      catalog_search), first-party unannotated, llms.txt unchanged.
  G14 MCP explicit "files": null → -32602; absent files key → default.
  (G5 publish.js escaping is locked by tests/ci/test_publish_escape_
  contract.js + the browser runtime gate publish-edit scenario.)
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

_REPO = _os.path.abspath(_os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
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
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)

import asyncio as _asyncio
import base64 as _b64
import json as _json
import time as _time
ts = int(_time.time())
_u = "ax" + _os.urandom(4).hex()

from database import db

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

_uids = []

# ══════════ G1. housekeeping reconciliation order ══════════
from sharing import housekeeping as _hk
from sharing import uploads as _sharing_uploads

_g1_user = _mkuser(_u + "g1")
_uids.append(_g1_user)

def _g1_row(public_id, gokapi_id):
    with db() as con:
        con.execute(
            "insert into uploads (user_id, ip, public_id, gokapi_id,"
            " file_name, content_type, size_bytes, expiry_days,"
            " allowed_downloads, expire_at, url_download, url_hotlink,"
            " created_at) values (%s, '127.0.0.1', %s, %s, 'f.txt',"
            " 'text/plain', 10, 7, 50, %s, '', '', %s)",
            (_g1_user, public_id, gokapi_id, ts + 86400, ts))

_g1_row(f"pub_{_u}_stale", f"gk_{_u}_stale")          # storage truly gone
_new_pid = f"pub_{_u}_fresh"
_orig_list = _hk.list_gokapi_files
def _landing_between_list(**kw):
    # called AFTER the local-row read: a share lands whose gokapi_id is
    # (legitimately) absent from the snapshot we are about to return
    _g1_row(_new_pid, f"gk_{_u}_fresh")
    return (True, [])                                   # snapshot knows nothing
_hk.list_gokapi_files = _landing_between_list
try:
    _hk.housekeeping_once()
finally:
    _hk.list_gokapi_files = _orig_list
with db() as con:
    stale_gone = con.execute(
        "select 1 from uploads where public_id=%s", (f"pub_{_u}_stale",)
    ).fetchone() is None
    fresh_kept = con.execute(
        "select 1 from uploads where public_id=%s", (_new_pid,)
    ).fetchone() is not None
check("G1a landing between read and snapshot survives",
      fresh_kept, f"fresh_kept={fresh_kept}")
check("G1b genuinely-absent storage still reconciled", stale_gone)

# ══════════ G2. subscription upgrade idem carries txn ref ══════════
from economy import creem as _creem
from economy import reconcile as _rec
from economy.subscription import handle_event as _hev
from economy.billing import spendable_balance
from repo import orders as orders_repo

_TXNS = {}
def _fake_fetch_transaction(txn_id):
    t = _TXNS.get(txn_id)
    return dict(t) if t else None
_rec.fetch_transaction = _fake_fetch_transaction

import hmac as _hmac
import hashlib as _hashlib
_creem.CREEM_WEBHOOK_SECRET = "ax-test-secret"
def _signed(event):
    raw = _json.dumps(event).encode()
    sig = _hmac.new(b"ax-test-secret", raw, _hashlib.sha256).hexdigest()
    return raw, sig

from fastapi.testclient import TestClient
from app.main import app as _app
client = TestClient(_app)

def _fire(event_type, txn_id):
    obj = {"id": ("ref_" if event_type == "refund.created" else "disp_") + txn_id,
           "object": "refund" if event_type == "refund.created" else "dispute",
           "transaction": {"id": txn_id}}
    event = {"id": f"evt_{txn_id}", "eventType": event_type, "object": obj}
    raw, sig = _signed(event)
    r = client.post("/webhooks/creem", content=raw,
                    headers={"creem-signature": sig,
                             "Content-Type": "application/json"})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text

from app.web_auth import create_browser_session, COOKIE_NAME
_op = _mkuser(_u + "op")
_uids.append(_op)
with db() as con:
    con.execute("update users set status='samelabs' where id=%s", (_op,))
_op_sess = create_browser_session(_op)

_MIRROR = {}
def _fp(pid):
    d = _MIRROR.get(pid)
    return dict(d) if d else None
def _pt(path, payload):
    pid = path.rsplit("/", 1)[-1]
    d = _MIRROR[pid]
    _MIRROR[pid] = {**d, "price": payload["price"],
                    "currency": payload["currency"]}
    return {"object": "product", "id": pid, "price": payload["price"],
            "currency": payload["currency"], "billing_type": "recurring",
            "billing_period": "every-month"}
_cm = _sys.modules["economy.creem"]
_cm._fetch_product = _fp
_cm._patch = _pt
_cm.CREEM_API_KEY = "test-key"

def _mkplan(code, prod, price, monthly):
    with db() as _c:
        so = int(_c.execute(
            "select coalesce(max(sort_order), 0) + 1 s from plans"
        ).fetchone()["s"])
    _MIRROR[prod] = {"price": price, "currency": "USD",
                     "billing_type": "recurring",
                     "billing_period": "every-month"}
    r = client.post("/samelabs/commerce/products/plans", json={
        "code": f"{code}_{_u}", "name": code.upper(),
        "monthly_credits": monthly, "price_cents": price,
        "creem_product_id": prod, "sort_order": so,
        "ingress_files_24h": 500, "ingress_bytes_24h": 524288000,
        "expiry_days": [7], "download_limits": [50],
    }, cookies={COOKIE_NAME: _op_sess})
    assert r.status_code == 200, r.text
    return int(r.json()["plan"]["id"])

def _iso(v):
    import datetime
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).isoformat()

_plus_code = f"axplus_{_u}"
_pro_code = f"axpro_{_u}"
_plus_id = _mkplan("axplus", f"prod_{_u}_plus", 999, 100)
_pro_id = _mkplan("axpro", f"prod_{_u}_pro", 2999, 500)
with db() as con:
    _plus_prod = con.execute("select creem_product_id from plans where id=%s",
                             (_plus_id,)).fetchone()["creem_product_id"]
    _pro_prod = con.execute("select creem_product_id from plans where id=%s",
                            (_pro_id,)).fetchone()["creem_product_id"]

PS = ts - 5 * 86400
PE = ts + 25 * 86400

def _sub_snap(status, product, sub_id, txn=None):
    s = {"object": "subscription", "id": sub_id, "status": status,
         "current_period_start_date": _iso(PS),
         "current_period_end_date": _iso(PE),
         "product": {"id": product}}
    if txn:
        s["last_transaction_id"] = txn
    return s

def _seed_sub_user(name, plan_id, sub_id):
    uid = _mkuser(name)
    _uids.append(uid)
    with db() as con:
        con.execute(
            "insert into orders (user_id, kind, item_id, provider,"
            " provider_order_id, status, amount_cents, currency, paid_at,"
            " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
            " 'USD', %s, %s)",
            (uid, plan_id, "sub:" + sub_id, ts, ts))
        con.execute(
            "insert into memberships (user_id, plan_id,"
            " provider_subscription_id, status, started_at, renews_at,"
            " last_grant_period_start, updated_at)"
            " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
            (uid, plan_id, "sub:" + sub_id, PS, PE, PS, ts))
    return uid

_g2 = _mkuser(_u + "g2")
_uids.append(_g2)
_sub2 = f"sub_{_u}g2"
with db() as con:
    con.execute(
        "insert into orders (user_id, kind, item_id, provider,"
        " provider_order_id, status, amount_cents, currency, paid_at,"
        " created_at) values (%s, 'plan', %s, 'creem', %s, 'paid', 999,"
        " 'USD', %s, %s)",
        (_g2, _plus_id, "sub:" + _sub2, ts, ts))
    con.execute(
        "insert into memberships (user_id, plan_id, provider_subscription_id,"
        " status, started_at, renews_at, last_grant_period_start, updated_at)"
        " values (%s, %s, %s, 'active', %s, %s, %s, %s)",
        (_g2, _plus_id, "sub:" + _sub2, PS, PE, PS, ts))

txn_base, txn_a, txn_b = f"txn_{_u}base", f"txn_{_u}a", f"txn_{_u}b"
_hev("subscription.paid", _sub_snap("active", _plus_prod, _sub2, txn=txn_base))
_hev("subscription.update", _sub_snap("active", _pro_prod, _sub2))
_hev("subscription.paid", _sub_snap("active", _pro_prod, _sub2, txn=txn_a))
check("G2a base(100) + upgrade top-up(400) → 500",
      spendable_balance(_g2) == 500, spendable_balance(_g2))

_TXNS[txn_a] = {"object": "transaction", "id": txn_a, "status": "refunded",
                "type": "invoice", "subscription": _sub2,
                "period_start": _iso(PS), "period_end": _iso(PE)}
sc, body = _fire("refund.created", txn_a)
check("G2b upgrade refund (webhook) → -400 net",
      sc == 200 and spendable_balance(_g2) == 100, f"{sc} {body} {spendable_balance(_g2)}")

_hev("subscription.paid", _sub_snap("active", _pro_prod, _sub2, txn=txn_b))
with db() as con:
    b_rows = con.execute(
        "select delta, idem_key from ledger where user_id=%s"
        " and kind='grant_plan_upgrade' and ref_id=%s",
        (_g2, txn_b)).fetchall()
    net = orders_repo.period_plan_credits(con, _g2, PS)
check("G2c second paid upgrade (txn B) grants for real",
      len(b_rows) == 1 and int(b_rows[0]["delta"]) == 400,
      str([dict(r) for r in b_rows]))
check("G2d idem key carries the txn ref",
      b_rows and b_rows[0]["idem_key"].endswith(":" + txn_b),
      b_rows and b_rows[0]["idem_key"])
check("G2e period net converges to plan total (500)",
      net == 500, net)
_hev("subscription.paid", _sub_snap("active", _pro_prod, _sub2, txn=txn_b))
check("G2f same-txn replay stays idempotent (no double grant)",
      spendable_balance(_g2) == 500, spendable_balance(_g2))

# reversed-period gate regression: BASE payment reversal still blocks replay
_g3 = _seed_sub_user(_u + "g3", _plus_id, f"sub_{_u}g3")
_sub3 = f"sub_{_u}g3"
_hev("subscription.paid", _sub_snap("active", _plus_prod, _sub3,
                                    txn=f"txn_{_u}g3"))
_TXNS[f"txn_{_u}g3"] = {"object": "transaction", "id": f"txn_{_u}g3",
                        "status": "refunded", "type": "invoice",
                        "subscription": _sub3,
                        "period_start": _iso(PS), "period_end": _iso(PE)}
sc, _b = _fire("refund.created", f"txn_{_u}g3")
_gate_blocked = False
try:
    _hev("subscription.paid", _sub_snap("active", _plus_prod, _sub3,
                                        txn=f"txn_{_u}g3"))
except KeyError as exc:
    _gate_blocked = str(exc) == "'paid_period_reversed'"
check("G2g base reversal still blocks same-period replay",
      _gate_blocked, "gate did not raise paid_period_reversed")

# ══════════ G3. worker contract codes are ToolError ══════════
import io as _io
import zipfile as _zipfile
import http.client as _hc
import urllib.parse as _urlparse
from tools import load_all as _load_all, ToolError as _TE
from execution.worker import process_tool as _process_tool
_load_all()

def _zip_blob(seed):
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_STORED) as z:
        z.writestr(f"r{seed}.bin", _os.urandom(3_200_000))
    return buf.getvalue()

try:
    _process_tool("zip_pack",
                  [(_zip_blob(i), f"{i}.zip", "application/zip", 0)
                   for i in range(20)], {})
    check("G3a direct worker oversize output → ToolError err_output_too_large",
          False, "no raise")
except _TE as exc:
    check("G3a direct worker oversize output → ToolError err_output_too_large",
          str(exc) == "err_output_too_large", str(exc))
except ValueError as exc:
    check("G3a direct worker oversize output → ToolError err_output_too_large",
          False, f"plain ValueError: {exc}")
try:
    _process_tool("no_such_tool_ax", [], {})
    check("G3b unknown tool stays ValueError", False, "no raise")
except _TE:
    check("G3b unknown tool stays ValueError", False, "raised ToolError")
except ValueError:
    check("G3b unknown tool stays ValueError", True)

# HTTP end-to-end against the LIVE app (the process pool lives in the
# server process): 20 incompressible zips → zip_pack output >50MB →
# typed 4xx err_output_too_large + exact refund, not 500 tool_failed.
_HOST = ("127.0.0.1", int(_TEST_PORT))
def _live(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=300)
    conn.request(method, path, body, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        parsed = _json.loads(data) if data else None
    except Exception:
        parsed = data.decode(errors="replace")
    return r.status, parsed, setc

def _mp_body(files, fields):
    bnd = "axb" + _u
    out = b""
    for k, v in fields.items():
        out += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\""
                f"\r\n\r\n{v}\r\n").encode()
    for fn, blob in files:
        out += (f"--{bnd}\r\nContent-Disposition: form-data;"
                f" name=\"file\"; filename=\"{fn}\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode()
        out += blob + b"\r\n"
    out += f"--{bnd}--\r\n".encode()
    return out, f"multipart/form-data; boundary={bnd}"

_g3name = _u + "g3u"
_form = _urlparse.urlencode({"username": _g3name, "email": f"{_g3name}@t.co",
                             "password": "Test12345",
                             "password_confirm": "Test12345"})
st, d, _sc = _live("POST", "/auth/register", _form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
_form = _urlparse.urlencode({"login": _g3name, "password": "Test12345"})
st, d, g3_setc = _live("POST", "/auth/login", _form,
                       {"Content-Type": "application/x-www-form-urlencoded"})
_g3_ck = g3_setc.split(";")[0]
with db() as con:
    _g3u = int(con.execute("select id from users where username=%s",
                           (_g3name,)).fetchone()["id"])
_uids.append(_g3u)
from economy.billing import grant as _grant
_grant(_g3u, 1000, "manual_topup", note="ax g3")

_zips = [(f"{i}.zip", _zip_blob(100 + i)) for i in range(20)]
_body, _ct = _mp_body(_zips, {"client_ref": f"axg3up-{_u}", "tool": "zip_pack"})
st, d, _sc = _live("POST", "/api/uploads", _body,
                   {"Content-Type": _ct, "Cookie": _g3_ck})
assert st == 200, f"{st} {str(d)[:200]}"
up_files = (d or {}).get("files") or [(d or {}).get("file")]
balance_before = spendable_balance(_g3u)
st, d, _sc = _live("POST", "/api/tools/execute", _json.dumps({
    "tool_id": "zip_pack",
    "source": {"files": [{"public_id": f["stagingId"],
                          "source_token": f["sourceToken"]}
                         for f in up_files]},
    "params": {},
    "delivery": {"expiry_days": 7, "allowed_downloads": 50},
    "client_ref": f"axg3run-{_u}",
}).encode(), {"Content-Type": "application/json", "Cookie": _g3_ck})
check("G3c HTTP oversize local output → 400 err_output_too_large",
      st == 400 and (d or {}).get("error") == "err_output_too_large",
      f"{st} {str(d)[:160]}")
check("G3d exact refund (balance unchanged)",
      spendable_balance(_g3u) == balance_before,
      f"{balance_before}→{spendable_balance(_g3u)}")
with db() as con:
    row = con.execute(
        "select status, error from tool_calls where user_id=%s"
        " and client_ref=%s", (_g3u, f"axg3run-{_u}")).fetchone()
check("G3e tool_call terminal failed with the typed code",
      row is not None and row["status"] == "failed"
      and row["error"] == "err_output_too_large",
      dict(row) if row else None)

# ══════════ G4. supply validator rejects control characters ══════════
from governance.tool_supply import (validate_tool_declaration as _vtd,
                                    DeclarationError as _DE)

def _decl(**kw):
    d = {"tool_key": f"cf_{_u}", "tool_id": f"cf_{_u}", "owner_id": None,
         "execution": "local", "tool_type": "produce",
         "label": {"en": "CF"}, "desc": None, "icon": "wand",
         "inputs": {"files": {"min": 0, "max": 1, "accept": None}},
         "outputs": ["text"], "params": None, "visibility": "unlisted",
         "enabled": True}
    d.update(kw)
    return d

def _rejected(decl):
    try:
        _vtd(decl)
        return False
    except _DE as exc:
        return str(exc) == "bad_label"

check("G4a desc with \\n rejected", _rejected(_decl(desc={"en": "a\nb"})))
check("G4b label with \\t rejected", _rejected(_decl(label={"en": "a\tb"})))
check("G4c one bad locale in a dict rejected",
      _rejected(_decl(desc={"en": "ok", "zh": "line\rbreak"})))
check("G4d clean multi-locale desc passes",
      _vtd(_decl(desc={"en": "ok", "zh": "好"}))["desc"] == {"en": "ok", "zh": "好"})

from tools import _registry as _reg
_seeds_ok = True
_seed_err = ""
for t in _reg:
    try:
        _vtd({"tool_key": t["id"], "tool_id": t["id"], "owner_id": None,
              "execution": "local", "tool_type": t["type"],
              "label": t["label"], "desc": t["desc"], "icon": t["icon"],
              "inputs": {"files": {"min": t["files_min"], "max": t["files_max"],
                                   "accept": sorted(t["accept"]) if t["accept"] else None}},
              "outputs": t["outputs"], "params": t["params"],
              "visibility": "public", "enabled": True})
    except _DE as exc:
        _seeds_ok = False
        _seed_err = f"{t['id']}: {exc}"
check("G4e every first-party seed declaration still passes",
      _seeds_ok and len(_reg) >= 9, _seed_err or f"registry={len(_reg)}")

_g4u = _mkuser(_u + "g4u")
_uids.append(_g4u)
_g4_sess = create_browser_session(_g4u)
r = client.post("/api/usertools", json={
    "tool_id": f"g4_{_u}", "label": "G4", "desc": "line1\nline2",
    "url": "https://93.184.216.34/x", "timeout_s": 30,
    "files_min": 0, "files_max": 1, "format_families": "image",
    "accept": [".png"], "outputs": ["text"], "cost": 0,
}, cookies={COOKIE_NAME: _g4_sess})
check("G4f HTTP publish with newline desc → 422 bad_label",
      r.status_code == 422 and r.json().get("error") == "bad_label",
      f"{r.status_code} {r.text[:120]}")

# ══════════ G6. zip-preview.js own content stamp ══════════
import subprocess as _sp
_zip_path = _os.path.join(_REPO, "static", "zip-preview.js")
def _versions():
    out = _sp.run([_sys.executable, "-c",
                   "import sys; sys.path.insert(0, '.');"
                   " sys.path.insert(0, 'core'); sys.path.insert(0, 'app');"
                   " from app.web_ctx import ZIP_PREVIEW_VERSION, PAGES_VERSION;"
                   " print(ZIP_PREVIEW_VERSION, PAGES_VERSION)"],
                  cwd=_REPO, capture_output=True, text=True, check=True)
    return tuple(out.stdout.split())
try:
    v1, p1 = _versions()
    with open(_zip_path, "a", encoding="utf-8") as f:
        f.write("\n// stamp-audit-probe\n")
    v2, p2 = _versions()
finally:
    _src = open(_zip_path, encoding="utf-8").read()
    with open(_zip_path, "w", encoding="utf-8") as f:
        f.write(_src.replace("\n// stamp-audit-probe\n", ""))
check("G6a zip stamp changes with the file's content", v1 != v2, f"{v1} vs {v2}")
check("G6b pages digest independent of zip-preview.js", p1 == p2, f"{p1} vs {p2}")
_detail_src = open(_os.path.join(_REPO, "static", "pages", "detail.js"),
                   encoding="utf-8").read()
check("G6c detail.js loads zip-preview with its own stamp",
      "zipPreviewVersion" in _detail_src and "pagesVersion || \"\"" not in
      _detail_src.split("zip-preview.js")[1][:200])

# ══════════ G7. reciprocal provider settlement (lock order) ══════════
from concurrent.futures import ThreadPoolExecutor as _TPE
from execution import gate as _gate
from execution.results import StagedItems, StagedFileItem
from execution.invocation import ToolInvocation, SourceFile

def _mkstaged(tag):
    return StagedItems(
        order=["file"], inline=[],
        files=[StagedFileItem(gokapi_id=f"gk_{_u}_{tag}", filename="out.bin",
                              content_type="application/octet-stream", size=10,
                              expiry_days=7, allowed_downloads=5,
                              expire_at=ts + 86400, url_hotlink="")],
        ip="127.0.0.1")

def _mk_local_tool(tool_id, owner_id, cost):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, tool_type, label_json, icon, visibility, enabled,"
            " created_at, updated_at)"
            " values (%s, %s, %s, 'local', 'produce', %s, 'wand',"
            " 'public', TRUE, %s, %s)",
            (tool_id, owner_id, tool_id,
             _json.dumps({"en": "AX"}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts))

_a, _b = _mkuser(_u + "pa"), _mkuser(_u + "pb")
_uids += [_a, _b]
_grant(_a, 2000, "manual_topup", note="ax")
_grant(_b, 2000, "manual_topup", note="ax")
_t_a, _t_b = f"axtool_{_u}a", f"axtool_{_u}b"
_mk_local_tool(_t_a, _a, 25)
_mk_local_tool(_t_b, _b, 25)

def _run_gate(payer, tool_id, owner, ref):
    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gate.run(
            {"id": payer},
            {"id": tool_id, "tool_key": tool_id, "type": "produce",
             "owner_id": owner},
            ToolInvocation(tool_id=tool_id, files=[SourceFile("p", "t")],
                           params={},
                           delivery={"expiry_days": 7, "allowed_downloads": 5},
                           client_ref=ref),
            lambda: _asyncio.sleep(0, result=_mkstaged(ref))))
    finally:
        loop.close()

_rounds = 20
_g7_fail = []
with _TPE(max_workers=2) as ex:
    futs = []
    for i in range(_rounds):
        futs.append(ex.submit(_run_gate, _a, _t_b, _b, f"axg7-{_u}-{i}-ab"))
        futs.append(ex.submit(_run_gate, _b, _t_a, _a, f"axg7-{_u}-{i}-ba"))
    for f in futs:
        try:
            res = f.result(timeout=120)
            if not (res and res[0] and res[0].get("ok")):
                _g7_fail.append(f"non-ok: {res}")
        except Exception as exc:   # noqa: BLE001
            _g7_fail.append(repr(exc)[:160])
check("G7a 20×2 reciprocal concurrent settlements, zero deadlock/failure",
      not _g7_fail, "; ".join(_g7_fail[:3]))
with db() as con:
    calls = con.execute(
        "select count(*) c from tool_calls where client_ref like %s",
        (f"axg7-{_u}-%",)).fetchone()["c"]
    cons = con.execute(
        "select count(*) c from ledger where idem_key like 'call:%%'"
        " and user_id in %s", ((_a, _b),)).fetchone()["c"]
check("G7b all 40 calls settled ok", int(calls) == 40, calls)
check("G7c every call consumed exactly once (40 charges)", int(cons) == 40, cons)

# ══════════ G8. takedown audit inside the delete transaction ══════════
from backoffice import content as _content_cmd
_g8u = _mkuser(_u + "g8u")
_uids.append(_g8u)
_g1_row(f"pub_{_u}_g8", f"gk_{_u}_g8")
with db() as con:
    _g8_id = int(con.execute(
        "select id from uploads where public_id=%s", (f"pub_{_u}_g8",)
    ).fetchone()["id"])

_op2 = _mkuser(_u + "op2")
_uids.append(_op2)
_rolled_back = False
try:
    from sharing import uploads as _su
    # the row was inserted by the G1 helper — its owner is _g1_user
    _su.delete_upload(_g8_id, _g1_user,
                      audit=lambda con, row: (_ for _ in ()).throw(
                          RuntimeError("audit-failure-probe")))
except RuntimeError:
    with db() as con:
        row = con.execute("select 1 from uploads where id=%s", (_g8_id,)).fetchone()
        audits = con.execute(
            "select count(*) c from operator_audit where actor_user_id=%s"
            " and action='upload.delete'", (_op2,)).fetchone()["c"]
    _rolled_back = row is not None and int(audits) == 0
check("G8a audit failure rolls the delete back (no row, no audit)",
      _rolled_back)

def _take():
    try:
        return _content_cmd.delete_upload(_op2, _g8_id)["deleted"]
    except KeyError:
        return False
with _TPE(max_workers=2) as ex:
    res = [f.result(timeout=60) for f in
           [ex.submit(_take), ex.submit(_take)]]
with db() as con:
    audits = int(con.execute(
        "select count(*) c from operator_audit where actor_user_id=%s"
        " and action='upload.delete' and target_id=%s",
        (_op2, str(_g8_id))).fetchone()["c"])
    gone = con.execute("select 1 from uploads where id=%s",
                       (_g8_id,)).fetchone() is None
check("G8b concurrent takedowns: one delete, one audit",
      sum(1 for d in res if d) == 1 and audits == 1 and gone,
      f"deleted={res} audits={audits} gone={gone}")

# ══════════ G9/G10. usertools price/enabled patch semantics ══════════
_g9u = _mkuser(_u + "g9u")
_uids.append(_g9u)
_g9_sess = create_browser_session(_g9u)
_tid = f"g9_{_u}"
_key = f"{_u}g9u/{_tid}"
r = client.post("/api/usertools", json={
    "tool_id": _tid, "label": "G9", "url": "https://93.184.216.34/x",
    "timeout_s": 30, "files_min": 0, "files_max": 1,
    "format_families": "image", "accept": [".png"], "outputs": ["text"],
    "cost": 5}, cookies={COOKIE_NAME: _g9_sess})
assert r.status_code == 200, r.text[:200]
r = client.post(f"/samelabs/tools/{_key}/pricing",
                json={"cost": 5, "enabled": False},
                cookies={COOKIE_NAME: _op_sess})
check("G9a operator pricing takedown applied",
      r.status_code == 200 and r.json()["pricing"]["enabled"] is False, r.text[:120])
r = client.patch(f"/api/usertools/{_tid}", json={"cost": 9},
                 cookies={COOKIE_NAME: _g9_sess})
from governance.pricing import get_pricing as _get_pricing
_pr = _get_pricing(_key)
check("G9b owner price PATCH keeps the operator takedown",
      r.status_code == 200 and _pr["enabled"] is False and _pr["cost"] == 9,
      f"{r.status_code} {dict(_pr)}")
r = client.patch(f"/api/usertools/{_tid}", json={"enabled": "yes"},
                 cookies={COOKIE_NAME: _g9_sess})
check("G10 PATCH enabled non-bool → 422 bad_enabled",
      r.status_code == 422 and r.json().get("error") == "bad_enabled",
      f"{r.status_code} {r.text[:120]}")

# ══════════ G11. reserved + folded usernames ══════════
_reg_seq = [0]
def _register_json(username):
    _reg_seq[0] += 1
    return client.post("/auth/register", data={
        "username": username, "email": f"{username.lower()}{_reg_seq[0]}@{_u}.t.co",
        "password": "Test12345", "password_confirm": "Test12345",
    }, headers={"Accept": "application/json"})

r = _register_json("Platform")
check("G11a reserved handle (case variant) rejected",
      r.json().get("error") == "err_username_reserved",
      f"{r.status_code} {r.text[:120]}")
_handle = _u + "person"
r = _register_json(_handle)
with db() as con:
    _uids.append(int(con.execute(
        "select id from users where lower(username)=lower(%s)",
        (_handle,)).fetchone()["id"]))
check("G11b0 control: first registration succeeds",
      r.json().get("ok") is True, f"{r.status_code} {r.text[:120]}")
r = _register_json(_handle.swapcase())
check("G11b folded duplicate handle rejected as username",
      r.json().get("error") == "err_username_taken",
      f"{r.status_code} {r.text[:120]}")
with db() as con:
    idx = con.execute(
        "select indexname from pg_indexes where tablename = 'users'"
        " and indexname = 'users_username_lower_uk'").fetchone()
check("G11c folded username unique index exists", idx is not None)
from identity.auth import upsert_x_user as _upsert_x
_x_id = "9" * 12
_x_uid = _upsert_x({"id": _x_id, "username": "Platform",
                    "name": "X Platform", "profile_image_url": ""})
_uids.append(_x_uid)
with db() as con:
    _x_name = con.execute("select username from users where id=%s",
                          (_x_uid,)).fetchone()["username"]
check("G11d X handle hitting a reserved name falls back to x_{id}",
      _x_name == f"x_{_x_id}", _x_name)

# ══════════ G12. no-declaration params cap ══════════
_g12u = _mkuser(_u + "g12u")
_uids.append(_g12u)
_g12_sess = create_browser_session(_g12u)
_grant(_g12u, 500, "manual_topup", note="ax g12")
_bal12 = spendable_balance(_g12u)
r = client.post("/api/tools/execute", json={
    "tool_id": "share", "source": {"files": []},
    "params": {"x": "A" * 100_001},
    "delivery": {"expiry_days": 1, "allowed_downloads": 50},
    "client_ref": f"axg12-{_u}",
}, cookies={COOKIE_NAME: _g12_sess})
check("G12a no-decl params over TEXT_PARAM_MAX_CHARS → 422 param_too_long",
      r.status_code == 422 and r.json().get("error") == "param_too_long",
      f"{r.status_code} {r.text[:120]}")
with db() as con:
    claimed = con.execute(
        "select 1 from tool_calls where user_id=%s and client_ref=%s",
        (_g12u, f"axg12-{_u}")).fetchone()
check("G12b rejected before claim (no tool_calls row)", claimed is None)
check("G12c rejected before charge (balance untouched)",
      spendable_balance(_g12u) == _bal12)

# ══════════ G13. MCP third-party annotation ══════════
_g13o = _mkuser(_u + "g13o")
_uids.append(_g13o)
_g13_sess = create_browser_session(_g13o)
_tid13 = f"g13_{_u}"
_key13 = f"{_u}g13o/{_tid13}"
r = client.post("/api/usertools", json={
    "tool_id": _tid13, "label": "AX Thirdparty", "desc": "community probe",
    "url": "https://93.184.216.34/x", "timeout_s": 30,
    "files_min": 0, "files_max": 1, "format_families": "image",
    "accept": [".png"], "outputs": ["text"], "cost": 0,
}, cookies={COOKIE_NAME: _g13_sess})
assert r.status_code == 200, r.text[:200]
with db() as con:
    con.execute("update platform_tools set visibility='public'"
                " where tool_key=%s", (_key13,))
r = client.post("/api/keys", json={"name": "ax"},
                cookies={COOKIE_NAME: _g13_sess})
_tok13 = r.json()["key"]["key"]
mname13 = _key13.replace("/", "__")

def _mcp(body, token):
    r = client.post("/mcp", json=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    return r.json()

_d = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _tok13)
_tools = (((_d or {}).get("result") or {}).get("tools") or [])
_by_name = {t["name"]: t.get("description", "") for t in _tools}
check("G13a community tool listed with third-party annotation",
      mname13 in _by_name
      and f"Third-party tool by @{_u}g13o." in _by_name.get(mname13, ""),
      _by_name.get(mname13, "")[:160])
check("G13b first-party tool carries no annotation",
      "share" in _by_name and "Third-party tool" not in _by_name["share"],
      _by_name.get("share", "")[:160])
_d = _mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "catalog_search",
                      "arguments": {"q": "AX Thirdparty"}}}, _tok13)
_found = ((((_d or {}).get("result") or {}).get("structuredContent")
           or {}).get("tools") or [])
_annotated = [t for t in _found if t.get("name") == mname13]
check("G13c catalog_search community result annotated",
      len(_annotated) == 1 and "Third-party tool by @" in
      (_annotated[0].get("description") or ""),
      str(_found)[:200])
r = client.get("/llms.txt")
_llms = r.text
check("G13d llms.txt unchanged (by @, no third-party marker)",
      ("Third-party tool" not in _llms)
      and (f"- {mname13}: AX Thirdparty by @{_u}g13o [free]" in _llms),
      [l for l in _llms.splitlines() if mname13 in l])

# ══════════ G14. MCP explicit files:null vs missing ══════════
r = client.post("/api/keys", json={"name": "ax2"},
                cookies={COOKIE_NAME: _g13_sess})
_tok14 = r.json()["key"]["key"]
_d = _mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "share", "arguments": {
               "files": None, "params": {},
               "client_ref": f"axg14n-{_u}",
               "delivery": {"expiry_days": 1, "allowed_downloads": 50}}}},
          _tok14)
_err = ((_d or {}).get("error") or {})
check("G14a explicit files:null → -32602",
      _err.get("code") == -32602, _d)
_d = _mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
           "params": {"name": "share", "arguments": {
               "params": {},
               "client_ref": f"axg14m-{_u}",
               "delivery": {"expiry_days": 1, "allowed_downloads": 50}}}},
          _tok14)
_err = ((_d or {}).get("error") or {})
_is_error = (((_d or {}).get("result") or {}).get("isError") is True)
check("G14b absent files key defaults (no -32602; proceeds to the count gate)",
      _err.get("code") != -32602 and _is_error,
      _d)

# ── cleanup ──
with db() as con:
    con.execute("delete from platform_tools where tool_key in %s",
                ((_t_a, _t_b, _key, _key13),))
    con.execute("delete from pricing where tool_id in %s",
                ((_t_a, _t_b, _key, _key13),))
    purge_users(con, _uids)   # drops memberships/orders first
    con.execute("delete from plans where code in %s",
                ((_plus_code, _pro_code),))

print(f"\n{COUNT - len(FAIL)}/{COUNT} passed")
_sys.exit(1 if FAIL else 0)
